"""PocketLedger Chat — talk to your ledger in the browser, no Claude Desktop needed.

How it works:
  your message → Gemini (free tier) figures out WHICH tool + params (one call)
  → the same deterministic PocketLedger code executes it → formatted reply.
Gemini never does the math. The layers that judge the money contain no ML.

Setup (in the pocketledger folder):
  1. pip install flask requests
  2. Put your key in a file named .env :   GEMINI_API_KEY=your_key_here
  3. python chat.py
  4. Open http://localhost:5050 in your browser
"""

from __future__ import annotations

import json
import os
import re
from datetime import date

import requests
from flask import Flask, jsonify, request

# reuse the exact same deterministic engine
import server as pl

# ------------------------------------------------------------------ config

def load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

load_env()
API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "")  # empty = auto-discover
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models?key={key}"

_PREFERRED = ["gemini-flash-latest", "gemini-2.0-flash", "gemini-2.5-flash",
              "gemini-2.0-flash-lite", "gemini-1.5-flash", "gemini-1.5-flash-latest"]


def redact(text: str) -> str:
    """Never show the API key in any error message."""
    return text.replace(API_KEY, "***KEY***") if API_KEY else text


def discover_model() -> str:
    """Ask Google which models THIS key can use, pick the best flash model.
    Same self-healing pattern as SahaayakAI."""
    global MODEL
    if MODEL:
        return MODEL
    r = requests.get(LIST_URL.format(key=API_KEY), timeout=20)
    r.raise_for_status()
    available = []
    for m in r.json().get("models", []):
        if "generateContent" in m.get("supportedGenerationMethods", []):
            available.append(m["name"].split("/")[-1])
    for pref in _PREFERRED:
        if pref in available:
            MODEL = pref
            return MODEL
    flash = [m for m in available if "flash" in m and "image" not in m and "tts" not in m]
    if flash:
        MODEL = sorted(flash, reverse=True)[0]
        return MODEL
    if available:
        MODEL = available[0]
        return MODEL
    raise RuntimeError("No usable Gemini models found for this API key.")

TODAY = lambda: date.today().isoformat()
THIS_MONTH = lambda: date.today().strftime("%Y-%m")

SYSTEM = """You are PocketLedger's agent: a personal-finance assistant for an Indian user.
You may call tools to read/write their local ledger, then answer naturally.

Protocol: reply with ONLY a JSON object, no markdown fences. Two forms:
  {"action":"tool","tool":"<name>", ...params}          <- to call a tool
  {"action":"final","reply":"<your answer to the user>"} <- to answer

Tools:
1. {"action":"tool","tool":"log_expense","amount":<rupees>,"merchant":"<who>","category":"<optional>","note":"<optional>","date":"<YYYY-MM-DD|today|yesterday>"}
2. {"action":"tool","tool":"monthly_summary","month":"YYYY-MM"}
3. {"action":"tool","tool":"query_expenses","category":"<opt>","start_date":"<opt>","end_date":"<opt>","merchant":"<opt>","limit":25}
4. {"action":"tool","tool":"set_budget","category":"<name>","monthly_limit":<rupees>}
5. {"action":"tool","tool":"check_budgets","month":"YYYY-MM"}
6. {"action":"tool","tool":"find_recurring"}
7. {"action":"tool","tool":"import_statement","csv_path":"<full path>"}

Rules:
- Today is {today}. Current month is {month}. "last month" = month before {month}.
- Hinglish is normal: "kal 120 ka chai" = amount 120, merchant chai, date yesterday.
- Chain tools when a question needs multiple (e.g. budgets + recurring), max 3 calls.
- After tool results arrive, give a short, concrete final reply using ONLY numbers from the results. Never invent amounts. Use plain rupee figures like Rs.450.
- If the user is just chatting or asks something unrelated to money, go straight to final.
"""


def parse_json_block(text: str) -> dict:
    """Strip ``` fences and parse the first JSON object (the usual Gemini dance)."""
    text = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON found in: {text[:120]}")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("Unbalanced JSON")


def call_gemini(full_prompt: str) -> dict:
    global MODEL
    contents = [{"parts": [{"text": full_prompt}]}]
    # thinking models burn the token budget on thinking → truncated JSON.
    # Disable thinking; if the model rejects thinkingConfig, retry without it.
    gen_with = {"temperature": 0, "maxOutputTokens": 2000,
                "thinkingConfig": {"thinkingBudget": 0}}
    gen_without = {"temperature": 0, "maxOutputTokens": 2000}

    model = discover_model()
    for gen in (gen_with, gen_without):
        r = requests.post(GEMINI_URL.format(model=model, key=API_KEY),
                          json={"contents": contents, "generationConfig": gen}, timeout=30)
        if r.status_code == 404:  # model vanished — rediscover once
            MODEL = ""
            model = discover_model()
            r = requests.post(GEMINI_URL.format(model=model, key=API_KEY),
                              json={"contents": contents, "generationConfig": gen}, timeout=30)
        if r.status_code == 400:  # likely thinkingConfig unsupported → try plain
            continue
        r.raise_for_status()
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return parse_json_block(text)
    r.raise_for_status()
    raise RuntimeError("Gemini rejected the request.")


# ------------------------------------------------------------ tool execution

def rup(s: str) -> str:  # tiny helper for display
    return s.replace("\u20b9", "₹")


def execute(intent: dict) -> str:
    tool = intent.get("tool", "chat")

    if tool == "log_expense":
        out = json.loads(pl.pocketledger_log_expense(pl.LogExpenseInput(
            amount=float(intent["amount"]), merchant=str(intent.get("merchant", "unknown")),
            category=intent.get("category") or None, note=intent.get("note") or "",
            date=intent.get("date") or "today")))
        if out.get("status") == "duplicate":
            return "⚠️ That exact expense already exists — not added again."
        return (f"✅ Logged {rup(out['amount'])} — <b>{out['merchant']}</b> "
                f"(category: {out['category']}, {out['date']})")

    if tool == "monthly_summary":
        out = json.loads(pl.pocketledger_monthly_summary(pl.MonthlySummaryInput(
            month=intent.get("month") or THIS_MONTH())))
        lines = [f"<b>{out['month']}</b> — total spent: <b>{rup(out['total_spent'])}</b> "
                 f"({rup(out['vs_previous_month']['change'])} {out['vs_previous_month']['direction']} than previous month)"]
        for c in out["by_category"]:
            lines.append(f"• {c['category']}: {rup(c['total'])} ({c['transactions']} txns)")
        if out["top_merchants"]:
            lines.append("Top merchants: " + ", ".join(
                f"{m['merchant']} {rup(m['total'])}" for m in out["top_merchants"]))
        return "<br>".join(lines) if out["by_category"] else f"No expenses found for {out['month']} yet."

    if tool == "query_expenses":
        out = json.loads(pl.pocketledger_query_expenses(pl.QueryExpensesInput(
            category=intent.get("category") or None, start_date=intent.get("start_date") or None,
            end_date=intent.get("end_date") or None, merchant=intent.get("merchant") or None,
            limit=int(intent.get("limit") or 25))))
        if not out["transactions"]:
            return "No matching transactions found."
        lines = [f"Found {out['count']} — expense total {rup(out['expense_total_of_shown'])}:"]
        for t in out["transactions"][:15]:
            lines.append(f"• {t['date']} — {rup(t['amount'])} — {t['merchant']} ({t['category']}) [id {t['id']}]")
        return "<br>".join(lines)

    if tool == "set_budget":
        out = json.loads(pl.pocketledger_set_budget(pl.SetBudgetInput(
            category=intent["category"], monthly_limit=float(intent["monthly_limit"]))))
        return f"✅ Budget saved: <b>{out['category']}</b> → {rup(out['monthly_limit'])}/month"

    if tool == "check_budgets":
        out = json.loads(pl.pocketledger_check_budgets(pl.CheckBudgetsInput(
            month=intent.get("month") or None)))
        if not out["budgets"]:
            return out.get("message", "No budgets set yet.")
        icon = {"OVER": "🔴", "WARNING": "🟡", "ok": "🟢"}
        return "<br>".join(
            f"{icon[b['status']]} {b['category']}: {rup(b['spent'])} / {rup(b['limit'])} ({b['used_pct']}%)"
            for b in out["budgets"])

    if tool == "find_recurring":
        out = json.loads(pl.pocketledger_find_recurring(pl.FindRecurringInput()))
        if not out["charges"]:
            return "No recurring charges detected yet (needs ≥3 similar payments to the same merchant)."
        lines = [f"Found {out['recurring_count']} recurring charges "
                 f"≈ <b>{rup(out['estimated_total_monthly_cost'])}/month</b>:"]
        for c in out["charges"]:
            lines.append(f"• {c['merchant']} — {rup(c['typical_amount'])} {c['cadence']} "
                         f"(≈{rup(c['monthly_equivalent'])}/mo, last {c['last_seen']})")
        return "<br>".join(lines)

    if tool == "import_statement":
        out = json.loads(pl.pocketledger_import_statement(pl.ImportStatementInput(
            csv_path=intent.get("csv_path", ""))))
        if out.get("status") == "error":
            return "⚠️ " + out["message"]
        msg = f"✅ Imported {out['imported']} transactions ({out['duplicates_skipped']} duplicates skipped)."
        if out["errors"]:
            msg += "<br>Some rows had issues: " + "; ".join(out["errors"][:3])
        return msg

    return intent.get("reply", "I can log expenses, show summaries, budgets, and recurring charges.")


# ------------------------------------------------------------------ web app

app = Flask(__name__)

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PocketLedger</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Big+Shoulders:wght@600;800&family=Geist+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a120d;--glass:rgba(255,255,255,.05);--edge:rgba(255,255,255,.1);
--cyan:#3ddc97;--coral:#ff6b5e;--gold:#f5c04a;--violet:#a3e635;
--ink:#edf3ee;--mut:#8fa697;--dim:#57705f}
*{box-sizing:border-box;margin:0;padding:0}
html{scrollbar-color:#2c4a38 transparent}
body{font-family:'Geist Mono',monospace;color:var(--ink);min-height:100vh;
background:var(--bg);padding:28px 20px 130px;overflow-x:hidden}
/* ---- living aurora background ---- */
#sky{position:fixed;inset:-20%;z-index:-1;filter:blur(90px);opacity:.55}
.blob{position:absolute;border-radius:50%}
.b1{width:55vw;height:55vw;background:radial-gradient(circle,#14532d,transparent 65%);
top:-10%;left:-10%;animation:drift1 26s ease-in-out infinite alternate}
.b2{width:45vw;height:45vw;background:radial-gradient(circle,#4a3a10,transparent 65%);
bottom:-15%;right:-5%;animation:drift2 32s ease-in-out infinite alternate}
.b3{width:30vw;height:30vw;background:radial-gradient(circle,rgba(163,230,53,.28),transparent 70%);
top:40%;left:55%;animation:drift3 38s ease-in-out infinite alternate}
@keyframes drift1{to{transform:translate(12vw,8vh) scale(1.15)}}
@keyframes drift2{to{transform:translate(-10vw,-10vh) scale(1.2)}}
@keyframes drift3{to{transform:translate(-14vw,10vh) scale(.85)}}
.wrap{max-width:1060px;margin:0 auto}
header{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:26px}
.brand{font-family:'Big Shoulders',sans-serif;font-weight:800;font-size:2.1rem;letter-spacing:.5px}
.brand span{background:linear-gradient(90deg,var(--cyan),var(--gold),var(--violet),var(--cyan));
background-size:300% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;
animation:hue 9s linear infinite}
@keyframes hue{to{background-position:300% 0}}
.tag{color:var(--mut);font-size:.72rem;margin-top:2px}
.monthpick{display:flex;align-items:center;gap:10px;font-size:.9rem}
.monthpick button{background:var(--glass);border:1px solid var(--edge);color:var(--ink);
border-radius:8px;padding:5px 12px;cursor:pointer;font-family:inherit;font-size:1rem;transition:.2s}
.monthpick button:hover{border-color:var(--cyan);transform:translateY(-1px)}
#mlabel{font-family:'Big Shoulders',sans-serif;font-weight:600;font-size:1.25rem;min-width:98px;text-align:center}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:14px;margin-bottom:22px}
.card{background:var(--glass);border:1px solid var(--edge);border-radius:18px;padding:18px 20px;
backdrop-filter:blur(14px);transition:transform .25s,border-color .25s,box-shadow .25s;position:relative}
.card:hover{transform:translateY(-3px);border-color:rgba(61,220,151,.45);
box-shadow:0 10px 34px rgba(61,220,151,.09)}
.card .lbl{color:var(--mut);font-size:.68rem;text-transform:uppercase;letter-spacing:.14em;margin-bottom:8px}
.card .big{font-family:'Big Shoulders',sans-serif;font-weight:800;font-size:2.5rem;line-height:1}
#total{background:linear-gradient(120deg,var(--cyan),var(--gold));
-webkit-background-clip:text;background-clip:text;color:transparent}
.card .sub{color:var(--mut);font-size:.72rem;margin-top:8px}
.up{color:var(--coral)}.down{color:var(--cyan)}.gold{color:var(--gold)}.cy{color:var(--cyan)}
.grid2{display:grid;grid-template-columns:1.25fr 1fr;gap:14px;margin-bottom:22px}
@media(max-width:820px){.grid2{grid-template-columns:1fr}body{padding-bottom:160px}}
h2{font-family:'Big Shoulders',sans-serif;font-weight:600;font-size:1.05rem;letter-spacing:.08em;
text-transform:uppercase;color:var(--mut);margin-bottom:14px}
.bar{margin-bottom:12px}
.bar .row{display:flex;justify-content:space-between;font-size:.8rem;margin-bottom:5px}
.bar .amt{color:var(--ink);font-weight:500}
.bar .trk{height:8px;border-radius:6px;background:rgba(255,255,255,.05);overflow:hidden}
.bar .fill{height:100%;border-radius:6px;width:0;transition:width .9s cubic-bezier(.2,.7,.2,1);
background:linear-gradient(90deg,#166b45,var(--cyan))}
.bud .fill.ok{background:linear-gradient(90deg,#166b45,var(--cyan))}
.bud .fill.warn{background:linear-gradient(90deg,#8a6a1a,var(--gold))}
.bud .fill.over{background:linear-gradient(90deg,#8f2f28,var(--coral))}
.bud .pct{font-size:.72rem;color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:.8rem}
td{padding:8px 6px;border-bottom:1px solid rgba(255,255,255,.05);vertical-align:top}
tr:last-child td{border-bottom:none}
tbody tr{transition:background .15s}
tbody tr:hover{background:rgba(255,255,255,.03)}
td.a{text-align:right;white-space:nowrap;font-weight:500}
td.a.credit{color:var(--cyan)}
.chip{display:inline-block;font-size:.64rem;padding:2px 8px;border-radius:20px;
border:1px solid var(--edge);color:var(--mut);margin-left:6px}
.mutt{color:var(--dim);font-size:.78rem}
.empty{color:var(--dim);font-size:.8rem;padding:8px 0}
.reclist .row{display:flex;justify-content:space-between;font-size:.8rem;
padding:7px 0;border-bottom:1px solid rgba(255,255,255,.05)}
.reclist .row:last-child{border-bottom:none}
#dock{position:fixed;left:0;right:0;bottom:0;padding:14px 20px 18px;
background:linear-gradient(180deg,transparent,rgba(5,12,8,.94) 35%)}
#dockin{max-width:1060px;margin:0 auto}
#toast{max-width:1060px;margin:0 auto 10px;background:var(--glass);border:1px solid rgba(61,220,151,.3);
border-radius:12px;padding:10px 16px;font-size:.85rem;display:none;backdrop-filter:blur(12px)}
#toast b{color:var(--cyan)}
form{display:flex;gap:10px;align-items:center;background:rgba(7,15,10,.9);
border:1px solid var(--edge);border-radius:14px;padding:6px 6px 6px 18px;backdrop-filter:blur(12px);
transition:border-color .2s,box-shadow .2s}
form:focus-within{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(61,220,151,.12)}
.prompt{color:var(--cyan);font-weight:700;animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{50%{opacity:.35}}
input{flex:1;background:none;border:none;outline:none;color:var(--ink);
font-family:inherit;font-size:.95rem;padding:10px 0}
input::placeholder{color:var(--dim)}
button.send{background:linear-gradient(120deg,var(--cyan),#a7f3d0);color:#06130c;border:none;border-radius:10px;
padding:10px 22px;font-family:'Big Shoulders',sans-serif;font-weight:800;font-size:1rem;
letter-spacing:.06em;cursor:pointer;transition:transform .15s}
button.send:hover{transform:translateY(-1px)}
button.send:disabled{opacity:.5}
.rise{opacity:0;transform:translateY(10px);animation:rise .5s ease forwards}
@keyframes rise{to{opacity:1;transform:none}}
@media(prefers-reduced-motion:reduce){
*,#sky .blob,.prompt,.brand span{animation:none!important;transition:none!important}
.rise{opacity:1;transform:none}.bar .fill{transition:none}}
</style></head><body>
<div id="sky"><div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div></div>
<div class="wrap">
<header class="rise">
  <div><div class="brand">Pocket<span>Ledger</span></div>
  <div class="tag">your money, deterministic — the LLM only translates</div></div>
  <div class="monthpick"><button id="prev" aria-label="Previous month">&#8249;</button>
  <span id="mlabel">&nbsp;</span><button id="next" aria-label="Next month">&#8250;</button></div>
</header>
<div class="cards">
  <div class="card rise" style="animation-delay:.05s"><div class="lbl">Spent this month</div>
    <div class="big" id="total">—</div><div class="sub" id="delta">&nbsp;</div></div>
  <div class="card rise" style="animation-delay:.12s"><div class="lbl">Budgets</div>
    <div class="big" id="budflag">—</div><div class="sub" id="budsub">&nbsp;</div></div>
  <div class="card rise" style="animation-delay:.19s"><div class="lbl">Recurring / month</div>
    <div class="big gold" id="rectotal">—</div><div class="sub" id="recsub">&nbsp;</div></div>
</div>
<div class="grid2">
  <div class="card rise" style="animation-delay:.26s"><h2>By category</h2><div id="cats"></div></div>
  <div>
    <div class="card rise" style="animation-delay:.3s;margin-bottom:14px"><h2>Budgets</h2><div id="buds" class="bud"></div></div>
    <div class="card rise" style="animation-delay:.34s"><h2>Recurring</h2><div id="recs" class="reclist"></div></div>
  </div>
</div>
<div class="card rise" style="animation-delay:.4s"><h2>Recent transactions</h2><table id="txns"><tbody></tbody></table></div>
</div>
<div id="dock"><div id="toast"></div><div id="dockin">
<form id="f"><span class="prompt">&gt;</span>
<input id="m" placeholder="log 120 rupees chai &middot; set food budget 5000 &middot; summary this month" autocomplete="off" autofocus>
<button class="send" id="b">SEND</button></form>
</div></div>
<script>
const $=id=>document.getElementById(id);
const REDUCE=matchMedia("(prefers-reduced-motion: reduce)").matches;
const rup=p=>{const s=p<0?"-":"";p=Math.abs(p);return s+"\u20b9"+Math.floor(p/100).toLocaleString("en-IN")+(p%100?"."+String(p%100).padStart(2,"0"):"")};
function countUp(el,paise){
 if(REDUCE||paise===0){el.textContent=rup(paise);return}
 const t0=performance.now(),dur=700;
 function tick(t){const k=Math.min(1,(t-t0)/dur),e=1-Math.pow(1-k,3);
 el.textContent=rup(Math.round(paise*e));if(k<1)requestAnimationFrame(tick)}
 requestAnimationFrame(tick)}
let month=new Date().toISOString().slice(0,7);
function shift(d){let[y,m]=month.split("-").map(Number);m+=d;if(m<1){m=12;y--}if(m>12){m=1;y++}
month=y+"-"+String(m).padStart(2,"0");load()}
$("prev").onclick=()=>shift(-1);$("next").onclick=()=>shift(1);
async function load(){
 $("mlabel").textContent=month;
 const r=await fetch("/api/dashboard?month="+month);const d=await r.json();
 countUp($("total"),d.total_paise);
 const diff=d.total_paise-d.prev_total_paise;
 $("delta").innerHTML=diff===0?"same as last month":
   (diff>0?`<span class="up">&#9650; ${rup(diff)}</span> more than last month`
          :`<span class="down">&#9660; ${rup(-diff)}</span> less than last month`);
 const over=d.budgets.filter(b=>b.status==="over").length,
       warn=d.budgets.filter(b=>b.status==="warn").length;
 $("budflag").innerHTML=d.budgets.length?
   (over?`<span class="up">${over} over</span>`:warn?`<span class="gold">${warn} near limit</span>`:`<span class="cy">all ok</span>`):"none set";
 $("budsub").textContent=d.budgets.length?d.budgets.length+" budget(s) tracked":"try: set food budget 5000";
 const rt=d.recurring.reduce((s,c)=>s+c.monthly_paise,0);
 if(d.recurring.length)countUp($("rectotal"),rt);else $("rectotal").textContent="—";
 $("recsub").textContent=d.recurring.length?d.recurring.length+" recurring charge(s)":"needs 3+ repeats to detect";
 const maxc=Math.max(1,...d.categories.map(c=>c.paise));
 $("cats").innerHTML=d.categories.length?d.categories.map(c=>`<div class="bar">
   <div class="row"><span>${c.category}<span class="chip">${c.n}</span></span>
   <span class="amt">${rup(c.paise)}</span></div>
   <div class="trk"><div class="fill" data-w="${Math.round(100*c.paise/maxc)}"></div></div>
   </div>`).join(""):`<div class="empty">No expenses in ${month} yet. Log one below.</div>`;
 $("buds").innerHTML=d.budgets.length?d.budgets.map(b=>`<div class="bar">
   <div class="row"><span>${b.category}</span>
   <span class="amt">${rup(b.spent_paise)} / ${rup(b.limit_paise)} <span class="pct">${b.pct}%</span></span></div>
   <div class="trk"><div class="fill ${b.status}" data-w="${Math.min(100,b.pct)}"></div></div>
   </div>`).join(""):`<div class="empty">No budgets yet.</div>`;
 $("recs").innerHTML=d.recurring.length?d.recurring.map(c=>`<div class="row">
   <span>${c.merchant} <span class="chip">${c.cadence}</span></span>
   <span class="amt">${rup(c.typical_paise)}</span></div>`).join(""):
   `<div class="empty">Nothing detected yet.</div>`;
 $("txns").querySelector("tbody").innerHTML=d.recent.length?d.recent.map(t=>`<tr>
   <td class="mutt">${t.date}</td><td>${t.merchant}<span class="chip">${t.category}</span></td>
   <td class="a ${t.paise<0?"credit":""}">${t.paise<0?"+"+rup(-t.paise):rup(t.paise)}</td></tr>`).join(""):
   `<tr><td class="empty">No transactions yet.</td></tr>`;
 requestAnimationFrame(()=>requestAnimationFrame(()=>{
   document.querySelectorAll(".fill").forEach(f=>f.style.width=f.dataset.w+"%")}));
}
const f=$("f"),m=$("m"),b=$("b"),toast=$("toast");
const hist=[];
f.onsubmit=async e=>{e.preventDefault();const t=m.value.trim();if(!t)return;
 b.disabled=true;toast.style.display="block";
 toast.innerHTML="<span class=\'mutt\'>you:</span> "+t.replace(/</g,"&lt;")+"<br>&#8230;";
 try{const r=await fetch("/chat",{method:"POST",headers:{"Content-Type":"application/json"},
 body:JSON.stringify({message:t,history:hist})});const j=await r.json();
 toast.innerHTML="<span class=\'mutt\'>you:</span> "+t.replace(/</g,"&lt;")+"<br>"+j.reply;
 hist.push({role:"user",text:t});
 hist.push({role:"assistant",text:j.reply.replace(/<[^>]*>/g,"")});
 if(hist.length>16)hist.splice(0,hist.length-16);}
 catch(err){toast.textContent="Error: "+err;}
 b.disabled=false;m.value="";m.focus();load();};
load();
</script></body></html>"""




# ------------------------------------------------------- deployment support

def seed_demo_data():
    """Idempotent demo dataset for the public deployment (DEMO_MODE=1).
    Hash-dedupe makes re-seeding on every boot a no-op."""
    from datetime import date as _d, timedelta as _td
    today = _d.today()
    def mago(months, day):
        y, m = today.year, today.month - months
        while m < 1:
            m += 12; y -= 1
        try:
            return _d(y, m, min(day, 28)).isoformat()
        except ValueError:
            return _d(y, m, 28).isoformat()
    demo = [
        (mago(0, 2), 380, "swiggy"), (mago(0, 4), 542, "zepto"),
        (mago(0, 6), 245, "uber"), (mago(0, 8), 1299, "amazon"),
        (mago(0, 10), 120, "chai"), (mago(0, 12), 799, "airtel broadband"),
        (mago(1, 3), 420, "swiggy"), (mago(1, 9), 315, "blinkit"),
        (mago(1, 15), 199, "uber"), (mago(1, 20), 2100, "dmart"),
        (mago(3, 5), 649, "netflix"), (mago(2, 5), 649, "netflix"),
        (mago(1, 4), 649, "netflix"), (mago(0, 5), 649, "netflix"),
        (mago(3, 12), 199, "spotify"), (mago(2, 12), 199, "spotify"),
        (mago(1, 12), 199, "spotify"), (mago(0, 12), 199, "spotify"),
    ]
    ledger = pl.get_ledger()
    for d, amt, merch in demo:
        from categorizer import categorize
        ledger.add_transaction(d, amt * 100, merch, note="demo",
                               category=categorize(merch), source="manual")
    ledger.set_budget("food", 5000 * 100)
    ledger.set_budget("groceries", 4000 * 100)


DEMO_MODE = os.environ.get("DEMO_MODE") == "1"
if DEMO_MODE:
    os.environ.setdefault("POCKETLEDGER_DB", "/tmp/pocketledger_demo.db")
    seed_demo_data()

# naive in-memory rate limit for the public demo: protects the Gemini quota
from collections import defaultdict as _dd
from time import time as _now
_hits = _dd(list)
RATE_PER_HOUR = int(os.environ.get("RATE_PER_HOUR", "15"))
GLOBAL_PER_DAY = int(os.environ.get("GLOBAL_PER_DAY", "400"))
_global_hits = []

def rate_limited(ip: str) -> str | None:
    if not DEMO_MODE:
        return None
    t = _now()
    _hits[ip] = [x for x in _hits[ip] if t - x < 3600]
    while _global_hits and t - _global_hits[0] > 86400:
        _global_hits.pop(0)
    if len(_global_hits) >= GLOBAL_PER_DAY:
        return "Demo has hit its daily message limit — the dashboard still works. Come back tomorrow!"
    if len(_hits[ip]) >= RATE_PER_HOUR:
        return f"Demo limit: {RATE_PER_HOUR} messages/hour. The dashboard and month browsing still work!"
    _hits[ip].append(t)
    _global_hits.append(t)
    return None


# ---------------------------------------------------------------- agent loop

import html as _html

def execute_raw(intent: dict) -> dict:
    """Run a tool and return its raw JSON result (for the agent's eyes)."""
    tool = intent.get("tool")
    if tool == "log_expense":
        return json.loads(pl.pocketledger_log_expense(pl.LogExpenseInput(
            amount=float(intent["amount"]), merchant=str(intent.get("merchant", "unknown")),
            category=intent.get("category") or None, note=intent.get("note") or "",
            date=intent.get("date") or "today")))
    if tool == "monthly_summary":
        return json.loads(pl.pocketledger_monthly_summary(pl.MonthlySummaryInput(
            month=intent.get("month") or THIS_MONTH())))
    if tool == "query_expenses":
        return json.loads(pl.pocketledger_query_expenses(pl.QueryExpensesInput(
            category=intent.get("category") or None, start_date=intent.get("start_date") or None,
            end_date=intent.get("end_date") or None, merchant=intent.get("merchant") or None,
            limit=int(intent.get("limit") or 25))))
    if tool == "set_budget":
        return json.loads(pl.pocketledger_set_budget(pl.SetBudgetInput(
            category=intent["category"], monthly_limit=float(intent["monthly_limit"]))))
    if tool == "check_budgets":
        return json.loads(pl.pocketledger_check_budgets(pl.CheckBudgetsInput(
            month=intent.get("month") or None)))
    if tool == "find_recurring":
        return json.loads(pl.pocketledger_find_recurring(pl.FindRecurringInput()))
    if tool == "import_statement":
        return json.loads(pl.pocketledger_import_statement(pl.ImportStatementInput(
            csv_path=intent.get("csv_path", ""))))
    return {"error": f"unknown tool {tool}"}


MAX_STEPS = 3

def run_agent(message: str, history: list) -> dict:
    """history: [{"role":"user"|"assistant","text":...}] from the browser session.
    Returns {"reply": html, "tools_used": [names]}."""
    base = SYSTEM.replace("{today}", TODAY()).replace("{month}", THIS_MONTH())
    convo = ""
    for h in history[-8:]:
        who = "User" if h.get("role") == "user" else "Assistant"
        convo += f"{who}: {h.get('text','')[:400]}\n"
    convo += f"User: {message}\n"

    transcript = ""
    tools_used = []
    for _ in range(MAX_STEPS + 1):
        prompt = (base + "\nConversation so far:\n" + convo + transcript +
                  "\nRespond now with ONE JSON object (action tool or final):")
        decision = call_gemini(prompt)
        action = decision.get("action") or ("tool" if decision.get("tool") else "final")
        if action == "final" or len(tools_used) >= MAX_STEPS:
            reply = decision.get("reply") or "Done."
            return {"reply": _html.escape(str(reply)).replace("\n", "<br>"),
                    "tools_used": tools_used}
        result = execute_raw(decision)
        tools_used.append(decision.get("tool", "?"))
        transcript += (f"\n[Tool call] {json.dumps(decision)}"
                       f"\n[Tool result] {json.dumps(result)[:1500]}\n")
    return {"reply": "I hit my tool-call limit for one message — ask me in smaller steps.",
            "tools_used": tools_used}


# ------------------------------------------------------------- dashboard API

def dashboard_data(month: str) -> dict:
    ledger = pl.get_ledger()
    y, m = map(int, month.split("-"))
    prev = f"{y-1}-12" if m == 1 else f"{y}-{m-1:02d}"
    total = ledger.monthly_total(month)
    prev_total = ledger.monthly_total(prev)
    cats = [{"category": r["category"], "paise": r["total_paise"], "n": r["n"]}
            for r in ledger.monthly_by_category(month)]
    spent_by_cat = {c["category"]: c["paise"] for c in cats}
    budgets = []
    for b in ledger.budgets():
        spent = spent_by_cat.get(b["category"], 0)
        limit = b["monthly_limit_paise"]
        pct = round(100 * spent / limit, 1) if limit else 0.0
        budgets.append({"category": b["category"], "limit_paise": limit,
                        "spent_paise": spent, "pct": pct,
                        "status": "over" if spent > limit else ("warn" if pct >= 80 else "ok")})
    rec = pl.find_recurring(ledger.all_expense_txns())
    recent = [dict(id=r["id"], date=r["txn_date"], paise=r["amount_paise"],
                   merchant=r["merchant"], category=r["category"])
              for r in ledger.query(limit=12)]
    return {"month": month, "total_paise": total, "prev_total_paise": prev_total,
            "categories": cats, "budgets": budgets,
            "recurring": [{"merchant": c.merchant, "cadence": c.cadence,
                           "typical_paise": c.typical_amount_paise,
                           "monthly_paise": c.monthly_cost_paise} for c in rec],
            "recent": recent}


@app.get("/api/dashboard")
def api_dashboard():
    from datetime import date as _d
    month = request.args.get("month") or _d.today().strftime("%Y-%m")
    try:
        return jsonify(dashboard_data(month))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/")
def index():
    return PAGE


@app.post("/chat")
def chat():
    body = request.get_json(force=True) or {}
    msg = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not msg:
        return jsonify({"reply": "Say something first :)"})
    limited = rate_limited(request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip())
    if limited:
        return jsonify({"reply": limited})
    if not API_KEY:
        return jsonify({"reply": "No GEMINI_API_KEY found. Create a .env file here with: GEMINI_API_KEY=your_key"})
    try:
        out = run_agent(msg, history)
        badge = ""
        if out["tools_used"]:
            badge = " <span class=\"chip\">" + " &rarr; ".join(out["tools_used"]) + "</span>"
        return jsonify({"reply": out["reply"] + badge})
    except Exception as e:
        return jsonify({"reply": redact(f"Something went wrong: {e}")})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    host = "0.0.0.0" if os.environ.get("RENDER") or os.environ.get("DEMO_MODE") else "127.0.0.1"
    print(f"\n  PocketLedger → http://localhost:{port}\n")
    app.run(host=host, port=port, debug=False)
