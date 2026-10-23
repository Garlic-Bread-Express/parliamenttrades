#!/usr/bin/env python3
"""
Scrape MP disclosure summaries + notices of material change from the
Office of the Conflict of Interest and Ethics Commissioner public registry
and write a CSV the "MP Holdings Tracker" page can import.

  pip install requests beautifulsoup4
  python scrape_mp_holdings.py --max-pages 1 --save-raw raw.jsonl   # smoke test first
  python scrape_mp_holdings.py --enrich --out mp_holdings.csv        # full run (~40 pages)
  python scrape_mp_holdings.py --from-raw raw.jsonl --enrich         # re-parse without re-downloading

Notes
- Disclosure summaries list holdings (no amounts). Date = date disclosed, action = "Held".
- Notices of material change carry a "Date of change" and wording like
  "I have purchased shares of..." / "I no longer own...", so they are the closest thing
  to dated trades. action = Acquired / Disposed / Changed.
- By default only the latest summary per MP is kept, plus changes disclosed after it.
"""
import argparse, csv, json, re, sys, time, unicodedata
from datetime import datetime
from urllib.parse import urljoin

BASE = "https://www.ethicscanada.ca"
LIST_URL = BASE + "/en/public-registry"
ROLE_MP = "cac94a19-d04e-e111-b8ea-00265535a320"        # copied from your URL (role = Members of Parliament)
STATUS_ACTIVE = "ecfdb65d-592d-4649-ad7c-a647f9862634"  # copied from your URL (the "s" filter)
UA = {"User-Agent": "mp-holdings-research/1.0 (personal project; add your contact email here)"}
KEEP = {"disclosure summary": "Disclosure summary", "notice of material change": "Material change"}
FIELDS = ["mp", "party", "riding", "company", "ticker", "sector", "type", "owner", "date",
          "action", "source_type", "disclosed", "url"]

# needle (regex, lowercase) -> canonical name, ticker, sector. Extend freely.
CANON = [
    ("royal bank", "Royal Bank of Canada", "RY", "Financials"), ("rbc", "Royal Bank of Canada", "RY", "Financials"),
    ("toronto-dominion", "TD Bank", "TD", "Financials"), ("td bank", "TD Bank", "TD", "Financials"),
    ("bank of montreal", "Bank of Montreal", "BMO", "Financials"),
    ("bank of nova scotia", "Bank of Nova Scotia", "BNS", "Financials"), ("scotiabank", "Bank of Nova Scotia", "BNS", "Financials"),
    ("canadian imperial", "CIBC", "CM", "Financials"), ("cibc", "CIBC", "CM", "Financials"),
    ("manulife", "Manulife", "MFC", "Financials"), ("sun life", "Sun Life", "SLF", "Financials"),
    ("brookfield", "Brookfield", "", "Financials"), ("visa", "Visa", "V", "Financials"),
    ("enbridge", "Enbridge", "ENB", "Energy"), ("suncor", "Suncor Energy", "SU", "Energy"),
    ("canadian natural", "Canadian Natural Resources", "CNQ", "Energy"), ("cenovus", "Cenovus Energy", "CVE", "Energy"),
    ("arc resources", "Arc Resources", "ARX", "Energy"), ("whitecap", "Whitecap Resources", "WCP", "Energy"),
    ("obsidian", "Obsidian Energy", "OBE", "Energy"), ("strathcona", "Strathcona Resources", "SCR", "Energy"),
    ("cameco", "Cameco", "CCO", "Energy"),
    ("shopify", "Shopify", "SHOP", "Technology"), ("apple", "Apple", "AAPL", "Technology"),
    ("microsoft", "Microsoft", "MSFT", "Technology"), ("intel", "Intel", "INTC", "Technology"),
    ("salesforce", "Salesforce", "CRM", "Technology"), ("sandisk", "Sandisk", "SNDK", "Technology"),
    ("tesla", "Tesla", "TSLA", "Consumer"), ("loblaw", "Loblaw", "L", "Consumer"),
    ("costco", "Costco", "COST", "Consumer"), ("tjx", "TJX", "TJX", "Consumer"), ("cineplex", "Cineplex", "CGX", "Consumer"),
    ("telus", "Telus", "T", "Telecom"), ("bce", "BCE", "BCE", "Telecom"),
    ("canadian national railway", "Canadian National Railway", "CNR", "Industrials"), ("spacex", "SpaceX", "", "Industrials"),
    ("nutrien", "Nutrien", "NTR", "Materials"), ("barrick", "Barrick", "", "Materials"),
    ("seabridge", "Seabridge Gold", "SEA", "Materials"), ("torex", "Torex Gold", "TXG", "Materials"),
    ("fortis", "Fortis", "FTS", "Utilities"), ("northland power", "Northland Power", "NPI", "Utilities"),
    ("johnson & johnson", "Johnson & Johnson", "JNJ", "Health care"), ("stryker", "Stryker", "SYK", "Health care"),
]


def norm(s):
    return s.replace("\u2019", "'").replace("\u2018", "'").replace("\xa0", " ").strip()


def parse_any_date(s):
    s = norm(s).rstrip(".")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


# ---------------------------------------------------------------- description parsing
HEAD = re.compile(
    r"^(assets|liabilities|trusts|activities|other sources of income|investment in private corporations"
    r"|positions with corporations[^\[]*|spouse's/common-law partner's [^\[:]+|dependent's/dependents' [^\[:]+)"
    r"\s*(\[[^\]]*\])?\s*:?$", re.I)
ACTION = re.compile(r"\b(purchased|bought|acquired|sold|disposed|divested|no longer (?:own|hold)|now (?:own|hold))\b", re.I)
LEAD = re.compile(r"^(?:i|my spouse|my partner|we)\s+(?:(?:have|has|now|no longer|recently|also)\s+)*"
                  r"(?:purchased|bought|acquired|sold|disposed of|divested|own|owned|hold|held)\s+", re.I)
SEC = re.compile(r"^(?:additional |common |preferred )?(?:shares|units|stock|options|warrants|bonds|debentures)"
                 r"\s+(?:of|in)\s+(?P<name>[^:]+)$", re.I)
REAL = re.compile(r"\b(residential|rental|land|property|properties|condo|cottage|real estate|building|lot)\b", re.I)


def canon(name):
    low = name.lower()
    for needle, c, t, s in CANON:
        if re.search(r"\b" + re.escape(needle) + r"\b", low):
            return c, t, s
    return None


def make_row(name, owner, date, action, typ_hint=None):
    name = re.sub(r"\s+", " ", name).strip(" .;,-")
    ticker = ""
    m = re.search(r"\(([A-Z0-9.\-]{1,8})\)$", name)
    if m:
        ticker, name = m.group(1), name[:m.start()].strip()
    low = name.lower()
    if typ_hint:
        typ, sector = typ_hint, typ_hint
    elif re.search(r"\b(bitcoin|ethereum|crypto)", low):
        typ, sector = "Crypto", "Crypto"
    elif re.match(r"^(gold|silver|platinum)\b", low):
        typ, sector = "Commodity", "Commodity"
    elif re.search(r"\b(etf|fund|index)\b", low):
        typ, sector = "Mutual fund / ETF", "Funds"
    else:
        typ, sector = "Stock", "Other"
        c = canon(name)
        if c:
            name, t, sector = c
            ticker = ticker or t
    return dict(company=name, ticker=ticker, sector=sector, type=typ, owner=owner, date=date, action=action)


def parse_holdings(desc, kind, disclosed, include_other=False):
    """desc: list of lines. kind: 'Disclosure summary' | 'Material change'."""
    rows, section, owner = [], None, "Self"
    date, action = disclosed, ("Held" if kind == "Disclosure summary" else "Changed")
    bare = last_head = False
    for raw in desc:
        ln = re.sub(r"^[-\u2022*\s]+", "", norm(raw))
        if not ln:
            continue
        if HEAD.match(ln):
            low = ln.lower()
            base = re.sub(r"\s*\[.*$", "", low).rstrip(":")
            if not (base == "assets" and last_head and owner != "Self"):
                owner = "Spouse" if low.startswith("spouse") else "Dependent" if low.startswith("dependent") else "Self"
            section, bare, last_head = base, False, True
            if kind != "Disclosure summary":
                action = "Changed"
            continue
        last_head = False
        m = re.match(r"date of change:?\s*(.+)$", ln, re.I)
        if m:
            date = parse_any_date(m.group(1)) or date
            continue
        if not section or "asset" not in section:
            continue
        a = ACTION.search(ln)
        if a:
            action = "Disposed" if re.match(r"(sold|dispos|divest|no longer)", a.group(1), re.I) else "Acquired"
            if re.search(r"\b(spouse|partner)\b", ln, re.I):
                owner = "Spouse"
        stripped = LEAD.sub("", ln)
        m = SEC.match(stripped)
        if m:
            rows.append(make_row(m.group("name"), owner, date, action))
            bare = False
            continue
        if a and ln.endswith(":"):
            bare = True
            continue
        if bare:
            rows.append(make_row(ln, owner, date, action))
            continue
        low = ln.lower()
        if re.search(r"\b(bitcoin|ethereum|crypto)", low) or re.match(r"^(gold|silver|platinum)\b", low):
            rows.append(make_row(ln, owner, date, action))
        elif include_other:
            rows.append(make_row(ln, owner, date, action, "Real estate" if REAL.search(ln) else "Other asset"))
    seen, out = set(), []
    for r in rows:
        k = (r["company"], r["owner"], r["date"], r["action"])
        if r["company"] and k not in seen:
            seen.add(k)
            out.append(r)
    return out


# ---------------------------------------------------------------- listing page parsing
DISCL = re.compile(r"Disclosed on\s+([A-Z][a-z]+ \d{1,2}, \d{4})")


def parse_page(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    all_dates = [parse_any_date(d) for d in DISCL.findall(soup.get_text(" "))]
    cards, seen = [], set()
    for a in soup.find_all("a", href=re.compile(r"Details\?declarationId=")):
        title = a.get_text(" ", strip=True).lower()
        if title not in KEEP:
            continue
        card = a
        while card.parent is not None and not card.find("a", href=re.compile(r"clientId=")):
            card = card.parent
        client = card.find("a", href=re.compile(r"clientId="))
        if client is None:
            continue
        url = urljoin(BASE, a["href"])
        if url in seen:
            continue
        seen.add(url)
        lines = [l for l in card.get_text("\n", strip=True).split("\n") if l.strip()]
        if "Description" not in lines:
            continue
        i = lines.index("Description")
        desc = []
        for l in lines[i + 1:]:
            if l.startswith(("Show more", "Conflict of Interest")):
                break
            desc.append(l)
        m = DISCL.search(" ".join(lines))
        cards.append(dict(kind=KEEP[title], mp=client.get_text(" ", strip=True), url=url,
                          disclosed=parse_any_date(m.group(1)) if m else "", desc=desc))
    return cards, all_dates


# ---------------------------------------------------------------- network helpers
def get(session, url, params=None, tries=4):
    import requests
    for i in range(tries):
        try:
            r = session.get(url, params=params, headers=UA, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 503):
                time.sleep(5 * (i + 1))
                continue
            r.raise_for_status()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))
    raise RuntimeError("failed: " + url)


def key(n):
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode().lower())


def party_map(session):
    """Party + riding for current MPs from Represent (Open North). Best effort."""
    try:
        data = get(session, "https://represent.opennorth.ca/representatives/house-of-commons/", {"limit": 1000}).json()
        return {key(o["name"]): (o.get("party_name", ""), o.get("district_name", "")) for o in data["objects"]}
    except Exception as e:
        print("  ! could not load party data:", e, file=sys.stderr)
        return {}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="mp_holdings.csv")
    ap.add_argument("--max-pages", type=int, default=0, help="0 = all")
    ap.add_argument("--since", default="", help="stop at declarations disclosed before YYYY-MM-DD")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    ap.add_argument("--role", default=ROLE_MP, help="role GUID; '' = all roles")
    ap.add_argument("--enrich", action="store_true", help="add party/riding via Represent API")
    ap.add_argument("--include-other-assets", action="store_true", help="also keep real estate etc.")
    ap.add_argument("--all-summaries", action="store_true", help="keep superseded summaries/changes too")
    ap.add_argument("--save-raw", default="", help="write parsed cards to this .jsonl")
    ap.add_argument("--from-raw", default="", help="parse a saved .jsonl instead of downloading")
    args = ap.parse_args()

    import requests
    cards = []
    if args.from_raw:
        cards = [json.loads(l) for l in open(args.from_raw, encoding="utf-8")]
    else:
        s = requests.Session()
        page, total = 1, None
        while True:
            params = dict(q="", r="", role=args.role, s=STATUS_ACTIVE, c="", df="", dt="",
                          sort="date", dir="desc", p=page)
            html = get(s, LIST_URL, params).text
            if total is None:
                m = re.search(r"page\s+\d+\s+of\s+(\d+)", html, re.I)
                total = int(m.group(1)) if m else 1
            got, dates = parse_page(html)
            cards += got
            print(f"page {page}/{total}: {len(got)} relevant cards", file=sys.stderr)
            if not got and page == 1:
                print("  ! no cards parsed on page 1: the page layout may have changed", file=sys.stderr)
            oldest = min([d for d in dates if d], default="")
            if page >= total or (args.max_pages and page >= args.max_pages) or (args.since and oldest and oldest < args.since):
                break
            page += 1
            time.sleep(args.delay)
        if args.save_raw:
            with open(args.save_raw, "w", encoding="utf-8") as f:
                for c in cards:
                    f.write(json.dumps(c, ensure_ascii=False) + "\n")

    # newest first -> first summary seen per MP is the current one
    cards.sort(key=lambda c: c["disclosed"], reverse=True)
    latest_summary, rows = {}, []
    for c in cards:
        if args.since and c["disclosed"] < args.since:
            continue
        if not args.all_summaries and c["mp"] in latest_summary:
            continue  # superseded by a newer summary
        if c["kind"] == "Disclosure summary":
            latest_summary[c["mp"]] = c["disclosed"]
        for r in parse_holdings(c["desc"], c["kind"], c["disclosed"], args.include_other_assets):
            rows.append(dict(r, mp=c["mp"], party="Unknown", riding="", source_type=c["kind"],
                             disclosed=c["disclosed"], url=c["url"]))

    if args.enrich and rows:
        pm = party_map(requests.Session())
        missing = set()
        for r in rows:
            p = pm.get(key(r["mp"]))
            if p:
                r["party"], r["riding"] = p
            else:
                missing.add(r["mp"])
        if missing:
            print("  ! no party match for:", ", ".join(sorted(missing)), file=sys.stderr)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows from {len({r['mp'] for r in rows})} MPs -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
