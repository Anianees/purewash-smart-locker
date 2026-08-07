"""
Purewash Smart Locker - Backend (6-Compartment Version)
-------------------------------------------------------
Extends the single-locker pilot to a 6-compartment cabinet.

Customer flow:
1. QR on cabinet -> /locker/<locker_id>
2. Customer enters name + phone, chooses PICKUP or DROP-OFF
3. DROP-OFF: sees only EMPTY compartments -> selects one -> it opens ->
   they load laundry, close door -> compartment marked OCCUPIED (their name/phone)
4. PICKUP: system finds THEIR compartment by phone -> opens it ->
   they take laundry, close door -> compartment marked EMPTY again

Hardware flow (unchanged in spirit from the pilot):
- ESP32 polls /api/check-unlock?device=locker1 every 2s
- Server replies which COMPARTMENT number (1-6) to open, once
- ESP32 fires that lock, then reports /api/door-latched
- Telegram alerts staff on every drop-off and pickup

Deploy free on Render.com. SQLite storage.
Run locally:  pip install flask requests
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
# CONFIG
# ----------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_STAFF_CHAT_ID = os.environ.get("TELEGRAM_STAFF_CHAT_ID", "")

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")

# Unlock flag expires after this many seconds if the ESP32 doesn't claim it
UNLOCK_FLAG_TTL = 120

# How many compartments this cabinet has
NUM_COMPARTMENTS = 6

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
        compartment INTEGER NOT NULL,
        customer_name TEXT NOT NULL,
        customer_phone TEXT NOT NULL,
        action TEXT NOT NULL,                   -- 'DROPOFF' or 'PICKUP'
        status TEXT NOT NULL DEFAULT 'OPENED',  -- OPENED -> DROPPED / PICKED_UP
        created_at INTEGER NOT NULL,
        closed_at INTEGER
    );

    -- One row per compartment tracking who (if anyone) is using it
    CREATE TABLE IF NOT EXISTS compartments (
        locker_id TEXT NOT NULL,
        compartment INTEGER NOT NULL,
        occupied INTEGER NOT NULL DEFAULT 0,    -- 0 = empty, 1 = occupied
        customer_name TEXT,
        customer_phone TEXT,
        since INTEGER,
        PRIMARY KEY (locker_id, compartment)
    );

    -- Unlock flag now carries WHICH compartment to open
    CREATE TABLE IF NOT EXISTS unlock_flags (
        locker_id TEXT PRIMARY KEY,
        compartment INTEGER,
        order_id INTEGER,
        set_at INTEGER
    );
    """)
    con.commit()
    con.close()

def ensure_compartments(locker_id):
    """Make sure the 6 compartment rows exist for this locker."""
    db = get_db()
    for c in range(1, NUM_COMPARTMENTS + 1):
        db.execute(
            "INSERT OR IGNORE INTO compartments (locker_id, compartment, occupied) "
            "VALUES (?, ?, 0)",
            (locker_id, c),
        )
    db.commit()

init_db()

# ----------------------------------------------------------------------
# NOTIFICATIONS
# ----------------------------------------------------------------------
import requests

def notify_staff(text):
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

def notify_customer_whatsapp(phone, name, action):
    if action == "DROPOFF":
        message = (
            f"Hi {name}! Purewash has received your laundry drop-off. "
            f"We'll pick it up shortly and send delivery updates here. Thank you!"
        )
    else:
        message = (
            f"Hi {name}! Your laundry has been picked up from the Purewash locker. "
            f"Thank you for using Purewash!"
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
                "to": f"91{phone}",
                "type": "text",
                "text": {"body": message},
            },
            timeout=8,
        )
    except Exception as e:
        print("WhatsApp notify failed:", e)

# ----------------------------------------------------------------------
# CUSTOMER-FACING PAGE
# ----------------------------------------------------------------------
@app.route("/")
def home():
    return redirect("/locker/locker1")

@app.route("/locker/<locker_id>")
def locker_page(locker_id):
    ensure_compartments(locker_id)
    return render_template("locker.html", locker_id=locker_id)

# ----------------------------------------------------------------------
# API - availability
# ----------------------------------------------------------------------
@app.route("/api/availability")
def availability():
    """
    Returns each compartment's status for the grid.
    /api/availability?locker_id=locker1
    """
    locker_id = request.args.get("locker_id", "")
    ensure_compartments(locker_id)
    db = get_db()
    rows = db.execute(
        "SELECT compartment, occupied FROM compartments "
        "WHERE locker_id = ? ORDER BY compartment",
        (locker_id,),
    ).fetchall()
    return jsonify([
        {"compartment": r["compartment"], "occupied": bool(r["occupied"])}
        for r in rows
    ])

@app.route("/api/my-compartment")
def my_compartment():
    """
    For PICKUP: find the compartment holding this phone's laundry.
    /api/my-compartment?locker_id=locker1&phone=9876543210
    """
    locker_id = request.args.get("locker_id", "")
    phone = (request.args.get("phone") or "").strip()
    db = get_db()
    row = db.execute(
        "SELECT compartment FROM compartments "
        "WHERE locker_id = ? AND occupied = 1 AND customer_phone = ?",
        (locker_id, phone),
    ).fetchone()
    if row:
        return jsonify(found=True, compartment=row["compartment"])
    return jsonify(found=False)

# ----------------------------------------------------------------------
# API - open a compartment (drop-off or pickup)
# ----------------------------------------------------------------------
@app.route("/api/open", methods=["POST"])
def open_compartment():
    """
    Customer selected a compartment (drop-off) or requested pickup.
    Body: { locker_id, name, phone, action ('DROPOFF'|'PICKUP'), compartment }
    Sets the unlock flag so the ESP32 opens that compartment.
    """
    data = request.get_json(force=True)
    locker_id = (data.get("locker_id") or "").strip()
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    action = (data.get("action") or "").strip().upper()
    compartment = data.get("compartment")

    # Validate
    if not locker_id or not name or len(phone) != 10 or not phone.isdigit():
        return jsonify(ok=False, error="Please enter a valid name and 10-digit phone."), 400
    if action not in ("DROPOFF", "PICKUP"):
        return jsonify(ok=False, error="Invalid action."), 400
    try:
        compartment = int(compartment)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Pick a compartment."), 400
    if compartment < 1 or compartment > NUM_COMPARTMENTS:
        return jsonify(ok=False, error="Invalid compartment."), 400

    ensure_compartments(locker_id)
    db = get_db()

    comp = db.execute(
        "SELECT occupied, customer_phone FROM compartments "
        "WHERE locker_id = ? AND compartment = ?",
        (locker_id, compartment),
    ).fetchone()

    # Guard rails so two people don't grab the same box
    if action == "DROPOFF" and comp["occupied"] == 1:
        return jsonify(ok=False, error="That compartment was just taken. Pick another."), 409
    if action == "PICKUP":
        if comp["occupied"] != 1 or comp["customer_phone"] != phone:
            return jsonify(ok=False, error="That compartment isn't holding your laundry."), 403

    # Record the order
    cur = db.execute(
        "INSERT INTO orders (locker_id, compartment, customer_name, customer_phone, "
        "action, status, created_at) VALUES (?, ?, ?, ?, ?, 'OPENED', ?)",
        (locker_id, compartment, name, phone, action, int(time.time())),
    )
    order_id = cur.lastrowid

    # Set the unlock flag for THIS compartment
    db.execute(
        "INSERT INTO unlock_flags (locker_id, compartment, order_id, set_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(locker_id) DO UPDATE SET "
        "compartment=excluded.compartment, order_id=excluded.order_id, set_at=excluded.set_at",
        (locker_id, compartment, order_id, int(time.time())),
    )
    db.commit()
    return jsonify(ok=True, order_id=order_id, compartment=compartment)

# ----------------------------------------------------------------------
# API - ESP32 polling (unchanged endpoint, now returns compartment)
# ----------------------------------------------------------------------
@app.route("/api/check-unlock")
def check_unlock():
    """ESP32 polls: /api/check-unlock?device=locker1
    Now returns which compartment to open."""
    locker_id = request.args.get("device", "")
    db = get_db()
    row = db.execute(
        "SELECT compartment, order_id, set_at FROM unlock_flags WHERE locker_id = ?",
        (locker_id,),
    ).fetchone()
    if row and (int(time.time()) - row["set_at"]) <= UNLOCK_FLAG_TTL:
        db.execute("DELETE FROM unlock_flags WHERE locker_id = ?", (locker_id,))
        db.commit()
        return jsonify(unlock=True, compartment=row["compartment"], order_id=row["order_id"])
    return jsonify(unlock=False)

@app.route("/api/door-latched", methods=["POST"])
def door_latched():
    """ESP32 reports the door closed & latched after an unlock.
    Body: { device, compartment }  (compartment optional but recommended)"""
    data = request.get_json(force=True)
    locker_id = (data.get("device") or "").strip()
    compartment = data.get("compartment")
    db = get_db()

    # Find the most recent open order for this locker (optionally this compartment)
    if compartment is not None:
        order = db.execute(
            "SELECT * FROM orders WHERE locker_id = ? AND compartment = ? AND status = 'OPENED' "
            "ORDER BY id DESC LIMIT 1",
            (locker_id, int(compartment)),
        ).fetchone()
    else:
        order = db.execute(
            "SELECT * FROM orders WHERE locker_id = ? AND status = 'OPENED' "
            "ORDER BY id DESC LIMIT 1",
            (locker_id,),
        ).fetchone()

    if not order:
        return jsonify(ok=True, note="no open order")

    comp = order["compartment"]

    if order["action"] == "DROPOFF":
        # Mark order dropped, compartment now occupied by this customer
        db.execute(
            "UPDATE orders SET status='DROPPED', closed_at=? WHERE id=?",
            (int(time.time()), order["id"]),
        )
        db.execute(
            "UPDATE compartments SET occupied=1, customer_name=?, customer_phone=?, since=? "
            "WHERE locker_id=? AND compartment=?",
            (order["customer_name"], order["customer_phone"], int(time.time()),
             locker_id, comp),
        )
        db.commit()
        notify_staff(
            f"New DROP-OFF at Locker {locker_id}, Compartment {comp}\n"
            f"Customer: {order['customer_name']}\n"
            f"Phone: {order['customer_phone']}\n"
            f"Order #{order['id']} - schedule pickup."
        )
        notify_customer_whatsapp(order["customer_phone"], order["customer_name"], "DROPOFF")

    else:  # PICKUP
        # Mark order picked up, compartment now empty
        db.execute(
            "UPDATE orders SET status='PICKED_UP', closed_at=? WHERE id=?",
            (int(time.time()), order["id"]),
        )
        db.execute(
            "UPDATE compartments SET occupied=0, customer_name=NULL, customer_phone=NULL, since=NULL "
            "WHERE locker_id=? AND compartment=?",
            (locker_id, comp),
        )
        db.commit()
        notify_staff(
            f"PICKUP at Locker {locker_id}, Compartment {comp}\n"
            f"Customer: {order['customer_name']}\n"
            f"Phone: {order['customer_phone']}\n"
            f"Compartment {comp} is now free."
        )
        notify_customer_whatsapp(order["customer_phone"], order["customer_name"], "PICKUP")

    return jsonify(ok=True)

@app.route("/api/orders")
def orders():
    """Ops view: latest 50 orders as JSON."""
    db = get_db()
    rows = db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify([dict(r) for r in rows])

# ----------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
