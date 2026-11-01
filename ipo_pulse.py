"""IPO Pulse v1 — weekly note on new IPO filings with SEBI.

Flow: fetch SEBI "Draft Offer Documents" register -> save snapshot ->
diff vs last week -> Claude writes a 5-line note from the diff only ->
write site/index.html (with a feedback link).

Env vars:
  OPENAI_API_KEY      AI note via OpenAI (model: OPENAI_MODEL, default gpt-4o-mini)
  ANTHROPIC_API_KEY   or AI note via Claude (used if no OpenAI key)
  FEEDBACK_URL        optional link to a Google Form / Tally form for reader feedback
  CLAUDE_MODEL        optional, default claude-sonnet-5-5
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SEBI_URL = ("https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
            "?doListing=yes&sid=3&ssid=15&smid=10")
ROOT = Path(__file__).parent
DATA = ROOT / "data"
SITE = ROOT / "site"
UA = {"User-Agent": "IPO-Pulse/1.0 (student project; weekly read)"}


# ---------- 1. fetch + parse ----------
def fetch_html(url: str = SEBI_URL) -> str:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.text


def classify(title: str) -> str:
    t = title.upper()
    if "ADDENDUM" in t:
        return "Addendum"
    if "UDRHP" in t:
        return "Updated DRHP"
    if "DRHP" in t:
        return "DRHP"
    return "Other"


def company_of(title: str) -> str:
    name = re.split(r"\s*[-–]\s*(?:Addendum|DRHP|UDRHP)", title, maxsplit=1, flags=re.I)[0]
    name = re.sub(r"\s+(Limited|Ltd\.?)\s*$", "", name.strip(), flags=re.I)
    return name.strip(" -–")


def parse_listing(page_html: str) -> list[dict]:
    """Return rows: {id, date, title, company, type, url}."""
    soup = BeautifulSoup(page_html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        date_txt = tds[0].get_text(" ", strip=True)
        try:
            date = dt.datetime.strptime(date_txt, "%b %d, %Y").date()
        except ValueError:
            continue
        a = tds[1].find("a", href=re.compile(r"/filings/public-issues/"))
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        href = a["href"]
        m = re.search(r"_(\d+)\.html", href)
        rows.append({
            "id": m.group(1) if m else href,
            "date": date.isoformat(),
            "title": title,
            "company": company_of(title),
            "type": classify(title),
            "url": href,
        })
    return rows


# ---------- 2. snapshot + diff ----------
def week_bounds(today: dt.date) -> tuple[dt.date, dt.date]:
    """Most recent Mon-Sun week. On a Sunday that is the current week (ending today);
    on any other day it is the last complete week."""
    if today.weekday() == 6:
        start = today - dt.timedelta(days=6)
    else:
        start = today - dt.timedelta(days=today.weekday() + 7)
    return start, start + dt.timedelta(days=6)


def in_range(rows, start, end):
    return [r for r in rows if start.isoformat() <= r["date"] <= end.isoformat()]


def build_metrics(rows: list[dict], today: dt.date) -> dict:
    ws, we = week_bounds(today)
    ps, pe = ws - dt.timedelta(days=7), we - dt.timedelta(days=7)
    this_wk, prev_wk = in_range(rows, ws, we), in_range(rows, ps, pe)
    early = [r for r in rows if r["date"] > we.isoformat()]
    cnt = lambda rs: dict(Counter(r["type"] for r in rs))
    return {
        "week_start": ws.isoformat(), "week_end": we.isoformat(),
        "this_week": {"counts": cnt(this_wk), "filings": this_wk},
        "prev_week": {"counts": cnt(prev_wk), "filings": prev_wk},
        "early_next_week": early,
    }


def save_snapshot(rows: list[dict], metrics: dict, today: dt.date) -> Path:
    DATA.mkdir(exist_ok=True)
    p = DATA / f"snapshot-{today.isoformat()}.json"
    p.write_text(json.dumps({"rows": rows, "metrics": metrics}, indent=2))
    return p


def new_since_last_snapshot(rows: list[dict], today: dt.date) -> list[dict]:
    prev = sorted(p for p in DATA.glob("snapshot-*.json") if p.stem < f"snapshot-{today.isoformat()}")
    if not prev:
        return rows
    seen = {r["id"] for r in json.loads(prev[-1].read_text())["rows"]}
    return [r for r in rows if r["id"] not in seen]


# ---------- 3. AI note ----------
PROMPT = """You write "IPO Pulse", a weekly 5-line note on India's IPO pipeline.
Use ONLY the JSON below (SEBI's register of draft offer documents). Never invent
issue sizes, sectors, valuations or dates. If you infer a theme from company names,
say "based on company names". Plain text only: no markdown, no links, no URLs. Ignore filings of type "Other" (notices) unless relevant.
Line 1 (the count of new DRHPs) is written separately and shown to you below; do NOT repeat it.
Write exactly 4 numbered lines, each under 35 words:
1) the trend
2) a theme or cluster, if any (else: notable names)
3) one filing to watch and why: prefer an Addendum or Updated DRHP (it means the filing is progressing through SEBI review); never call a fresh DRHP "progressing"
4) "So what:" one takeaway for an IPO-watching investor or IB/PE aspirant.

LINE 1 (already written): {line1}

DATA:
{data}"""


def fact_line(m: dict) -> str:
    """Line 1, computed from data so the headline number is always exact."""
    tw = [r for r in m["this_week"]["filings"] if r["type"] == "DRHP"]
    n_prev = sum(1 for r in m["prev_week"]["filings"] if r["type"] == "DRHP")
    upd = [r for r in m["this_week"]["filings"] if r["type"] in ("Addendum", "Updated DRHP")]
    head = "No new DRHPs" if not tw else f"{len(tw)} new DRHP{'s' if len(tw) != 1 else ''} ({', '.join(r['company'] for r in tw)})"
    tail = f"; {len(upd)} existing filing{'s' if len(upd) != 1 else ''} updated" if upd else ""
    return f"{head} filed with SEBI this week, vs {n_prev} last week{tail}."


def _lines(text: str) -> list[str]:
    return [re.sub(r"^\s*\d+[\).]\s*", "", l).strip() for l in text.splitlines() if l.strip()]


def ai_note(metrics: dict) -> list[str]:
    """Uses OpenAI if OPENAI_API_KEY is set, else Anthropic if ANTHROPIC_API_KEY is set,
    else a rule-based note. Any API error also falls back to the rule-based note."""
    line1 = fact_line(metrics)
    prompt = PROMPT.format(line1=line1, data=json.dumps(metrics, indent=1))
    try:
        if os.environ.get("OPENAI_API_KEY"):
            from openai import OpenAI
            client = OpenAI()
            resp = client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL") or "gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=600,
            )
            text = resp.choices[0].message.content or ""
        elif os.environ.get("ANTHROPIC_API_KEY"):
            import anthropic
            client = anthropic.Anthropic()
            msg = client.messages.create(
                model=os.environ.get("CLAUDE_MODEL") or "claude-sonnet-5-5",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in msg.content if b.type == "text")
        else:
            return rule_note(metrics)
    except Exception as exc:  # keep the weekly run alive
        print(f"AI step failed, using rule-based note: {exc}", file=sys.stderr)
        return rule_note(metrics)
    lines = [l for l in _lines(text) if not l.lower().startswith("line 1")]
    return [line1] + lines[:4] if len(lines) >= 4 else rule_note(metrics)


def rule_note(m: dict) -> list[str]:
    tw, pw = m["this_week"], m["prev_week"]
    d_now, d_prev = tw["counts"].get("DRHP", 0), pw["counts"].get("DRHP", 0)
    names = ", ".join(r["company"] for r in tw["filings"] if r["type"] == "DRHP") or "none"
    adds = [r["company"] for r in tw["filings"] + m["early_next_week"] if r["type"] in ("Addendum", "Updated DRHP")]
    trend = "up" if d_now > d_prev else "down" if d_now < d_prev else "flat"
    return [
        fact_line(m),
        f"New filings are {trend} week on week.",
        f"{len(tw['filings'])} documents in total hit SEBI's register this week.",
        f"Filings progressing (addenda / updated DRHPs): {', '.join(adds) or 'none'}.",
        "So what: check next week's count to see whether the trend holds.",
    ]


# ---------- 4. render ----------
DEFAULT_FEEDBACK = "https://docs.google.com/forms/d/e/1FAIpQLSfQZ6C4ob-sq7mzRZ9Zxy6Nax07ioTTsDDY0lxFWTBpUgh2Bg/viewform?usp=sharing"
SITE_URL = os.environ.get("SITE_URL", "https://bespoke-souffle-de95ef.netlify.app")


def _fmt(d: str) -> str:
    return dt.date.fromisoformat(d).strftime("%-d %b")


def render(metrics: dict, note: list[str], feedback_url: str | None) -> str:
    e = html.escape
    fb = feedback_url or DEFAULT_FEEDBACK
    ws, we = metrics["week_start"], metrics["week_end"]
    tw, pw = metrics["this_week"], metrics["prev_week"]
    d_now, d_prev = tw["counts"].get("DRHP", 0), pw["counts"].get("DRHP", 0)
    add_now = tw["counts"].get("Addendum", 0) + tw["counts"].get("Updated DRHP", 0)
    pill = lambda t: f'<span class="pill{"" if t == "DRHP" else " amber"}">{e(t)}</span>'

    def rows(title, rs):
        if not rs:
            return f'<tr class="sep"><td colspan="3">{e(title)}</td></tr><tr><td colspan="3" class="small">No filings</td></tr>'
        body = "".join(
            f'<tr><td class="mono">{e(_fmt(r["date"]))}</td><td><a href="{e(r["url"])}" target="_blank" rel="noopener">{e(r["company"])}</a></td><td>{pill(r["type"])}</td></tr>'
            for r in rs)
        return f'<tr class="sep"><td colspan="3">{e(title)}</td></tr>{body}'

    table = (rows("Early signal · after week end", metrics["early_next_week"])
             + rows(f"This week · {_fmt(ws)}–{_fmt(we)}", tw["filings"])
             + rows("Previous week", pw["filings"]))
    li = "".join(f"<li><span>{e(x)}</span></li>" for x in note)
    headline = note[0] if note else "This week's IPO filings"
    desc = "A five-line weekly note on new IPO filings with SEBI: new DRHPs, trends, sector themes and what it means for investors."
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IPO Pulse</title>
<meta property="og:type" content="website"><meta property="og:title" content="IPO Pulse: weekly AI note on India's IPO pipeline">
<meta property="og:description" content="{desc}"><meta property="og:url" content="{SITE_URL}/">
<meta property="og:image" content="{SITE_URL}/og-image.png"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="627">
<meta name="twitter:card" content="summary_large_image"><meta name="description" content="{desc}">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700&family=Instrument+Sans:wght@400;600&family=JetBrains+Mono:wght@500&display=swap">
<style>
:root{{--paper:#F4F6F3;--surface:#fff;--ink:#132320;--muted:#5B6B67;--rule:#D5DDD9;--accent:#0E6E55;--accent-soft:#E1F0EA;--amber:#A8661A;--amber-soft:#F6EBDC}}
@media (prefers-color-scheme:dark){{:root{{--paper:#0F1716;--surface:#16211F;--ink:#E4ECE9;--muted:#97A8A3;--rule:#2A3936;--accent:#4CC79F;--accent-soft:#173A31;--amber:#E0A458;--amber-soft:#3A2C18;color-scheme:dark}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 "Instrument Sans",system-ui,sans-serif;padding:28px 16px 56px}}
.wrap{{max-width:760px;margin:0 auto;display:flex;flex-direction:column;gap:36px}}
h1,h2{{font-family:"Bricolage Grotesque",system-ui,sans-serif;margin:0;text-wrap:balance}}h1{{font-size:clamp(2rem,6vw,2.8rem);line-height:1.05}}h2{{font-size:1.3rem}}
.label{{font:500 .72rem/1 "JetBrains Mono",monospace;text-transform:uppercase;letter-spacing:.09em;color:var(--muted)}}.mono{{font-family:"JetBrains Mono",monospace}}
header{{display:flex;flex-direction:column;gap:12px;border-bottom:2px solid var(--ink);padding-bottom:20px}}.meta{{display:flex;flex-wrap:wrap;gap:8px 18px;color:var(--muted);font-size:.9rem}}
.pill{{display:inline-block;padding:3px 10px;border-radius:999px;font:500 .75rem/1.4 "JetBrains Mono",monospace;background:var(--accent-soft);color:var(--accent)}}.pill.amber{{background:var(--amber-soft);color:var(--amber)}}
section{{display:flex;flex-direction:column;gap:14px}}ol{{list-style:none;margin:0;padding:0;counter-reset:n}}
ol li{{counter-increment:n;display:grid;grid-template-columns:2rem 1fr;gap:10px;padding:13px 0;border-top:1px solid var(--rule)}}ol li:last-child{{border-bottom:1px solid var(--rule)}}
ol li::before{{content:counter(n);font:500 .85rem/1.6 "JetBrains Mono",monospace;color:var(--accent)}}
.kpis{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}.kpi{{background:var(--surface);border:1px solid var(--rule);border-radius:6px;padding:14px}}
.kpi b{{display:block;font:700 2rem/1.1 "Bricolage Grotesque",sans-serif;margin-top:6px}}.kpi span.d{{font-size:.82rem;color:var(--muted)}}
.tablebox{{overflow-x:auto;border:1px solid var(--rule);border-radius:6px;background:var(--surface)}}table{{border-collapse:collapse;width:100%;min-width:460px;font-size:.92rem}}
td,th{{text-align:left;padding:10px 12px;border-bottom:1px solid var(--rule)}}th{{font:500 .72rem "JetBrains Mono",monospace;text-transform:uppercase;color:var(--muted)}}
tr.sep td{{background:var(--paper);font:500 .72rem "JetBrains Mono",monospace;text-transform:uppercase;color:var(--muted)}}a{{color:var(--accent)}}.small{{font-size:.85rem;color:var(--muted)}}
.fb{{background:var(--surface);border:1px solid var(--ink);border-radius:8px;padding:20px;display:flex;flex-direction:column;gap:12px}}
.btn{{display:inline-block;align-self:flex-start;padding:13px 22px;border-radius:6px;background:var(--accent);color:var(--surface);text-decoration:none;font-weight:600}}
@media (max-width:520px){{.kpis{{grid-template-columns:1fr}}}}
</style></head><body><div class="wrap">
<header><span class="label">IPO Pulse · Weekly note</span><h1>{e(headline)}</h1>
<div class="meta"><span class="mono">Week of {e(_fmt(ws))} – {e(_fmt(we))} {we[:4]}</span><span>Source: SEBI, Draft Offer Documents filed</span><span class="pill">Auto-updated every Monday</span></div></header>
<section><h2>The five-line note</h2><ol>{li}</ol></section>
<section><h2>The numbers</h2><div class="kpis">
<div class="kpi"><span class="label">New DRHPs</span><b class="mono">{d_now}</b><span class="d">vs {d_prev} the week before</span></div>
<div class="kpi"><span class="label">Addenda / updates</span><b class="mono">{add_now}</b><span class="d">filings progressing</span></div>
<div class="kpi"><span class="label">All documents</span><b class="mono">{len(tw['filings'])}</b><span class="d">filed this week</span></div></div></section>
<section><h2>Filing log</h2><div class="tablebox"><table><thead><tr><th>Date</th><th>Company</th><th>Document</th></tr></thead><tbody>{table}</tbody></table></div>
<p class="small">Full register: <a href="{e(SEBI_URL)}" target="_blank" rel="noopener">SEBI, Draft Offer Documents filed with SEBI</a>.</p></section>
<section><div class="fb"><span class="label">Help shape IPO Pulse</span><h2>Was this week's note useful?</h2>
<p style="margin:0">Three quick questions. It takes about 30 seconds.</p><a class="btn" href="{e(fb)}" target="_blank" rel="noopener">Give feedback</a></div></section>
<section><h2>How it works</h2><p class="small">Every Monday a script reads SEBI's register, saves a snapshot, compares it with the previous week, and Claude writes the five lines from that comparison only. Every number can be traced to a SEBI listing.</p></section>
</div></body></html>"""


def main(argv: list[str]) -> int:
    today = dt.date.fromisoformat(argv[2]) if len(argv) > 2 and argv[1] == "--today" else dt.date.today()
    fixture = os.environ.get("SEBI_HTML_FILE")
    page = Path(fixture).read_text() if fixture else fetch_html()
    rows = parse_listing(page)
    if not rows:
        print("No rows parsed — SEBI page layout may have changed.", file=sys.stderr)
        return 1
    metrics = build_metrics(rows, today)
    metrics["new_since_last_run"] = len(new_since_last_snapshot(rows, today))
    save_snapshot(rows, metrics, today)
    note = ai_note(metrics)
    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(render(metrics, note, os.environ.get("FEEDBACK_URL")))
    print("\n".join(f"{i}. {l}" for i, l in enumerate(note, 1)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
