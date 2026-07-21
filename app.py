"""
Purewash Smart Locker - Backend
--------------------------------
One Flask app that does everything:
1. Serves the customer-facing drop-off page (QR code points here)
2. Receives the customer's name + phone, creates an order, sets unlock flag
3. ESP32 polls /api/check-unlock every 2s; gets the flag once, fires the lock
4. ESP32 reports door re-latch to /api/door-latched -> order marked DROPPED
5. Sends notifications: Telegram to Purewash staff (free, instant),
   WhatsApp to customer (Phase 2 - via Meta Cloud API, stub included)

Deploy free on Render.com or Railway.app. SQLite for storage (fine for
one or a handful of lockers; upgrade to Postgres later if you scale).

To run locally:  pip install flask requests
                 python app.py
"""

import sqlite3
import time
import os
from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    redirect,
    g,
)

app = Flask(__name__)
DB_PATH = os.environ.get("DB_PATH", "purewash.db")

# ----------------------------------------------------------------------
# CONFIG - fill these in
# ----------------------------------------------------------------------
# Telegram bot for staff notifications (free, 5-min setup):
#   1. Message @BotFather on Telegram -> /newbot -> get the token
#   2. Message your new bot once, then visit
#      https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat_id
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_STAFF_CHAT_ID = os.environ.get("TELEGRAM_STAFF_CHAT_ID", "")

# WhatsApp Cloud API (Phase 2 - needs Meta Business setup)
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")

# Unlock flag expires after this many seconds if the ESP32 doesn't claim it
UNLOCK_FLAG_TTL = 30

# ----------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------
def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        locker_id TEXT NOT NULL,
        customer_name TEXT NOT NULL,
        customer_phone TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPENED',  -- OPENED -> DROPPED -> PICKED_UP -> DELIVERED
        created_at INTEGER NOT NULL,
        dropped_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS unlock_flags (
        locker_id TEXT PRIMARY KEY,
        order_id INTEGER,
        set_at INTEGER
    );
    """)
    con.commit()
    con.close()
init_db()
# ----------------------------------------------------------------------
# NOTIFICATIONS
# ----------------------------------------------------------------------
import requests

def notify_staff(text):
    """Telegram message to Purewash staff. Free and instant."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_STAFF_CHAT_ID:
        print("[notify_staff - not configured]", text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_STAFF_CHAT_ID, "text": text},
            timeout=5,
        )
    except Exception as e:
        print("Telegram notify failed:", e)

def notify_customer_whatsapp(phone, name):
    """
    Phase 2: WhatsApp Cloud API template message.
    Until that's approved, this logs the message; staff can also
    manually WhatsApp the customer using the number from the staff alert.
    """
    message = (
        f"Hi {name}! Purewash has received your laundry drop-off. "
        f"We'll pick it up shortly and send delivery updates here. Thank you!"
    )
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        print(f"[whatsapp stub -> {phone}]", message)
        return
    try:
        requests.post(
            f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            json={
                "messaging_product": "whatsapp",
                "to": f"91{phone}",  # assumes Indian numbers
                "type": "text",
                "text": {"body": message},
            },
            timeout=8,
        )
    except Exception as e:
        print("WhatsApp notify failed:", e)

# ----------------------------------------------------------------------
# CUSTOMER-FACING PAGE (QR on the locker points to /locker/<locker_id>)
# ----------------------------------------------------------------------

 
 
@app.route("/")
def home():
    return redirect("/locker/locker1")

@app.route("/locker/<locker_id>")
def locker_page(locker_id):
    return render_template("drop_off.html", locker_id=locker_id)

# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
@app.route("/api/dropoff", methods=["POST"])
def dropoff():
    data = request.get_json(force=True)
    locker_id = (data.get("locker_id") or "").strip()
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()

    if not locker_id or not name or len(phone) != 10 or not phone.isdigit():
        return jsonify(ok=False, error="Please enter a valid name and 10-digit phone."), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO orders (locker_id, customer_name, customer_phone, status, created_at) "
        "VALUES (?, ?, ?, 'OPENED', ?)",
        (locker_id, name, phone, int(time.time())),
    )
    order_id = cur.lastrowid
    db.execute(
        "INSERT INTO unlock_flags (locker_id, order_id, set_at) VALUES (?, ?, ?) "
        "ON CONFLICT(locker_id) DO UPDATE SET order_id=excluded.order_id, set_at=excluded.set_at",
        (locker_id, order_id, int(time.time())),
    )
    db.commit()
    return jsonify(ok=True, order_id=order_id)

@app.route("/api/check-unlock")
def check_unlock():
    """ESP32 polls this every 2 seconds: /api/check-unlock?device=locker1"""
    locker_id = request.args.get("device", "")
    db = get_db()
    row = db.execute(
        "SELECT order_id, set_at FROM unlock_flags WHERE locker_id = ?", (locker_id,)
    ).fetchone()
    if row and (int(time.time()) - row["set_at"]) <= UNLOCK_FLAG_TTL:
        # Clear the flag so the lock fires exactly once
        db.execute("DELETE FROM unlock_flags WHERE locker_id = ?", (locker_id,))
        db.commit()
        return jsonify(unlock=True, order_id=row["order_id"])
    return jsonify(unlock=False)

@app.route("/api/door-latched", methods=["POST"])
def door_latched():
    """ESP32 reports the door closed & latched after an unlock."""
    data = request.get_json(force=True)
    locker_id = (data.get("device") or "").strip()
    db = get_db()
    order = db.execute(
        "SELECT * FROM orders WHERE locker_id = ? AND status = 'OPENED' "
        "ORDER BY id DESC LIMIT 1",
        (locker_id,),
    ).fetchone()
    if not order:
        return jsonify(ok=True, note="no open order")

    db.execute(
        "UPDATE orders SET status='DROPPED', dropped_at=? WHERE id=?",
        (int(time.time()), order["id"]),
    )
    db.commit()

    notify_staff(
        f"🧺 New drop-off at Locker {locker_id}\n"
        f"Customer: {order['customer_name']}\n"
        f"Phone: {order['customer_phone']}\n"
        f"Order #{order['id']} — schedule pickup."
    )
    notify_customer_whatsapp(order["customer_phone"], order["customer_name"])
    return jsonify(ok=True)

@app.route("/api/orders")
def orders():
    """Simple ops view: latest 50 orders as JSON. Add auth before going public."""
    db = get_db()
    rows = db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify([dict(r) for r in rows])

# ----------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
