import os
import json
import re
import base64
import time
import httpx
from groq import Groq

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

PROMPT = """
You are an expert receipt analyst.

Analyze:

1. Store/vendor name


2. Item names inside receipt



Extract:

vendor_name

total_amount

tax_amount

payment_method

category

date


CATEGORY CLASSIFICATION RULES:

Electronics:
Electronic devices, gadgets, accessories,
speakers, Bluetooth devices, chargers,
computers, mobiles, TVs, tablets, tech items.

Grocery:
Daily household items, vegetables, fruits,
snacks, packaged foods, dairy products,
supermarket grocery items.

Food:
Restaurant bills, cafes, dining, food delivery,
meals, beverages.

Fuel:
Petrol pump, diesel, gas station receipts.

Medical:
Pharmacy, medicines, hospital, clinic receipts.

Utilities:
Electricity, water, gas, internet, recharge bills.

Shopping:
Clothes, fashion, footwear, lifestyle,
accessories, retail shopping.


IMPORTANT:

Do NOT classify using only store name.

Analyze item names carefully.

Multi-category stores like Costco/Walmart/Dmart
can contain different categories.

If mixed categories exist, choose category
based on highest-value items.


Return ONLY valid JSON.

{
"vendor_name": "...",
"total_amount": 0.00,
"tax_amount": 0.00,
"payment_method": "...",
"category": "...",
"date": "DD/MM/YYYY"
}
"""

def image_to_base64(file_bytes):
    return base64.b64encode(file_bytes).decode("utf-8")

def extract_with_vision(file_bytes, retry=0):
    try:
        b64 = image_to_base64(file_bytes)

        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            temperature=0.0,
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}"
                        }
                    },
                    {"type": "text", "text": PROMPT}
                ]
            }]
        )

        raw = response.choices[0].message.content.strip()
        print(f"[VISION RAW]\n{raw}\n")
        raw = re.sub(r'```(?:json)?', '', raw).strip().rstrip('`').strip()

        json_match = re.search(r'\{[\s\S]*?\}', raw)
        if not json_match:
            raise ValueError("No JSON found")

        data = json.loads(json_match.group())
        data["total_amount"] = _to_float(data.get("total_amount", 0))
        data["tax_amount"]   = _to_float(data.get("tax_amount", 0))

        if not (10 <= data["total_amount"] <= 50000):
            print(f"[WARN] Unreasonable total: {data['total_amount']} — setting 0")
            data["total_amount"] = 0.0

        print("[EXTRACTED]", data)
        return data

    except Exception as e:
        error_str = str(e)

        if "429" in error_str and retry < 3:
            wait = 60 * (retry + 1)
            print(f"[RATE LIMIT] {wait}s wait... (attempt {retry+1}/3)")
            time.sleep(wait)
            return extract_with_vision(file_bytes, retry + 1)

        elif ("Connection error" in error_str or "timed out" in error_str) and retry < 3:
            print(f"[CONNECTION] Retry {retry+1}/3 — 5s wait...")
            time.sleep(5)
            return extract_with_vision(file_bytes, retry + 1)

        print(f"[VISION ERROR] {e}")
        return _fallback()

def _to_float(val):
    try:
        return float(str(val).replace(",", "").replace("₹", "").replace("Rs.", "").replace("rs.", "").strip())
    except:
        return 0.0

def _fallback():
    return {
        "vendor_name":    "Unknown",
        "total_amount":   0.0,
        "tax_amount":     0.0,
        "payment_method": "Unknown",
        "category":       "Unknown",
        "date":           "Unknown"
    }