"""Spot-check: is the MRP we read the MRP a shopper sees?

Abhisekh, 28 Sep 2026: the spot-check, and the daily check that uses it,
compare the MRP, not the selling price (a run still reports the selling
price). For every own-website link it is given, this reads the page the
app's way (`sites.fetch_listing`) and ALSO loads it in a headless browser,
reading the MRP shown beside the product's heading (`browser.SHOWN_JS`:
struck through or labelled "MRP"; a page showing neither has no discount,
so the price it shows is its MRP). The two are compared:

  MATCH        every MRP the page shows is one the app read
  DIFFERENT    the page shows an MRP the app did not read -- look at it
  APP MISSED   the page shows an MRP, the app read no price
  CAN'T TELL   the page shows no single price beside its heading
  BOTH EMPTY   neither found a price (sold out, gone, not rupees ...)

It is a check, not part of a run: nothing here changes a price. It makes
two page loads per link, paced like any other request.

    python engine/spotcheck.py                  # saved brands + spotcheck_links.csv
    python engine/spotcheck.py --brand BownBee  # one saved brand
    python engine/spotcheck.py --limit 10
    python engine/spotcheck.py --recheck spotcheck_<date>.csv   # the misses

Writes spotcheck_<date>.csv in the project folder.
"""
import sys, re, csv, argparse, datetime, pathlib, collections, urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sites, browser

ROOT = HERE.parent
LINKS = ROOT / "spotcheck_links.csv"          # brand,url -- any extra links
MARKETPLACES = ("myntra.", "amazon.", "flipkart.", "firstcry.")


def catalogue_links(only=None):
    """(brand, url) for every saved design's own-website link. Read-only."""
    import openpyxl, catalogue
    if not catalogue.PATH.exists():
        return []
    wb = openpyxl.load_workbook(catalogue.PATH, read_only=True)
    out = []
    for ws in wb.worksheets:
        if only and ws.title.lower() != only.lower():
            continue
        rows = ws.iter_rows(values_only=True)
        head = [str(h or "").strip().lower() for h in next(rows, [])]
        if "website" not in head:
            continue
        col = head.index("website")
        for r in rows:
            url = str(r[col] or "").strip() if col < len(r) else ""
            if url.startswith("http"):
                out.append((ws.title, url))
    wb.close()
    return out


def extra_links(only=None):
    if not LINKS.exists():
        return []
    with open(LINKS, encoding="utf-8-sig", newline="") as f:
        return [(r.get("brand", ""), r["url"].strip()) for r in csv.DictReader(f)
                if (r.get("url") or "").startswith("http") and
                (not only or r.get("brand", "").lower() == only.lower())]


def _near(a, b):
    return abs(a - b) < 1.0


_MYNTRA_SUMMARY = re.compile(
    r'<meta[^>]+name="description"[^>]+content="[^"]*?\bat Rs\.?\s*([0-9][0-9,]*)', re.I)


_MYNTRA_SELLER_MRP = re.compile(r'"sizeSellerData":\[\{"mrp":([0-9.]+)')


def myntra_summary(text):
    """Myntra's own page summary: "... from VASTRAMAY at Rs. 984. Style ID
    ..." -- the price as Myntra states it in the page it sends, a second
    place from the pdpData the app reads -- and the MRP each size's seller
    states ("sizeSellerData":[{"mrp":2599,...), a second place from the
    product-level MRP the app reads. (Myntra refuses the headless browser:
    HTTP/2 reset, 28 Sep 2026 -- not got round.)"""
    m = _MYNTRA_SUMMARY.search(text or "")
    sell = [float(m.group(1).replace(",", ""))] if m else []
    mrps = sorted({float(x) for x in _MYNTRA_SELLER_MRP.findall(text or "")})
    return {"h1": "", "sell": sell, "sizes": [],
            "struck": [x for x in mrps if not sell or x > sell[0]]}


def shopper_view(url):
    """What a shopper is shown for this link, as {"shown": {...}} or
    {"error": why}. Own websites and Amazon, Flipkart, FirstCry: the page
    in a headless browser (a marketplace's own price box, browser.MARKET_JS).
    Myntra: its page summary."""
    host = urllib.parse.urlsplit(url).netloc
    allowed, why = sites.robots_allows(url)
    if not allowed:
        return {"error": why}
    if "myntra." in host:
        r, why = sites._get(url, tries=2)
        if r is None or r.status_code != 200:
            return {"error": why or "no answer"}
        return {"shown": myntra_summary(r.text)}
    if not browser.available():
        return {"error": "the browser reader is not installed"}
    sites._wait_turn(host)
    return browser.render(url, sites.robots_allows, user_agent=sites.UA,
                          js=browser.market_probe(url))


def app_mrps(app):
    """The MRPs the app read: each priced size's own MRP (compare_at), else
    the page's one MRP (listed_mrp); a size with neither above its price
    has no discount, so its MRP is its price. Pure."""
    if not app.ok:
        return []
    out = set()
    for v in app.variants:
        if not v.price:
            continue
        out.add(v.compare_at if v.compare_at and v.compare_at > v.price else
                app.listed_mrp if app.listed_mrp and app.listed_mrp > v.price
                else v.price)
    return sorted(out)


def page_mrps(shown):
    """The MRPs a shopper is shown: struck through or labelled "MRP"; a page
    showing its price with neither has no discount, so its MRP is that
    price. Pure."""
    shown = shown or {}
    sell = {float(p) for p in shown.get("sell") or [] if p}
    if len(sell) > 1:
        # A price range ("Rs 549 - Rs 599"): the page shows each size's MRP
        # only once a size is chosen, and an MRP beside the range is the
        # product's, not a size's (boyzngalz.com: 1199 beside the range,
        # 999 once size 13 is chosen -- which the app reads). Not compared.
        return []
    mrp = [float(p) for p in (shown.get("mrp") or shown.get("struck") or []) if p]
    return sorted(set(mrp or sell))


def verdict(app, shown):
    """(verdict, app MRPs, page MRPs) for one link. Abhisekh, 28 Sep 2026:
    the spot-check and the daily check compare the MRP, not the selling
    price. (A run still reports the selling price.)"""
    got, seen = app_mrps(app), page_mrps(shown)
    if not got and not seen:
        return "BOTH EMPTY", got, seen
    if not got:
        return "APP MISSED", got, seen
    if not seen:
        return "CAN'T TELL", got, seen
    if all(any(_near(s, g) for g in got) for s in seen):
        return "MATCH", got, seen
    return "DIFFERENT", got, seen


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--brand", help="only this brand")
    ap.add_argument("--limit", type=int, help="at most this many links")
    ap.add_argument("--recheck", metavar="CSV",
                    help="only the links an earlier spotcheck CSV did not MATCH")
    a = ap.parse_args(argv)
    if not browser.available():
        print("the browser reader is not installed: pip install playwright, "
              "then python -m playwright install chromium")
        return 1
    if a.recheck:
        with open(a.recheck, encoding="utf-8", newline="") as f:
            links = [(r["brand"], r["url"]) for r in csv.DictReader(f)
                     if r["verdict"] != "MATCH"]
    else:
        links = catalogue_links(a.brand) + extra_links(a.brand)
    links = list(collections.OrderedDict.fromkeys(links))
    links = [(b, u) for b, u in links
             if not any(m in urllib.parse.urlsplit(u).netloc for m in MARKETPLACES)]
    if a.limit:
        links = links[:a.limit]
    sites.new_run()
    out_path = ROOT / ("spotcheck_%s%s.csv" % (datetime.date.today().isoformat(),
                                               "_recheck" if a.recheck else ""))
    tally = collections.Counter()
    rows = []
    for i, (brand, url) in enumerate(links, 1):
        app = sites.fetch_listing(url)
        seen = shopper_view(url)
        v, got, shown = verdict(app, seen.get("shown"))
        if seen.get("error"):
            v = "CAN'T TELL"
        tally[v] += 1
        fmt = lambda xs: " ".join("%g" % x for x in xs)
        print("%3d/%d  %-10s  %-12s app MRP %-14s page MRP %-10s %s" % (
            i, len(links), v, brand[:12], fmt(got)[:18], fmt(shown)[:14], url[:70]))
        rows.append([brand, url, v, fmt(got), fmt(shown), app.note,
                     seen.get("error", "")])
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(["brand", "url", "verdict", "app MRP", "page MRP",
                    "app note", "browser"])
        w.writerows(rows)
    print("\n" + ", ".join("%s %d" % kv for kv in tally.most_common()))
    print("written: %s" % out_path)
    return 1 if tally["DIFFERENT"] or tally["APP MISSED"] else 0


if __name__ == "__main__":
    sys.exit(main())
