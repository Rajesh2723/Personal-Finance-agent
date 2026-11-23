"""Real-time finance agent.

Phone SMS forwarder --POST--> /sms --> parse + store --> Telegram alert
You <--> Telegram bot --POST--> /telegram --> Claude agent (finance_agent.ask) --> reply
"""
import hashlib, hmac, os, time
from contextlib import asynccontextmanager

os.environ.setdefault("TZ", "Asia/Kolkata")  # "this month" should mean IST, not server time
if hasattr(time, "tzset"):
    time.tzset()

import anthropic, httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

import finance_agent as fa

SMS_TOKEN = os.environ["SMS_TOKEN"]                   # secret used by the SMS forwarder
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]           # from @BotFather
OWNER = int(os.environ.get("OWNER_CHAT_ID") or 0)     # only this chat may talk to the bot
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
TG_SECRET = hashlib.sha256(TG_TOKEN.encode()).hexdigest()[:32]
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

client = anthropic.Anthropic()
HISTORY = {}


def tg(chat_id, text):
    try:
        httpx.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text[:4000]}, timeout=15)
    except httpx.HTTPError as e:
        print("telegram send failed:", e)


@asynccontextmanager
async def lifespan(app):
    if PUBLIC_URL:  # register the Telegram webhook on every start
        httpx.post(f"{TG_API}/setWebhook", timeout=15,
                   json={"url": f"{PUBLIC_URL}/telegram", "secret_token": TG_SECRET})
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def health():
    return {"status": "ok"}


# ---------- SMS in ----------
def handle_sms(body):
    if not fa.add_sms(body):  # not a transaction, or already stored
        return
    with fa.LOCK:
        date, amt, kind, merchant, cat = fa.db.execute(
            "SELECT date, amount, kind, merchant, category FROM txns WHERE hash=?",
            (hashlib.sha1(body.encode()).hexdigest(),)).fetchone()
    if not OWNER:
        return
    if kind == "credit":
        return tg(OWNER, f"💰 ₹{amt:,.0f} received from {merchant}")
    today = time.strftime("%Y-%m-%d")
    total = fa.run_tool("spending_summary", {"start_date": today[:8] + "01", "end_date": today,
                                             "category": cat})["total_spent"]
    tg(OWNER, f"💸 ₹{amt:,.0f} at {merchant} ({cat})\nThis month on {cat}: ₹{total:,.0f}")


@app.post("/sms")
async def sms(request: Request, bg: BackgroundTasks, token: str = ""):
    if not hmac.compare_digest(token, SMS_TOKEN):
        raise HTTPException(401)
    try:
        data = await request.json()
    except Exception:  # forwarder sent plain text
        data = {"body": (await request.body()).decode(errors="ignore")}
    body = data.get("body") or data.get("message") or data.get("text") or "" if isinstance(data, dict) else ""
    if body:
        bg.add_task(handle_sms, body)
    return {"ok": True}


# ---------- Telegram in ----------
def handle_chat(chat_id, text):
    if not OWNER:
        return tg(chat_id, f"Your chat ID is {chat_id}. Set OWNER_CHAT_ID to this value and restart the server.")
    if chat_id != OWNER:
        return
    hist = HISTORY.setdefault(chat_id, [])
    if len(hist) > 30:
        hist.clear()
    try:
        reply = fa.ask(client, hist, text)
    except Exception as e:
        hist.clear()  # avoid a half-finished tool call poisoning later turns
        reply = f"Sorry, something went wrong ({type(e).__name__}). Try again."
    tg(chat_id, reply)


@app.post("/telegram")
async def telegram(request: Request, bg: BackgroundTasks,
                   x_telegram_bot_api_secret_token: str = Header("")):
    if not hmac.compare_digest(x_telegram_bot_api_secret_token, TG_SECRET):
        raise HTTPException(401)
    msg = (await request.json()).get("message") or {}
    chat_id, text = (msg.get("chat") or {}).get("id"), msg.get("text")
    if chat_id and text:
        bg.add_task(handle_chat, chat_id, text)
    return {"ok": True}
