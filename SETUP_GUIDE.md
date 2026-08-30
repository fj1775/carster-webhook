# CARSTER Lead Notification Webhook — Setup Guide

This sets up a free, self-hosted email notifier for CARSTER leads —
no Zapier or other paid automation tool required.

## What this does

When a call with CARSTER ends, ElevenLabs sends a webhook with the
transcript and the lead fields your agent captured (name, phone,
vehicle of interest, budget, etc.). This small app catches that
webhook and emails you a clean summary.

---

## Step 1 — Get a Gmail App Password

You'll send the notification emails through your Gmail account, but
Gmail requires a special "App Password" instead of your normal
password for this kind of use.

1. Go to https://myaccount.google.com/security
2. Turn on **2-Step Verification** if it isn't already on (required
   for App Passwords to be available).
3. Go to https://myaccount.google.com/apppasswords
4. Create a new App Password (name it something like "CARSTER
   Webhook"), and copy the 16-character password shown. You won't be
   able to see it again after this step.

Keep this password somewhere safe — you'll paste it into your host's
environment variables in Step 3.

---

## Step 2 — Deploy this app for free on Render

1. Go to https://render.com and sign up (free tier is fine).
2. Create a new **Web Service**.
3. Upload/connect this folder (`app.py`, `requirements.txt`,
   `Procfile`) — either via a GitHub repo or Render's manual deploy
   option.
4. Set the **Build Command** to: `pip install -r requirements.txt`
5. Set the **Start Command** to: `gunicorn app:app`
6. Under **Environment Variables**, add:

   | Key | Value |
   |---|---|
   | `SMTP_USERNAME` | your Gmail address |
   | `SMTP_PASSWORD` | the App Password from Step 1 |
   | `NOTIFY_TO_EMAIL` | the inbox that should receive lead emails |
   | `ELEVENLABS_WEBHOOK_SECRET` | (added in Step 4, leave blank for now) |

7. Deploy. Once live, Render will give you a URL like:
   `https://carster-webhook.onrender.com`

Your webhook endpoint will be:
`https://carster-webhook.onrender.com/webhook/elevenlabs`

> Note: Render's free tier "sleeps" after inactivity and can take
> ~30-60 seconds to wake up on the first request. For a low-volume
> dealership line this is usually fine, but if you notice delayed
> emails, that's why. Upgrading to a paid Render tier ($7/mo) removes
> the sleep delay if it becomes an issue.

---

## Step 3 — Register the webhook in ElevenLabs

1. In ElevenLabs, go to **Settings → Webhooks** (workspace level).
2. Click to add a new webhook.
3. Paste your Render URL:
   `https://carster-webhook.onrender.com/webhook/elevenlabs`
4. Select the event type: **post_call_transcription**
5. Save. ElevenLabs will show you a **webhook secret** — copy it.

---

## Step 4 — Add the webhook secret

Go back to Render, add the copied secret as the
`ELEVENLABS_WEBHOOK_SECRET` environment variable, and redeploy. This
lets the app verify that incoming requests genuinely came from
ElevenLabs.

---

## Step 5 — Connect the webhook to your CARSTER agent

1. In the CARSTER agent's own settings, find where webhooks/alerts
   are configured (the "External notifications" panel you saw under
   Settings → Alerting → Configure notifications).
2. Select the webhook you just created so this specific agent uses it.

---

## Step 6 — Test it

1. Run a Preview conversation with CARSTER.
2. Give it a name, phone number, and ask about a vehicle so a few
   Update State fields get captured.
3. End the call.
4. Check your inbox — you should get an email within a few seconds
   (or up to a minute if Render's free tier had to wake up).

---

## Troubleshooting

- **No email arrives**: check Render's logs (Dashboard → your
  service → Logs) for errors — most common issues are a wrong SMTP
  password or the wrong lead field names.
- **"Invalid signature" errors**: double-check the
  `ELEVENLABS_WEBHOOK_SECRET` was copied correctly and the app was
  redeployed after adding it.
- **Fields show "Not provided"**: ElevenLabs' webhook payload
  structure can vary slightly by API version — if fields aren't
  populating, share a sample payload (visible in Render's logs) and
  the field-extraction logic in `app.py` can be adjusted.
