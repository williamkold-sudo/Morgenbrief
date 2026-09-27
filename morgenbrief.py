"""Daglig morgenbrief: henter nyheder via RSS, sammenfatter dem på dansk med Claude
og poster resultatet i en Slack-kanal via en incoming webhook."""

import calendar
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import trafilatura
from anthropic import Anthropic

# --- Indstillinger ---------------------------------------------------------
TZ = ZoneInfo("Europe/Copenhagen")
POST_HOUR = 8                      # Poster kun når klokken er 08 i København
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
LOOKBACK_HOURS = 26                # Lidt over et døgn, så intet falder imellem
PER_FEED = 4                       # Maks. artikler pr. feed
PER_SECTION = 10                   # Maks. artikler pr. sektion
ARTICLE_CHARS = 2000               # Hvor meget tekst pr. artikel sendes til Claude
STATE_FILE = Path("state/last_brief.json")
UA = {"User-Agent": "Mozilla/5.0 (morgenbrief; personal news digest)"}

SECTIONS = [
    {"key": "verden", "title": "🌍 VERDEN & POLITIK", "feeds": {
        "BBC": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "The Guardian": "https://www.theguardian.com/world/rss",
        "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    }},
    {"key": "tek", "title": "💻 TEK & AI", "feeds": {
        "The Verge": "https://www.theverge.com/rss/index.xml",
        "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
        "TechCrunch": "https://techcrunch.com/feed/",
        "MIT Technology Review": "https://www.technologyreview.com/feed/",
    }},
    {"key": "erhverv", "title": "📈 ERHVERV & ØKONOMI", "feeds": {
        "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
        "The Guardian Business": "https://www.theguardian.com/business/rss",
        "CNBC": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    }},
    {"key": "danmark", "title": "🇩🇰 DANMARK", "feeds": {
        "DR": "https://www.dr.dk/nyheder/service/feeds/allenyheder",
        "TV 2": "https://feeds.tv2.dk/nyheder/rss",
        # Tilføj flere danske kilder her, fx Politiken eller Altinget,
        # når du har fundet deres RSS-adresse.
    }},
]

WEEKDAYS = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
MONTHS = ["januar", "februar", "marts", "april", "maj", "juni", "juli",
          "august", "september", "oktober", "november", "december"]

SYSTEM_PROMPT = """Du er redaktør på en personlig dansk morgenbrief til én læser.
Du får artikler fra de seneste ca. 24 timer, grupperet i fire sektioner.

For hver sektion skal du:
- Skrive en kort, præcis overskrift der fanger dagens hovedtema.
- Skrive ét samlet resumé på ca. 150–175 ord, baseret på flere artikler og helst
  flere kilder – ikke én historie pr. kilde.
- Dække hovedtemaet i dybden plus 1–2 sekundære udviklinger.
- Nævne hvor kilderne er uenige eller vægter forskelligt, og hvad man skal holde øje med.
- Angive de 2–4 artikler du faktisk har brugt (kildenavn + artiklens url).

Regler:
- Skriv naturligt, flydende dansk, også når kilderne er engelske. Behold navne,
  virksomheder og fagudtryk i deres normale form.
- Neutral, saglig tone. Ingen indledning, hilsen eller afslutning.
- Brug kun information fra de vedlagte artikler. Opfind intet.
- Gentag ikke gårsdagens historier, medmindre der er sket noget nyt – så fokusér på det nye.
- Har en sektion kun lidt nyt, så gør den kortere i stedet for at fylde op.
- Ingen overskrifter med #; du må bruge *fed* sparsomt. Afsnit adskilles med en tom linje.

Svar KUN med gyldig JSON i præcis dette format, uden forklaring og uden kodeblok:
{"sektioner":[{"key":"verden","overskrift":"...","resume":"...","kilder":[{"navn":"...","url":"..."}]}]}
Brug præcis disse keys i denne rækkefølge: verden, tek, erhverv, danmark."""


# --- Hjælpefunktioner ------------------------------------------------------
def log(msg):
    print(msg, flush=True)


def danish_date(d):
    return f"{WEEKDAYS[d.weekday()]} {d.day}. {MONTHS[d.month - 1]}"


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def entry_time(entry):
    for attr in ("published_parsed", "updated_parsed"):
        t = entry.get(attr)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return None


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(now, text):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"date": now.date().isoformat(), "text": text},
                                     ensure_ascii=False, indent=2), encoding="utf-8")


# --- Indsamling ------------------------------------------------------------
def fetch_feed(name, url, cutoff):
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log(f"  ⚠️  {name}: kunne ikke hente feed ({e})")
        return []
    items = []
    for e in feedparser.parse(r.content).entries:
        t = entry_time(e)
        if t is None or t < cutoff:
            continue
        items.append({"source": name, "title": strip_html(e.get("title", "")),
                      "url": e.get("link", ""), "summary": strip_html(e.get("summary", "")),
                      "time": t})
    items.sort(key=lambda x: x["time"], reverse=True)
    log(f"  {name}: {len(items)} nye artikler")
    return items[:PER_FEED]


def fetch_text(url, fallback):
    text = ""
    try:
        r = requests.get(url, headers=UA, timeout=15)
        text = trafilatura.extract(r.text) or ""
    except Exception:
        pass
    return (text.strip() or fallback)[:ARTICLE_CHARS]


def collect(cutoff):
    result = {}
    for sec in SECTIONS:
        log(f"{sec['title']}")
        lists = [fetch_feed(n, u, cutoff) for n, u in sec["feeds"].items()]
        merged = []  # Fletter feeds, så flere kilder kommer med
        for i in range(PER_FEED):
            for lst in lists:
                if i < len(lst):
                    merged.append(lst[i])
        merged = merged[:PER_SECTION]
        for a in merged:
            a["text"] = fetch_text(a["url"], a["summary"])
        result[sec["key"]] = merged
    return result


# --- Claude ----------------------------------------------------------------
def generate(articles, previous, now):
    parts = [f"I dag er {danish_date(now)} {now.year}."]
    if previous:
        parts.append(f"GÅRSDAGENS BRIEF (gentag ikke uden nyt):\n{previous}")
    parts.append("ARTIKLER:")
    for sec in SECTIONS:
        parts.append(f"\n=== SEKTION: {sec['key']} ({sec['title']}) ===")
        for n, a in enumerate(articles.get(sec["key"], []), 1):
            parts.append(f"[{n}] {a['source']} | {a['title']} | {a['url']} | "
                         f"{a['time']:%Y-%m-%d %H:%M} UTC\n{a['text']}")

    client = Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": "\n\n".join(parts)}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    return json.loads(raw)


# --- Slack -----------------------------------------------------------------
def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_blocks(brief, now):
    header = f"📰 Morgenbrief – {danish_date(now)}"
    titles = {s["key"]: s["title"] for s in SECTIONS}
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}}]
    for sec in brief["sektioner"]:
        kilder = " · ".join(f"<{k['url']}|{esc(k['navn'])}>"
                            for k in sec.get("kilder", []) if k.get("url"))
        text = (f"*{titles.get(sec['key'], sec['key'])}*\n"
                f"*{esc(sec['overskrift'])}*\n{esc(sec['resume'])}")
        if kilder:
            text += f"\n_Kilder: {kilder}_"
        blocks.append({"type": "divider"})
        # Slack tillader max 3000 tegn pr. tekstblok
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text[:2990]}})
    return header, blocks


def to_plain(brief):
    return "\n\n".join(f"{s['overskrift']}\n{s['resume']}" for s in brief["sektioner"])


# --- Main ------------------------------------------------------------------
def main():
    force = os.getenv("FORCE", "").lower() == "true"
    dry_run = os.getenv("DRY_RUN", "").lower() == "true"
    now = datetime.now(TZ)
    state = load_state()

    if not force:
        if now.hour != POST_HOUR:
            log(f"Klokken er {now:%H:%M} i København – springer over.")
            return
        if state.get("date") == now.date().isoformat():
            log("Morgenbriefen er allerede postet i dag – springer over.")
            return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    articles = collect(cutoff)
    if not any(articles.values()):
        sys.exit("Ingen artikler fundet – tjek feed-adresserne.")

    brief = generate(articles, state.get("text", ""), now)
    header, blocks = build_blocks(brief, now)

    if dry_run:
        print(json.dumps(blocks, ensure_ascii=False, indent=2))
        return

    webhook = os.environ["SLACK_WEBHOOK_URL"]
    r = requests.post(webhook, json={"text": header, "blocks": blocks}, timeout=20)
    r.raise_for_status()
    save_state(now, to_plain(brief))
    log("✅ Morgenbrief postet i Slack.")


if __name__ == "__main__":
    main()
