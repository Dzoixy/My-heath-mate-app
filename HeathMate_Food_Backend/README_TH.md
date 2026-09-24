# HeathMate Food Backend

Backend นี้ทำมาให้เข้ากับ `FoodApi.kt` ของแอปโดยตรง

## Endpoint contract

### `POST /v1/analyze`

Headers:

```text
Authorization: Bearer <Firebase ID token>
Content-Type: application/json
```

Body:

```json
{
  "imageBase64": "<JPEG/PNG/WebP Base64>"
}
```

Response:

```json
{
  "foods": [
    {
      "name": "ข้าวสวย",
      "searchQuery": "cooked white rice",
      "estimatedGrams": 180
    }
  ]
}
```

สูงสุด 6 รายการ

### `POST /v1/foods`

Headers:

```text
Authorization: Bearer <Firebase ID token>
Content-Type: application/json
```

Body:

```json
{
  "query": "cooked white rice"
}
```

Response:

```json
{
  "foods": [
    {
      "fdcId": 123456,
      "name": "Rice, white, cooked",
      "kcalPer100g": 130.0,
      "proteinPer100g": 2.7,
      "fatPer100g": 0.3,
      "carbsPer100g": 28.2
    }
  ]
}
```

## 1. Firebase Admin

ไปที่ Firebase Console:

Project settings -> Service accounts -> Generate new private key

จะได้ไฟล์ JSON ของ service account

**ห้ามใส่ไฟล์นี้ลง GitHub**

บน Render ให้สร้าง Environment Variable:

```text
FIREBASE_SERVICE_ACCOUNT_JSON
```

ค่าเป็น JSON ทั้งก้อนแบบ one-line

และใส่:

```text
FIREBASE_PROJECT_ID=<project-id ของ Firebase>
REQUIRE_EMAIL_VERIFIED=true
```

## 2. AI วิเคราะห์ภาพ

ค่าเริ่มต้นใช้ Gemini ผ่าน REST API:

```text
AI_PROVIDER=gemini
GEMINI_API_KEY=<your key>
GEMINI_MODEL=gemini-2.5-flash
```

ถ้าจะใช้ provider แบบ OpenAI-compatible:

```text
AI_PROVIDER=openai_compatible
OPENAI_COMPAT_BASE_URL=https://provider.example.com
OPENAI_COMPAT_API_KEY=<your key>
OPENAI_COMPAT_MODEL=<vision model>
```

## 3. Nutrition database

ระบบค้นหาแคลอรี/สารอาหารผ่าน USDA FoodData Central

ทดลองก่อนได้ด้วย:

```text
USDA_API_KEY=DEMO_KEY
```

สำหรับใช้งานจริงควรเปลี่ยนเป็น API key ของตัวเอง

## 4. Deploy บน Render แบบเร็ว

1. แตก ZIP
2. สร้าง GitHub repo ใหม่ แล้ว upload ไฟล์ทั้งหมด
3. Render -> New -> Blueprint
4. เลือก repo นี้
5. Render จะอ่าน `render.yaml`
6. ใส่ Secret/Environment Variables ที่ยังว่าง:
   - `GEMINI_API_KEY`
   - `FIREBASE_SERVICE_ACCOUNT_JSON`
   - `FIREBASE_PROJECT_ID`
7. Deploy

หลัง deploy จะได้ URL เช่น:

```text
https://heathmate-food-api.onrender.com
```

ทดสอบ:

```text
https://heathmate-food-api.onrender.com/health
```

ควรได้ JSON ที่มี `"status":"ok"`

## 5. ต่อกับ Android

ใน `bloomfit.properties` ของ Android:

```properties
FOOD_API_URL=https://heathmate-food-api.onrender.com
```

**ไม่ต้อง**ใส่ `/v1/analyze` ต่อท้าย เพราะ `FoodApi.kt` ต่อ path ให้เอง

จากนั้น:

```text
Sync Project with Gradle Files
Build -> Clean Project
Build -> Rebuild Project
Run
```

## หมายเหตุ

- `/v1/analyze` และ `/v1/foods` ตรวจ Firebase ID token
- ค่าเริ่มต้นบังคับ email verification
- Backend ไม่บันทึกภาพอาหารลงดิสก์
- ภาพถูกส่งต่อให้ AI provider เพื่อวิเคราะห์ตามคำสั่งของผู้ใช้
- ปริมาณกรัมและการระบุอาหารจากภาพเป็นค่าประมาณ ผู้ใช้ควรตรวจแก้ก่อนบันทึก
