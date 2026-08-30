"""
CARSTER Lead Notification Webhook
----------------------------------
Receives ElevenLabs post-call webhooks (post_call_transcription events),
pulls out the lead fields captured by the Update State tool, and emails
a summary to the dealership inbox.

No third-party automation tools (Zapier, Make, etc.) required — this is
a small, self-hosted Flask app you deploy for free on a host like Render
or PythonAnywhere.
"""

import os
import hmac
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import Flask, request, jsonify

app = Flask(__name__)

# ---- Configuration (set these as environment variables on your host) ----
ELEVENLABS_WEBHOOK_SECRET = os.environ.get("ELEVENLABS_WEBHOOK_SECRET", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")   # your Gmail address
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")   # Gmail App Password (not your normal password)
NOTIFY_TO_EMAIL = os.environ.get("NOTIFY_TO_EMAIL", "")  # where lead emails should be sent

# The Update State field names you configured in ElevenLabs.
LEAD_FIELDS = [
    "first_name",
    "last_name",
    "phone",
    "email",
    "vehicle_of_interest",
    "appointment_request",
    "buying_motivation",
    "hot_buttons",
    "budget",
    "trade_in",
    "financing_interest",
    "purchase_urgency",
    "customer_concerns",
]


def verify_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """Verify the HMAC-SHA256 signature ElevenLabs sends with each webhook."""
    if not ELEVENLABS_WEBHOOK_SECRET:
        # No secret configured — skip verification (not recommended for production).
        return True
    if not signature_header:
        return False

    # ElevenLabs sends a header like: t=timestamp,v0=hex_signature
    parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
    timestamp = parts.get("t", "")
    signature = parts.get("v0", "")

    signed_payload = f"{timestamp}.{payload_bytes.decode('utf-8')}"
    expected = hmac.new(
        ELEVENLABS_WEBHOOK_SECRET.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


def extract_lead_data(payload: dict) -> dict:
    """Pull transcript, summary, and Update State lead fields out of the payload."""
    data = payload.get("data", {})

    conversation_id = data.get("conversation_id", "unknown")
    agent_id = data.get("agent_id", "unknown")

    analysis = data.get("analysis", {}) or {}
    summary = analysis.get("transcript_summary") or analysis.get("call_summary") or ""

    # Update State / dynamic variables are typically under data_collection_results
    # or conversation_initiation_client_data depending on the payload version —
    # we check a couple of likely locations to be safe.
    collected = {}
    for key in ("data_collection_results", "collected_data", "dynamic_variables"):
        block = data.get(key)
        if isinstance(block, dict):
            collected.update(block)

    lead = {}
    for field in LEAD_FIELDS:
        value = collected.get(field)
        if isinstance(value, dict):
            value = value.get("value")
        lead[field] = value or "Not provided"

    transcript_turns = data.get("transcript", []) or []
    transcript_text = "\n".join(
        f"{turn.get('role', '?')}: {turn.get('message', '')}"
        for turn in transcript_turns
        if turn.get("message")
    )

    return {
        "conversation_id": conversation_id,
        "agent_id": agent_id,
        "summary": summary,
        "lead": lead,
        "transcript_text": transcript_text,
    }


def build_email(info: dict) -> MIMEMultipart:
    lead = info["lead"]

    lines = [
        f"New CARSTER call — Conversation ID: {info['conversation_id']}",
        "",
        "== Lead Details ==",
    ]
    for field in LEAD_FIELDS:
        label = field.replace("_", " ").title()
        lines.append(f"{label}: {lead.get(field, 'Not provided')}")

    if info["summary"]:
        lines += ["", "== Call Summary ==", info["summary"]]

    if info["transcript_text"]:
        lines += ["", "== Full Transcript ==", info["transcript_text"]]

    body = "\n".join(lines)

    msg = MIMEMultipart()
    msg["Subject"] = f"New CARSTER Lead — {lead.get('first_name', 'Unknown')} {lead.get('last_name', '')}".strip()
    msg["From"] = SMTP_USERNAME
    msg["To"] = NOTIFY_TO_EMAIL
    msg.attach(MIMEText(body, "plain"))
    return msg


def send_email(msg: MIMEMultipart) -> None:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(SMTP_USERNAME, [NOTIFY_TO_EMAIL], msg.as_string())


@app.route("/webhook/elevenlabs", methods=["POST"])
def elevenlabs_webhook():
    payload_bytes = request.get_data()
    signature_header = request.headers.get("elevenlabs-signature", "")

    if not verify_signature(payload_bytes, signature_header):
        return jsonify({"error": "invalid signature"}), 401

    payload = request.get_json(silent=True) or {}
    event_type = payload.get("type", "")

    # Only act on completed call transcription events.
    if event_type != "post_call_transcription":
        return jsonify({"status": "ignored", "type": event_type}), 200

    info = extract_lead_data(payload)

    try:
        msg = build_email(info)
        send_email(msg)
    except Exception as exc:  # noqa: BLE001 — log and still ack the webhook
        print(f"Failed to send email: {exc}")
        return jsonify({"status": "received", "email_sent": False}), 200

    return jsonify({"status": "received", "email_sent": True}), 200


@app.route("/", methods=["GET"])
def health_check():
    return "CARSTER webhook receiver is running.", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
