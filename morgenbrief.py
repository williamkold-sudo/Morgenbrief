"""Daglig pengefokuseret morgenbrief: henter nyheder via RSS og markedstal,
sammenfatter på dansk med Claude og poster i Slack via en incoming webhook."""

import calendar
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
import trafilatura
from anthropic import Anthropic

# --- Indstillinger ---------------------------------------------------------
TZ = ZoneInfo("Europe/Copenhagen")
POST_HOUR = 7                      # Briefen postes kl. 07:00 i København
WINDOW = ((6, 30), (9, 0))         # Kørsler i dette tidsrum må poste (backup ved forsinkelser)
PREP_MINUTES = 12                  # Hvor mange minutter før postetidspunktet nyhederne hentes
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
LOOKBACK_HOURS = 26
PER_FEED = 4                       # Maks. artikler pr. feed
PER_SECTION = 14                   # Maks. artikler pr. emnegruppe
ARTICLE_CHARS = 2000
STATE_FILE = Path("state/last_brief.json")
UA = {"User-Agent": "Mozilla/5.0 (morgenbrief; personal news digest)"}

# Hvem briefen er til. Tilføj gerne din branche og rolle, så karrierevinklen
# bliver mere præcis, fx: "Arbejder som projektleder i en dansk softwarevirksomhed."
PROFIL = "Investerer i aktier og ETF'er og vil styrke sin karriere og løn og studerer på nuværende tidspunkt ved CBS international business and politics og er interesseret i en karriere indenfor makroinvestering eller venturekapital."

# Emnegrupper med egne kilder
FEED_GROUPS = [
    {"key": "verden", "feeds": {
        "BBC": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "The Guardian": "https://www.theguardian.com/world/rss",
        "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    }},
    {"key": "tek", "feeds": {
        "The Verge": "https://www.theverge.com/rss/index.xml",
        "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
        "TechCrunch": "https://techcrunch.com/feed/",
        "Hacker News": "https://hnrss.org/frontpage?points=150",
    }},
    {"key": "erhverv", "feeds": {
        "CNBC": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "CNBC Investing": "https://www.cnbc.com/id/15839069/device/rss/rss.html",
        "CNBC Economy": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
        "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
        "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
        "The Guardian Business": "https://www.theguardian.com/business/rss",
    }},
    {"key": "danmark", "feeds": {
        "DR": "https://www.dr.dk/nyheder/service/feeds/allenyheder",
    }},
]

# Sektionerne i Slack, i denne rækkefølge
SECTION_ORDER = ["vigtigste", "verden", "tek", "erhverv", "muligheder", "danmark"]
TITLES = {
    "vigtigste": "🔥 DAGENS VIGTIGSTE",
    "verden": "🌍 VERDEN & POLITIK",
    "tek": "💻 TEK & AI",
    "erhverv": "📈 ERHVERV & MARKEDER",
    "muligheder": "💰 MULIGHEDER & HOLD ØJE MED",
    "danmark": "🇩🇰 DANMARK",
}

# Markedslinjen øverst (Yahoo Finance-symboler)
MARKETS = [
    ("S&P 500", "^GSPC", 0, "idx"),
    ("Nasdaq", "^IXIC", 0, "idx"),
    ("STOXX 600", "^STOXX", 1, "idx"),
    ("C25", "^OMXC25", 0, "idx"),
    ("EUR/USD", "EURUSD=X", 4, "idx"),
    ("Brent", "BZ=F", 1, "usd"),
    ("Guld", "GC=F", 0, "usd"),
    ("US 10-årig", "^TNX", 2, "rate"),
]

WEEKDAYS = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
MONTHS = ["januar", "februar", "marts", "april", "maj", "juni", "juli",
          "august", "september", "oktober", "november", "december"]

SYSTEM_PROMPT = f"""Du er redaktør på en personlig, pengefokuseret morgenbrief på dansk til én læser.
Læserens profil: {PROFIL}
Formålet er, at læseren (1) kender dagens store nyheder og (2) forstår, hvad de betyder
for hans investeringer og karriere.

Du får artikler fra de seneste ca. 24 timer i emnegrupperne verden, tek, erhverv og
danmark, samt de seneste markedstal. Lav seks sektioner i denne rækkefølge:

1. vigtigste – De 2–3 vigtigste historier på tværs af alt. Ca. 100 ord. Ingen pengevinkel.
2. verden – Resumé på ca. 250 ord + pengevinkel på ca. 50 ord.
3. tek – Resumé på ca. 250 ord + pengevinkel på ca. 50 ord.
4. erhverv – Resumé på ca. 250 ord + pengevinkel på ca. 50 ord.
5. muligheder – Ca. 200 ord: 2–3 konkrete tråde, der er værd at undersøge nærmere for
   en investor i aktier/ETF'er eller for læserens karriere, samt kommende begivenheder
   nævnt i artiklerne (rentemøder, regnskaber, afstemninger, deadlines).
6. danmark – Præcis én sætning om dagens vigtigste danske nyhed.
I alt ca. 1200 ord.

Resuméerne: ét samlet resumé på tværs af flere artikler og helst flere kilder, ikke én
historie pr. kilde. Hovedtemaet i dybden plus sekundære udviklinger. Nævn hvor kilderne
er uenige eller vægter forskelligt.

Pengevinklen: hvilke sektorer, typer af virksomheder, råvarer, valutaer eller renter der
får medvind eller modvind, og hvorfor. Og for karrieren: hvilke kompetencer, brancher og
roller der er i vækst eller under pres (ansættelser, fyringsrunder, løn). Henvis til
markedstallene, hvor de viser, hvordan markedet reagerede.

Regler:
- Giv aldrig købs- eller salgsanbefalinger, kursmål eller porteføljeråd. Beskriv signaler,
  og hvad der er værd at undersøge nærmere.
- Skeln tydeligt mellem hvad kilderne siger, og din egen vurdering ("det kan betyde ...").
- Brug kun information fra de vedlagte artikler og markedstal. Opfind intet, heller ikke tal.
- Skriv naturligt, flydende dansk, også når kilderne er engelske. Behold navne,
  virksomheder og fagudtryk i deres normale form.
- Neutral, saglig tone. Ingen hilsen eller afslutning.
- Gentag ikke gårsdagens historier, medmindre der er sket noget nyt.
- Har en sektion kun lidt nyt, så gør den kortere i stedet for at fylde op.
- Ingen overskrifter med #; brug *fed* sparsomt. Afsnit adskilles med en tom linje.
- Kilder: 2–4 artikler du faktisk har brugt, én pr. linje i formatet: Navn | url

Aflevér briefen via værktøjet lever_brief ved at udfylde felterne for hver sektion
(fx verden_overskrift, verden_resume, verden_pengevinkel, verden_kilder)."""

# Claude afleverer briefen som simple tekstfelter pr. sektion. Flade tekstfelter er
# langt mere robuste end lister i lister, som tidligere gav formatfejl.
MONEY_SECTIONS = {"verden", "tek", "erhverv"}


def _brief_tool():
    props, required = {}, []
    for key in SECTION_ORDER:
        props[f"{key}_overskrift"] = {"type": "string"}
        props[f"{key}_resume"] = {"type": "string"}
        required += [f"{key}_overskrift", f"{key}_resume"]
        if key in MONEY_SECTIONS:
            props[f"{key}_pengevinkel"] = {"type": "string"}
        if key != "danmark":
            props[f"{key}_kilder"] = {"type": "string",
                                     "description": "Én kilde pr. linje: Navn | url"}
    return {"name": "lever_brief", "description": "Aflevér den færdige morgenbrief.",
            "input_schema": {"type": "object", "properties": props, "required": required}}


BRIEF_TOOL = _brief_tool()


# --- Hjælpefunktioner ------------------------------------------------------
def log(msg):
    print(msg, flush=True)


def danish_date(d):
    return f"{WEEKDAYS[d.weekday()]} {d.day}. {MONTHS[d.month - 1]}"


def dk_num(x, dec):
    s = f"{x:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


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


def save_state(now, text, mark_posted=True):
    """Gemmer briefens tekst. Kun planlagte kørsler markerer dagen som postet,
    så en manuel testkørsel ikke blokerer morgenens brief."""
    state = load_state()
    state["text"] = text
    if mark_posted:
        state["date"] = now.date().isoformat()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --- Markedstal ------------------------------------------------------------
def fetch_markets():
    """Returnerer én linje med markedstal. Fejler et tal, springes det over."""
    try:
        import yfinance as yf
    except Exception:
        log("  ⚠️  yfinance ikke installeret – springer markedstal over")
        return ""
    parts = []
    for name, symbol, dec, kind in MARKETS:
        try:
            closes = yf.Ticker(symbol).history(period="7d")["Close"].dropna()
            if len(closes) < 2:
                raise ValueError("for få datapunkter")
            last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
            if kind == "rate":
                bp = round((last - prev) * 100)
                parts.append(f"{name} {dk_num(last, dec)} % ({'+' if bp >= 0 else '−'}{abs(bp)} bp)")
            else:
                pct = (last / prev - 1) * 100
                sign = "+" if pct >= 0 else "−"
                value = dk_num(last, dec) + (" $" if kind == "usd" else "")
                parts.append(f"{name} {value} ({sign}{dk_num(abs(pct), 1)} %)")
        except Exception as e:
            log(f"  ⚠️  {name}: kunne ikke hente kurs ({e})")
    return " · ".join(parts)


# --- Nyheder ---------------------------------------------------------------
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
    for group in FEED_GROUPS:
        log(group["key"].upper())
        lists = [fetch_feed(n, u, cutoff) for n, u in group["feeds"].items()]
        merged, seen = [], set()
        for i in range(PER_FEED):
            for lst in lists:
                if i < len(lst) and lst[i]["url"] not in seen:
                    seen.add(lst[i]["url"])
                    merged.append(lst[i])
        merged = merged[:PER_SECTION]
        for a in merged:
            a["text"] = fetch_text(a["url"], a["summary"])
        result[group["key"]] = merged
    return result


# --- Claude ----------------------------------------------------------------
def generate(articles, markets, previous, now):
    parts = [f"I dag er {danish_date(now)} {now.year}."]
    parts.append(f"MARKEDSTAL (seneste lukning/kurs og ændring):\n{markets or 'Ikke tilgængelige i dag.'}")
    if previous:
        parts.append(f"GÅRSDAGENS BRIEF (gentag ikke uden nyt):\n{previous}")
    parts.append("ARTIKLER:")
    for group in FEED_GROUPS:
        parts.append(f"\n=== EMNEGRUPPE: {group['key']} ===")
        for n, a in enumerate(articles.get(group["key"], []), 1):
            parts.append(f"[{n}] {a['source']} | {a['title']} | {a['url']} | "
                         f"{a['time']:%Y-%m-%d %H:%M} UTC\n{a['text']}")

    client = Anthropic()
    for attempt in (1, 2):  # Ét genforsøg, kun hvis svaret ikke kan bruges
        resp = client.messages.create(
            model=MODEL,
            max_tokens=12000,
            system=SYSTEM_PROMPT,
            tools=[BRIEF_TOOL],
            tool_choice={"type": "tool", "name": "lever_brief"},
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
        )
        data = next((b.input for b in resp.content if b.type == "tool_use"), None)
        try:
            return normalize(data)
        except Exception as e:
            fields = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            log(f"  ⚠️  Forsøg {attempt}: {e} (stop_reason={resp.stop_reason}, felter={fields})")
    raise RuntimeError("Claude leverede ikke en brugbar brief efter to forsøg.")


def parse_kilder(v):
    """Læser kilder, uanset om de kommer som linjer 'Navn | url', JSON eller en liste."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            v = v.splitlines()
    if not isinstance(v, list):
        return []
    out = []
    for k in v:
        navn, url = "", ""
        if isinstance(k, dict):
            navn, url = k.get("navn", ""), k.get("url", "")
        elif isinstance(k, str):
            m = re.search(r"https?://\S+", k)
            if m:
                url = m.group(0).rstrip(").,>")
                navn = k[:m.start()].strip(" -–|:•*")
        if url:
            navn = navn or urlparse(url).netloc.replace("www.", "")
            out.append({"navn": navn, "url": url})
    return out


def normalize(data):
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise RuntimeError("svaret var ikke et objekt")
    sections = []
    for key in SECTION_ORDER:
        resume = (data.get(f"{key}_resume") or "").strip()
        if not resume:
            continue
        sections.append({"key": key,
                         "overskrift": (data.get(f"{key}_overskrift") or "").strip(),
                         "resume": resume,
                         "pengevinkel": (data.get(f"{key}_pengevinkel") or "").strip(),
                         "kilder": parse_kilder(data.get(f"{key}_kilder"))})
    if len(sections) < 3:
        raise RuntimeError("svaret indeholdt for få brugbare sektioner")
    return {"sektioner": sections}


# --- Slack -----------------------------------------------------------------
def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text_block(text):
    # Slack tillader max 3000 tegn pr. tekstblok
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:2990]}}


def build_blocks(brief, markets, now):
    header = f"📰 Morgenbrief – {danish_date(now)}"
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}}]
    if markets:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": f"📊 {esc(markets)}"}]})

    for sec in brief["sektioner"]:
        title = esc(TITLES.get(sec["key"], sec["key"]))
        blocks.append({"type": "divider"})
        if sec["key"] == "danmark":
            blocks.append(text_block(f"*{title}:* {esc(sec['resume'])}"))
            continue
        blocks.append(text_block(f"*{title}*\n*{esc(sec['overskrift'])}*\n{esc(sec['resume'])}"))
        extra = ""
        if sec["pengevinkel"]:
            extra += f"💸 *Pengevinkel:* {esc(sec['pengevinkel'])}"
        kilder = " · ".join(f"<{k['url']}|{esc(k['navn'])}>" for k in sec["kilder"])
        if kilder:
            extra += f"\n_Kilder: {kilder}_"
        if extra.strip():
            blocks.append(text_block(extra.strip()))
    return header, blocks


def to_plain(brief):
    return "\n\n".join(f"{s['overskrift']}\n{s['resume']}\n{s['pengevinkel']}".strip()
                       for s in brief["sektioner"])


# --- Main ------------------------------------------------------------------
def main():
    force = os.getenv("FORCE", "").lower() == "true"
    dry_run = os.getenv("DRY_RUN", "").lower() == "true"
    now = datetime.now(TZ)
    state = load_state()

    if not force:
        if state.get("date") == now.date().isoformat():
            log("Morgenbriefen er allerede postet i dag – springer over.")
            return
        start = now.replace(hour=WINDOW[0][0], minute=WINDOW[0][1], second=0, microsecond=0)
        end = now.replace(hour=WINDOW[1][0], minute=WINDOW[1][1], second=0, microsecond=0)
        if not (start <= now < end):
            log(f"Klokken er {now:%H:%M} i København – uden for tidsvinduet, springer over.")
            return

    # Kørslen starter tidligt (især om vinteren). Vent, så nyhederne hentes lige før postetidspunktet
    if not force:
        prep = now.replace(hour=POST_HOUR, minute=0, second=0, microsecond=0) - timedelta(minutes=PREP_MINUTES)
        early = (prep - datetime.now(TZ)).total_seconds()
        if early > 0:
            log(f"Venter {int(early // 60)} min. før nyhederne hentes.")
            time.sleep(early)

    log("MARKEDSTAL")
    markets = fetch_markets()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    articles = collect(cutoff)
    if not any(articles.values()):
        sys.exit("Ingen artikler fundet – tjek feed-adresserne.")

    brief = generate(articles, markets, state.get("text", ""), now)
    header, blocks = build_blocks(brief, markets, now)

    if dry_run:
        print(json.dumps(blocks, ensure_ascii=False, indent=2))
        return

    # Er briefen klar før postetidspunktet, venter vi til præcis det klokkeslæt
    if not force:
        target = now.replace(hour=POST_HOUR, minute=0, second=0, microsecond=0)
        wait = (target - datetime.now(TZ)).total_seconds()
        if wait > 0:
            log(f"Briefen er klar – venter {int(wait // 60)} min. til kl. {POST_HOUR:02d}:00.")
            time.sleep(wait)

    webhook = os.environ["SLACK_WEBHOOK_URL"]
    r = requests.post(webhook, json={"text": header, "blocks": blocks}, timeout=20)
    r.raise_for_status()
    save_state(now, to_plain(brief), mark_posted=not force)
    log("✅ Morgenbrief postet i Slack.")


if __name__ == "__main__":
    main()
