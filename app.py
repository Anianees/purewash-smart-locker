"""
Purewash Smart Locker - Backend (7-Compartment, Full Lifecycle)
---------------------------------------------------------------
Customer flow:  drop-off / pick-up  (via /locker/<id>)
Staff flow:     collect, mark washing/ready, return clean laundry  (via /staff)

Order lifecycle:
  DROPPED     - customer left dirty laundry (compartment occupied)
  COLLECTED   - staff took it for washing (compartment freed)
  WASHING     - being washed at the shop
  READY       - washed, ready to return
  RETURNED    - staff placed clean laundry back in a compartment (occupied)
  PICKED_UP   - customer collected clean laundry (compartment freed)

Compartments free up at every handoff, so 7 boxes serve many customers.

Hardware: ESP32 polls /api/check-unlock?device=locker1, opens the compartment
the flag names, then reports /api/door-latched.
"""

import sqlite3, time, os, threading, re, secrets, urllib.parse, hmac
from functools import wraps
from flask import (Flask, request, jsonify, render_template,
                   redirect, session, g, url_for)
import requests
from itsdangerous import URLSafeTimedSerializer, BadSignature

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "purewash-secret-change-me")
DB_PATH = os.environ.get("DB_PATH", "purewash.db")

# If DATABASE_URL is set (Neon Postgres), orders are stored there and survive
# every deploy/restart. Without it, the app falls back to the local SQLite file
# (fine for testing on your own computer, but wiped on every Render deploy).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
if USE_PG:
    import psycopg
    from psycopg.rows import dict_row

# ---------------- CONFIG ----------------
TELEGRAM_BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_STAFF_CHAT_ID = os.environ.get("TELEGRAM_STAFF_CHAT_ID", "")
WHATSAPP_TOKEN         = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID      = os.environ.get("WHATSAPP_PHONE_ID", "")
WHATSAPP_API_VERSION   = os.environ.get("WHATSAPP_API_VERSION", "v25.0")
WHATSAPP_TEMPLATE_LANG = os.environ.get("WHATSAPP_TEMPLATE_LANG", "en")  # template language "English" = en

# WhatsApp verification at the locker: the customer confirms by sending a
# short code from their own WhatsApp, so the order always gets their real
# number. Set REQUIRE_WA_VERIFY=0 in Render to switch it off instantly.
REQUIRE_WA_VERIFY  = os.environ.get("REQUIRE_WA_VERIFY", "1") != "0"
WA_API_NUMBER      = re.sub(r"\D", "", os.environ.get("WA_API_NUMBER", "918848639844"))     # automatic updates number
WA_SUPPORT_NUMBER  = re.sub(r"\D", "", os.environ.get("WA_SUPPORT_NUMBER", "918075316525")) # staff chat number
VERIFY_TTL         = 10 * 60          # a code is valid for 10 minutes
WA_OTP_TEMPLATE    = os.environ.get("WA_OTP_TEMPLATE", "locker_otp")   # Authentication template with "Copy code" button
DEVICE_DAYS        = 90               # a verified phone is remembered in that browser for 90 days
OTP_RESEND_GAP     = 30               # seconds between code requests for one number
OTP_MAX_PER_HOUR   = 5
OTP_MAX_TRIES      = 5
AUTO_REPLY_EVERY   = 12 * 60 * 60     # auto-reply to the same customer at most every 12 hours

# ---------------- PRICES (edit here; the locker page and staff portal read these) ----------------
# Wash services: 2 kg or more -> charged per kg; under 2 kg -> charged per piece.
PRICES = {
    "WASH_FOLD": {"label": "Wash & Fold", "per_kg": 65,  "per_piece": 39},
    "WASH_IRON": {"label": "Wash & Iron", "per_kg": 110, "per_piece": 49},
    "DRY_CLEAN": {"label": "Dry Clean",   "per_kg": None, "per_piece": 70},
}
MIN_KG_FOR_KG_RATE = 2.0
SPECIAL_ITEMS_NOTE = ("Delicates (silk, chiffon, wool, embroidered or zari wear) and heavy items "
                      "(blazers, suits, sarees, blankets, quilts, curtains) are priced after we check the fabric.")

def calc_amount(service, pieces, weight_kg):
    """Standard price in rupees. Special items can be adjusted by staff on top of this."""
    p = PRICES.get(service)
    if not p:
        return None
    pieces = int(pieces or 0)
    if p["per_kg"] and weight_kg and float(weight_kg) >= MIN_KG_FOR_KG_RATE:
        return int(round(float(weight_kg) * p["per_kg"]))
    return pieces * p["per_piece"]

# Staff portal password (set STAFF_PASSWORD in Render; fallback works immediately)
STAFF_PASSWORD = os.environ.get("STAFF_PASSWORD", "purewash123")

UNLOCK_FLAG_TTL  = 120
NUM_COMPARTMENTS = 7

# ---------------- DB ----------------
class _PgDB:
    """Small wrapper so the rest of the code can keep using db.execute("... ?", params)."""
    def __init__(self):
        self.con = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=15)
    def execute(self, sql, params=()):
        return self.con.execute(sql.replace("?", "%s"), params)
    def commit(self):
        self.con.commit()
    def close(self):
        self.con.close()

def _connect():
    if USE_PG:
        return _PgDB()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = _connect()
    return db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("_db", None)
    if db is not None:
        db.close()

def init_db():
    id_col = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    con = _connect()
    for stmt in (f"""
    CREATE TABLE IF NOT EXISTS orders (
        id {id_col},
        locker_id TEXT NOT NULL,
        drop_compartment INTEGER,
        return_compartment INTEGER,
        customer_name TEXT NOT NULL,
        customer_phone TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'DROPPED',
        created_at INTEGER NOT NULL,
        updated_at INTEGER
    )""", """
    CREATE TABLE IF NOT EXISTS compartments (
        locker_id TEXT NOT NULL,
        compartment INTEGER NOT NULL,
        occupied INTEGER NOT NULL DEFAULT 0,
        order_id INTEGER,
        kind TEXT,
        PRIMARY KEY (locker_id, compartment)
    )"""):
        con.execute(stmt)
    con.commit()
    # Columns added later (service, counts, price). Safe to run on every start.
    for col, typ in (("service", "TEXT"), ("item_count", "INTEGER"), ("special_items", "INTEGER"),
                     ("staff_count", "INTEGER"), ("weight_kg", "REAL"), ("amount", "INTEGER"),
                     ("price_note", "TEXT")):
        try:
            if USE_PG:
                con.execute(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {col} {typ}")
            else:
                con.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")
            con.commit()
        except Exception:
            if USE_PG:
                con.con.rollback()
    con.close()
    print("Database:", "Postgres (DATABASE_URL)" if USE_PG else f"SQLite file {DB_PATH}")

def ensure_compartments(locker_id):
    db = get_db()
    for c in range(1, NUM_COMPARTMENTS + 1):
        db.execute("""INSERT INTO compartments (locker_id, compartment, occupied) VALUES (?,?,0)
                      ON CONFLICT (locker_id, compartment) DO NOTHING""",
                   (locker_id, c))
    db.commit()

init_db()

# ---------------- NOTIFICATIONS ----------------
def notify_staff(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_STAFF_CHAT_ID:
        print("[staff]", text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_STAFF_CHAT_ID, "text": text}, timeout=5)
    except Exception as e:
        print("Telegram failed:", e)

def notify_customer(phone, template, params, summary):
    """Send an approved WhatsApp template to a customer.

    template: template name in WhatsApp Manager (e.g. "laundry_ready")
    params:   values for {{1}}, {{2}}, ... in order
    summary:  plain-language version, used in the staff alert if sending fails
    """
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        print(f"[whatsapp -> {phone}] {template} {params}"); return
    payload = {
        "messaging_product": "whatsapp",
        "to": f"91{phone}",
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": WHATSAPP_TEMPLATE_LANG},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": str(v).strip() or "-"} for v in params],
            }],
        },
    }
    try:
        r = requests.post(f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_ID}/messages",
                          headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                          json=payload, timeout=8)
        if r.ok:
            return
        try:
            err = r.json().get("error", {})
            reason = err.get("error_user_msg") or err.get("message") or r.text[:200]
        except ValueError:
            reason = r.text[:200]
        print("WhatsApp send failed:", r.status_code, r.text[:500])
    except Exception as e:
        reason = str(e)
        print("WhatsApp send failed:", e)
    # Customer did not get the message -> tell staff so they can inform them manually
    notify_staff(f"⚠️ WhatsApp '{template}' to {phone} FAILED: {reason}\n"
                 f"Please inform the customer manually: {summary}")

def send_text(wa_id, text):
    """Plain WhatsApp text. Only allowed within 24h after the customer messaged us (free)."""
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        print(f"[whatsapp text -> {wa_id}] {text}"); return True
    try:
        r = requests.post(f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_ID}/messages",
                          headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                          json={"messaging_product": "whatsapp", "to": wa_id, "type": "text",
                                "text": {"body": text, "preview_url": True}}, timeout=8)
        if not r.ok:
            print("WhatsApp text failed:", r.status_code, r.text[:300])
        return r.ok
    except Exception as e:
        print("WhatsApp text failed:", e)
        return False

# ---------------- HELPERS ----------------
# Pending "open this door" commands for the ESP32. Kept in memory, not in the
# database: the ESP32 polls every few seconds, and a command only lives 2 minutes,
# so there is nothing worth keeping across a restart. This also lets the free
# Postgres database sleep when nobody is using the locker.
_unlock_flags = {}
_unlock_lock = threading.Lock()

def set_unlock(locker_id, compartment, order_id):
    with _unlock_lock:
        _unlock_flags[locker_id] = {"compartment": compartment, "order_id": order_id,
                                    "set_at": int(time.time())}

def free_compartments(locker_id):
    db = get_db()
    rows = db.execute("SELECT compartment FROM compartments WHERE locker_id=? AND occupied=0 ORDER BY compartment",
                      (locker_id,)).fetchall()
    return [r["compartment"] for r in rows]

# ---------------- INPUT CLEANUP ----------------
def clean_locker_id(v):
    """'Locker1.', ' locker1/', 'LOCKER1' -> 'locker1' (links pasted into WhatsApp often pick up stray characters)."""
    return re.sub(r"[^a-z0-9_-]", "", (v or "").strip().lower())

def clean_phone(v):
    """'+91 98765 43210', '919876543210', '09876543210' -> '9876543210'."""
    digits = re.sub(r"\D", "", v or "")
    return digits[-10:] if len(digits) >= 10 else digits

# ---------------- CUSTOMER PAGES ----------------
@app.route("/")
def home():
    return redirect("/locker/locker1")

@app.route("/locker/<locker_id>")
def locker_page(locker_id):
    clean = clean_locker_id(locker_id)
    if not clean:
        return redirect("/locker/locker1")
    if clean != locker_id:
        return redirect(f"/locker/{clean}")
    ensure_compartments(locker_id)
    return render_template("locker.html", locker_id=locker_id)

@app.route("/api/availability")
def availability():
    locker_id = clean_locker_id(request.args.get("locker_id",""))
    ensure_compartments(locker_id)
    db = get_db()
    rows = db.execute("SELECT compartment, occupied FROM compartments WHERE locker_id=? ORDER BY compartment",
                      (locker_id,)).fetchall()
    return jsonify([{"compartment":r["compartment"],"occupied":bool(r["occupied"])} for r in rows])

@app.route("/api/prices")
def prices():
    return jsonify(services=[dict(code=k, **v) for k, v in PRICES.items()],
                   min_kg=MIN_KG_FOR_KG_RATE, special_note=SPECIAL_ITEMS_NOTE)

@app.route("/api/my-compartment")
def my_compartment():
    locker_id = clean_locker_id(request.args.get("locker_id",""))
    phone = clean_phone(request.args.get("phone"))
    db = get_db()
    # Customer pickup = a RETURNED order (clean laundry waiting) for this phone
    row = db.execute("""SELECT return_compartment, amount, service, staff_count FROM orders
                        WHERE locker_id=? AND customer_phone=? AND status='RETURNED'
                        ORDER BY id DESC LIMIT 1""", (locker_id, phone)).fetchone()
    if row and row["return_compartment"]:
        svc = PRICES.get(row["service"] or "", {}).get("label")
        return jsonify(found=True, compartment=row["return_compartment"], amount=row["amount"],
                       service=svc, items=row["staff_count"])
    return jsonify(found=False)

# ---------------- CUSTOMER: open (dropoff / pickup) ----------------
def do_dropoff(locker_id, compartment, name, phone, service=None, item_count=None, special=0):
    """Create the order and open the door. Returns (order_id, error)."""
    ensure_compartments(locker_id)
    db = get_db()
    comp = db.execute("SELECT occupied FROM compartments WHERE locker_id=? AND compartment=?",
                      (locker_id, compartment)).fetchone()
    if not comp:
        return None, "That compartment does not exist."
    if comp["occupied"] == 1:
        return None, "That compartment was just taken. Please pick another."
    cur = db.execute("""INSERT INTO orders (locker_id, drop_compartment, customer_name, customer_phone,
                        status, created_at, service, item_count, special_items)
                        VALUES (?,?,?,?, 'DROPPED', ?, ?, ?, ?) RETURNING id""",
                     (locker_id, compartment, name, phone, int(time.time()),
                      service, item_count, 1 if special else 0))
    order_id = cur.fetchone()["id"]
    db.commit()
    set_unlock(locker_id, compartment, order_id)
    return order_id, None

def find_ready_order(locker_id, phone, compartment):
    return get_db().execute("""SELECT * FROM orders WHERE locker_id=? AND customer_phone=?
                               AND status='RETURNED' AND return_compartment=?
                               ORDER BY id DESC LIMIT 1""",
                            (locker_id, phone, compartment)).fetchone()

def do_pickup(locker_id, compartment, order):
    """Open the door, close the order, free the compartment."""
    db = get_db()
    set_unlock(locker_id, compartment, order["id"])
    db.execute("UPDATE orders SET status='PICKED_UP', updated_at=? WHERE id=?",
               (int(time.time()), order["id"]))
    db.execute("UPDATE compartments SET occupied=0, order_id=NULL, kind=NULL WHERE locker_id=? AND compartment=?",
               (locker_id, compartment))
    db.commit()
    notify_staff(f"PICKED UP · Locker {locker_id} · Compartment {compartment}\n"
                 f"{order['customer_name']} · Order #{order['id']} complete.")

# ---------------- WhatsApp verification (pending codes, kept in memory) ----------------
_pending = {}            # token -> dict(code, action, locker_id, compartment, name, phone, order_id, created, state, error)
_pending_lock = threading.Lock()
_auto_replied = {}       # wa_id -> last auto-reply time

def _new_pending(action, locker_id, compartment, name, phone, order_id=None, extra=None):
    now = time.time()
    with _pending_lock:
        for t in [t for t, p in _pending.items() if now - p["created"] > VERIFY_TTL * 3]:
            del _pending[t]                                  # tidy old entries
        live = {p["code"] for p in _pending.values() if p["state"] == "pending"}
        code = f"{secrets.randbelow(9000) + 1000}"
        while code in live:
            code = f"{secrets.randbelow(9000) + 1000}"
        token = secrets.token_urlsafe(16)
        _pending[token] = dict(code=code, action=action, locker_id=locker_id, compartment=compartment,
                               name=name, phone=phone, order_id=order_id, created=now,
                               state="pending", error=None, extra=extra or {})
    word = "DROP" if action == "DROPOFF" else "PICK"
    text = f"{word} {code}"
    link = f"https://wa.me/{WA_API_NUMBER}?text={urllib.parse.quote(text)}"
    return token, text, link

# ---------------- OTP on WhatsApp + "remember this phone" ----------------
_device_signer = URLSafeTimedSerializer(app.secret_key, salt="pw-device")
_otps = {}                 # phone -> dict(code, created, tries, sends=[timestamps])
_otp_lock = threading.Lock()

def device_phone():
    """The phone number this browser has verified before (or None)."""
    tok = request.cookies.get("pw_dev")
    if not tok:
        return None
    try:
        data = _device_signer.loads(tok, max_age=DEVICE_DAYS * 86400)
        return data.get("p"), data.get("n", "")
    except BadSignature:
        return None
    except Exception:
        return None

def _verified_phone():
    d = device_phone()
    return d[0] if d else None

def send_otp_template(phone, code):
    """Send the code with the Authentication template. Returns True if WhatsApp accepted it."""
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        print(f"[whatsapp OTP -> {phone}] {code}"); return True
    payload = {"messaging_product": "whatsapp", "to": f"91{phone}", "type": "template",
               "template": {"name": WA_OTP_TEMPLATE, "language": {"code": WHATSAPP_TEMPLATE_LANG},
                            "components": [
                                {"type": "body", "parameters": [{"type": "text", "text": code}]},
                                {"type": "button", "sub_type": "url", "index": "0",
                                 "parameters": [{"type": "text", "text": code}]}]}}
    try:
        r = requests.post(f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{WHATSAPP_PHONE_ID}/messages",
                          headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, json=payload, timeout=8)
        if not r.ok:
            print("OTP send failed:", r.status_code, r.text[:300])
        return r.ok
    except Exception as e:
        print("OTP send failed:", e)
        return False

@app.route("/api/me")
def api_me():
    d = device_phone()
    if not d:
        return jsonify(known=False)
    return jsonify(known=True, phone=d[0], name=d[1])

@app.route("/api/otp/send", methods=["POST"])
def otp_send():
    data = request.get_json(force=True)
    phone = clean_phone(data.get("phone"))
    if len(phone) != 10:
        return jsonify(ok=False, error="Enter a valid 10-digit mobile number."), 400
    now = time.time()
    with _otp_lock:
        rec = _otps.get(phone) or {"sends": []}
        rec["sends"] = [t for t in rec["sends"] if now - t < 3600]
        if rec["sends"] and now - rec["sends"][-1] < OTP_RESEND_GAP:
            wait = int(OTP_RESEND_GAP - (now - rec["sends"][-1])) + 1
            return jsonify(ok=False, error=f"Please wait {wait} seconds before asking for a new code.", wait=wait), 429
        if len(rec["sends"]) >= OTP_MAX_PER_HOUR:
            return jsonify(ok=False, error="Too many codes requested. Please try again in an hour, "
                                            "or WhatsApp us at +91 80753 16525."), 429
        code = f"{secrets.randbelow(900000) + 100000}"
        rec.update(code=code, created=now, tries=0)
        rec["sends"].append(now)
        _otps[phone] = rec
    if not send_otp_template(phone, code):
        with _otp_lock:
            _otps.get(phone, {}).pop("code", None)
        # The page then falls back to "send a message to confirm"
        return jsonify(ok=False, fallback=True, error="Could not send the code on WhatsApp."), 502
    return jsonify(ok=True, resend_after=OTP_RESEND_GAP)

@app.route("/api/otp/verify", methods=["POST"])
def otp_verify():
    data = request.get_json(force=True)
    phone = clean_phone(data.get("phone"))
    code = re.sub(r"\D", "", data.get("code") or "")
    name = (data.get("name") or "").strip()[:60]
    with _otp_lock:
        rec = _otps.get(phone)
        if not rec or not rec.get("code") or time.time() - rec["created"] > VERIFY_TTL:
            return jsonify(ok=False, error="This code has expired. Tap \"Resend code\"."), 400
        if rec["tries"] >= OTP_MAX_TRIES:
            return jsonify(ok=False, error="Too many wrong tries. Tap \"Resend code\" for a new one."), 400
        rec["tries"] += 1
        if not hmac.compare_digest(code, rec["code"]):
            left = OTP_MAX_TRIES - rec["tries"]
            return jsonify(ok=False, error=f"That code is not right. {left} tries left." if left else
                                           "Too many wrong tries. Tap \"Resend code\" for a new one."), 400
        _otps.pop(phone, None)
    resp = jsonify(ok=True)
    resp.set_cookie("pw_dev", _device_signer.dumps({"p": phone, "n": name}),
                    max_age=DEVICE_DAYS * 86400, httponly=True, samesite="Lax",
                    secure=request.is_secure or request.headers.get("X-Forwarded-Proto") == "https")
    return resp

@app.route("/api/forget-me", methods=["POST"])
def forget_me():
    resp = jsonify(ok=True)
    resp.delete_cookie("pw_dev")
    return resp

@app.route("/api/open", methods=["POST"])
def open_compartment():
    data = request.get_json(force=True)
    locker_id = clean_locker_id(data.get("locker_id"))
    name  = (data.get("name") or "").strip()
    phone = clean_phone(data.get("phone"))
    action = (data.get("action") or "").strip().upper()
    compartment = data.get("compartment")

    if not locker_id or not name or len(phone)!=10 or not phone.isdigit():
        return jsonify(ok=False, error="Enter a valid name and 10-digit phone."), 400
    try: compartment = int(compartment)
    except: return jsonify(ok=False, error="Pick a compartment."), 400

    ensure_compartments(locker_id)
    db = get_db()
    needs_verify = REQUIRE_WA_VERIFY and _verified_phone() != phone

    if action == "DROPOFF":
        service = (data.get("service") or "").strip().upper()
        if service not in PRICES:
            return jsonify(ok=False, error="Please choose a service."), 400
        try:
            item_count = int(data.get("item_count"))
            assert 1 <= item_count <= 500
        except Exception:
            return jsonify(ok=False, error="Please enter how many clothes you are dropping."), 400
        special = 1 if data.get("special_items") else 0
        extra = dict(service=service, item_count=item_count, special=special)
        comp = db.execute("SELECT occupied FROM compartments WHERE locker_id=? AND compartment=?",
                          (locker_id, compartment)).fetchone()
        if not comp or comp["occupied"] == 1:
            return jsonify(ok=False, error="That compartment was just taken. Pick another."), 409
        if needs_verify:
            token, text, link = _new_pending("DROPOFF", locker_id, compartment, name, phone, extra=extra)
            return jsonify(ok=True, verify=True, token=token, text=text, wa_link=link, compartment=compartment)
        order_id, err = do_dropoff(locker_id, compartment, name, phone, **extra)
        if err:
            return jsonify(ok=False, error=err), 409
        return jsonify(ok=True, order_id=order_id, compartment=compartment)

    elif action == "PICKUP":
        order = find_ready_order(locker_id, phone, compartment)
        if not order:
            return jsonify(ok=False, error="No ready laundry found for you in that compartment."), 403
        if needs_verify:
            token, text, link = _new_pending("PICKUP", locker_id, compartment, name, phone, order["id"])
            return jsonify(ok=True, verify=True, token=token, text=text, wa_link=link, compartment=compartment)
        do_pickup(locker_id, compartment, order)
        return jsonify(ok=True, order_id=order["id"], compartment=compartment)

    return jsonify(ok=False, error="Invalid action."), 400

@app.route("/api/verify-status")
def verify_status():
    token = request.args.get("token", "")
    with _pending_lock:
        p = _pending.get(token)
        if not p:
            return jsonify(state="expired")
        if p["state"] == "pending" and time.time() - p["created"] > VERIFY_TTL:
            p["state"] = "expired"
        return jsonify(state=p["state"], compartment=p["compartment"], error=p["error"])

def handle_incoming_whatsapp(wa_id, wa_name, text):
    """Called by whatsapp_webhook.py for every customer message. True = handled."""
    m = re.search(r"\b(DROP|PICK)\s*-?\s*(\d{4})\b", text or "", re.IGNORECASE)
    if m:
        word, code = m.group(1).upper(), m.group(2)
        action = "DROPOFF" if word == "DROP" else "PICKUP"
        sender_phone = clean_phone(wa_id)
        with _pending_lock:
            match = None
            for p in _pending.values():
                if p["code"] == code and p["action"] == action and p["state"] == "pending" \
                        and time.time() - p["created"] <= VERIFY_TTL:
                    match = p; break
            if match:
                match["state"] = "working"
        if not match:
            with _pending_lock:
                seen = any(p["code"] == code and p["action"] == action and p["state"] != "pending"
                           and time.time() - p["created"] <= VERIFY_TTL for p in _pending.values())
            if not seen:   # (a repeat delivery of a code we already used is ignored quietly)
                send_text(wa_id, "This code has expired or is not valid. Please start again on the locker page.")
            return True

        if action == "DROPOFF":
            # Use the number WhatsApp tells us, not what was typed
            order_id, err = do_dropoff(match["locker_id"], match["compartment"], match["name"], sender_phone,
                                       **match.get("extra", {}))
            if err:
                match.update(state="failed", error=err)
                send_text(wa_id, f"Sorry, {err}")
                return True
            match.update(state="done")
            if sender_phone != match["phone"]:
                notify_staff(f"ℹ️ Order #{order_id}: customer typed {match['phone']} but verified from "
                             f"{sender_phone}. Saved the verified number.")
            send_text(wa_id, f"✅ Verified. Compartment {match['compartment']} is opening now. "
                             f"Place your laundry inside and close the door.")
        else:
            order = get_db().execute("SELECT * FROM orders WHERE id=?", (match["order_id"],)).fetchone()
            if not order or order["status"] != "RETURNED":
                match.update(state="failed", error="This laundry has already been collected.")
                send_text(wa_id, "This laundry has already been collected.")
                return True
            if clean_phone(order["customer_phone"]) != sender_phone:
                match.update(state="failed",
                             error="Please send the code from the WhatsApp number this order belongs to.")
                send_text(wa_id, "This WhatsApp number does not match the order, so the locker was not opened. "
                                 "Please send the code from the number you used at drop-off.")
                return True
            do_pickup(match["locker_id"], match["compartment"], order)
            match.update(state="done")
            send_text(wa_id, f"✅ Verified. Compartment {match['compartment']} is opening now. "
                             f"Please collect your laundry and close the door. Thank you!")
        return True

    # Any other message: point the customer to the staff chat number (at most every 12 hours),
    # and let the webhook forward their message to staff on Telegram.
    now = time.time()
    if now - _auto_replied.get(wa_id, 0) > AUTO_REPLY_EVERY:
        _auto_replied[wa_id] = now
        send_text(wa_id, "Hi! This number sends automatic Purewash updates. To chat with our team, "
                         f"tap here: https://wa.me/{WA_SUPPORT_NUMBER}")
    return False

# ---------------- ESP32 polling ----------------
@app.route("/api/check-unlock")
def check_unlock():
    locker_id = request.args.get("device","")
    with _unlock_lock:
        row = _unlock_flags.pop(locker_id, None)
    if row and (int(time.time()) - row["set_at"]) <= UNLOCK_FLAG_TTL:
        return jsonify(unlock=True, compartment=row["compartment"], order_id=row["order_id"])
    return jsonify(unlock=False)

@app.route("/api/door-latched", methods=["POST"])
def door_latched():
    data = request.get_json(force=True)
    locker_id = (data.get("device") or "").strip()
    compartment = data.get("compartment")
    db = get_db()

    # Which order is this? the one whose current step involves this compartment
    order = db.execute("""SELECT * FROM orders WHERE locker_id=? AND
                          (status='DROPPED' AND drop_compartment=?)
                          ORDER BY id DESC LIMIT 1""",
                       (locker_id, int(compartment) if compartment is not None else -1)).fetchone()

    if compartment is None:
        return jsonify(ok=True, note="no compartment")

    comp = int(compartment)

    # A DROP-OFF just closed -> mark compartment occupied (dirty), alert staff, WhatsApp customer
    dropped = db.execute("""SELECT * FROM orders WHERE locker_id=? AND status='DROPPED' AND drop_compartment=?
                           ORDER BY id DESC LIMIT 1""", (locker_id, comp)).fetchone()
    if dropped:
        db.execute("UPDATE compartments SET occupied=1, order_id=?, kind='DIRTY' WHERE locker_id=? AND compartment=?",
                   (dropped["id"], locker_id, comp))
        db.commit()
        svc = PRICES.get(dropped["service"] or "", {}).get("label", "—")
        extra = f"\n{svc} · customer says {dropped['item_count'] or '?'} items"
        if dropped["special_items"]:
            extra += " · ⚠️ includes delicate/heavy items"
        notify_staff(f"NEW DROP-OFF · Locker {locker_id} · Compartment {comp}\n"
                     f"{dropped['customer_name']} · {dropped['customer_phone']}{extra}\n"
                     f"Order #{dropped['id']} — please collect for washing.")
        notify_customer(dropped["customer_phone"], "laundry_received",
                        [dropped["customer_name"], comp],
                        f"laundry received in compartment {comp} (order #{dropped['id']})")
        return jsonify(ok=True)

    # A RETURN just closed -> mark compartment occupied (clean), WhatsApp customer it's ready
    returning = db.execute("""SELECT * FROM orders WHERE locker_id=? AND status='RETURNED' AND return_compartment=?
                             ORDER BY id DESC LIMIT 1""", (locker_id, comp)).fetchone()
    if returning:
        db.execute("UPDATE compartments SET occupied=1, order_id=?, kind='CLEAN' WHERE locker_id=? AND compartment=?",
                   (returning["id"], locker_id, comp))
        db.commit()
        notify_customer(returning["customer_phone"], "laundry_ready",
                        [returning["customer_name"], returning["id"]],
                        f"order #{returning['id']} is ready for pickup in compartment {comp}")
        return jsonify(ok=True)

    return jsonify(ok=True, note="no matching open step")

# =====================================================================
# STAFF PORTAL
# =====================================================================
def staff_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if not session.get("staff"):
            return redirect(url_for("staff_login"))
        return f(*a, **k)
    return wrap

@app.route("/staff/login", methods=["GET","POST"])
def staff_login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == STAFF_PASSWORD:
            session["staff"] = True
            return redirect(url_for("staff_home"))
        error = "Wrong password."
    return render_template("staff_login.html", error=error)

@app.route("/staff/logout")
def staff_logout():
    session.clear()
    return redirect(url_for("staff_login"))

@app.route("/staff")
@staff_required
def staff_home():
    return render_template("staff.html")

@app.route("/staff/api/orders")
@staff_required
def staff_orders():
    db = get_db()
    rows = db.execute("""SELECT * FROM orders WHERE status IN
                        ('DROPPED','COLLECTED','WASHING','READY','RETURNED')
                        ORDER BY id DESC LIMIT 100""").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/staff/api/compartments")
@staff_required
def staff_comps():
    locker_id = request.args.get("locker_id","locker1")
    ensure_compartments(locker_id)
    db = get_db()
    rows = db.execute("SELECT compartment, occupied, kind FROM compartments WHERE locker_id=? ORDER BY compartment",
                      (locker_id,)).fetchall()
    return jsonify([{"compartment":r["compartment"],"occupied":bool(r["occupied"]),"kind":r["kind"]} for r in rows])

@app.route("/staff/api/collect", methods=["POST"])
@staff_required
def staff_collect():
    """Staff opens the drop-off compartment to take dirty laundry for washing.
    Frees that compartment. Notifies customer."""
    data = request.get_json(force=True)
    order_id = data.get("order_id")
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order["status"] != "DROPPED":
        return jsonify(ok=False, error="Order not ready to collect."), 400

    comp = order["drop_compartment"]
    set_unlock(order["locker_id"], comp, order["id"])            # open the door
    db.execute("UPDATE orders SET status='COLLECTED', updated_at=? WHERE id=?", (int(time.time()), order_id))
    db.execute("UPDATE compartments SET occupied=0, order_id=NULL, kind=NULL WHERE locker_id=? AND compartment=?",
               (order["locker_id"], comp))
    db.commit()
    notify_customer(order["customer_phone"], "laundry_collected",
                    [order["customer_name"], comp],
                    f"laundry collected from compartment {comp} for washing (order #{order['id']})")
    return jsonify(ok=True, opened=comp)

@app.route("/staff/api/assess", methods=["POST"])
@staff_required
def staff_assess():
    """Staff enter their own count (and weight), the price is worked out, and the customer
    gets a WhatsApp message with the count and amount (flagging any count difference)."""
    data = request.get_json(force=True)
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id=?", (data.get("order_id"),)).fetchone()
    if not order or order["status"] not in ("COLLECTED", "WASHING", "READY"):
        return jsonify(ok=False, error="Collect the laundry first."), 400
    service = (data.get("service") or order["service"] or "").upper()
    if service not in PRICES:
        return jsonify(ok=False, error="Choose a service."), 400
    try:
        staff_count = int(data.get("staff_count"))
        assert staff_count >= 1
    except Exception:
        return jsonify(ok=False, error="Enter the number of items you counted."), 400
    weight = data.get("weight_kg")
    try:
        weight = float(weight) if weight not in (None, "") else None
        assert weight is None or 0 < weight < 200
    except Exception:
        return jsonify(ok=False, error="Weight must be a number in kg, e.g. 2.5"), 400
    standard = calc_amount(service, staff_count, weight)
    extra_amt = data.get("extra_amount") or 0
    try:
        extra_amt = int(round(float(extra_amt)))
        assert extra_amt >= 0
    except Exception:
        return jsonify(ok=False, error="Extra charge must be a number."), 400
    amount = standard + extra_amt
    note = (data.get("price_note") or "").strip()[:200]
    db.execute("""UPDATE orders SET service=?, staff_count=?, weight_kg=?, amount=?, price_note=?, updated_at=?
                  WHERE id=?""", (service, staff_count, weight, amount, note, int(time.time()), order["id"]))
    db.commit()

    # Tell the customer (free if within 24h of their WhatsApp confirmation)
    label = PRICES[service]["label"]
    if weight and weight >= MIN_KG_FOR_KG_RATE and PRICES[service]["per_kg"]:
        basis = f"{weight:g} kg × ₹{PRICES[service]['per_kg']}/kg"
    else:
        basis = f"{staff_count} pcs × ₹{PRICES[service]['per_piece']}"
    lines = [f"Hi {order['customer_name']}, we've checked your Purewash order #{order['id']}.",
             f"Items counted: {staff_count}"]
    mismatch = order["item_count"] and order["item_count"] != staff_count
    if mismatch:
        lines[-1] += f" (you entered {order['item_count']})"
    lines += [f"Service: {label} ({basis})"]
    if extra_amt:
        lines += [f"Special care: ₹{extra_amt}" + (f" ({note})" if note else "")]
    lines += [f"Amount: ₹{amount}"]
    if mismatch:
        lines += ["If the count looks wrong, please reply within 2 hours so we can check before washing."]
    sent = send_text("91" + clean_phone(order["customer_phone"]), "\n".join(lines))
    if not sent:
        notify_staff(f"⚠️ Could not WhatsApp the bill for order #{order['id']} to {order['customer_phone']} "
                     f"(more than 24h since they messaged). Please call them: {staff_count} items, ₹{amount}.")
    if mismatch:
        notify_staff(f"⚠️ Order #{order['id']}: customer entered {order['item_count']} items, staff counted {staff_count}.")
    return jsonify(ok=True, amount=amount, standard=standard, mismatch=bool(mismatch), sent=sent)

@app.route("/staff/api/status", methods=["POST"])
@staff_required
def staff_status():
    """Move an order between WASHING / READY (no door action)."""
    data = request.get_json(force=True)
    order_id = data.get("order_id"); new = (data.get("status") or "").upper()
    if new not in ("WASHING","READY"):
        return jsonify(ok=False, error="Invalid status."), 400
    db = get_db()
    row = db.execute("SELECT amount FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        return jsonify(ok=False, error="Order not found."), 404
    if new == "WASHING" and row["amount"] is None:
        return jsonify(ok=False, error="Count & price the order first."), 400
    db.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (new, int(time.time()), order_id))
    db.commit()
    return jsonify(ok=True)

@app.route("/staff/api/return", methods=["POST"])
@staff_required
def staff_return():
    """Staff returns clean laundry: picks a free compartment, opens it.
    On door-latched it becomes occupied(clean) and the customer is told which box."""
    data = request.get_json(force=True)
    order_id = data.get("order_id"); comp = data.get("compartment")
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order["status"] not in ("READY","WASHING","COLLECTED"):
        return jsonify(ok=False, error="Order not ready to return."), 400
    try: comp = int(comp)
    except: return jsonify(ok=False, error="Pick a free compartment."), 400

    c = db.execute("SELECT occupied FROM compartments WHERE locker_id=? AND compartment=?",
                   (order["locker_id"], comp)).fetchone()
    if not c or c["occupied"] == 1:
        return jsonify(ok=False, error="That compartment is not free."), 409

    set_unlock(order["locker_id"], comp, order["id"])            # open the door
    db.execute("UPDATE orders SET status='RETURNED', return_compartment=?, updated_at=? WHERE id=?",
               (comp, int(time.time()), order_id))
    db.commit()
    # (compartment marked occupied+customer notified when door-latched fires)
    return jsonify(ok=True, opened=comp)

@app.route("/staff/api/open-comp", methods=["POST"])
@staff_required
def staff_open_comp():
    """Manual override: open any compartment (maintenance)."""
    data = request.get_json(force=True)
    locker_id = data.get("locker_id","locker1"); comp = int(data.get("compartment"))
    set_unlock(locker_id, comp, -1)
    return jsonify(ok=True, opened=comp)

# ---------------- WhatsApp webhook (whatsapp_webhook.py) ----------------
from whatsapp_webhook import wa_bp, register_message_handler
app.register_blueprint(wa_bp)
register_message_handler(handle_incoming_whatsapp)

# ---------------- run ----------------
if __name__ == "__main__":
    init_db()
    # Debug mode exposes Flask's interactive debugger to the internet; keep it off on Render.
    # Set FLASK_DEBUG=1 only when testing on your own computer.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")
