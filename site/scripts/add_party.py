#!/usr/bin/env python3
"""
Fill in party, riding and province for every MP in an mp_holdings.csv, using the
House of Commons "Current Members of Parliament" list.

  python add_party.py mp_holdings.csv                 # downloads the list and edits the CSV in place
  python add_party.py mp_holdings.csv --html members.html   # use a page you saved from your browser

A backup is written to <file>.bak first. Names that can't be matched are printed.
"""
import argparse, csv, difflib, re, shutil, sys, unicodedata

URL = "https://www.ourcommons.ca/members/en/search"
UA = {"User-Agent": "mp-holdings-research/1.0 (personal project; add your contact email here)"}
HON = re.compile(r"^(?:Right Hon\.|Hon\.|Dr\.|Rev\.)\s*", re.I)
PARTIES = "Conservative|Liberal|Bloc Québécois|NDP|Green Party|Independent|Canadian Senators Group"
PROVINCES = ["Newfoundland and Labrador", "Prince Edward Island", "Northwest Territories", "British Columbia",
             "New Brunswick", "Nova Scotia", "Saskatchewan", "Manitoba", "Alberta", "Ontario", "Quebec",
             "Nunavut", "Yukon"]


def key(n):
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", n)


def display_name(sortname):
    """'Ali, Hon. Shafqat' -> 'Shafqat Ali'"""
    surname, rest = sortname.split(",", 1)
    return f"{HON.sub('', rest.strip())} {surname.strip()}".strip()


def parse_members(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for tr in soup.find_all("tr"):                      # the "Name | Political Affiliation | Constituency | Province" table
        c = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(c) >= 4 and "," in c[0]:
            out[display_name(c[0])] = (c[1], c[2], c[3])
    if out:
        return out
    pat = re.compile(rf"^(?P<n>[^,]+,.+?)\s+(?P<p>{PARTIES})\s+(?P<r>.+)$")   # fallback: one text line per MP
    for line in soup.get_text("\n").split("\n"):
        m = pat.match(line.strip())
        if not m:
            continue
        rest = m.group("r")
        prov = next((p for p in PROVINCES if rest.endswith(p)), "")
        riding = rest[: len(rest) - len(prov)].strip() if prov else rest
        out[display_name(m.group("n"))] = (m.group("p"), riding, prov)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_file")
    ap.add_argument("--html", default="", help="saved copy of the members page instead of downloading")
    args = ap.parse_args()

    if args.html:
        html = open(args.html, encoding="utf-8").read()
    else:
        import requests
        r = requests.get(URL, headers=UA, timeout=60)
        r.raise_for_status()
        html = r.text
    members = parse_members(html)
    if not members:
        sys.exit("Couldn't find the member list in the page. Save it from your browser and use --html.")
    print(f"{len(members)} current MPs loaded", file=sys.stderr)

    by_key = {key(n): v for n, v in members.items()}
    by_ends = {}
    for n, v in members.items():
        t = n.split()
        by_ends.setdefault(key(t[0] + t[-1]), []).append(v)     # ignores middle names / initials

    def lookup(name):
        k = key(name)
        if k in by_key:
            return by_key[k]
        t = name.split()
        hit = by_ends.get(key(t[0] + t[-1])) if t else None
        if hit and len(hit) == 1:
            return hit[0]
        close = difflib.get_close_matches(k, by_key, n=1, cutoff=0.9)
        return by_key[close[0]] if close else None

    with open(args.csv_file, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        fields, rows = list(rd.fieldnames), list(rd)
    for col in ("party", "riding", "province"):
        if col not in fields:
            fields.append(col)
    shutil.copyfile(args.csv_file, args.csv_file + ".bak")

    missing, cache = set(), {}
    for r in rows:
        if r["mp"] not in cache:
            cache[r["mp"]] = lookup(r["mp"])
        v = cache[r["mp"]]
        if v:
            r["party"], r["riding"], r["province"] = v
        else:
            missing.add(r["mp"])
            r["party"] = r.get("party") or "Unknown"
            r["riding"], r["province"] = r.get("riding", ""), r.get("province", "")
    with open(args.csv_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"updated {len(rows)} rows ({len(cache) - len(missing)} of {len(cache)} MPs matched)", file=sys.stderr)
    if missing:
        print("no match for:", ", ".join(sorted(missing)), file=sys.stderr)
        print("(former MPs, or a name spelled differently on the two sites)", file=sys.stderr)


if __name__ == "__main__":
    main()
