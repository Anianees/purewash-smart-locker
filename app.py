"""
Purewash Smart Locker - Backend (6-Compartment, Full Lifecycle)
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

Compartments free up at every handoff, so 6 boxes serve many customers.

Hardware: ESP32 polls /api/check-unlock?device=locker1, opens the compartment
the flag names, then reports /api/door-latched.
"""

import sqlite3, time, os
from functools import wraps
from flask import (Flask, request, jsonify, render_template,
                   redirect, session, g, url_for)
import requests

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "purewash-secret-change-me")
DB_PATH = os.environ.get("DB_PATH", "purewash.db")

# ---------------- CONFIG ----------------
TELEGRAM_BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_STAFF_CHAT_ID = os.environ.get("TELEGRAM_STAFF_CHAT_ID", "")
WHATSAPP_TOKEN         = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID      = os.environ.get("WHATSAPP_PHONE_ID", "")

# Staff portal password (set STAFF_PASSWORD in Render; fallback works immediately)
STAFF_PASSWORD = os.environ.get("STAFF_PASSWORD", "purewash123")

UNLOCK_FLAG_TTL  = 120
NUM_COMPARTMENTS = 6

# ---------------- DB ----------------
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
        drop_compartment INTEGER,
        return_compartment INTEGER,
        customer_name TEXT NOT NULL,
        customer_phone TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'DROPPED',
        created_at INTEGER NOT NULL,
        updated_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS compartments (
        locker_id TEXT NOT NULL,
        compartment INTEGER NOT NULL,
        occupied INTEGER NOT NULL DEFAULT 0,
        order_id INTEGER,
        kind TEXT,                 -- 'DIRTY' or 'CLEAN'
        PRIMARY KEY (locker_id, compartment)
    );
    CREATE TABLE IF NOT EXISTS unlock_flags (
        locker_id TEXT PRIMARY KEY,
        compartment INTEGER,
        order_id INTEGER,
        set_at INTEGER
    );
    """)
    con.commit(); con.close()

def ensure_compartments(locker_id):
    db = get_db()
    for c in range(1, NUM_COMPARTMENTS + 1):
        db.execute("INSERT OR IGNORE INTO compartments (locker_id, compartment, occupied) VALUES (?,?,0)",
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

def notify_customer(phone, message):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
        print(f"[whatsapp -> {phone}] {message}"); return
    try:
        requests.post(f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages",
                      headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                      json={"messaging_product":"whatsapp","to":f"91{phone}",
                            "type":"text","text":{"body":message}}, timeout=8)
    except Exception as e:
        print("WhatsApp failed:", e)

# ---------------- HELPERS ----------------
def set_unlock(locker_id, compartment, order_id):
    db = get_db()
    db.execute("""INSERT INTO unlock_flags (locker_id, compartment, order_id, set_at)
                  VALUES (?,?,?,?)
                  ON CONFLICT(locker_id) DO UPDATE SET
                  compartment=excluded.compartment, order_id=excluded.order_id, set_at=excluded.set_at""",
               (locker_id, compartment, order_id, int(time.time())))
    db.commit()

def free_compartments(locker_id):
    db = get_db()
    rows = db.execute("SELECT compartment FROM compartments WHERE locker_id=? AND occupied=0 ORDER BY compartment",
                      (locker_id,)).fetchall()
    return [r["compartment"] for r in rows]

# ---------------- CUSTOMER PAGES ----------------
@app.route("/")
def home():
    return redirect("/locker/locker1")

@app.route("/locker/<locker_id>")
def locker_page(locker_id):
    ensure_compartments(locker_id)
    return render_template("locker.html", locker_id=locker_id)

@app.route("/api/availability")
def availability():
    locker_id = request.args.get("locker_id","")
    ensure_compartments(locker_id)
    db = get_db()
    rows = db.execute("SELECT compartment, occupied FROM compartments WHERE locker_id=? ORDER BY compartment",
                      (locker_id,)).fetchall()
    return jsonify([{"compartment":r["compartment"],"occupied":bool(r["occupied"])} for r in rows])

@app.route("/api/my-compartment")
def my_compartment():
    locker_id = request.args.get("locker_id","")
    phone = (request.args.get("phone") or "").strip()
    db = get_db()
    # Customer pickup = a RETURNED order (clean laundry waiting) for this phone
    row = db.execute("""SELECT return_compartment FROM orders
                        WHERE locker_id=? AND customer_phone=? AND status='RETURNED'
                        ORDER BY id DESC LIMIT 1""", (locker_id, phone)).fetchone()
    if row and row["return_compartment"]:
        return jsonify(found=True, compartment=row["return_compartment"])
    return jsonify(found=False)

# ---------------- CUSTOMER: open (dropoff / pickup) ----------------
@app.route("/api/open", methods=["POST"])
def open_compartment():
    data = request.get_json(force=True)
    locker_id = (data.get("locker_id") or "").strip()
    name  = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    action = (data.get("action") or "").strip().upper()
    compartment = data.get("compartment")

    if not locker_id or not name or len(phone)!=10 or not phone.isdigit():
        return jsonify(ok=False, error="Enter a valid name and 10-digit phone."), 400
    try: compartment = int(compartment)
    except: return jsonify(ok=False, error="Pick a compartment."), 400

    ensure_compartments(locker_id)
    db = get_db()
    comp = db.execute("SELECT occupied FROM compartments WHERE locker_id=? AND compartment=?",
                      (locker_id, compartment)).fetchone()

    if action == "DROPOFF":
        if comp["occupied"] == 1:
            return jsonify(ok=False, error="That compartment was just taken. Pick another."), 409
        cur = db.execute("""INSERT INTO orders (locker_id, drop_compartment, customer_name, customer_phone,
                            status, created_at) VALUES (?,?,?,?, 'DROPPED', ?)""",
                         (locker_id, compartment, name, phone, int(time.time())))
        order_id = cur.lastrowid
        db.commit()
        set_unlock(locker_id, compartment, order_id)
        return jsonify(ok=True, order_id=order_id, compartment=compartment)

    elif action == "PICKUP":
        # find their RETURNED order in this compartment
        order = db.execute("""SELECT * FROM orders WHERE locker_id=? AND customer_phone=?
                              AND status='RETURNED' AND return_compartment=?
                              ORDER BY id DESC LIMIT 1""",
                           (locker_id, phone, compartment)).fetchone()
        if not order:
            return jsonify(ok=False, error="No ready laundry found for you in that compartment."), 403
        set_unlock(locker_id, compartment, order["id"])
        return jsonify(ok=True, order_id=order["id"], compartment=compartment)

    return jsonify(ok=False, error="Invalid action."), 400

# ---------------- ESP32 polling ----------------
@app.route("/api/check-unlock")
def check_unlock():
    locker_id = request.args.get("device","")
    db = get_db()
    row = db.execute("SELECT compartment, order_id, set_at FROM unlock_flags WHERE locker_id=?",
                     (locker_id,)).fetchone()
    if row and (int(time.time()) - row["set_at"]) <= UNLOCK_FLAG_TTL:
        db.execute("DELETE FROM unlock_flags WHERE locker_id=?", (locker_id,))
        db.commit()
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
        notify_staff(f"NEW DROP-OFF · Locker {locker_id} · Compartment {comp}\n"
                     f"{dropped['customer_name']} · {dropped['customer_phone']}\n"
                     f"Order #{dropped['id']} — please collect for washing.")
        notify_customer(dropped["customer_phone"],
                        f"Hi {dropped['customer_name']}! Purewash has received your laundry in compartment {comp}. "
                        f"We'll pick it up for washing shortly.")
        return jsonify(ok=True)

    # A RETURN just closed -> mark compartment occupied (clean), WhatsApp customer it's ready
    returning = db.execute("""SELECT * FROM orders WHERE locker_id=? AND status='RETURNED' AND return_compartment=?
                             ORDER BY id DESC LIMIT 1""", (locker_id, comp)).fetchone()
    if returning:
        db.execute("UPDATE compartments SET occupied=1, order_id=?, kind='CLEAN' WHERE locker_id=? AND compartment=?",
                   (returning["id"], locker_id, comp))
        db.commit()
        notify_customer(returning["customer_phone"],
                        f"Hi {returning['customer_name']}! Your clean laundry is ready to pick up in "
                        f"compartment {comp} at the Purewash locker.")
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
    notify_customer(order["customer_phone"],
                    f"Hi {order['customer_name']}! Your laundry has been picked up from compartment {comp} "
                    f"and is on its way for washing.")
    return jsonify(ok=True, opened=comp)

@app.route("/staff/api/status", methods=["POST"])
@staff_required
def staff_status():
    """Move an order between WASHING / READY (no door action)."""
    data = request.get_json(force=True)
    order_id = data.get("order_id"); new = (data.get("status") or "").upper()
    if new not in ("WASHING","READY"):
        return jsonify(ok=False, error="Invalid status."), 400
    db = get_db()
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

# ---------------- run ----------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=True)
