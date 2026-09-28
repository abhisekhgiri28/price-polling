"""Audit a finished comparison. Hard gates -- never ship a FAIL.

`validate.py` tests the *code*. This tests one *run's output*, which is a
different claim: a green suite says the adapters behave, it says nothing about
whether the numbers on today's page can be defended. Until this existed the
project could only say "the suite is green", never "this run is sound".

Taken from `ZOCS_Price_Scraping_Handoff_v1.md` stage 8, which is the stage its
near-100% accuracy actually rests on, and adapted to this engine's data model
rather than copied.

Pure: no network, no database, no clock. It reads the records that went in,
the rows that came out and the listings that were fetched, and returns
findings plus the statistics that say how well matching went.

The severities mean different things and must not be collapsed:

  FAIL  the output contradicts itself or the input. This is never shippable
        and is not a judgement about a garment -- it is arithmetic, a missing
        row, a price with no link. If one of these fires, the run is wrong.
  WARN  the output is possible but suspicious, and a person should look. A
        retailer really can sell below a fifth of MRP in a clearance.
  NOTE  something worth counting that is not a defect.

**The most valuable thing here is `mrp_agree` / `mrp_disagree`**, and it is
deliberately a NOTE rather than a gate. A retailer's nominal MRP is never
compared for *pricing* -- that rule stands and is the oldest one in this
project. But MRP *agreement* is evidence the match is right, which is a
different use of the same number: if the listing we tied to design 2454 quotes
the MRP the intake file quotes, we probably have the right garment. The
handoff calls this "the strongest single signal that matching worked" and
scored 73/1, 13/1 and 29/0 on its clean runs against 29/9 on the one brand
that reprices per channel. This engine has recorded `listed_mrp` on every
quote since the first day and never once looked at it.
"""
import collections

import compare

# A finding. `row` is the barcode it concerns, or "" for a whole-run finding.
Finding = collections.namedtuple("Finding", "level code detail row")

FAIL, WARN, NOTE = "FAIL", "WARN", "NOTE"

# Below this fraction of MRP a selling price is worth a second look. Not a
# gate: clearance is real, and the handoff's own threshold is the same.
SUSPICIOUS_FRACTION = 0.05

# The priced channels, as (row price field, row link field, label). Kept as
# data so a new channel cannot be added to the comparison and quietly skip
# every check here -- which is exactly how `other_price` would have slipped
# past a hand-written list.
CHANNELS = (("own_site_price", "own_site_link", "own site"),
            ("myntra_price", "myntra_link", "myntra"),
            ("firstcry_price", "firstcry_link", "firstcry"),
            ("amazon_price", "amazon_link", "amazon"),
            ("flipkart_price", "flipkart_link", "flipkart"),
            ("other_price", "", "other"))


def _num(v):
    """A price cell as a number, or None. Cells are strings on the way out."""
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _listing_for(fetched, url):
    """The Listing behind a link, tolerating the tracking-query spellings."""
    if not url:
        return None
    hit = fetched.get(url)
    if hit is not None:
        return hit
    want = compare.canonical_url(url)
    for u, lst in fetched.items():
        if compare.canonical_url(u) == want:
            return lst
    return None


def check(records, rows, fetched, confirmed=None):
    """(findings, stats). Nothing here raises: an audit that crashes on a
    malformed run tells you less than one that reports it."""
    findings = []
    stats = collections.Counter()
    add = lambda lvl, code, detail, row="": findings.append(
        Finding(lvl, code, detail, row))

    # -- 1. every input row out exactly once, carrying the input's own numbers
    by_barcode = collections.Counter(r.zoddle_barcode for r in rows)
    in_barcodes = [rec.get("zoddle_barcode") for rec in records]
    missing = [b for b in in_barcodes if b not in by_barcode]
    if missing:
        add(FAIL, "row_missing",
            "%d input barcode(s) produced no row: %s"
            % (len(missing), ", ".join(map(str, missing[:5]))))
    dupes = [b for b, n in by_barcode.items() if n > 1]
    if dupes:
        add(FAIL, "row_duplicated",
            "%d barcode(s) produced more than one row: %s"
            % (len(dupes), ", ".join(map(str, dupes[:5]))))

    rec_by = {}
    for rec in records:
        rec_by.setdefault(rec.get("zoddle_barcode"), rec)
    for r in rows:
        rec = rec_by.get(r.zoddle_barcode)
        if rec is None:
            add(FAIL, "row_unknown",
                "row for barcode %r was not in the input" % r.zoddle_barcode,
                r.zoddle_barcode)
            continue
        for field, key in (("zoddle_mrp", "mrp"), ("zoddle_price", "zoddle_price")):
            out, src = _num(getattr(r, field)), rec.get(key)
            if src is None and out is None:
                continue
            if src is None or out is None or abs(out - src) > 0.01:
                add(FAIL, "input_altered",
                    "%s is %s on the row and %s in the intake file"
                    % (field, out, src), r.zoddle_barcode)

    # -- 2. an own-site or FirstCry price must be a real variant price of that
    #       page. FirstCry's is computed from its own MRP and discount, which
    #       is exactly the kind of figure that should be re-found on the page.
    for r in rows:
        for pf, lf, label, code in (
                ("own_site_price", "own_site_link", "own-site",
                 "own_price_invented"),
                ("firstcry_price", "firstcry_link", "FirstCry",
                 "firstcry_price_invented")):
            price = _num(getattr(r, pf))
            if price is None:
                continue
            lst = _listing_for(fetched, getattr(r, lf))
            if lst is None or not getattr(lst, "variants", None):
                continue      # discovered pages are not in `fetched`; not a gate
            offered = {round(v.price, 2) for v in lst.variants
                       if v.price is not None}
            if offered and round(price, 2) not in offered:
                add(FAIL, code,
                    "%s price %s is not one this page offers (%s)"
                    % (label, price,
                       ", ".join("%.0f" % p for p in sorted(offered)[:6])),
                    r.zoddle_barcode)

    # -- 3. prices that cannot be right, and prices worth a look
    #
    # A retailer's price is compared against THAT RETAILER'S OWN nominal MRP,
    # never against Zoddle's. Written the other way first, this check fired 27
    # times on one clean Peekaboo run -- Myntra sells at 1537 against its own
    # MRP of 1999 while the file says 1499 -- which is not a defect, it is the
    # oldest rule in this project (retailers set a different nominal MRP per
    # channel) being broken by its own audit. A check that cries wolf on
    # correct data is worse than no check, because it trains you to skim.
    #
    # Zoddle's own price is the one figure that MUST sit under the file's MRP,
    # because there both numbers are Zoddle's.
    for r in rows:
        zmrp = _num(r.zoddle_mrp)
        zprice = _num(r.zoddle_price)
        if zmrp and zprice and zprice > zmrp:
            add(WARN, "zoddle_price_over_mrp",
                "Zoddle's price %s is above Zoddle's own MRP %s" % (zprice, zmrp),
                r.zoddle_barcode)
        for pf, lf, label in CHANNELS:
            price = _num(getattr(r, pf))
            if price is None:
                continue
            stats["prices"] += 1
            if price <= 0:
                add(FAIL, "price_not_positive",
                    "%s price is %s" % (label, price), r.zoddle_barcode)
                continue
            lst = _listing_for(fetched, getattr(r, lf, "")) if lf else None
            theirs = _num(getattr(lst, "listed_mrp", None)) if lst else None
            if theirs is None:
                continue          # no nominal MRP of their own to judge against
            if price > theirs:
                add(WARN, "price_over_listed_mrp",
                    "%s sells at %s above its own listed MRP %s"
                    % (label, price, theirs), r.zoddle_barcode)
            if price < theirs * SUSPICIOUS_FRACTION:
                add(WARN, "price_implausibly_low",
                    "%s price %s is under %d%% of its own listed MRP %s"
                    % (label, price, int(SUSPICIOUS_FRACTION * 100), theirs),
                    r.zoddle_barcode)

    # -- 4. one product page cannot be two designs' price
    #
    # Unless they are one product filed twice. The handoff hit this on
    # Vastramay 447/466 -- two ZOCS design codes, different MRPs, one vendor
    # style code -- and had to exempt it. Here the exemption is a shared
    # design_code, which is the same idea with the identifier this engine has.
    by_url = collections.defaultdict(set)
    for r in rows:
        for _pf, lf, _label in CHANNELS:
            if not lf:
                continue
            url = getattr(r, lf, "")
            if url:
                by_url[compare.canonical_url(url)].add(r.design_code)
    for url, designs in by_url.items():
        if len(designs) > 1:
            add(WARN, "url_serves_two_designs",
                "%d designs share one listing (%s): %s"
                % (len(designs), url[:70], ", ".join(sorted(designs)[:5])))

    # -- 5. every price must carry the link it came from
    for r in rows:
        for pf, lf, label in CHANNELS:
            if not lf:
                continue          # other_price has no link column by design
            if _num(getattr(r, pf)) is not None and not getattr(r, lf, ""):
                add(FAIL, "price_without_link",
                    "%s price %s carries no link"
                    % (label, getattr(r, pf)), r.zoddle_barcode)

    # -- 6. the cheapest column, recomputed from scratch
    #
    # Recomputed rather than re-read, because a verdict that agrees with
    # itself proves nothing. Out-of-stock channels are excluded here exactly
    # as they are in compare, so this checks the arithmetic and not the rule.
    for r in rows:
        contenders = {}
        zp = _num(r.zoddle_price)
        if zp is not None:
            contenders["Zoddle"] = zp
        for pf, _lf, label in CHANNELS:
            price = _num(getattr(r, pf))
            if price is None:
                continue
            # Each channel states out-of-stock in its own status column (the
            # own site in its verdict); compare leaves those out of cheapest.
            status = str(getattr(r, pf.replace("_price", "_status"), "") or "")
            if pf == "own_site_price":
                status = str(r.own_site_match or "")
            if pf == "other_price" and r.other_site and (
                    "%s: out of stock" % r.other_site) in str(r.note or ""):
                # The other marketplace (Hopscotch) has no status column;
                # compare says it in the note. Hopscotch's sizes read as
                # sold out only since 28 Sep 2026, which is when this showed.
                status = "OUT OF STOCK"
            if "OUT OF STOCK" in status.upper():
                continue
            if pf == "myntra_price" and status.startswith("UNCONFIRMED"):
                pass          # still a contender; `lowest_confirmed` says so
            contenders[label] = price
        stated = _num(r.lowest_price)
        if not contenders:
            if stated is not None:
                add(FAIL, "lowest_from_nothing",
                    "a lowest price of %s with no channel priced" % stated,
                    r.zoddle_barcode)
            continue
        want = min(contenders.values())
        if stated is None:
            if r.lowest_source:
                add(FAIL, "lowest_missing",
                    "lowest_source is %r but there is no lowest price"
                    % r.lowest_source, r.zoddle_barcode)
            continue
        if abs(stated - want) > 0.01:
            add(FAIL, "lowest_wrong",
                "lowest price is %s; recomputing gives %s from %s"
                % (stated, want, ", ".join("%s=%s" % kv
                                           for kv in sorted(contenders.items()))),
                r.zoddle_barcode)

    # -- 7. MRP corroboration: the strongest single signal that matching worked
    for r in rows:
        mrp = _num(r.zoddle_mrp)
        if not mrp:
            continue
        for _pf, lf, label in CHANNELS:
            if not lf:
                continue
            lst = _listing_for(fetched, getattr(r, lf, ""))
            listed = _num(getattr(lst, "listed_mrp", None)) if lst else None
            if listed is None:
                continue
            if abs(listed - mrp) <= 0.01:
                stats["mrp_agree"] += 1
                stats["mrp_agree_" + label] += 1
            else:
                stats["mrp_disagree"] += 1
                stats["mrp_disagree_" + label] += 1
                add(NOTE, "mrp_disagrees",
                    "%s lists an MRP of %s against the file's %s -- worth "
                    "checking the match" % (label, listed, mrp),
                    r.zoddle_barcode)

    stats["rows"] = len(rows)
    stats["fail"] = sum(1 for f in findings if f.level == FAIL)
    stats["warn"] = sum(1 for f in findings if f.level == WARN)
    stats["note"] = sum(1 for f in findings if f.level == NOTE)
    return findings, dict(stats)


def summary(findings, stats):
    """What the page shows: a verdict, and the accuracy signal behind it.

    The MRP figure is reported PER CHANNEL and not as one number, because a
    single rate is actively misleading here. Measured on a clean Peekaboo run:
    4 of 41 overall, which reads like a disaster and is not one -- every
    disagreement was Myntra quoting its own higher nominal MRP, which is the
    documented behaviour this project has always assumed. The handoff's 73/1
    came from brands that do not reprice per channel.

    So: agreement on the brand's OWN storefront is evidence the match is
    right. Agreement on a marketplace is a bonus, and disagreement there is
    close to meaningless on its own.
    """
    agree, disagree = stats.get("mrp_agree", 0), stats.get("mrp_disagree", 0)
    checked = agree + disagree
    per = {}
    for _pf, lf, label in CHANNELS:
        if not lf:
            continue
        a = stats.get("mrp_agree_" + label, 0)
        d = stats.get("mrp_disagree_" + label, 0)
        if a + d:
            per[label] = {"agree": a, "checked": a + d,
                          "rate": a / float(a + d)}
    return {
        "ok": stats.get("fail", 0) == 0,
        "fail": stats.get("fail", 0),
        "warn": stats.get("warn", 0),
        "note": stats.get("note", 0),
        "mrp_agree": agree,
        "mrp_disagree": disagree,
        "mrp_checked": checked,
        "mrp_rate": (agree / float(checked)) if checked else None,
        "mrp_by_channel": per,
        "findings": [f._asdict() for f in findings[:200]],
    }
