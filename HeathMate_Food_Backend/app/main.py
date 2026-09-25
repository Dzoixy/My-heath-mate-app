from __future__ import annotations

import base64
import binascii
import json
import os
import re
from typing import Any

import firebase_admin
from fastapi import Depends, FastAPI, Header, HTTPException
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials
import httpx
from pydantic import BaseModel, Field, field_validator


APP_NAME = "HeathMate Food API"
MAX_IMAGE_BASE64_CHARS = 3_000_000
MAX_RESPONSE_FOODS = 6
USDA_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

app = FastAPI(
    title=APP_NAME,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

APP_NAME = "HeathMate Food API"
MAX_IMAGE_BASE64_CHARS = 3_000_000
MAX_RESPONSE_FOODS = 6
USDA_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

AI_TIMEOUT = httpx.Timeout(
    connect=15.0,
    read=120.0,
    write=45.0,
    pool=20.0,
)
_firebase_ready = False
_firebase_error: str | None = None


class AnalyzeRequest(BaseModel):
    imageBase64: str = Field(min_length=32, max_length=MAX_IMAGE_BASE64_CHARS)


class FoodSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100)

    @field_validator("query")
    @classmethod
    def trim_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


class DetectedFood(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    searchQuery: str = Field(min_length=1, max_length=120)
    estimatedGrams: int = Field(ge=1, le=10_000)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def init_firebase() -> None:
    global _firebase_ready, _firebase_error
    if _firebase_ready:
        return
    if _firebase_error is not None:
        raise RuntimeError(_firebase_error)

    try:
        if firebase_admin._apps:
            _firebase_ready = True
            return

        raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
        if raw:
            info = json.loads(raw)
            firebase_admin.initialize_app(credentials.Certificate(info))
        else:
            # Works automatically on Google Cloud. On Render, set
            # FIREBASE_SERVICE_ACCOUNT_JSON.
            firebase_admin.initialize_app(credentials.ApplicationDefault())

        _firebase_ready = True
    except Exception as exc:
        _firebase_error = (
            "Firebase Admin is not configured. Set FIREBASE_SERVICE_ACCOUNT_JSON "
            f"or provide Application Default Credentials. ({exc.__class__.__name__})"
        )
        raise RuntimeError(_firebase_error) from exc


def verify_bearer_token(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Firebase bearer token")

    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing Firebase bearer token")

    try:
        init_firebase()
        decoded = firebase_auth.verify_id_token(token, check_revoked=False)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired Firebase token") from exc

    if env_bool("REQUIRE_EMAIL_VERIFIED", True) and not bool(decoded.get("email_verified")):
        raise HTTPException(status_code=403, detail="Email address is not verified")

    allowed_project = os.getenv("FIREBASE_PROJECT_ID", "").strip()
    audience = str(decoded.get("aud", ""))
    if allowed_project and audience and audience != allowed_project:
        raise HTTPException(status_code=403, detail="Token belongs to a different Firebase project")

    return decoded


def decode_food_image(encoded: str) -> tuple[bytes, str]:
    if len(encoded) > MAX_IMAGE_BASE64_CHARS:
        raise HTTPException(status_code=413, detail="Image is too large")

    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="imageBase64 is not valid Base64") from exc

    if len(data) < 32:
        raise HTTPException(status_code=400, detail="Image payload is empty")

    if data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data[:4] in {b"RIFF"} and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise HTTPException(status_code=415, detail="Only JPEG, PNG or WebP images are supported")

    return data, mime


def strip_json_fence(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def normalize_ai_foods(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        items = payload.get("foods", [])
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    if not isinstance(items, list):
        raise HTTPException(status_code=502, detail="AI returned an invalid foods list")

    result: list[dict[str, Any]] = []
    for item in items[:MAX_RESPONSE_FOODS]:
        if not isinstance(item, dict):
            continue
        try:
            food = DetectedFood.model_validate(item)
        except Exception:
            continue
        result.append(food.model_dump())

    return result


AI_PROMPT = """
You are the food-recognition component of a health tracking application.

Analyze the meal photo. Return ONLY JSON with this exact shape:
{
  "foods": [
    {
      "name": "Thai/common display name",
      "searchQuery": "short English food name suitable for USDA FoodData Central search",
      "estimatedGrams": 250
    }
  ]
}

Rules:
- Identify at most 6 nutritionally distinct visible foods/components.
- If a plate contains rice plus a separate meat dish, return separate items.
- estimatedGrams must be a whole number from 1 to 10000.
- Use a concise Thai/common name for "name".
- Use an English generic food description for "searchQuery".
- Do not invent ingredients that are not visibly plausible.
- If no food can be identified, return {"foods":[]}.
- Do not include markdown, comments, explanations, calories, or extra keys.
""".strip()


async def analyze_with_gemini(image_base64: str, mime: str) -> list[dict[str, Any]]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY is not configured")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{
            "parts": [
                {"text": AI_PROMPT},
                {"inlineData": {"mimeType": mime, "data": image_base64}},
            ]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    try:
         async with httpx.AsyncClient(timeout=AI_TIMEOUT) as client:
            response = await client.post(
                url,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="AI request timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Could not reach AI provider") from exc

    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="AI quota reached")
    if response.status_code >= 500:
        raise HTTPException(status_code=503, detail="AI provider is temporarily unavailable")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"AI provider rejected the request ({response.status_code})")

    try:
        body = response.json()
        text = body["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(strip_json_fence(text))
    except Exception as exc:
        raise HTTPException(status_code=502, detail="AI returned unreadable JSON") from exc

    return normalize_ai_foods(parsed)


async def analyze_with_openai_compatible(image_base64: str, mime: str) -> list[dict[str, Any]]:
    base_url = os.getenv("OPENAI_COMPAT_BASE_URL", "").strip().rstrip("/")
    api_key = os.getenv("OPENAI_COMPAT_API_KEY", "").strip()
    model = os.getenv("OPENAI_COMPAT_MODEL", "").strip()

    if not base_url or not api_key or not model:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_COMPAT_BASE_URL, OPENAI_COMPAT_API_KEY and OPENAI_COMPAT_MODEL are required",
        )

    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": AI_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{image_base64}"},
                },
            ],
        }],
    }

    try:
        async with httpx.AsyncClient(timeout=AI_TIMEOUT) as client:
            response = await client.post(
                f"{base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="AI request timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Could not reach AI provider") from exc

    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="AI quota reached")
    if response.status_code >= 500:
        raise HTTPException(status_code=503, detail="AI provider is temporarily unavailable")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"AI provider rejected the request ({response.status_code})")

    try:
        text = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(strip_json_fence(text))
    except Exception as exc:
        raise HTTPException(status_code=502, detail="AI returned unreadable JSON") from exc

    return normalize_ai_foods(parsed)


async def identify_foods(image_base64: str, mime: str) -> list[dict[str, Any]]:
    provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()
    if provider == "gemini":
        return await analyze_with_gemini(image_base64, mime)
    if provider in {"openai", "openai_compatible", "compatible"}:
        return await analyze_with_openai_compatible(image_base64, mime)
    raise HTTPException(status_code=503, detail=f"Unsupported AI_PROVIDER: {provider}")


def nutrient_value(food: dict[str, Any], number: str, *, kcal_only: bool = False) -> float | None:
    for nutrient in food.get("foodNutrients") or []:
        if str(nutrient.get("nutrientNumber", "")) != number:
            continue

        unit = str(nutrient.get("unitName", "")).upper()
        if kcal_only and unit not in {"KCAL", "KCAL."}:
            continue

        value = nutrient.get("value")
        try:
            result = float(value)
        except (TypeError, ValueError):
            continue

        if result >= 0:
            return result
    return None


def map_usda_food(food: dict[str, Any]) -> dict[str, Any] | None:
    kcal = nutrient_value(food, "208", kcal_only=True)
    if kcal is None or kcal < 0 or kcal > 1000:
        return None

    fdc_id = food.get("fdcId")
    description = str(food.get("description", "")).strip()
    try:
        fdc_id = int(fdc_id)
    except (TypeError, ValueError):
        return None

    if fdc_id <= 0 or not description:
        return None

    result: dict[str, Any] = {
        "fdcId": fdc_id,
        "name": description[:180],
        "kcalPer100g": round(kcal, 2),
    }

    optional = {
        "proteinPer100g": nutrient_value(food, "203"),
        "fatPer100g": nutrient_value(food, "204"),
        "carbsPer100g": nutrient_value(food, "205"),
    }
    for key, value in optional.items():
        if value is not None and 0 <= value <= 100:
            result[key] = round(value, 2)

    return result


async def search_usda(query: str) -> list[dict[str, Any]]:
    api_key = os.getenv("USDA_API_KEY", "DEMO_KEY").strip() or "DEMO_KEY"

    payload = {
        "query": query,
        "pageSize": 18,
        "pageNumber": 1,
        "dataType": ["Foundation", "SR Legacy", "Survey (FNDDS)", "Branded"],
        "sortBy": "dataType.keyword",
        "sortOrder": "asc",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                USDA_URL,
                params={"api_key": api_key},
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Nutrition database timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Could not reach nutrition database") from exc

    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="Nutrition database quota reached")
    if response.status_code >= 500:
        raise HTTPException(status_code=503, detail="Nutrition database is temporarily unavailable")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Nutrition database rejected the request ({response.status_code})")

    try:
        foods = response.json().get("foods", [])
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Nutrition database returned unreadable data") from exc

    if not isinstance(foods, list):
        raise HTTPException(status_code=502, detail="Nutrition database returned an invalid response")

    mapped: list[dict[str, Any]] = []
    seen: set[int] = set()
    for food in foods:
        if not isinstance(food, dict):
            continue
        item = map_usda_food(food)
        if item is None:
            continue
        if item["fdcId"] in seen:
            continue
        seen.add(item["fdcId"])
        mapped.append(item)
        if len(mapped) >= 12:
            break

    return mapped


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "name": APP_NAME,
        "status": "ok",
        "endpoints": ["/health", "/v1/analyze", "/v1/foods"],
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()
    return {
        "status": "ok",
        "aiProvider": provider,
        "firebaseConfigured": bool(os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")) or bool(firebase_admin._apps),
        "usdaConfigured": bool(os.getenv("USDA_API_KEY")),
    }


@app.post("/v1/analyze")
async def analyze(
    request: AnalyzeRequest,
    _: dict[str, Any] = Depends(verify_bearer_token),
) -> dict[str, Any]:
    _, mime = decode_food_image(request.imageBase64)
    foods = await identify_foods(request.imageBase64, mime)
    return {"foods": foods}


@app.post("/v1/foods")
async def foods(
    request: FoodSearchRequest,
    _: dict[str, Any] = Depends(verify_bearer_token),
) -> dict[str, Any]:
    result = await search_usda(request.query)
    return {"foods": result}

import logging
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("uvicorn.error")

@app.exception_handler(HTTPException)
async def log_http_exception(request: Request, exc: HTTPException):
    logger.error(
        "%s %s -> HTTP %s: %s",
        request.method,
        request.url.path,
        exc.status_code,
        exc.detail,
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )
    