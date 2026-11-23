# Personal Finance Agent (real-time)

Bank/UPI SMS arrive on your phone -> forwarded to this server -> parsed and stored ->
instant Telegram alert. Ask the bot "how much did I spend on food this month?" any time.

```
Phone (SMS forwarder app) --POST /sms?token=...--> FastAPI --> SQLite
                                                      |--> Telegram alert for every transaction
You <-> Telegram bot <--> /telegram --> Claude (tool use over your SQLite data)
```

## Setup
1. **Bot:** message @BotFather on Telegram, `/newbot`, copy the token.
2. **Deploy** (Railway / Render / Fly.io): start command is in `Procfile`. Copy `.env.example`
   to your host's environment variables. Mount a persistent volume and point `FIN_DB` at it.
3. **Chat ID:** deploy once with `OWNER_CHAT_ID` empty, message your bot, it replies with your ID.
   Set `OWNER_CHAT_ID` and redeploy. Only that chat can use the bot.
4. **SMS forwarding (Android):** in an SMS-forwarding app (SMS Forwarder, MacroDroid, Tasker...)
   create a rule for messages from your bank senders that POSTs JSON `{"body": "<message text>"}`
   to `https://YOUR_APP/sms?token=YOUR_SMS_TOKEN`. Use the app's message-body variable.
5. Optional backfill: `python finance_agent.py import sms-backup.xml`.

## Local test
```bash
pip install -r requirements.txt
export SMS_TOKEN=test TELEGRAM_BOT_TOKEN=... ANTHROPIC_API_KEY=...
uvicorn server:app --reload
curl -X POST "localhost:8000/sms?token=test" -H 'content-type: application/json' \
  -d '{"body":"Rs.450.00 debited from A/c XX1234 on 03-11-26 to VPA swiggy@icici. UPI Ref 1"}'
```
(Telegram needs a public HTTPS URL, e.g. via ngrok, to deliver messages to a local server.)

## Notes
- Privacy: raw SMS stay in your database; Claude only sees date, amount, merchant, category.
- Identical SMS text is stored once (protects against forwarder retries). Messages normally contain
  a UPI ref or balance, so real repeat purchases stay distinct.
- iPhone can't forward SMS automatically; use the CLI import or an Android phone.
- Single-user by design. Multi-user would need a user_id column and per-user tokens.
