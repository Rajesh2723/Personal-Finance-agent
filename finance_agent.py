#!/usr/bin/env python3
"""Personal finance agent: parses UPI/bank SMS into SQLite, answers questions with Claude.

  python finance_agent.py import sample_sms.txt     # or an SMS Backup & Restore .xml
  python finance_agent.py chat
"""
import argparse, datetime as dt, hashlib, json, os, re, sqlite3, threading, xml.etree.ElementTree as ET

DB_PATH = os.environ.get("FIN_DB", "finance.db")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

# ---------- storage ----------
LOCK = threading.RLock()  # the server shares one connection across threads
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS txns(id INTEGER PRIMARY KEY, hash TEXT UNIQUE, date TEXT,
  amount REAL, kind TEXT, merchant TEXT, category TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS rules(merchant TEXT PRIMARY KEY, category TEXT);
""")

# ---------- parsing ----------
CATEGORIES = {
    "food": ["swiggy", "zomato", "restaurant", "cafe", "dominos", "mcdonald", "kfc",
             "starbucks", "bakery", "biryani", "pizza", "hotel"],
    "groceries": ["bigbasket", "blinkit", "zepto", "instamart", "dmart", "more retail", "reliance fresh"],
    "transport": ["uber", "ola", "rapido", "irctc", "redbus", "metro", "petrol", "fuel", "hpcl", "bpcl", "fastag"],
    "shopping": ["amazon", "flipkart", "myntra", "ajio", "meesho", "nykaa"],
    "bills": ["airtel", "jio", "vodafone", "bescom", "electricity", "broadband", "recharge", "fibernet"],
    "entertainment": ["netflix", "hotstar", "spotify", "bookmyshow", "youtube", "prime"],
    "health": ["pharmacy", "apollo", "medplus", "1mg", "pharmeasy", "hospital", "clinic"],
}
AMOUNT = re.compile(r"(?:\b(?:rs\.?|inr)|₹)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
DEBIT = re.compile(r"\b(debited|spent|paid|sent|withdrawn|purchased?)\b", re.I)
CREDIT = re.compile(r"\b(credited|received|deposited|refund(?:ed)?)\b", re.I)
SKIP = re.compile(r"\b(otp|will be|due|request(?:ed)?|offer)\b", re.I)  # not real transactions
END = r"(?=\s+(?:on|Ref|UPI|Avl|via|using|from)\b|[.,;(]|$)"
MERCHANT_PATTERNS = [
    r"VPA\s+([\w.\-]+)@",
    r"\b(?:at|towards|trf to|to)\s+(?!A/c|VPA)([A-Za-z0-9&.' \-]{2,30}?)" + END,
    r"\b(?:from|by)\s+(?:VPA\s+)?(?!A/c)([A-Za-z0-9&.' \-]{2,30}?)" + END,
]
DATE_PATTERNS = [r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})\b",              # 12-11-26 (dd-mm-yy)
                 r"\b(\d{1,2})[-/ ]?([A-Za-z]{3})[a-z]*[-/ ]?(\d{2,4})\b"]  # 12Nov26, 12-Nov-2026


def parse_date(text):
    for pat in DATE_PATTERNS:
        for d, mo, y in re.findall(pat, text):
            try:
                y = int(y) + (2000 if int(y) < 100 else 0)
                mo = int(mo) if mo.isdigit() else dt.datetime.strptime(mo.title(), "%b").month
                return dt.date(y, mo, int(d)).isoformat()
            except ValueError:
                continue
    return None


def parse_sms(body):
    if SKIP.search(body):
        return None
    amt, d, c = AMOUNT.search(body), DEBIT.search(body), CREDIT.search(body)
    if not amt or not (d or c):
        return None
    merchant = "unknown"
    for pat in MERCHANT_PATTERNS:
        m = re.search(pat, body, re.I)
        if m:
            merchant = m.group(1).strip().lower()
            break
    return {"amount": float(amt.group(1).replace(",", "")),
            "kind": "debit" if d and (not c or d.start() < c.start()) else "credit",
            "merchant": merchant, "date": parse_date(body)}


def categorize(merchant):
    for kw, cat in db.execute("SELECT merchant, category FROM rules"):
        if kw in merchant:
            return cat
    for cat, kws in CATEGORIES.items():
        if any(k in merchant for k in kws):
            return cat
    return "other"


def add_sms(body, date=None):
    with LOCK:
        return _add_sms(body, date)


def _add_sms(body, date=None):
    p = parse_sms(body)
    if not p:
        return False
    cur = db.execute(
        "INSERT OR IGNORE INTO txns(hash,date,amount,kind,merchant,category,raw) VALUES(?,?,?,?,?,?,?)",
        (hashlib.sha1(body.encode()).hexdigest(), date or p["date"] or dt.date.today().isoformat(),
         p["amount"], p["kind"], p["merchant"], categorize(p["merchant"]), body))
    db.commit()
    return cur.rowcount == 1


def load_messages(path):
    if path.endswith(".xml"):  # "SMS Backup & Restore" export
        for s in ET.parse(path).getroot().iter("sms"):
            day = dt.datetime.fromtimestamp(int(s.get("date")) / 1000).date().isoformat()
            yield s.get("body", ""), day
    else:  # one SMS per line
        for line in open(path, encoding="utf-8"):
            if line.strip():
                yield line.strip(), None

# ---------- tools the agent can call ----------
def _filter(a):
    sql, args = "kind='debit' AND date BETWEEN ? AND ?", [a["start_date"], a["end_date"]]
    if a.get("category"):
        sql += " AND category=?"; args.append(a["category"].lower())
    if a.get("merchant"):
        sql += " AND merchant LIKE ?"; args.append(f"%{a['merchant'].lower()}%")
    return sql, args


def run_tool(name, a):
    with LOCK:
        return _run_tool(name, a)


def _run_tool(name, a):
    if name == "set_category":
        k, c = a["merchant_keyword"].lower(), a["category"].lower()
        db.execute("INSERT OR REPLACE INTO rules VALUES(?,?)", (k, c))
        n = db.execute("UPDATE txns SET category=? WHERE merchant LIKE ?", (c, f"%{k}%")).rowcount
        db.commit()
        return {"updated_transactions": n}
    where, args = _filter(a)
    limit = int(a.get("limit", 10))
    if name == "spending_summary":
        rows = db.execute(f"SELECT category, ROUND(SUM(amount),2), COUNT(*) FROM txns WHERE {where} "
                          "GROUP BY category ORDER BY 2 DESC", args).fetchall()
        return {"total_spent": round(sum(r[1] for r in rows), 2),
                "by_category": [{"category": r[0], "spent": r[1], "count": r[2]} for r in rows]}
    if name == "top_merchants":
        rows = db.execute(f"SELECT merchant, category, ROUND(SUM(amount),2), COUNT(*) FROM txns WHERE {where} "
                          f"GROUP BY merchant ORDER BY 3 DESC LIMIT {limit}", args).fetchall()
        return [{"merchant": r[0], "category": r[1], "spent": r[2], "count": r[3]} for r in rows]
    if name == "list_transactions":  # raw SMS (account numbers etc.) is never returned
        rows = db.execute(f"SELECT date, amount, merchant, category FROM txns WHERE {where} "
                          f"ORDER BY date DESC LIMIT {limit}", args).fetchall()
        return [{"date": r[0], "amount": r[1], "merchant": r[2], "category": r[3]} for r in rows]
    return {"error": f"unknown tool {name}"}


def _tool(name, desc, extra=None):
    props = {"start_date": {"type": "string", "description": "YYYY-MM-DD"},
             "end_date": {"type": "string", "description": "YYYY-MM-DD"},
             "category": {"type": "string", "description": "Optional: " + ", ".join(CATEGORIES) + ", other"},
             "merchant": {"type": "string", "description": "Optional merchant substring"}, **(extra or {})}
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": ["start_date", "end_date"]}}


TOOLS = [
    _tool("spending_summary", "Total money spent (debits only) and per-category breakdown for a date range."),
    _tool("top_merchants", "Merchants ranked by total spend.", {"limit": {"type": "integer"}}),
    _tool("list_transactions", "Individual debit transactions, newest first.", {"limit": {"type": "integer"}}),
    {"name": "set_category", "description": "Teach the agent: assign a category to every merchant containing a keyword.",
     "input_schema": {"type": "object", "required": ["merchant_keyword", "category"],
                      "properties": {"merchant_keyword": {"type": "string"}, "category": {"type": "string"}}}},
]

SYSTEM = """You are a personal finance assistant for an Indian user. Today is {today}. Amounts are in INR (₹).
Always use the tools for numbers; never guess. Convert phrases like "this month" or "last week" into explicit
YYYY-MM-DD ranges. Only debits count as spending. If a result looks too low, mention that merchants may sit in
'other' or a neighbouring category (e.g. groceries vs food) and offer to recategorise. Be brief."""

# ---------- agent loop ----------
def ask(client, history, question):
    history.append({"role": "user", "content": question})
    while True:
        resp = client.messages.create(model=MODEL, max_tokens=1000, tools=TOOLS, messages=history,
                                      system=SYSTEM.format(today=dt.date.today().isoformat()))
        history.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")
        results = [{"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(run_tool(b.name, b.input))}
                   for b in resp.content if b.type == "tool_use"]
        history.append({"role": "user", "content": results})


def chat():
    import anthropic  # reads ANTHROPIC_API_KEY from the environment
    client, history = anthropic.Anthropic(), []
    print("Ask about your spending, e.g. 'how much did I spend on food this month?' (Ctrl+C to quit)")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q:
            print(ask(client, history, q))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import", help="import SMS (.txt one per line, or .xml export)").add_argument("file")
    sub.add_parser("chat", help="talk to the agent")
    args = ap.parse_args()
    if args.cmd == "import":
        total = new = 0
        for body, day in load_messages(args.file):
            total += 1
            new += add_sms(body, day)
        print(f"Read {total} messages, stored {new} new transactions in {DB_PATH}")
    else:
        chat()


if __name__ == "__main__":
    main()
