"""
CARSTER Lead Notification Webhook
----------------------------------
Receives ElevenLabs post-call webhooks and emails ONE combined summary
(lead details + call recording, when available) to the dealership inbox
per call.

Sends email via the Resend API (HTTPS) instead of raw SMTP, since many
free hosting platforms (including Render's free tier) block or heavily
throttle outbound SMTP connections, causing sends to hang and time out.
Resend has a free tier (100 emails/day) and works over normal HTTPS.

ElevenLabs delivers the transcript and the audio recording as two
separate webhook events (post_call_transcription and post_call_audio),
which can arrive in either order. This app holds whichever piece
arrives first in memory, keyed by conversation_id, and sends a single
combined email once both pieces for that conversation have arrived. If
only one piece ever arrives (e.g. audio disabled, or the app restarts
between the two deliveries), a short timeout sends what's available
rather than waiting forever.
"""

import os
import json
import hmac
import hashlib
import base64
import threading

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---- Configuration (set these as environment variables on your host) ----
ELEVENLABS_WEBHOOK_SECRET = os.environ.get("ELEVENLABS_WEBHOOK_SECRET", "")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
# "From" address must be on a domain you've verified with Resend, OR use
# Resend's shared test sender "onboarding@resend.dev" (fine for getting
# started; swap in your own verified domain later for production use).
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")
NOTIFY_TO_EMAIL = os.environ.get("NOTIFY_TO_EMAIL", "")  # where lead emails should be sent

# How long to wait for the second piece (transcript or audio) before
# sending an email with just whichever piece has arrived.
PAIRING_TIMEOUT_SECONDS = int(os.environ.get("PAIRING_TIMEOUT_SECONDS", "45"))

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

# In-memory store: conversation_id -> {"lead_info": ..., "audio_bytes": ..., "timer": ...}
_pending = {}
_pending_lock = threading.Lock()


def verify_signature(payload_bytes, signature_header):
    """Verify the HMAC-SHA256 signature ElevenLabs sends with each webhook."""
    if not ELEVENLABS_WEBHOOK_SECRET:
        return True
    if not signature_header:
        return False

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


def extract_lead_data(payload):
    """Pull transcript, summary, and Update State lead fields out of the payload."""
    data = payload.get("data", {})

    conversation_id = data.get("conversation_id", "unknown")
    agent_id = data.get("agent_id", "unknown")

    analysis = data.get("analysis", {}) or {}
    summary = analysis.get("transcript_summary") or analysis.get("call_summary") or ""

    collected = {}
    for key in ("data_collection_results", "collected_data", "dynamic_variables"):
        block = data.get(key)
        if isinstance(block, dict):
            collected.update(block)
    dcr = analysis.get("data_collection_results")
    if isinstance(dcr, dict):
        collected.update(dcr)

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


def build_email_payload(conversation_id, lead_info, audio_bytes):
    """Build the JSON payload for the Resend API (https://resend.com/docs/api-reference/emails/send-email)."""
    lead = (lead_info or {}).get("lead", {})

    lines = [f"New CARSTER call — Conversation ID: {conversation_id}"]

    if lead_info:
        lines += ["", "== Lead Details =="]
        for field in LEAD_FIELDS:
            label = field.replace("_", " ").title()
            lines.append(f"{label}: {lead.get(field, 'Not provided')}")

        if lead_info.get("summary"):
            lines += ["", "== Call Summary ==", lead_info["summary"]]

        if lead_info.get("transcript_text"):
            lines += ["", "== Full Transcript ==", lead_info["transcript_text"]]
    else:
        lines += ["", "(Lead details were not received for this call.)"]

    if not audio_bytes:
        lines += ["", "(No call recording was received for this call.)"]

    body_text = "\n".join(lines)
    # Resend renders "text" with line breaks preserved when no html is given,
    # but wrapping in <pre> via html keeps formatting predictable everywhere.
    body_html = "<pre style='font-family: monospace; white-space: pre-wrap;'>" + \
        body_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + \
        "</pre>"

    name_part = f"{lead.get('first_name', 'Unknown')} {lead.get('last_name', '')}".strip()
    subject = f"New CARSTER Lead — {name_part}" if lead_info else f"CARSTER Call — {conversation_id}"

    payload = {
        "from": RESEND_FROM_EMAIL,
        "to": [NOTIFY_TO_EMAIL],
        "subject": subject,
        "text": body_text,
        "html": body_html,
    }

    if audio_bytes:
        payload["attachments"] = [{
            "filename": f"carster_call_{conversation_id}.mp3",
            "content": base64.b64encode(audio_bytes).decode("ascii"),
        }]

    return payload


def send_email(payload):
    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Resend API error {response.status_code}: {response.text}")


def _send_and_clear(conversation_id):
    """Send whatever we have for this conversation, then clear it out."""
    with _pending_lock:
        entry = _pending.pop(conversation_id, None)
    if entry is None:
        return
    try:
        payload = build_email_payload(conversation_id, entry.get("lead_info"), entry.get("audio_bytes"))
        send_email(payload)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to send combined email for {conversation_id}: {exc}")


def _schedule_timeout(conversation_id):
    timer = threading.Timer(PAIRING_TIMEOUT_SECONDS, _send_and_clear, args=[conversation_id])
    timer.daemon = True
    timer.start()
    return timer


def handle_piece(conversation_id, lead_info, audio_bytes):
    with _pending_lock:
        entry = _pending.get(conversation_id)
        if entry is None:
            entry = {"lead_info": None, "audio_bytes": None, "timer": None}
            _pending[conversation_id] = entry

        if lead_info is not None:
            entry["lead_info"] = lead_info
        if audio_bytes is not None:
            entry["audio_bytes"] = audio_bytes

        have_both = entry["lead_info"] is not None and entry["audio_bytes"] is not None

        if have_both:
            if entry.get("timer"):
                entry["timer"].cancel()
            should_send_now = True
        else:
            if entry.get("timer") is None:
                entry["timer"] = _schedule_timeout(conversation_id)
            should_send_now = False

    if should_send_now:
        _send_and_clear(conversation_id)


def read_request_body():
    """Read the full request body, handling chunked-transfer audio webhooks."""
    if request.headers.get("transfer-encoding", "").lower() == "chunked":
        chunks = bytearray()
        while True:
            chunk = request.stream.read(8192)
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)
    return request.get_data()


@app.route("/webhook/elevenlabs", methods=["POST"])
def elevenlabs_webhook():
    payload_bytes = read_request_body()
    signature_header = request.headers.get("elevenlabs-signature", "")

    if not verify_signature(payload_bytes, signature_header):
        return jsonify({"error": "invalid signature"}), 401

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        payload = {}

    event_type = payload.get("type", "")

    if event_type == "post_call_transcription":
        info = extract_lead_data(payload)
        handle_piece(info["conversation_id"], lead_info=info, audio_bytes=None)
        return jsonify({"status": "received"}), 200

    if event_type == "post_call_audio":
        data = payload.get("data", {})
        conversation_id = data.get("conversation_id", "unknown")
        full_audio_b64 = data.get("full_audio", "")
        try:
            audio_bytes = base64.b64decode(full_audio_b64)
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to decode audio for {conversation_id}: {exc}")
            return jsonify({"status": "received", "audio_decoded": False}), 200
        handle_piece(conversation_id, lead_info=None, audio_bytes=audio_bytes)
        return jsonify({"status": "received"}), 200

    # Any other event type (call_initiation_failure, etc.) — ignore gracefully.
    return jsonify({"status": "ignored", "type": event_type}), 200


@app.route("/", methods=["GET"])
def health_check():
    return "CARSTER webhook receiver is running.", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
