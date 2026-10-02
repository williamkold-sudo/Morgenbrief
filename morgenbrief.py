"""Daglig morgenbrief: henter nyheder via RSS, markedstal og fodboldkampe,
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
POST_HOUR = 7
WINDOW = ((6, 30), (9, 0))
PREP_MINUTES = 12
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
LOOKBACK_HOURS = 26
PER_FEED = 4
PER_SECTION = 12
ARTICLE_CHARS = 2000
STATE_FILE = Path("state/last_brief.json")
UA = {"User-Agent": "Mozilla/5.0 (morgenbrief; personal news digest)"}

PROFIL = (
    "Investerer i aktier og ETF'er og vil styrke sin karriere og løn og studerer "
    "på nuværende tidspunkt ved CBS international business and politics og er "
    "interesseret i en karriere indenfor makroinvestering eller venturekapital."
)

# Feeds er råstof. Claude bestemmer sektionerne, ikke feed-navnet.
FEED_GROUPS = [
    {"key": "verden", "feeds": {
        "BBC": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "The Guardian": "https://www.theguardian.com/world/rss",
        "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
        "Politico Europe": "https://www.politico.eu/feed/",
    }},
    {"key": "tek", "feeds": {
        "The Verge": "https://www.theverge.com/rss/index.xml",
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
    {"key": "fodbold", "feeds": {
        "BBC Sport": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    }},
]

# Rækkefølge i Slack. Tomme sektioner springes over.
SECTION_ORDER = [
    "historie1", "historie2", "historie3",
    "politik", "karriere", "kalender", "fodbold", "danmark",
]
TITLES = {
    "historie1": "📈 MARKEDER",
    "historie2": "📈 MARKEDER",
    "historie3": "📈 MARKEDER",
    "politik": "🌐 INTERNATIONAL POLITIK",
    "karriere": "💼 KARRIERE",
    "kalender": "🗓 NÆSTE 48 TIMER",
    "fodbold": "⚽ FODBOLD",
    "danmark": "🇩🇰 DANMARK",
}
# Kun første markedshistorie får sektions-overskriften i Slack.
SKIP_TITLE = {"historie2", "historie3"}

# Aktiestrippen og krydsaktiv-linjen hentes hver for sig.
EQUITIES = [
    ("S&P 500", "^GSPC", 0, "idx"),
    ("Nasdaq", "^IXIC", 0, "idx"),
    ("STOXX 600", "^STOXX", 1, "idx"),
    ("C25", "^OMXC25", 0, "idx"),
    ("Brent", "BZ=F", 1, "usd"),
    ("Guld", "GC=F", 0, "usd"),
]
CROSS_ASSET = [
    ("US 2-årig", "2YY=F", 2, "rate"),
    ("US 10-årig", "^TNX", 2, "rate"),
    ("EUR/USD", "EURUSD=X", 4, "idx"),
    ("HYG", "HYG", 2, "idx"),  # kredit-proxy, ikke et spread
]

FIXTURE_URLS = [
    "https://www.bbc.co.uk/sport/football/scores-fixtures",
    "https://www.bbc.com/sport/football/premier-league/scores-fixtures",
    "https://www.bbc.com/sport/football/champions-league/scores-fixtures",
]

WEEKDAYS = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
MONTHS = ["januar", "februar", "marts", "april", "maj", "juni", "juli",
          "august", "september", "oktober", "november", "december"]

SYSTEM_PROMPT = f"""Du er redaktør på en personlig morgenbrief på dansk til én læser.
Læserens profil: {PROFIL}
Formålet er, at læseren på omkring 700 ord kender dagens bevægelser i kapital, statslig magt og sin karriereretning.

Du får artikler fra de seneste ca. 24 timer, markedstal og et fodboldudtræk. Skriv kun ud fra det. Opfind ingen tal, kampe eller begivenheder.

Sektioner, i denne rækkefølge:

1–3. historie1, historie2, historie3 – op til tre markedshistorier. Drop en historie ved at lade felterne være tomme, hvis der ikke er stof nok. Tekn og AI kun med, hvis det flytter kurser, funding eller ansættelser. Danmark kun med her, hvis det rører C25, finanspolitik eller arbejdsmarkedet.
4. politik – præcis én historie om international politik, skrevet realistisk: stater, kapabiliteter, sikkerhed og relativ fordel. Institutioner er arenaer, ikke aktører. Ingen moral, ingen "regelbaseret orden" som forklaring. Gentag ikke prisbevægelsen fra markedshistorierne; forklar hvem der pressede hvem. Tom, hvis intet har flyttet magtbalancen.
5. karriere – kort hale, én gren, om kapital eller ansættelser i makro, venture eller growth. Tom, hvis intet konkret er meldt.
6. kalender – flad liste, næste 48 timer og den næste kendte dato. For hvert nøgletal: forrige tal og konsensus, hvis kilderne har dem.
7. fodbold – højst fire korte linjer. Kun store kampe i Premier League, Champions League eller landshold, i dag plus næste spilledag inden for tre dage. Modstander, tidspunkt, hvorfor den er den værd at kende. Ingen odds, ingen forudsagt stilling. Tom, hvis udtrækket ikke har en stor kamp.
8. danmark – to eller tre sætninger, kun hvis dagens stof rører C25, finanspolitik eller arbejdsmarkedet. Ellers tom.

Format for historie1–3, politik og karriere. Overskriften er påstanden, ikke emnet. Brødteksten er denne kontrakt, én sætning pr. knude, højst to grene:

1. Fakta. Tal mod konsensus, hvis kilderne har begge. Mærk kilden: statistikbureau, centralbank, regnskab, eller "kilder siger" ved et læk. Nævn hvis kilderne er uenige.
    1.1 Mekanisme: hvorfor faktum flytter renter, indtjening, flows eller kapabilitet.
        1.1.1 So-what: hvilken sektor, faktor, valuta eller stat der får medvind eller modvind. Et signal, aldrig et køb eller salg.

Kalender og fodbold er flade nummererede linjer, uden 1.1.

Omfang: hele briefen ca. 700 ord eksklusiv fodbold. Politik ca. 150 ord. Karriere ca. 70 ord. Kalender ca. 50 ord. Fyld ikke op. Tom sektion er bedre end en tynd.

Regler:
- Aldrig købs- eller salgsanbefaling, kursmål eller porteføljeråd.
- Skeln mellem hvad kilderne siger, og din læsning ("det kan betyde ...").
- Naturligt dansk. Behold navne og fagudtryk.
- Saglig tone. Ingen hilsen eller afslutning.
- Gentag ikke gårsdagens historier, medmindre der er noget nyt.
- Kilder: 2–4 artikler du faktisk har brugt, én pr. linje: Navn | url

Aflevér via lever_brief. Lad felterne være tomme strenge for sektioner du dropper."""

STORY_KEYS = {"historie1", "historie2", "historie3", "politik", "karriere"}


def _brief_tool():
    props, required = {}, ["historie1_overskrift", "historie1_resume"]
    for key in SECTION_ORDER:
        props[f"{key}_overskrift"] = {"type": "string"}
        props[f"{key}_resume"] = {"type": "string"}
        if key in STORY_KEYS:
            props[f"{key}_kilder"] = {
                "type": "string",
                "description": "Én kilde pr. linje: Navn | url. Tom, hvis sektionen droppes.",
            }
    props["kalender_kilder"] = {"type": "string"}
    return {
        "name": "lever_brief",
        "description": "Aflevér den færdige morgenbrief. Tomme strenge betyder, at sektionen droppes.",
        "input_schema": {"type": "object", "properties": props, "required": required},
    }


BRIEF_TOOL = _brief_tool()


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
    state = load_state()
    state["text"] = text
    if mark_posted:
        state["date"] = now.date().isoformat()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _quote_line(name, symbol, dec, kind):
    import yfinance as yf
    closes = yf.Ticker(symbol).history(period="7d")["Close"].dropna()
    if len(closes) < 2:
        raise ValueError("for få datapunkter")
    last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
    if kind == "rate":
        bp = round((last - prev) * 100)
        sign = "+" if bp >= 0 else "−"
        return f"{name} {dk_num(last, dec)} % ({sign}{abs(bp)} bp)"
    pct = (last / prev - 1) * 100
    sign = "+" if pct >= 0 else "−"
    value = dk_num(last, dec) + (" $" if kind == "usd" else "")
    suffix = " (kredit-proxy)" if name == "HYG" else ""
    return f"{name} {value} ({sign}{dk_num(abs(pct), 1)} %){suffix}"


def fetch_markets():
    """To linjer: aktier/råvarer og krydsaktiv. Et tal der fejler, springes over."""
    try:
        import yfinance  # noqa: F401
    except Exception:
        log("  ⚠️  yfinance ikke installeret – springer markedstal over")
        return "", ""
    lines = []
    for basket in (EQUITIES, CROSS_ASSET):
        parts = []
        for name, symbol, dec, kind in basket:
            try:
                parts.append(_quote_line(name, symbol, dec, kind))
            except Exception as e:
                log(f"  ⚠️  {name}: kunne ikke hente kurs ({e})")
        lines.append(" · ".join(parts))
    return lines[0], lines[1]


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
        items.append({
            "source": name,
            "title": strip_html(e.get("title", "")),
            "url": e.get("link", ""),
            "summary": strip_html(e.get("summary", "")),
            "time": t,
        })
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


def fetch_fixtures():
    """Programudtræk, så fodboldlinjerne ikke digtes. Fejler det, droppes sektionen."""
    chunks = []
    for url in FIXTURE_URLS:
        try:
            r = requests.get(url, headers=UA, timeout=15)
            r.raise_for_status()
            text = trafilatura.extract(r.text) or ""
            if text.strip():
                chunks.append(f"[{url}]\n{text.strip()[:2500]}")
        except Exception as e:
            log(f"  ⚠️  Fodbold: kunne ikke hente {url} ({e})")
    return "\n\n".join(chunks)


def generate(articles, markets, cross, fixtures, previous, now):
    parts = [f"I dag er {danish_date(now)} {now.year}."]
    parts.append(f"AKTIELINJE:\n{markets or 'Ikke tilgængelig.'}")
    parts.append(f"KRYDSAKTIV (2-årig, 10-årig, EUR/USD, HYG som kredit-proxy):\n{cross or 'Ikke tilgængelig.'}")
    if previous:
        parts.append(f"GÅRSDAGENS BRIEF (gentag ikke uden nyt):\n{previous}")
    parts.append("FODBOLDUDTRÆK (brug kun disse kampe, og kun de store):")
    parts.append(fixtures or "Ingen kampdata hentet. Lad fodbold være tom.")
    parts.append("ARTIKLER:")
    for group in FEED_GROUPS:
        parts.append(f"\n=== EMNEGRUPPE: {group['key']} ===")
        for n, a in enumerate(articles.get(group["key"], []), 1):
            parts.append(
                f"[{n}] {a['source']} | {a['title']} | {a['url']} | "
                f"{a['time']:%Y-%m-%d %H:%M} UTC\n{a['text']}"
            )

    client = Anthropic()
    for attempt in (1, 2):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
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
        sections.append({
            "key": key,
            "overskrift": (data.get(f"{key}_overskrift") or "").strip(),
            "resume": resume,
            "kilder": parse_kilder(data.get(f"{key}_kilder")),
        })
    if not any(s["key"].startswith("historie") for s in sections):
        raise RuntimeError("svaret manglede en markedshistorie")
    return {"sektioner": sections}


def esc(s):
    return (s or "").replace("&", "&").replace("<", "<").replace(">", ">")


def keep_indent(s):
    """Slack fjerner almindelige indryk. Em-space bevarer note-formatet."""
    lines = []
    for line in (s or "").splitlines():
        n = len(line) - len(line.lstrip(" "))
        lines.append(("\u2003" * (n // 4)) + line.lstrip(" "))
    return "\n".join(lines)


def text_block(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:2990]}}


def build_blocks(brief, markets, cross, now):
    header = f"📰 Morgenbrief – {danish_date(now)}"
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}}]
    if markets:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"📊 {esc(markets)}"}]})
    if cross:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"↕ {esc(cross)}"}]})

    shown_markets = False
    for sec in brief["sektioner"]:
        blocks.append({"type": "divider"})
        body = f"*{esc(sec['overskrift'])}*\n{esc(keep_indent(sec['resume']))}" if sec["overskrift"] else esc(keep_indent(sec["resume"]))
        if sec["key"] not in SKIP_TITLE and not (sec["key"].startswith("historie") and shown_markets):
            title = esc(TITLES.get(sec["key"], sec["key"]))
            body = f"*{title}*\n{body}"
        if sec["key"].startswith("historie"):
            shown_markets = True
        blocks.append(text_block(body))
        kilder = " · ".join(f"<{k['url']}|{esc(k['navn'])}>" for k in sec["kilder"])
        if kilder:
            blocks.append(text_block(f"_Kilder: {kilder}_"))
    return header, blocks


def to_plain(brief):
    return "\n\n".join(
        f"{s['overskrift']}\n{s['resume']}".strip() for s in brief["sektioner"]
    )


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
        prep = now.replace(hour=POST_HOUR, minute=0, second=0, microsecond=0) - timedelta(minutes=PREP_MINUTES)
        early = (prep - datetime.now(TZ)).total_seconds()
        if early > 0:
            log(f"Venter {int(early // 60)} min. før nyhederne hentes.")
            time.sleep(early)

    log("MARKEDSTAL")
    markets, cross = fetch_markets()
    log("FODBOLD")
    fixtures = fetch_fixtures()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    articles = collect(cutoff)
    if not any(articles.get(g["key"]) for g in FEED_GROUPS if g["key"] != "fodbold"):
        sys.exit("Ingen artikler fundet – tjek feed-adresserne.")

    brief = generate(articles, markets, cross, fixtures, state.get("text", ""), now)
    header, blocks = build_blocks(brief, markets, cross, now)

    if dry_run:
        print(json.dumps(blocks, ensure_ascii=False, indent=2))
        return

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
