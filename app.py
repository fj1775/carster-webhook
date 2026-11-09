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

# Agents in this ElevenLabs workspace share this webhook. Map agent IDs to the
# business name used in the email. Anything not listed is labeled CARSTER.
FOXTROT_AGENT_ID = os.environ.get("FOXTROT_AGENT_ID", "agent_1201m6ac1kw4en5t73rm5bmg7dxs")
FOXTROT_PHONE_AGENT_ID = os.environ.get("FOXTROT_PHONE_AGENT_ID", "agent_3501m6n6zg34ff1rq75yj7m3a6ah")
AGENT_BRANDS = {
    FOXTROT_AGENT_ID: "Foxtrot Motors",
    FOXTROT_PHONE_AGENT_ID: "Foxtrot Motors",
}
# Agents that only chat (no call recording to wait for or mention).
CHAT_ONLY_AGENTS = {FOXTROT_AGENT_ID}
DEFAULT_BRAND = "CARSTER"


def brand_for(lead_info):
    agent_id = (lead_info or {}).get("agent_id", "")
    return AGENT_BRANDS.get(agent_id, DEFAULT_BRAND)


def is_chat_agent(lead_info):
    return (lead_info or {}).get("agent_id", "") in CHAT_ONLY_AGENTS


# How long to wait for the second piece (transcript or audio) before
# sending an email with just whichever piece has arrived.
PAIRING_TIMEOUT_SECONDS = int(os.environ.get("PAIRING_TIMEOUT_SECONDS", "45"))

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


# Newer ElevenLabs data collection items get auto-generated ids (for example
# "aitem_0901m6...") instead of the name typed in the dashboard. Map them to a
# readable label using the start of the field's description.
DESCRIPTION_LABELS = {
    "when the customer plans to buy": "purchase_timeline",
    "the payment or out-the-door estimate": "payment_estimate",
    "whether the customer mentioned military": "military_affiliation",
}


def readable_field_id(field_id, entry):
    if not str(field_id).lower().startswith("aitem"):
        return field_id
    schema = entry.get("json_schema") or {}
    description = (schema.get("description") or "").strip()
    lowered = description.lower()
    for prefix, label in DESCRIPTION_LABELS.items():
        if lowered.startswith(prefix):
            return label
    if description:
        return description.split(".")[0][:40]
    return field_id


def extract_lead_data(payload):
    """Pull transcript, summary, and collected lead fields out of the payload.

    ElevenLabs' post-call "Data Collection" analysis (configured under the
    agent's Analysis tab) returns whatever fields are actually defined there
    under analysis.data_collection_results, each shaped like:
        {"<field_id>": {"value": ..., "json_schema": {...}, "rationale": ...}}
    Rather than assuming fixed field names, we display every field that's
    actually present, labeled from its id, so this keeps working even if
    the configured field set changes.
    """
    data = payload.get("data", {})

    conversation_id = data.get("conversation_id", "unknown")
    agent_id = data.get("agent_id", "unknown")

    analysis = data.get("analysis", {}) or {}
    summary = analysis.get("transcript_summary") or analysis.get("call_summary") or ""

    dcr = analysis.get("data_collection_results") or {}

    lead = {}
    if isinstance(dcr, dict):
        for field_id, entry in dcr.items():
            value = entry
            if isinstance(entry, dict):
                value = entry.get("value")
                field_id = readable_field_id(field_id, entry)
            lead[field_id] = value if value not in (None, "") else "Not provided"

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
    brand = brand_for(lead_info)
    is_chat_brand = is_chat_agent(lead_info)

    lines = [f"New {brand} {'conversation' if is_chat_brand else 'call'} — Conversation ID: {conversation_id}"]

    if lead_info:
        lines += ["", "== Lead Details =="]
        if lead:
            for field_id, value in lead.items():
                label = field_id.replace("_", " ").title()
                lines.append(f"{label}: {value}")
        else:
            lines.append("(No data collection fields were configured or returned for this call.)")

        if lead_info.get("summary"):
            lines += ["", "== Call Summary ==", lead_info["summary"]]

        if lead_info.get("transcript_text"):
            lines += ["", "== Full Transcript ==", lead_info["transcript_text"]]
    else:
        lines += ["", "(Lead details were not received for this call.)"]

    if not audio_bytes and not is_chat_brand:
        lines += ["", "(No call recording was received for this call.)"]

    body_text = "\n".join(lines)
    # Resend renders "text" with line breaks preserved when no html is given,
    # but wrapping in <pre> via html keeps formatting predictable everywhere.
    body_html = "<pre style='font-family: monospace; white-space: pre-wrap;'>" + \
        body_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + \
        "</pre>"

    # Try a few likely name field ids; fall back to "Unknown" if none present.
    name_part = "Unknown"
    for possible_key in ("customer_name", "first_name", "name", "caller_name"):
        if lead.get(possible_key) and lead[possible_key] != "Not provided":
            name_part = lead[possible_key]
            break
    subject = f"New {brand} Lead — {name_part}" if lead_info else f"{brand} Call — {conversation_id}"

    payload = {
        "from": RESEND_FROM_EMAIL,
        "to": [NOTIFY_TO_EMAIL],
        "subject": subject,
        "text": body_text,
        "html": body_html,
    }

    if audio_bytes:
        payload["attachments"] = [{
            "filename": f"{brand.lower().replace(' ', '_')}_call_{conversation_id}.mp3",
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


# ---------------------------------------------------------------------------
# Foxtrot Motors website lead form
# ---------------------------------------------------------------------------
# The Foxtrot Motors website (static, on Cloudflare Pages) posts its request
# form here as JSON. Leads are emailed through the same Resend setup as the
# ElevenLabs leads above. No database; nothing is stored.

FOXTROT_NOTIFY_TO_EMAIL = os.environ.get("FOXTROT_NOTIFY_TO_EMAIL", "") or NOTIFY_TO_EMAIL
# Sender for Foxtrot website leads, e.g. "Foxtrot Motors <leads@foxtrotmotorsca.com>"
# (domain must be verified in Resend). Falls back to the shared sender.
FOXTROT_FROM_EMAIL = os.environ.get("FOXTROT_FROM_EMAIL", "") or RESEND_FROM_EMAIL

# Browsers may only post from these sites (CORS). Add more with the
# FOXTROT_EXTRA_ORIGINS env var, comma separated.
FOXTROT_ALLOWED_ORIGINS = {
    "https://foxtrotmotorsca.com",
    "https://www.foxtrotmotorsca.com",
    "https://foxtrotmotors-site.pages.dev",
} | {o.strip() for o in os.environ.get("FOXTROT_EXTRA_ORIGINS", "").split(",") if o.strip()}

LEAD_FIELDS = [
    ("name", "Name", 120),
    ("email", "Email", 200),
    ("phone", "Phone", 40),
    ("vehicle", "Vehicle of interest", 200),
    ("message", "Message", 4000),
    ("estimate", "Your estimate", 4000),
    ("form", "Form", 60),
    ("page", "Page", 300),
]

# Simple in-memory rate limit: max submissions per IP per window.
_LEAD_RATE_MAX = 10
_LEAD_RATE_WINDOW = 600  # seconds
_lead_hits = {}
_lead_hits_lock = threading.Lock()


def _origin_allowed(origin):
    if origin in FOXTROT_ALLOWED_ORIGINS:
        return True
    # Cloudflare Pages branch/commit previews: https://<hash>.foxtrotmotors-site.pages.dev
    return origin.startswith("https://") and origin.endswith(".foxtrotmotors-site.pages.dev")


def _cors(response):
    origin = request.headers.get("Origin", "")
    if _origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Max-Age"] = "86400"
    return response


def _rate_limited(ip):
    import time
    now = time.time()
    with _lead_hits_lock:
        hits = [t for t in _lead_hits.get(ip, []) if now - t < _LEAD_RATE_WINDOW]
        limited = len(hits) >= _LEAD_RATE_MAX
        if not limited:
            hits.append(now)
        _lead_hits[ip] = hits
    return limited


@app.route("/lead/foxtrot", methods=["POST", "OPTIONS"])
def foxtrot_lead():
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))

    if request.headers.get("Origin") and not _origin_allowed(request.headers.get("Origin", "")):
        return jsonify({"error": "origin not allowed"}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = request.form.to_dict()

    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()
    if _rate_limited(ip):
        return _cors(jsonify({"error": "too many requests"})), 429

    lead = {key: str(data.get(key, "") or "").strip()[:limit] for key, _, limit in LEAD_FIELDS}
    consent = str(data.get("consent", "")).strip().lower() in ("yes", "true", "on", "1")

    if not lead["name"] or not (lead["email"] or lead["phone"]):
        return _cors(jsonify({"error": "name and email or phone are required"})), 400
    if not consent:
        return _cors(jsonify({"error": "consent is required"})), 400
    if not RESEND_API_KEY or not FOXTROT_NOTIFY_TO_EMAIL:
        print("Foxtrot lead received but email is not configured (RESEND_API_KEY / NOTIFY_TO_EMAIL).")
        return _cors(jsonify({"error": "lead email not configured"})), 500

    lines = ["New Foxtrot Motors website request", ""]
    for key, label, _ in LEAD_FIELDS:
        if key in ("form", "page"):
            continue
        value = lead[key] or "Not provided"
        if "\n" in value:
            lines += [f"{label}:", value, ""]
        else:
            lines.append(f"{label}: {value}")
    lines += ["", "Consent: Agreed to be contacted by phone, text, or email about this request.",
              f"Form: {lead['form'] or 'website'}"]
    if lead["page"]:
        lines.append(f"Page: {lead['page']}")

    body_text = "\n".join(lines)
    body_html = "<pre style='font-family: monospace; white-space: pre-wrap;'>" + \
        body_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</pre>"

    payload = {
        "from": FOXTROT_FROM_EMAIL,
        "to": [FOXTROT_NOTIFY_TO_EMAIL],
        "subject": f"New Foxtrot Motors Lead: {lead['name']}",
        "text": body_text,
        "html": body_html,
    }
    if lead["email"] and "@" in lead["email"]:
        payload["reply_to"] = lead["email"]

    try:
        send_email(payload)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to send Foxtrot website lead email: {exc}")
        return _cors(jsonify({"error": "could not send"})), 502

    return _cors(jsonify({"status": "sent"})), 200


@app.route("/", methods=["GET"])
def health_check():
    return "CARSTER / Foxtrot Motors webhook receiver is running.", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
