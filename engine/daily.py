"""The daily check: can the app still read every brand, on every channel?

Run by Windows Task Scheduler every day at 11:15 am, or by hand:
    python engine/daily.py

Abhisekh, 28 Sep 2026: its purpose is that "every brand is readable and
the system doesn't fail on any brand, as well as marketplaces". Dead links
and single products are his to see when he runs a brand (Refetch). So this
is a HEALTH check, not a product audit:

  * For every saved brand and every channel it links (its own website,
    Myntra, FirstCry, Amazon, Flipkart ...), up to SAMPLE random links are
    read the app's way -- a different few each day.
  * A link READS when the app gets a price from it (sold out included).
    A product that is gone or does not sell the size is an answer about
    the product, not the site: another link is tried instead.
  * A link FAILS when the site could not be read: "not fetched", "check
    page", "not in rupees", "not allowed". Such links are tried once more
    after RETRY_AFTER_SECONDS (a site that did not answer, Amazon's check
    page), and only then counted.
  * EVERY link that reads, on every channel, is spot-checked: its MRP --
    not its selling price (Abhisekh, 28 Sep 2026) -- is compared with the
    MRP a shopper sees (spotcheck.shopper_view, spotcheck.verdict) -- own
    websites and Amazon, Flipkart, FirstCry in a headless browser (the
    marketplace's own price box), Myntra from its page summary (it refuses
    the headless browser). A channel that reads but could not be
    spot-checked is reported too.

It reports ONLY when something fails -- and no page opens by itself
(Abhisekh, 29 Sep 2026): the open problems are kept in checks/status.json,
and the app's page (serve.py) shows a flashing "System Fix !" button for
as long as any is open. The button opens the details (/system-fix). A
problem stays open until a later check on its brand and channel passes:
the next day's check, or "Run the check again" on the details page
(python engine/daily.py --recheck). The re-check first runs the code's
own tests (validate.py), then checks EVERY brand and channel, so a new
problem a code change caused is caught too (Abhisekh, 29 Sep 2026); a
failed test is itself a problem, and the live check then waits for the
tests to pass. When all is well nothing is shown. Each day's results go
to checks/<date>.json and one line to checks/log.txt. It never writes to
the brand catalogue.
"""
import sys, json, time, random, datetime, pathlib, collections, html
import argparse, traceback, urllib.parse

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
RUNNING_STALE_SECONDS = 4 * 3600   # a "running" mark older than this is dead


def status_path():
    """checks/status.json: the open problems. A function, not a constant,
    so a demo that points CHECKS elsewhere never touches the real one."""
    return CHECKS / "status.json"


def running_path():
    return CHECKS / "running.json"


def load_status():
    """{"checked": ..., "stats": ..., "issues": [dict, ...]} -- the open
    problems the app's "System Fix !" button shows. Empty when none."""
    try:
        st = json.loads(status_path().read_text(encoding="utf-8"))
        if isinstance(st.get("issues"), list):
            return st
    except (OSError, ValueError):
        pass
    return {"checked": None, "stats": "", "issues": []}


def running_since():
    """The time ("HH:MM") a check now running started, else None."""
    try:
        st = json.loads(running_path().read_text(encoding="utf-8"))
        if time.time() - float(st["t"]) < RUNNING_STALE_SECONDS:
            return st["at"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def as_issue(t, found):
    kind, brand, chan, detail, link = t
    return {"kind": kind, "brand": brand, "channel": chan, "detail": detail,
            "link": link, "found": found}


def merge_status(old, new, checked):
    """The problems still open after a check. Pure, tested offline.

    old, new: [issue dict]. checked: None for a full check (every old
    problem is replaced by what it found), else the (brand, channel) pairs
    this check covered -- their old problems are replaced, the others stay
    open. A problem found again keeps the day it was first found."""
    first = {(i["kind"], i["brand"], i["channel"], i["link"]): i.get("found")
             for i in old}
    kept = ([] if checked is None else
            [i for i in old if (i["brand"], i["channel"]) not in checked])
    out = []
    for i in new:
        i = dict(i)
        i["found"] = first.get((i["kind"], i["brand"], i["channel"],
                                i["link"])) or i.get("found")
        out.append(i)
    return kept + out


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
                        "the app reads MRP %s; the page shows MRP %s" % (
                            " ".join("%g" % x for x in shopper[1]) or "none",
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


def run(only=None, recheck=False, retry_after=RETRY_AFTER_SECONDS,
        seed=None):
    """(status file or None, this check's issues). only: one brand (a
    trial); recheck: the code's tests, then every brand and channel."""
    CHECKS.mkdir(exist_ok=True)
    now = datetime.datetime.now()
    running_path().write_text(json.dumps(
        {"t": time.time(), "at": now.strftime("%H:%M")}), encoding="utf-8")
    try:
        return _run(only, recheck, retry_after, seed)
    finally:
        try:
            running_path().unlink()
        except OSError:
            pass


TESTS_BRAND = "Code tests"


def run_tests():
    """The offline tests (validate.py) in their own process: [] when all
    pass, else a line per failed test (or why they could not run)."""
    import subprocess
    try:
        out = subprocess.run(
            [sys.executable, str(HERE / "validate.py")], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=1800,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as ex:
        return ["the tests could not be run: %r" % (ex,)]
    failed = [l.strip()[len("FAILED: "):] for l in out.stdout.splitlines()
              if l.strip().startswith("FAILED: ")]
    if out.returncode and not failed:
        tail = (out.stderr or out.stdout).strip().splitlines()
        failed = ["the tests stopped: %s" % (tail[-1] if tail else
                                              "exit code %d" % out.returncode)]
    return failed


def _run(only, recheck, retry_after, seed):
    day = datetime.date.today().isoformat()
    t0 = time.time()
    old = load_status()["issues"]
    if recheck:
        # Abhisekh, 29 Sep 2026: after a code change the re-check must also
        # catch a NEW problem the change caused. So: the code's own tests
        # first, then EVERY brand and channel -- not only the listed ones.
        failures = run_tests()
        if failures:
            save_status(old, [as_issue(("The code's own tests failed",
                                        TESTS_BRAND, "", f, ""), day)
                              for f in failures],
                        {(TESTS_BRAND, "")}, "%d test(s) failed; the live "
                        "check was not run" % len(failures), "re-check")
            return status_path(), []
    sites.new_run()
    # A different sample each day, the same one if re-run the same day.
    rnd = random.Random(seed if seed is not None
                        else datetime.date.today().toordinal())
    groups = pools([only] if only else None)
    name = lambda chan: CHANNEL_NAME.get(chan, chan.title())
    checked = None                       # a full check replaces everything
    if only:
        checked = ({(b, name(c)) for b, c in groups} |
                   {(i["brand"], i["channel"]) for i in old
                    if i["brand"].lower() == only.lower()})
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
    what = ("re-check (tests + every brand and channel)" if recheck
            else "check of %s only" % only if only else "full check")
    open_now = save_status(old, [as_issue(i, day) for i in issues],
                           checked, stats, what)
    with open(CHECKS / "log.txt", "a", encoding="utf-8") as f:
        f.write("%s  %d issue(s)  %s  (%s; %d still open)\n"
                % (day, len(issues), stats, what, len(open_now)))
    return (status_path() if open_now else None), issues


def save_status(old, new, checked, stats, what):
    """Write checks/status.json -- what the app's button reads."""
    open_now = merge_status(old, new, checked)
    status_path().write_text(json.dumps({
        "checked": datetime.datetime.now().strftime("%d %b %Y, %H:%M"),
        "what": what, "stats": stats, "issues": open_now},
        indent=1), encoding="utf-8")
    return open_now


def main(argv=None):
    ap = argparse.ArgumentParser(description="the daily health check")
    ap.add_argument("--brand", help="only this brand (a trial)")
    ap.add_argument("--recheck", action="store_true",
                    help="the code's tests, then every brand and channel "
                    "(the app's \"Run the check again\")")
    ap.add_argument("--no-open", action="store_true",
                    help="no longer does anything: no page opens by itself")
    ap.add_argument("--no-retry-wait", action="store_true",
                    help="retry failures at once (a quick trial)")
    a = ap.parse_args(argv)
    try:
        path, issues = run(only=a.brand, recheck=a.recheck,
                           retry_after=0 if a.no_retry_wait
                           else RETRY_AFTER_SECONDS)
    except Exception:
        # A check that crashed is itself a problem worth showing.
        CHECKS.mkdir(exist_ok=True)
        day = datetime.date.today().isoformat()
        err = traceback.format_exc()
        with open(CHECKS / "log.txt", "a", encoding="utf-8") as f:
            f.write("%s  CHECK FAILED\n%s\n" % (day, err))
        # Kept open (beside the others) until a full check runs cleanly.
        save_status(load_status()["issues"],
                    [as_issue(("The daily check itself failed", "", "",
                               err.strip().splitlines()[-1] +
                               " -- see checks/log.txt", ""), day)],
                    {("", "")}, "the check stopped before it finished", "failed check")
        return 1
    print("%d issue(s)%s" % (len(issues), (" -- " + str(path)) if path else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
