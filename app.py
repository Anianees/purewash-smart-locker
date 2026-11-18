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

import sqlite3, time, os, threading
from functools import wraps
from flask import (Flask, request, jsonify, render_template,
                   redirect, session, g, url_for)
import requests

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
    con.commit(); con.close()
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
                            status, created_at) VALUES (?,?,?,?, 'DROPPED', ?) RETURNING id""",
                         (locker_id, compartment, name, phone, int(time.time())))
        order_id = cur.fetchone()["id"]
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
        # Customer is collecting -> close the order and free the compartment
        db.execute("UPDATE orders SET status='PICKED_UP', updated_at=? WHERE id=?",
                   (int(time.time()), order["id"]))
        db.execute("UPDATE compartments SET occupied=0, order_id=NULL, kind=NULL WHERE locker_id=? AND compartment=?",
                   (locker_id, compartment))
        db.commit()
        notify_staff(f"PICKED UP · Locker {locker_id} · Compartment {compartment}\n"
                     f"{order['customer_name']} · Order #{order['id']} complete.")
        return jsonify(ok=True, order_id=order["id"], compartment=compartment)

    return jsonify(ok=False, error="Invalid action."), 400

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
        notify_staff(f"NEW DROP-OFF · Locker {locker_id} · Compartment {comp}\n"
                     f"{dropped['customer_name']} · {dropped['customer_phone']}\n"
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

# ---------------- WhatsApp webhook (whatsapp_webhook.py) ----------------
from whatsapp_webhook import wa_bp
app.register_blueprint(wa_bp)

# ---------------- run ----------------
if __name__ == "__main__":
    init_db()
    # Debug mode exposes Flask's interactive debugger to the internet; keep it off on Render.
    # Set FLASK_DEBUG=1 only when testing on your own computer.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")
