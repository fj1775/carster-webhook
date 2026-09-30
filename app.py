"""
CARSTER Lead Notification Webhook
----------------------------------
Receives ElevenLabs post-call webhooks and emails a summary to the
dealership inbox.

Handles two event types:
- post_call_transcription: lead fields, call summary, full transcript
- post_call_audio: the call recording (MP3), sent as an email attachment

No third-party automation tools (Zapier, Make, etc.) required — this is
a small, self-hosted Flask app deployed for free on Render.
"""

import os
import hmac
import hashlib
import base64
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

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
    agent_id = data.get("agent_id",
