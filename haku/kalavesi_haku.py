"""
Kalavesi: lupatietojen haku ja poiminta.

Ketju jokaiselle kohteelle:
  1. HAKU      Claude etsii verkosta lähteet (lupamyyjät, kalatalousalueen sivut).
  2. NOUTO     Skripti hakee sivut itse, jotta jokaisen tiedon voi tarkistaa.
  3. POIMINTA  Claude täyttää rakenteiset kentät. Jokaiseen kenttään tulee
               suora lainaus lähdesivulta.
  4. TARKISTUS Lainauksen pitää löytyä sivun tekstistä. Jos ei löydy, kenttä
               hylätään. Ristiriidat ja epävarmat kohteet menevät tarkistuslistalle.
  5. TALLENNUS sites.json (sivun käyttämä muoto) ja tarkistettavat.csv.

Käyttö:
  pip install -r requirements.txt
  export ANTHROPIC_API_KEY=...
  python kalavesi_haku.py               # lukee kohteet.csv, kirjoittaa ../data/sites.json
  python kalavesi_haku.py --selftest      # tarkistuslogiikan testi ilman verkkoa
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from urllib import robotparser

# ---------------------------------------------------------------------------
# Asetukset
# ---------------------------------------------------------------------------

MODEL = "claude-sonnet-5-5"           # halvempi malli riittää poimintaan
SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 6,
               "user_location": {"type": "approximate", "country": "FI", "timezone": "Europe/Helsinki"}}
USER_AGENT = "KalavesiBot/0.1 (kalastuslupatietojen kokoaja; yhteys: vaihda-oma-osoite@example.com)"
MAX_SOURCES = 4                        # montako lähdesivua per kohde
MAX_PAGE_CHARS = 30_000                # kuinka paljon sivun tekstiä annetaan mallille
REQUEST_DELAY_S = 2.0                  # kohteliaisuustauko saman palvelimen pyyntöjen välillä
HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "cache"


# ---------------------------------------------------------------------------
# Tietorakenteet
# ---------------------------------------------------------------------------

@dataclass
class Site:
    id: str
    name: str
    municipality: str
    lat: float
    lon: float
    water_system: str = ""
    rapids: bool = False               # myöhemmin automaattisesti rajoitusaineistosta
    aliases: list[str] = field(default_factory=list)


@dataclass
class Page:
    url: str
    title: str
    text: str
    content_hash: str
    fetched: str


# ---------------------------------------------------------------------------
# 1. Haku
# ---------------------------------------------------------------------------

SEARCH_PROMPT = """Etsi verkosta lähteitä, jotka kertovat kalastusluvista ja kalastussäännöistä tässä kohteessa:

Kohde: {name}
Kunta: {municipality}
Vesistö: {water_system}

Etsi erityisesti:
- lupamyyjät (esim. Kalakortti.com, Eräluvat.fi, kalatalousalueen tai osakaskunnan oma sivu)
- paikalliset rauhoitukset, kiellot ja kalastussäännöt
- kalatalousalueen käyttö- ja hoitosuunnitelma, jos se koskee kohdetta

Älä käytä keskustelupalstoja tai uutisia, jos virallisempi lähde löytyy.
Vastaa lopuksi VAIN JSON-listana, enintään {max_sources} kohdetta:
[{{"url": "...", "title": "...", "why": "lyhyt perustelu"}}]"""


def find_sources(client, site: Site) -> list[dict]:
    """Pyydä Claudea etsimään lähde-URLit verkkohaulla."""
    msg = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        tools=[SEARCH_TOOL],
        messages=[{"role": "user", "content": SEARCH_PROMPT.format(
            name=site.name, municipality=site.municipality,
            water_system=site.water_system or "ei tiedossa", max_sources=MAX_SOURCES)}],
    )
    # Viimeinen tekstilohko sisältää JSON-listan
    text = "".join(b.text for b in msg.content if b.type == "text")
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    seen, out = set(), []
    for it in items:
        url = str(it.get("url", "")).strip()
        if url.startswith("http") and url not in seen:
            seen.add(url)
            out.append(it)
    return out[:MAX_SOURCES]


# ---------------------------------------------------------------------------
# 2. Nouto
# ---------------------------------------------------------------------------

_robots: dict[str, robotparser.RobotFileParser] = {}
_last_hit: dict[str, float] = {}


def allowed_by_robots(url: str) -> bool:
    host = urlparse(url)
    base = f"{host.scheme}://{host.netloc}"
    if base not in _robots:
        rp = robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = None  # robots.txt puuttuu tai ei vastaa: sallitaan
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def fetch_page(url: str) -> Page | None:
    """Hae sivu (välimuistista, jos haettu tänään) ja muuta tekstiksi."""
    import requests
    from bs4 import BeautifulSoup

    if not allowed_by_robots(url):
        print(f"    ohitetaan (robots.txt kieltää): {url}")
        return None

    today = dt.date.today().isoformat()
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached["fetched"] == today:
            return Page(**cached)

    host = urlparse(url).netloc
    wait = REQUEST_DELAY_S - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()

    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"    nouto epäonnistui: {url} ({e})")
        return None

    if "pdf" in r.headers.get("Content-Type", ""):
        print(f"    PDF ohitetaan toistaiseksi: {url}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title else url
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()[:MAX_PAGE_CHARS]
    page = Page(url=url, title=title, text=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest()[:16], fetched=today)
    cache_file.write_text(json.dumps(page.__dict__, ensure_ascii=False), encoding="utf-8")
    return page


# ---------------------------------------------------------------------------
# 3. Poiminta
# ---------------------------------------------------------------------------

EVIDENCE = {"type": "object", "properties": {
    "source": {"type": "integer", "description": "Lähdesivun numero (0, 1, ...)"},
    "quote": {"type": "string", "description": "Sanatarkka lainaus lähdesivulta, enintään 300 merkkiä"}},
    "required": ["source", "quote"]}

EXTRACT_TOOL = {
    "name": "tallenna_kohde",
    "description": "Tallenna kohteen lupatiedot. Täytä vain kentät, joille löytyy lainaus lähteestä.",
    "input_schema": {
        "type": "object",
        "properties": {
            "permits": {"type": "array", "items": {"type": "object", "properties": {
                "product": {"type": "string", "description": "Luvan nimi"},
                "seller": {"type": "string", "description": "Myyjä tai myyntikanava"},
                "url": {"type": "string", "description": "Ostosivun osoite, jos tiedossa"},
                "price": {"type": ["string", "null"], "description": "Hinta lähteen mukaan, esim. '15 € / vrk'. null jos ei mainita"},
                "methods": {"type": "array", "items": {"type": "string"}, "description": "Sallitut kalastustavat"},
                "evidence": EVIDENCE}, "required": ["product", "seller", "evidence"]}},
            "closures": {"type": "array", "items": {"type": "object", "properties": {
                "text": {"type": "string", "description": "Rauhoitus tai kielto omin sanoin, suomeksi"},
                "evidence": EVIDENCE}, "required": ["text", "evidence"]}},
            "nets_banned": {"type": "object", "properties": {
                "value": {"type": "boolean"}, "evidence": EVIDENCE}, "required": ["value", "evidence"]},
            "notes_for_reviewer": {"type": "string", "description": "Ristiriidat tai epäselvyydet lähteiden välillä"},
        },
        "required": ["permits", "closures"],
    },
}

EXTRACT_PROMPT = """Poimi alla olevista lähdesivuista kalastuslupa- ja sääntötiedot kohteelle "{name}" ({municipality}).

Säännöt:
- Käytä vain lähdesivujen tietoja. Älä arvaa äläkä käytä omaa tietämystäsi.
- Jokaiseen tietoon on liitettävä sanatarkka lainaus lähdesivulta ja lähteen numero.
- Jos tieto koskee eri kohdetta tai koko vesistöä, mainitse se tekstissä.
- Jos lähteet ovat ristiriidassa, tallenna molemmat ja kerro ristiriidasta kentässä notes_for_reviewer.
- Jos jotain tietoa ei löydy, jätä se pois.

{pages}"""


def extract(client, site: Site, pages: list[Page]) -> dict:
    blocks = "\n\n".join(
        f"=== LÄHDE {i}: {p.title} ({p.url}) ===\n{p.text}" for i, p in enumerate(pages))
    msg = client.messages.create(
        model=MODEL,
        max_tokens=3000,
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "tallenna_kohde"},
        messages=[{"role": "user", "content": EXTRACT_PROMPT.format(
            name=site.name, municipality=site.municipality, pages=blocks)}],
    )
    for b in msg.content:
        if b.type == "tool_use":
            return b.input
    return {"permits": [], "closures": []}


# ---------------------------------------------------------------------------
# 4. Tarkistus
# ---------------------------------------------------------------------------

def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ")
    s = re.sub(r"[\"'“”‘’«»]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def quote_found(evidence: dict | None, pages: list[Page]) -> bool:
    """Lainauksen pitää löytyä lähdesivulta lähes sanatarkasti."""
    if not evidence:
        return False
    i, quote = evidence.get("source"), evidence.get("quote", "")
    if not isinstance(i, int) or not (0 <= i < len(pages)) or len(quote.strip()) < 8:
        return False
    return normalize(quote) in normalize(pages[i].text)


def validate(raw: dict, pages: list[Page]) -> tuple[dict, list[str]]:
    """Pidä vain tiedot, joiden lainaus löytyy. Palauttaa (hyväksytyt, huomiot)."""
    issues: list[str] = []
    ok: dict = {"permits": [], "closures": [], "nets_banned": None}

    for p in raw.get("permits", []):
        if quote_found(p.get("evidence"), pages):
            ok["permits"].append({k: p.get(k) for k in ("product", "seller", "url", "price", "methods")}
                                 | {"source": p["evidence"]["source"]})
        else:
            issues.append(f"Lupa hylätty, lainausta ei löytynyt: {p.get('product')}")

    for c in raw.get("closures", []):
        if quote_found(c.get("evidence"), pages):
            ok["closures"].append({"text": c["text"], "source": c["evidence"]["source"]})
        else:
            issues.append(f"Rauhoitus hylätty, lainausta ei löytynyt: {c.get('text')}")

    nb = raw.get("nets_banned")
    if nb and quote_found(nb.get("evidence"), pages):
        ok["nets_banned"] = bool(nb["value"])

    if raw.get("notes_for_reviewer"):
        issues.append("Mallin huomio: " + raw["notes_for_reviewer"])
    return ok, issues


def review_status(ok: dict, issues: list[str], n_pages: int) -> str:
    if n_pages == 0:
        return "ei_lahteita"
    if issues:
        return "tarkistettava"
    if not ok["permits"] and not ok["closures"]:
        return "ei_tietoja"
    return "tekoaly_tarkistamaton"   # ihminen hyväksyy ennen kuin merkitään "tarkistettu"


# ---------------------------------------------------------------------------
# 5. Ajo ja tallennus
# ---------------------------------------------------------------------------

def read_sites(path: Path) -> list[Site]:
    out = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append(Site(
                id=row["id"], name=row["name"], municipality=row["municipality"],
                lat=float(row["lat"]), lon=float(row["lon"]),
                water_system=row.get("water_system", ""),
                rapids=row.get("rapids", "").strip().lower() in ("1", "true", "kyllä", "x"),
                aliases=[a.strip() for a in row.get("aliases", "").split(";") if a.strip()]))
    return out


def load_previous(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {s["id"]: s for s in data.get("sites", [])}


def process_site(client, site: Site, previous: dict | None) -> tuple[dict, list[str]]:
    print(f"\n▸ {site.name}")
    candidates = find_sources(client, site)
    print(f"  lähde-ehdokkaita: {len(candidates)}")
    pages = [p for c in candidates if (p := fetch_page(c["url"]))]
    print(f"  noudettuja sivuja: {len(pages)}")

    ok, issues = ({"permits": [], "closures": [], "nets_banned": None}, [])
    if pages:
        ok, issues = validate(extract(client, site, pages), pages)

    # Muutosvahti: jos lähdesivu on muuttunut edellisestä ajosta, nostetaan esiin
    if previous:
        old_hashes = {s["url"]: s.get("content_hash") for s in previous.get("sources", [])}
        for p in pages:
            if p.url in old_hashes and old_hashes[p.url] != p.content_hash:
                issues.append(f"Lähdesivu muuttunut edellisestä ajosta: {p.url}")

    record = {
        "id": site.id, "name": site.name, "aliases": site.aliases,
        "kind": "Koskialue" if site.rapids else (site.water_system or "Vesistö"),
        "municipality": site.municipality, "lat": site.lat, "lon": site.lon,
        "rapids": site.rapids,
        "nets_banned": bool(ok["nets_banned"]),
        "permits": ok["permits"], "closures": ok["closures"],
        "sources": [{"title": p.title, "url": p.url, "content_hash": p.content_hash, "fetched": p.fetched}
                    for p in pages],
        "status": review_status(ok, issues, len(pages)),
    }
    print(f"  luvat: {len(ok['permits'])}, rauhoitukset: {len(ok['closures'])}, tila: {record['status']}")
    return record, issues


def main():
    ap = argparse.ArgumentParser(description="Kalavesi: lupatietojen haku ja poiminta")
    ap.add_argument("--input", default=str(HERE / "kohteet.csv"))
    ap.add_argument("--output", default=str(HERE.parent / "data" / "sites.json"))
    ap.add_argument("--review", default=str(HERE / "tarkistettavat.csv"))
    ap.add_argument("--only", help="aja vain tämä kohde (id)")
    ap.add_argument("--selftest", action="store_true", help="testaa tarkistuslogiikka ilman verkkoa")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    import anthropic
    client = anthropic.Anthropic()

    sites = read_sites(Path(args.input))
    if args.only:
        sites = [s for s in sites if s.id == args.only]
    previous = load_previous(Path(args.output))

    records, review_rows = [], []
    for site in sites:
        try:
            rec, issues = process_site(client, site, previous.get(site.id))
        except Exception as e:  # yksi epäonnistunut kohde ei kaada koko ajoa
            print(f"  VIRHE: {e}")
            rec, issues = previous.get(site.id), [f"Ajo epäonnistui: {e}"]
            if rec is None:
                continue
        records.append(rec)
        for msg in issues:
            review_rows.append({"id": site.id, "kohde": site.name, "huomio": msg})

    # säilytä aiemmat kohteet, joita ei ajettu tällä kertaa
    done = {r["id"] for r in records}
    records += [s for sid, s in previous.items() if sid not in done]

    out = {"checked": dt.date.today().isoformat(), "sites": records}
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(args.review, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "kohde", "huomio"])
        w.writeheader()
        w.writerows(review_rows)
    print(f"\nValmis: {args.output} ({len(records)} kohdetta), tarkistettavia huomioita {len(review_rows)}")


# ---------------------------------------------------------------------------
# Testi ilman verkkoa
# ---------------------------------------------------------------------------

def selftest():
    page = Page(url="https://example.fi", title="Kalastus Aurajoella",
                text="Verkkokalastus on kielletty koko joessa. Lohikalojen rauhoitusaika on 1.9.–30.11. "
                     "Luvan saa Halistenkoskelle ja Nautelankoskelle.",
                content_hash="x", fetched="2026-10-19")
    raw = {
        "permits": [
            {"product": "Halistenkoski", "seller": "Kalakortti.com",
             "evidence": {"source": 0, "quote": "Luvan saa Halistenkoskelle"}},
            {"product": "Keksitty lupa", "seller": "?", "price": "10 €",
             "evidence": {"source": 0, "quote": "Päivälupa maksaa 10 euroa"}},
        ],
        "closures": [{"text": "Lohikalat rauhoitettu 1.9.–30.11.",
                      "evidence": {"source": 0, "quote": "rauhoitusaika on 1.9.-30.11."}}],
        "nets_banned": {"value": True, "evidence": {"source": 0, "quote": "Verkkokalastus on kielletty koko joessa"}},
    }
    ok, issues = validate(raw, [page])
    assert len(ok["permits"]) == 1, "keksityn luvan pitäisi hylkääntyä"
    assert ok["permits"][0]["product"] == "Halistenkoski"
    assert len(ok["closures"]) == 1, "ajatusviivan ja tavuviivan ero ei saa kaataa lainausta"
    assert ok["nets_banned"] is True
    assert any("Keksitty lupa" in i for i in issues)
    assert review_status(ok, issues, 1) == "tarkistettava"
    assert not quote_found({"source": 5, "quote": "Verkkokalastus on kielletty"}, [page])
    print("selftest ok: keksitty tieto hylätään, oikeat lainaukset hyväksytään")


if __name__ == "__main__":
    sys.exit(main())
