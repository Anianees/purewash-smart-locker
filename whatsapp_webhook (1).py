"""
WhatsApp Cloud API webhook for Purewash (Flask blueprint).

HOW TO ADD IT
1. Put this file next to app.py in the purewash-smart-locker repo.
2. app.py registers it (see the "WhatsApp webhook" lines near the bottom of app.py).
3. On Render -> your service -> Environment, add:
       WA_VERIFY_TOKEN     any random string you invent, e.g. purewash_wh_7Kq92x
                           (type the SAME string in Meta's "Verify token" box)
       WA_APP_SECRET       Meta developer app -> App settings -> Basic -> App secret
                           (optional but recommended: rejects fake requests)
   It reuses your existing TELEGRAM_BOT_TOKEN and TELEGRAM_STAFF_CHAT_ID.
4. Callback URL for Meta:
       https://purewash-smart-locker.onrender.com/whatsapp/webhook

No extra packages needed (uses only Flask + Python standard library).
"""

import hashlib
import hmac
import json
import logging
import os
import urllib.parse
import urllib.request

from flask import Blueprint, abort, request

wa_bp = Blueprint("whatsapp", __name__)
log = logging.getLogger("whatsapp")

VERIFY_TOKEN = os.environ.get("WA_VERIFY_TOKEN", "")
APP_SECRET = os.environ.get("WA_APP_SECRET", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_STAFF_CHAT_ID", "")


# ---------------------------------------------------------------------------
# 1. Verification handshake (Meta calls this once when you click "Verify and save")
# ---------------------------------------------------------------------------
@wa_bp.route("/whatsapp/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and VERIFY_TOKEN and token == VERIFY_TOKEN:
        # Meta expects the challenge back as plain text with status 200
        return challenge, 200, {"Content-Type": "text/plain"}
    return "Forbidden", 403


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _signature_ok(raw_body: bytes) -> bool:
    """Check Meta's X-Hub-Signature-256 header. Skipped if WA_APP_SECRET is not set."""
    if not APP_SECRET:
        return True
    sent = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent, expected)


def _telegram(text: str) -> None:
    """Forward a short alert to the staff Telegram chat, if configured."""
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=5
        )
    except Exception as exc:  # never let Telegram problems break the webhook
        log.warning("Telegram forward failed: %s", exc)


# ---------------------------------------------------------------------------
# 2. Incoming events (customer messages + delivery statuses)
# ---------------------------------------------------------------------------
@wa_bp.route("/whatsapp/webhook", methods=["POST"])
def receive():
    raw = request.get_data()
    if not _signature_ok(raw):
        abort(403)

    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return "ok", 200  # always 200 so Meta doesn't keep retrying junk

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # Customer names, keyed by WhatsApp ID
            names = {
                c.get("wa_id"): c.get("profile", {}).get("name", "")
                for c in value.get("contacts", [])
            }

            # Messages customers send to the API number
            for msg in value.get("messages", []):
                sender = msg.get("from", "")
                kind = msg.get("type", "")
                if kind == "text":
                    body = msg.get("text", {}).get("body", "")
                elif kind == "button":
                    body = msg.get("button", {}).get("text", "")
                elif kind == "interactive":
                    inter = msg.get("interactive", {})
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    body = reply.get("title", "[interactive]")
                else:
                    body = f"[{kind}]"
                log.info("WA message from %s: %s", sender, body)
                _telegram(f"📩 WhatsApp from {names.get(sender) or sender} (+{sender}):\n{body}")

            # Delivery status updates for messages Purewash sent
            for st in value.get("statuses", []):
                status = st.get("status")
                log.info("WA status %s -> %s: %s", st.get("id"), st.get("recipient_id"), status)
                if status == "failed":
                    err = (st.get("errors") or [{}])[0]
                    _telegram(
                        f"⚠️ WhatsApp message to +{st.get('recipient_id')} failed: "
                        f"{err.get('title', 'unknown error')}"
                    )

    return "ok", 200
