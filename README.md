# WOI Lead CRM — backend (FastAPI)

Meta lead ad → webhook → **this service** → Neon Postgres → WhatsApp invite → CRM (Next.js).

This version is built to **keep working without anyone touching it**:

| Problem | What the system does now |
|---|---|
| Two ads (Friday + Sunday), a new one every week | Each lead form maps to a day/time/template in **Form Config**. A new form is discovered by itself; if its name contains *Friday* / *Sunday* it is mapped automatically (flagged "Review"). If the day can't be told, the lead is **kept, no wrong invite is sent**, and the form appears in Form Config — pick the day, save, waiting leads are processed instantly. A form shared by several days can use **Auto** (the lead's own answer decides). |
| Meta / WhatsApp token expires | A lead id is **saved before anything can fail**. If the token is dead the lead waits (`Details not fetched`) and is completed automatically once a token works. Tokens are checked every ~15 min; you get a Telegram/e-mail alert and a red banner in the CRM. A new token is **pasted on the CRM → System page** (validated, encrypted, live in seconds — no Render redeploy). A pasted short-lived Facebook token is exchanged for a **non-expiring Page token**. |
| Missed webhook (service asleep, Meta hiccup) | Every 20 min the service pulls the last 4 days of leads for every active form from Meta and inserts anything missing. |
| Nobody trusts it | **System** tab: green/amber/red verdict, plain-language "what to do", Repair-now button, event log. |

Safety rules baked in: a lead older than 36 h is **never** auto-messaged (recoveries can't spam old leads); permanent WhatsApp errors (e.g. number not on WhatsApp) are not retried; duplicate webhooks never double-send.

## One-time setup (≈15 min)

### 1. Render environment variables
See `.env.example`. The important ones:

* `META_APP_ID`, `META_APP_SECRET` — Meta Developer → your app → Settings → Basic. **Needed so tokens can be made non-expiring and expiry dates shown.**
* `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_BUSINESS_ACCOUNT_ID`, `WHATSAPP_ENABLED=true`
* `ADMIN_PASSWORD`, `JWT_SECRET` — change from the defaults.
* `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — so a dying token reaches your phone (create a bot with @BotFather, send it a message, read the chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`). E-mail (`SMTP_*`, `ALERT_EMAIL_TO`) also works.

You don't have to put the tokens in Render at all — paste them in the CRM (step 2). Env tokens remain a fallback.

### 2. Tokens that don't expire
**Meta (lead ads)** — easiest: open *developers.facebook.com/tools/explorer*, pick your app, **Generate Access Token** with `leads_retrieval, pages_show_list, pages_read_engagement, pages_manage_metadata, ads_read` and select your Page. Paste it on **CRM → System → Facebook / Instagram token**. The CRM converts it to a Page token that never expires and re-subscribes the Page to lead notifications.
(Alternative: a Business-Manager *System User* token with expiry "Never"; paste it with the Page ID.)

**WhatsApp** — the "temporary token" on the API-setup page lasts 24 h and is the usual culprit. Use a **System User** token: Business Settings → Users → System users → Add → assign the WhatsApp account → Generate token, app = yours, expiry **Never**, permissions `whatsapp_business_messaging` + `whatsapp_business_management`. Paste on **CRM → System → WhatsApp token**.

### 3. Webhook (unchanged)
Callback `https://<render-url>/webhook/meta-leads`, verify token = `META_VERIFY_TOKEN`; subscribe **Page → leadgen** and **WhatsApp → messages**.

### 4. Keep the free Render service awake (recommended)
Add a free monitor (UptimeRobot / cron-job.org) hitting `https://<render-url>/health` every 5 min. `/health` does **not** touch the database, so it costs no Neon compute. Even without it, the sync loop recovers anything missed once the service wakes.

## Day to day
* Open the CRM; if the top banner is red, click **Fix now** and follow the one-line instruction.
* New ad launched → nothing to do if the form name has the day. Otherwise you'll get an alert; open **Form Config**, click **Edit** on the highlighted row, choose the day, **Update**.
* Alert says a token expired → paste a new one on **System**. Leads that arrived meanwhile are completed automatically (WhatsApp only for those ≤ 36 h old; older ones keep a **Send Invite** button).

## API additions
`GET /system/health`, `POST /system/check`, `POST /system/repair`, `POST /system/tokens/meta|whatsapp`, `DELETE /system/tokens/{meta|whatsapp}`, `POST /system/test-alert`, `POST /leads/{id}/retry`, `GET /form-configs/defaults`, `GET /health` (public, no DB).

## Fixed while doing this
Reports, the "Day" filter, follow-up history, *Add Lead* and the test endpoints were crashing after the Thursday→Friday / seminar→session rename (they still used the old column names). Existing databases are upgraded automatically on start (new columns are added; nothing is dropped). `/templates/inspect-all` now requires login. Legacy Google-Sheets code moved to `_legacy/`.

## Tests
`pip install -r requirements.txt -r requirements-dev.txt && pytest` — simulates Meta/WhatsApp (expired tokens, unknown forms, missed webhooks, duplicate deliveries, old leads, column migration).

## Neon note
The self-heal loop touches the database once per run (default every 20 min), which lets Neon sleep in between. Raise `SYNC_INTERVAL_MINUTES` if you ever hit compute limits; webhooks still arrive instantly.
