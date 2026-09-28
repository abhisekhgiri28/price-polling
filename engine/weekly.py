"""The weekly check: can the app still read every brand, on every channel?

Run by Windows Task Scheduler every Monday at 11 am, or by hand:
    python engine/weekly.py

Abhisekh, 28 Sep 2026: its purpose is that "every brand is readable and
the system doesn't fail on any brand, as well as marketplaces". Dead links
and single products are his to see when he runs a brand (Refetch). So this
is a HEALTH check, not a product audit:

  * For every saved brand and every channel it links (its own website,
    Myntra, FirstCry, Amazon, Flipkart ...), up to SAMPLE random links are
    read the app's way -- a different few each week.
  * A link READS when the app gets a price from it (sold out included).
    A product that is gone or does not sell the size is an answer about
    the product, not the site: another link is tried instead.
  * A link FAILS when the site could not be read: "not fetched", "check
    page", "not in rupees", "not allowed". Such links are tried once more
    after RETRY_AFTER_SECONDS (a site that did not answer, Amazon's check
    page), and only then counted.
  * EVERY link that reads, on every channel, is spot-checked: its price
    is compared with what a shopper sees (spotcheck.shopper_view) -- own
    websites and Amazon, Flipkart, FirstCry in a headless browser (the
    marketplace's own price box), Myntra from its page summary (it refuses
    the headless browser). A channel that reads but could not be
    spot-checked is reported too.

It reports ONLY when something fails: weekly_check_issues_<date>.html in
the project folder, opened in the browser. When all is well nothing is
shown. Each week's results go to checks/<date>.json and one line to
checks/log.txt. It never writes to the brand catalogue.
"""
import sys, json, time, random, datetime, pathlib, collections, html
import argparse, traceback, urllib.parse, webbrowser

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sites, compare, catalogue, spotcheck, browser

ROOT = HERE.parent
CHECKS = ROOT / "checks"
SAMPLE = 2                  # links that must read, per brand and channel
EXTRA_TRIES = 3             # more links tried when a sampled product is gone
RETRY_AFTER_SECONDS = 900   # before a failed link is tried again
CHANNEL_NAME = {"own site": "Website", "myntra": "Myntra",
                "firstcry": "FirstCry", "amazon": "Amazon",
                "flipkart": "Flipkart", "hopscotch": "Hopscotch"}
READ_FAILURES = ("not fetched", "check page", "not in rupees", "not allowed")


def pools(brands=None):
    """{(brand, channel): [unique links]} from the saved catalogue."""
    out = collections.OrderedDict()
    for brand, _designs, _barcodes in catalogue.brands():
        if brands and brand.lower() not in {b.lower() for b in brands}:
            continue
        recs, _issues, _meta = catalogue.records_for(brand)
        for rec in recs:
            for chan, url in compare.channels_for(rec):
                links = out.setdefault((brand, chan), [])
                if url not in links:
                    links.append(url)
    return out


def judge(lst):
    """("reads" | "answer" | "fails", why) for one fetched page. Pure."""
    if lst.ok and any(v.price for v in lst.variants):
        return "reads", ""
    kind = sites.empty_kind(lst.note)
    if kind in READ_FAILURES:
        return "fails", "%s: %s" % (kind, lst.note or "no reason given")
    return "answer", lst.note          # the product: gone, size not sold ...


def group_issues(brand, chan, results):
    """[(kind, brand, channel, detail, link)] for one brand and channel,
    from its [(url, verdict, why, shopper)] results. Pure, tested offline."""
    name = CHANNEL_NAME.get(chan, chan.title())
    out = []
    for url, verdict, why, shopper in results:
        if verdict == "fails":
            out.append(("Not readable", brand, name, why, url))
        elif shopper and shopper[0] in ("DIFFERENT", "APP MISSED"):
            out.append(("Read, but not what shoppers see", brand, name,
                        "the app reads %s; the page shows %s" % (
                            " ".join("%g" % x for x in shopper[1]) or "no price",
                            " ".join("%g" % x for x in shopper[2])), url))
    read = [(u, s) for u, v, _w, s in results if v == "reads"]
    if read and not out and not any(s and s[0] == "MATCH" for _u, s in read):
        # The spot-check is mandatory: a channel that reads but could not
        # be compared with what a shopper sees is itself reported.
        u, s = read[0]
        why = (s[3] if s and len(s) > 3 else
               "the page showed no single price to compare" if s else
               "not compared")
        out.append(("Spot-check could not be done", brand, name,
                    "the app reads it, but it could not be compared with "
                    "what a shopper sees: %s" % why, u))
    if results and not out and not any(v == "reads" for _u, v, _w, _s in results):
        out.append(("No live product to test", brand, name,
                    "none of the %d links tried has a product on sale (%s) -- "
                    "the saved links may be out of date" % (
                        len(results), results[0][2][:80]), results[0][0]))
    return out


def _check_group(brand, chan, links, rnd):
    order = list(links)
    rnd.shuffle(order)
    results, reads = [], 0
    for url in order[:SAMPLE + EXTRA_TRIES]:
        if reads >= SAMPLE:
            break
        lst = sites.fetch_listing(url)
        verdict, why = judge(lst)
        shopper = None
        if verdict == "reads":
            reads += 1
            # The spot-check on every channel (Abhisekh, 28 Sep 2026:
            # "mandatory for every brand and marketplace").
            seen = spotcheck.shopper_view(url)
            shopper = (spotcheck.verdict(lst, seen.get("shown"))
                       if not seen.get("error")
                       else ("NOT CHECKED", [], [], seen["error"]))
        results.append([url, verdict, why, shopper])
    return results


def run(open_report=True, only=None, retry_after=RETRY_AFTER_SECONDS,
        seed=None):
    day = datetime.date.today().isoformat()
    t0 = time.time()
    CHECKS.mkdir(exist_ok=True)
    sites.new_run()
    # A different sample each week, the same one if re-run the same week.
    rnd = random.Random(seed if seed is not None
                        else datetime.date.today().isocalendar()[1])
    groups = pools([only] if only else None)
    results = collections.OrderedDict()
    for (brand, chan), links in groups.items():
        results[(brand, chan)] = _check_group(brand, chan, links, rnd)
    # Failures get one more try, later: a site that did not answer or
    # Amazon's "slow down" page passes with time.
    failed = [(k, r) for k, rs in results.items() for r in rs if r[1] == "fails"]
    if failed and retry_after:
        time.sleep(retry_after)
        sites.new_run()
    for (brand, chan), r in failed:
        lst = sites.fetch_listing(r[0])
        r[1], r[2] = judge(lst)
        if r[1] == "fails":
            r[2] += " (and again %d minutes later)" % max(1, retry_after // 60)
    issues = []
    for (brand, chan), rs in results.items():
        issues += group_issues(brand, chan, [tuple(r) for r in rs])
    minutes = (time.time() - t0) / 60
    brands = sorted({b for b, _c in groups})
    stats = ("%d brands, %d brand-channels, %d links tried, %.0f minutes"
             % (len(brands), len(groups),
                sum(len(rs) for rs in results.values()), minutes))
    (CHECKS / ("%s.json" % day)).write_text(json.dumps({
        "day": day, "stats": stats,
        "results": {"%s|%s" % k: rs for k, rs in results.items()},
        "issues": issues}, indent=1, default=str), encoding="utf-8")
    with open(CHECKS / "log.txt", "a", encoding="utf-8") as f:
        f.write("%s  %d issue(s)  %s\n" % (day, len(issues), stats))
    if issues:
        path = _report(issues, day, stats)
        if open_report:
            webbrowser.open(path.as_uri())
        return path, issues
    return None, issues


def _report(issues, day, stats):
    by = collections.OrderedDict()
    for kind, brand, chan, detail, link in issues:
        by.setdefault(kind, []).append((brand, chan, detail, link))
    parts = []
    for kind, items in by.items():
        lis = "".join(
            "<li><b>%s</b>%s &mdash; %s%s</li>" % (
                html.escape(b), (" &middot; " + html.escape(c)) if c else "",
                html.escape(d),
                (' &nbsp;<a href="%s" target="_blank">open page</a>'
                 % html.escape(l)) if l else "")
            for b, c, d, l in items)
        parts.append("<h2>%s (%d)</h2><ul>%s</ul>" % (html.escape(kind),
                                                       len(items), lis))
    body = ("<!doctype html><meta charset='utf-8'><title>Weekly check: %d "
            "issues</title><style>body{font:15px/1.5 system-ui,sans-serif;"
            "max-width:1000px;margin:24px auto;padding:0 16px;color:#222}"
            "h1{font-size:22px}h2{font-size:17px;margin-top:28px}"
            "li{margin:6px 0}a{color:#0b57d0}.s{color:#666}</style>"
            "<h1>Weekly check, %s: %d problem%s</h1>"
            "<p class='s'>%s. Only brands or channels that could not be read "
            "correctly are listed; everything else passed.</p>%s") % (
        len(issues), day, len(issues), "" if len(issues) == 1 else "s",
        html.escape(stats), "".join(parts))
    path = ROOT / ("weekly_check_issues_%s.html" % day)
    path.write_text(body, encoding="utf-8")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="the weekly health check")
    ap.add_argument("--brand", help="only this brand (a trial)")
    ap.add_argument("--no-open", action="store_true",
                    help="do not open the report page")
    ap.add_argument("--no-retry-wait", action="store_true",
                    help="retry failures at once (a quick trial)")
    a = ap.parse_args(argv)
    try:
        path, issues = run(open_report=not a.no_open, only=a.brand,
                           retry_after=0 if a.no_retry_wait
                           else RETRY_AFTER_SECONDS)
    except Exception:
        # A check that crashed is itself a problem worth showing.
        CHECKS.mkdir(exist_ok=True)
        day = datetime.date.today().isoformat()
        err = traceback.format_exc()
        with open(CHECKS / "log.txt", "a", encoding="utf-8") as f:
            f.write("%s  CHECK FAILED\n%s\n" % (day, err))
        p = _report([("The weekly check itself failed", "", "",
                      err.strip().splitlines()[-1], "")], day,
                    "see checks/log.txt for the details")
        webbrowser.open(p.as_uri())
        return 1
    print("%d issue(s)%s" % (len(issues), (" -- " + str(path)) if path else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
