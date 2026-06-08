from flask import Flask, render_template, request, redirect, session
import pymysql
import pymysql.cursors
import asyncio
import hashlib
import os
from dotenv import load_dotenv
from ai_extractor import extract_with_vision
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta
import csv
import io
from flask import make_response
import csv
import io
from flask import make_response

load_dotenv()

print("SECRET_KEY = ",os.getenv("SECRET_KEY"))
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")
app.permanent_session_lifetime = timedelta(days=30)

ALLOWED_TYPES = [
    "image/jpeg", "image/jpg", "image/png",
    "image/webp", "image/heic", "image/heif",
    "application/pdf"
]

# ── DB Connection ─────────────────────────────────────
def get_db():
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASS", ""),
        database=os.environ.get("DB_NAME", "receipt_scanner"),
        cursorclass=pymysql.cursors.DictCursor
    )

# ── Init DB ───────────────────────────────────────────
def init_db():
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("""
CREATE TABLE IF NOT EXISTS receipts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    hash VARCHAR(32) UNIQUE NOT NULL,
    vendor_name VARCHAR(255),
    date VARCHAR(20),
    total_amount DECIMAL(10,2) DEFAULT 0.00,
    tax_amount DECIMAL(10,2) DEFAULT 0.00,
    payment_method VARCHAR(50),
    category VARCHAR(50),
    user_id INT NULL
)""")
    conn.commit()
    conn.close()
    print("✅ DB Ready")

def reset_db():
    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute("DELETE FROM receipts")
    conn.commit()
    conn.close()
    print("🗑️ DB Cleared")

init_db()

# ── Helpers ───────────────────────────────────────────
def make_hash(data):
    return hashlib.md5(data).hexdigest()

def normalize_category(cat):
    if not cat:
        return "Unknown"

    cat = cat.strip().lower()

    # Direct categories first
    direct_map = {
        "electronics": "Electronics",
        "grocery": "Grocery",
        "food": "Food",
        "fuel": "Fuel",
        "medical": "Medical",
        "shopping": "Shopping",
        "utilities": "Utilities",
    }

    if cat in direct_map:
        return direct_map[cat]

    # Keyword-based fallback
    keyword_map = {
        "Electronics": [
            "bluetooth", "speaker", "charger",
            "mobile", "laptop", "tv",
            "electronics", "headphone"
        ],

        "Grocery": [
            "grocery", "mart", "supermarket",
            "rice", "milk", "bread"
        ],

        "Food": [
            "restaurant", "food", "pizza",
            "burger", "zomato", "swiggy"
        ],

        "Medical": [
            "pharma", "medicine", "hospital",
            "medical", "clinic"
        ],

        "Fuel": [
            "petrol", "diesel", "fuel"
        ],

        "Shopping": [
            "fashion", "clothes", "myntra"
        ],

        "Utilities": [
            "electricity", "water", "gas bill"
        ]
    }

    for label, keywords in keyword_map.items():
        if any(k in cat for k in keywords):
            return label

    return "Unknown"

def save(data, user_id):
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # Pehle check karo same user ki same hash hai kya
            cursor.execute(
                "DELETE FROM receipts WHERE hash=%s AND user_id=%s",
                (data["hash"], user_id)
            )
            # Ab fresh insert karo
            cursor.execute("""
            INSERT INTO receipts
            (hash, vendor_name, date, total_amount, tax_amount, payment_method, category, user_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (data["hash"], data["vendor_name"], data["date"],
             data["total_amount"], data["tax_amount"],
             data["payment_method"], data["category"], user_id))
        conn.commit()
        print(f"✅ Saved: {data['vendor_name']}")
    except Exception as e:
        print(f"[SAVE ERROR] {e}")
    finally:
        conn.close()

# ── Dynamic Chunk Size ────────────────────────────────
def get_chunk_size(total):
    if total <= 10:
        return total
    elif total <= 50:
        return 25
    else:
        return 50

# ── PDF → Image Convert ───────────────────────────────
def pdf_to_image_bytes(file_bytes):
    try:
        import fitz  # pip install pymupdf
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        page = doc[0]  # Pehla page
        mat = fitz.Matrix(2, 2)  # 2x zoom — better quality
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("jpeg")
        doc.close()
        print(f"✅ PDF converted to image ({len(img_bytes)} bytes)")
        return img_bytes
    except Exception as e:
        print(f"[PDF ERROR] {e}")
        return None

# ── Scan Single Bill ──────────────────────────────────
async def scan_single(file_bytes, filename, content_type):
    try:
        print(f"\n📸 Processing: {filename} ({content_type})")

        # PDF ko image mein convert karo
        if content_type == "application/pdf":
            converted = await asyncio.to_thread(pdf_to_image_bytes, file_bytes)
            if converted:
                file_bytes = converted
            else:
                print(f"[WARN] PDF convert failed: {filename}")
                return {"status": "failed", "filename": filename}

        extracted = await asyncio.to_thread(extract_with_vision, file_bytes)
        return {
            "hash":           make_hash(file_bytes),
            "vendor_name":    extracted.get("vendor_name", "Unknown"),
            "date":           extracted.get("date", "Unknown"),
            "total_amount":   extracted.get("total_amount", 0.0),
            "tax_amount":     extracted.get("tax_amount", 0.0),
            "payment_method": extracted.get("payment_method", "Unknown"),
            "category":       normalize_category(extracted.get("category", "")),
            "status":         "success"
        }
    except Exception as e:
        print(f"[ERROR] {filename}: {e}")
        return {"status": "failed", "filename": filename}

# ── Process All Bills ─────────────────────────────────
async def process_all_bills(bill_list, user_id):
    # Sirf current batch mein duplicate skip karo
    seen_hashes = set()
    unique_bills = []
    for file_bytes, filename, content_type in bill_list:
        h = make_hash(file_bytes)
        if h not in seen_hashes:
            seen_hashes.add(h)
            unique_bills.append((file_bytes, filename, content_type))
        else:
            print(f"⚠️ Same batch duplicate skipped: {filename}")

    total = len(unique_bills)
    print(f"\n📥 Total unique bills: {total}")

    # Dynamic chunking
    chunk_size = get_chunk_size(total)
    chunks = [
        unique_bills[i:i+chunk_size]
        for i in range(0, total, chunk_size)
    ]
    print(f"📦 Chunk size: {chunk_size} | Chunks: {len(chunks)}")

    # Process chunks
    all_results = []
    for idx, chunk in enumerate(chunks):
        print(f"\n🔄 Scanning chunk {idx+1}/{len(chunks)}...")
        tasks = [scan_single(fb, fn, ct) for fb, fn, ct in chunk]
        results = await asyncio.gather(*tasks)
        all_results.extend(results)
        if idx + 1 < len(chunks):
            await asyncio.sleep(2)

    # Save to MySQL
    success = 0
    failed  = 0
    for r in all_results:
        if r["status"] == "success":
            save(r, user_id)
            success += 1
        else:
            failed += 1

    print(f"\n🎯 Done! ✅ Success: {success} | ❌ Failed: {failed}")

# ── Routes ────────────────────────────────────────────
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        password = request.form["password"]

        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM users WHERE email=%s",
                    (email,)
                )
                existing = cursor.fetchone()

                if existing:
                    return "Email already exists"

                hashed_password = generate_password_hash(password)

                cursor.execute("""
                    INSERT INTO users
                    (name, email, password)
                    VALUES (%s, %s, %s)
                """, (
                    name,
                    email,
                    hashed_password
                ))
            conn.commit()
            return redirect("/login")
        finally:
            conn.close()

    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM users WHERE email=%s",
                    (email,)
                )
                user = cursor.fetchone()

            if user and check_password_hash(
                user["password"],
                password
            ):
                session.permanent = True
                session["user_id"] = user["id"]
                session["user_name"] = user["name"]
                return redirect("/")

            return render_template("login.html", error="Invalid Email or Password")
        finally:
            conn.close()

    return render_template("login.html")

@app.route("/")
def index():
    logged_in = "user_id" in session
    return render_template("home.html", logged_in=logged_in)

@app.route("/upload", methods=["GET", "POST"])
def upload():
    if "user_id" not in session:
        return redirect("/login")

    if request.method == "POST":
        # ── 1. URL ka mode parameter check karein (append hai ya refresh) ──
        mode = request.args.get("mode", "refresh")

        # Agar normal upload screen se request aayi hai (mode == "refresh"),
        # toh is user ka purana saara data pehle delete (clear) hoga.
        if mode == "refresh":
            conn = get_db()
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM receipts WHERE user_id=%s", (session["user_id"],))
            conn.commit()
            conn.close()
            print(f"🔄 Refresh Mode: Purana data clear kiya user {session['user_id']} ka")

        bill_list = []
        for file in request.files.getlist("files"):
            if not file or file.filename == "": continue
            content_type = file.content_type or ""
            file_bytes = file.read()
            if len(file_bytes) < 5 * 1024 or content_type not in ALLOWED_TYPES: continue
            bill_list.append((file_bytes, file.filename, content_type))

        # ── 2. Naye files ko scan karke save karein ──
        asyncio.run(process_all_bills(bill_list, session["user_id"]))
        return redirect("/dashboard")

    return render_template("upload.html")

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT * FROM receipts
            WHERE user_id=%s
            """,
            (session["user_id"],)
        )
        rows = cursor.fetchall()
    conn.close()

    total = sum(r["total_amount"] for r in rows)
    tax_total = sum(r["tax_amount"] for r in rows)
    count = len(rows)
    avg = total / count if count else 0

    return render_template(
        "dashboard.html",
        rows=rows,
        total=round(total, 2),
        tax_total=round(tax_total, 2),
        count=count,
        avg=round(avg, 2)
    )


@app.route("/export")
def export():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT vendor_name, date, total_amount, tax_amount, payment_method, category FROM receipts WHERE user_id=%s",
            (session["user_id"],)
        )
        rows = cursor.fetchall()
    conn.close()

    # CSV banao memory mein
    output = io.StringIO()
    writer = csv.writer(output)

    # Header row
    writer.writerow(["Vendor Name", "Date", "Total Amount (₹)", "Tax Amount (₹)", "Payment Method", "Category"])

    # Data rows
    for r in rows:
        writer.writerow([
            r["vendor_name"],
            r["date"],
            r["total_amount"],
            r["tax_amount"],
            r["payment_method"],
            r["category"]
        ])

    output.seek(0)

    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=receipts.csv"
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    return response

@app.route("/clear")
def clear():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db()
    with conn.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM receipts
            WHERE user_id=%s
            """,
            (session["user_id"],)
        )
    conn.commit()
    conn.close()
    
    # Redirects back to dashboard to display a completely cleaned state
    return redirect("/dashboard")

if __name__ == "__main__":
    app.run(debug=True)