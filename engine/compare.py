"""Turn one input file into a price comparison. Stateless -- nothing is stored.

    rows, summary = run(records, progress=None)

`records` come from linkfile.parse_bytes (or a saved brand, via catalogue).
Nothing is stored here: the comparison is generated and handed back.

**One row per barcode**, with every channel's price side by side in that row --
Zoddle, the brand's own website, Myntra, FirstCry, Amazon, Flipkart and any
other marketplace the file names. `lowest_source` names whichever is cheapest.

One fetch per unique URL, not per row: a Shopify product JSON already holds
every variant, so a 49-row Peekaboo file is 11 requests, not 49. Every price
comes from a link in the file -- nothing is searched for.

A row is produced for every barcode whether or not any price came back. A
blank with a stated reason is a result; a missing row is not.
"""
import collections, re, threading, urllib.parse

import sites, audit

MARKETPLACE_HOSTS = ("myntra", "firstcry", "amazon", "flipkart", "meesho",
                     "tatacliq", "hopscotch")

# How a FirstCry price is read, for the page. Until 23 Sep 2026 this said the
# channel could not be priced at all; that was measured on discontinued
# products, whose page really is an empty shell. A live product page carries
# FirstCry's own MRP and discount for every size -- see sites.firstcry_listing.
FIRSTCRY = ("FirstCry is read from the product page the intake file names. "
            "That page lists every size of the style with FirstCry's own MRP "
            "and discount, and the selling price is the one computed from "
            "both -- checked against FirstCry's listing pages to the paisa. "
            "Each size is priced separately, so a size FirstCry does not list "
            "is left blank rather than given its neighbour's price. "
            "Discontinued products are blank, and say so.")

# The first seven columns and the price/verdict names are the ones already in
# use in Skipzy\Inbound\With Barcodes\*_price_comparison.csv. Everything after
# them is what that shape could not say: which channel, which is cheapest, and
# why a price is missing.
CSV_COLUMNS = [
    "zoddle_barcode", "design_code", "design_name", "brand", "size",
    "zoddle_mrp", "zoddle_price",
    "own_site", "own_site_price", "own_site_match", "own_site_diff",
    "myntra_price", "myntra_status", "myntra_product",
    "firstcry_price", "firstcry_status",
    "amazon_price", "amazon_status", "flipkart_price", "flipkart_status",
    "other_site", "other_price",
    "lowest_price", "lowest_source", "lowest_confirmed", "zoddle_vs_lowest",
    "resolved_by", "note",
    "own_site_link", "myntra_link", "firstcry_link", "amazon_link",
    "flipkart_link",
    "zoddle_image", "retailer_image",
]

Row = collections.namedtuple("Row", CSV_COLUMNS)


# Labels that carry no identity, so they are skipped when asking whether a
# host belongs to a brand: "www.hopscotch.in" is Hopscotch's, and so is
# "hopscotch.myshopify.com".
_HOST_NOISE = {"www", "com", "in", "co", "net", "org", "shop", "store",
               "myshopify"}


def owns_host(brand, host):
    """Is this host the brand's own storefront rather than a marketplace?"""
    b = re.sub(r"[^a-z0-9]", "", (brand or "").lower())
    if not b:
        return False
    for label in (host or "").lower().split("."):
        if label in _HOST_NOISE:
            continue
        if re.sub(r"[^a-z0-9]", "", label) == b:
            return True
    return False


def channel_of(url, brand=None):
    """'own site', 'myntra', 'firstcry', or another marketplace's name.

    The brand decides, because one host can be both. hopscotch.in is a
    marketplace for most of this corpus -- someone else's brand sold there --
    but the Hopscotch intake is 66 rows of brand_name "Hopscotch" whose
    product pages are on hopscotch.in, and there it is the brand's own
    storefront. Matching the host list first left own_site_price blank on
    every one of those rows while the price it had successfully read sat in
    the other_price column instead.
    """
    host = urllib.parse.urlsplit(url).netloc.lower()
    if owns_host(brand, host):
        return "own site"
    for m in MARKETPLACE_HOSTS:
        if m in host:
            return m
    return "own site"


def canonical_url(url):
    """One identity per product page, for de-duplication only.

    napchief.com URLs arrive both bare and carrying Shopify's search tracking
    (`?_pos=1&_sid=...&_ss=r`), and 5 designs hold both spellings of the same
    page. They are one product -- shopify_listing already drops the query
    before fetching -- so without this they were fetched twice and then read
    as two rival own-site URLs for one design.
    """
    s = urllib.parse.urlsplit((url or "").strip())
    return urllib.parse.urlunsplit(
        (s.scheme.lower(), s.netloc.lower(), s.path.rstrip("/"), "", ""))


# Hosts that serve a brand's pictures rather than its shop. They carry the
# brand's own name -- static.hopscotch.in really is Hopscotch's -- so
# owns_host says yes and own-site discovery then asks an image CDN for a
# product catalogue it cannot have. Measured on the Hopscotch run: one
# wasted request to static.hopscotch.in/products.json, every time.
_CDN_LABELS = {"static", "cdn", "res", "assets", "img", "images", "media",
               "cloudinary", "akamaized", "cloudfront"}


def is_cdn_host(host):
    """Is this hostname an image CDN rather than a storefront?"""
    return any(label in _CDN_LABELS
               for label in (host or "").lower().split("."))


def own_host_for(record):
    """The brand's own storefront host, from any URL the row carries.

    Only a host the brand demonstrably owns is accepted. The row's images are
    the evidence: Googo Gaaga's rows name no product page at all, but their
    photographs are served from googogaaga.com, which owns_host ties to the
    brand. A supplier's CDN or a re-host is never guessed at, because a wrong
    storefront would price a different company's garment -- and a picture
    host is never a storefront however well its name matches.
    """
    for img in record.get("images") or []:
        host = (img.get("host") or "").lower()
        if host and not is_cdn_host(host) and owns_host(record.get("brand"), host):
            return host
    return ""


def channels_for(record):
    """Every page URL on a design, as (channel, url), de-duplicated.

    image_url_3 is usually the brand's own product page and image_url_2 is
    sometimes a Google Drive link; only real pages on real hosts are channels.
    """
    out, seen = [], set()
    # The input file names each link's marketplace in its column heading, and
    # linkfile has already checked the link sits on that marketplace. That
    # label wins over guessing a channel from the host.
    if record.get("links") is not None:
        for channel, url in record["links"]:
            key = (channel, canonical_url(url))
            if key not in seen:
                seen.add(key)
                out.append((channel, url))
        return out
    for img in record.get("images") or []:
        host = (img.get("host") or "").lower()
        if not img.get("is_page") or not host:
            continue
        if "cloudinary" in host or "google" in host:
            continue          # a re-host or a Drive link, not a storefront
        url = img["url"]
        key = canonical_url(url)
        if key in seen:
            continue          # image_url_1 and _2 often repeat
        seen.add(key)
        out.append((channel_of(url, record.get("brand")), url))
    return out


def zoddle_image(record):
    """The intake photograph: the first URL that is really an image."""
    for im in record.get("images") or []:
        if not im.get("is_page"):
            return im["url"]
    return ""


_PACK = re.compile(r"\b(?:pack|set|combo)\s*(?:of|-)?\s*(\d+)\b", re.I)
# The count also comes first: '2 Pcs Interlock Joggers', '3-Piece Kurta Set'.
# Missing this shape is why orangesugar.in's 2-piece combo listing passed the
# like-for-like guard and was reported as the price of a single jogger.
_PACK_N = re.compile(r"\b(\d+)\s*-?\s*(?:pcs?|pieces?|pack)\b", re.I)


def pack_size(name):
    """'Pack Of 3 Typography Printed T-shirts' -> 3. None when not stated.

    Both orders count: 'Pack Of 5 Co-ords' and '2 Pcs Interlock Joggers'.
    """
    for pat in (_PACK, _PACK_N):
        m = pat.search(name or "")
        if m:
            n = int(m.group(1))
            if 2 <= n <= 24:
                return n
    return None


def like_for_like(record, product_name):
    """Warn when the two sides are not the same quantity of garment.

    A Googo Gaaga design at Zoddle's 609 against a Myntra "Pack Of 3" at 1400
    is not a price advantage, it is a different product. The price is real, so
    it is still reported -- but no cheapest is crowned from it.

    The design's *own name* is asked first, and only then units_in_the_piece.
    Zwende's "Handmade Krishna & Evil Eye Kids Rakhi ... | Set Of 2" carries
    units_in_the_piece = 1 while both sides plainly describe the same set of
    two; trusting the field over the name reported a same-product match as a
    quantity mismatch. Where the name says nothing, the field still decides.
    """
    theirs = pack_size(product_name)
    if not theirs:
        return ""
    ours = pack_size(record.get("name")) or record.get("units_in_piece") or 1
    if ours != theirs:
        return ("retailer listing is a pack of %d but this barcode is %d "
                "piece(s) -- not a like-for-like comparison" % (theirs, ours))
    return ""


def _verdict(zoddle_price, other):
    """MATCH / MISMATCH / '' plus the signed difference.

    Difference is the retailer's price minus Zoddle's, so a positive number
    means the retailer is dearer and Zoddle is the cheaper of the two.
    """
    if zoddle_price is None or other is None:
        return "", None
    diff = round(other - zoddle_price, 2)
    return ("MATCH" if abs(diff) < 0.005 else "MISMATCH"), diff


# A link in the file IS the listing (Abhisekh, 24 Sep 2026): photographs and
# page titles are not compared against the design, and the file gives one
# link per barcode per marketplace. What is still checked costs no request
# and is not a judgement of the link: a pack-size mismatch ("Pack of 2"
# against a single piece) and one page named by two designs.


def corroborate(record, quote):
    """Is the page's price comparable with this design's -- same pack size?

    Design 435 (Interlock Joggers, 1 piece) was once linked to a two-garment
    combo at a combo price. Returns (ok, problems). A failure does not mean
    the price is unreal; it means it is not a like-for-like price.
    The page's title is no longer compared with the design name: the file's
    link is the listing (textmatch.py removed, 24 Sep 2026).
    """
    warn = like_for_like(record, quote.product_name or "")
    problems = [warn] if warn else []
    return (not problems), problems


def _resolve_channel(record, quotes):
    """(url, quote_or_None, note) for one channel.

    The file gives one link per barcode per marketplace, so this is that
    link's quote. Should a file ever carry two, the first is used -- the
    order the file gives them in.
    """
    if not quotes:
        return None, None, ""
    return quotes[0][0], quotes[0][1], ""


def _fetch_all(urls, fetched, progress, step, total):
    """Read every URL into `fetched`: one queue per site, the sites side by
    side. Each site's pages still go one at a time at that site's own pace
    (sites.py enforces it); what changes is that a slow site -- Amazon,
    paced slowly so it does not show its "continue shopping" check --
    no longer holds up every other site's pages behind it. Returns the
    new step count."""
    queues = collections.OrderedDict()
    for url in urls:
        queues.setdefault(urllib.parse.urlsplit(url).netloc.lower(), []).append(url)
    lock = threading.Lock()
    state = {"step": step}

    def work(host, todo):
        for url in todo:
            with lock:
                if progress:
                    progress(state["step"], total, host)
            got = sites.fetch_listing(url)
            with lock:
                fetched[url] = got
                state["step"] += 1

    threads = [threading.Thread(target=work, args=item, daemon=True)
               for item in queues.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return state["step"]


def run(records, progress=None, fetched=None):
    """Price every barcode against every channel the file names. One row each.

    `progress(done, total, label)` is called as each page is fetched, so a
    long file reports where it is instead of appearing to hang.

    `fetched` is a dict of pages already read ({url: Listing}); it is filled
    in place. Given the dict from an earlier run, only the pages that failed
    for a passing reason (sites.retry_reason) are fetched again -- the
    page's Refetch button. `summary["retry"]` lists what still failed.

    **Polling only.** This project prices links the intake file already
    carries. It never searches a catalogue and never decides which listing a
    design is -- that is the parent project's job, and the absence is the
    point: every price here is traceable to a URL a person put in the file.
    """
    sites.new_run()
    urls = collections.OrderedDict()
    claimed = collections.defaultdict(set)
    for rec in records:
        for _, url in channels_for(rec):
            urls[url] = True
            claimed[canonical_url(url)].add(rec["design_code"])
    # One product page claimed by several designs cannot be that design's
    # price for all of them. bornplus.co.in/...pack-of-5-co-ords is named by
    # three separate designs; at most one of them can be right.
    shared_urls = {u for u, designs in claimed.items() if len(designs) > 1}
    # One fetch per unique URL is the whole cost of a polling run, so the
    # progress bar is simply how many of them are done.
    fetched = {} if fetched is None else fetched
    todo = [u for u in urls
            if u not in fetched or sites.retry_reason(fetched[u])]
    total = len(todo)
    step = _fetch_all(todo, fetched, progress, 0, total)

    # Amazon and Flipkart give each size its own page. Where the file's link
    # is another size of the same listing, that listing's page for this
    # barcode's size is read too -- its own size selector, not a search.
    # AJ Dezines 489's Amazon link is the 3-4Y; its 12-18M row used to be
    # given the 3-4Y price.
    more = collections.OrderedDict()
    for rec in records:
        for _, url in channels_for(rec):
            sib = _sibling(rec, fetched[url])
            if sib and (sib not in fetched
                        or sites.retry_reason(fetched[sib])):
                more[sib] = True
    total += len(more)
    step = _fetch_all(list(more), fetched, progress, step, total)

    # Nothing else happens. A polling run is fetches and nothing more --
    # no catalogue walk, no ranking, no candidate to confirm. That is the
    # whole difference between this project and its parent.

    if progress:
        progress(total, total, "done")
    rows = [_row_for(rec, fetched, shared_urls) for rec in records]
    summary = summarise(rows, fetched)
    # Every run is audited, because an audit that has to be remembered is one
    # that stops being run. It is pure and costs no request -- see audit.py.
    # It never suppresses the comparison: a FAIL is loud, not silent, because
    # a page that vanishes tells Abhisekh less than one that says what is
    # wrong with it.
    findings, astats = audit.check(records, rows, fetched)
    summary["audit"] = audit.summary(findings, astats)
    # The pages this run actually used that failed for a passing reason.
    used = collections.OrderedDict()
    for rec in records:
        for _, url in channels_for(rec):
            sib = _sibling(rec, fetched[url])
            used[sib if sib and sib in fetched else url] = True
    summary["retry"] = [(u, sites.retry_reason(fetched[u])) for u in used
                        if sites.retry_reason(fetched[u])]
    return rows, summary


def _sizes(rec):
    return [rec.get("brand_size"), rec.get("size"), rec.get("zoddle_size")]


def _sibling(rec, lst):
    """This listing's page for the barcode's size, if the link is another
    size's page (see sites.sibling_for)."""
    return sites.sibling_for(lst, _sizes(rec), rec.get("color"))


def _row_for(rec, fetched, shared_urls=frozenset()):
    """Assemble one barcode's row across every channel."""
    zp = rec.get("zoddle_price")
    sizes = _sizes(rec)
    notes, resolved = [], ""

    own_site = own_link = ""
    own_price = own_match = ""
    own_diff = None
    myn_price = myn_link = myn_product = myn_status = ""
    fc_price = fc_link = fc_status = ""
    # Amazon and Flipkart used to share other_price, and on a design linking
    # both, one silently overwrote the other: Vastramay 447/466 lost Amazon's
    # 959 to Flipkart's 1499 and reported Zoddle as cheapest. One column each.
    mkt = {m: {"price": "", "link": "", "status": ""}
           for m in ("amazon", "flipkart")}
    other_site = other_price = ""
    retailer_image = ""
    # Channels whose price is real but could not be tied to this design.
    unconfirmed = set()
    # Channels the retailer is not currently selling. A price you cannot pay
    # is not a price this design competes with -- see the contenders below.
    out_of_stock = set()

    # Every URL's quote is kept, grouped by channel. Collapsing them as they
    # arrive is what let a combo-pack listing displace a design's own page.
    per_channel = collections.OrderedDict()
    file_link = {}          # a size's own page -> the link the file gave
    for channel, url in channels_for(rec):
        lst = fetched[url]
        file_link[url] = url
        sib = _sibling(rec, lst)
        if sib and sib in fetched:
            # The size's own page: its price, and its link on the page.
            file_link[sib] = url
            url, lst = sib, fetched[sib]
        q = sites.pick(lst, sizes, colour=rec.get("color"))
        per_channel.setdefault(channel, []).append((url, q))

    # The thumbnail's second photograph: the brand's own page when there is
    # one (set below), otherwise the first marketplace page that has one.
    any_image = ""
    for channel, quotes in per_channel.items():
        url, q, why = _resolve_channel(rec, quotes)
        if why:
            notes.append(why)
        any_image = any_image or ((q.image if q else "") or "")

        if channel == "firstcry":
            fc_link = url
            if q and q.ok and q.sale_price is not None:
                fc_price, fc_status = q.sale_price, "from the intake file"
            else:
                fc_status = (q.note if q else why) or "no price"
        elif channel in mkt:
            mkt[channel]["link"] = url
            if q and q.ok and q.sale_price is not None:
                mkt[channel]["price"] = q.sale_price
                mkt[channel]["status"] = "from the input file"
            else:
                mkt[channel]["status"] = (q.note if q else why) or "no price"
        elif channel == "myntra":
            myn_link = url
            if q and q.ok and q.sale_price is not None:
                myn_price, myn_product = q.sale_price, q.product_name or ""
                myn_status = "from the intake file"
            else:
                myn_status = (q.note if q else why) or "no price"
        elif channel == "own site":
            own_link = url or ""
            own_site = (q.site if q else
                        urllib.parse.urlsplit(own_link).netloc)
            retailer_image = retailer_image or ((q.image if q else "") or "")
            if q and q.ok and q.sale_price is not None:
                own_price = q.sale_price
                own_match, own_diff = _verdict(zp, q.sale_price)
                resolved = q.resolved_by
            elif q is not None and q.note:
                notes.append("%s: %s" % (q.site, q.note))
        else:
            other_site = q.site if q else ""
            if q and q.ok and q.sale_price is not None:
                other_price = q.sale_price
            elif q is not None and q.note:
                notes.append("%s: %s" % (q.site, q.note))

        # A price we cannot tie to this design is reported, but never
        # confirmed. This is the check the intake URLs never faced.
        if q and q.ok and q.sale_price is not None:
            # Out of stock is stated, not inferred, and only where the
            # retailer says so outright: `in_stock` is None wherever
            # availability is unknown -- every Shopify store in this corpus
            # omits the key -- so a blank never reads as sold out.
            #
            # Myntra keeps the listing up and sets `discounted` equal to the
            # MRP once it stops selling, so an out-of-stock product still
            # answers with a number: 38593121, the confirmed listing for
            # NapChief 2149, quotes 3290 against Zoddle's 890. Reported as a
            # live price that makes Zoddle look 2,400 cheaper than a garment
            # nobody can buy.
            if q.in_stock == 0:
                out_of_stock.add(channel)
                notes.append("%s: out of stock -- listed at %s, but no size "
                             "is available to buy, so it is not compared"
                             % (q.site, q.sale_price))
            _, problems = corroborate(rec, q)
            if url and canonical_url(file_link.get(url, url)) in shared_urls:
                problems.append("this product page is named by more than one "
                                "design in the file, so it cannot be this "
                                "design's price for all of them")
            if problems:
                notes.extend(problems)
                unconfirmed.add(channel)

    # Nothing fills a blank in here. In the parent project a ranked
    # candidate could occupy an empty own-site or Myntra column, marked
    # UNVERIFIED; this project has no ranking, so an empty column means the
    # file named no page for that channel, or the page named gave no price.
    # The note says which. That is the whole contract of a polling run.

    # MATCH on a listing we could not tie to this design is the most
    # misleading thing this file can print: it reads as "our price is right"
    # when it means "some price on some page equals ours".
    if own_price != "" and "own site" in unconfirmed:
        own_match = "UNVERIFIED"

    # Say it where a reader looks first, not only in the note.
    if "myntra" in out_of_stock:
        myn_status = "OUT OF STOCK -- %s" % (myn_status or "listed")
    if "firstcry" in out_of_stock:
        fc_status = "OUT OF STOCK -- %s" % (fc_status or "listed")
    for m in mkt:
        if m in out_of_stock:
            mkt[m]["status"] = "OUT OF STOCK -- %s" % (mkt[m]["status"]
                                                       or "listed")
    if "own site" in out_of_stock and own_match not in ("NPL",):
        own_match = "OUT OF STOCK"

    # NPL -- no product link. Neither the intake file nor discovery named a
    # page on the brand's own store, so nothing was ever fetched for this row.
    # That is a different thing from a page that refused or went missing, and
    # the report has to distinguish them: an empty verdict beside an empty
    # price reads as a failed read, when the truth is that there was no input
    # to read. Nothing here can be fixed by the engine -- it is fixed in the
    # intake file.
    if not own_link:
        own_match = "NPL"

    # -- which source is cheapest -----------------------------------------
    # Zoddle counts as a source: the question is which channel sells this
    # garment for least, and "us" is a valid answer.
    # A channel that is out of stock keeps its price in its own column -- it
    # is a real listed price and worth seeing -- but it never competes for
    # cheapest, because it cannot be bought.
    contenders = [("zoddle", zp, True)]
    if own_price != "" and "own site" not in out_of_stock:
        contenders.append((own_site or "own site", own_price,
                           "own site" not in unconfirmed))
    if myn_price != "" and "myntra" not in out_of_stock:
        contenders.append(("myntra", myn_price,
                           "myntra" not in unconfirmed
                           and not str(myn_status).startswith("UNCONFIRMED")))
    if fc_price != "" and "firstcry" not in out_of_stock:
        contenders.append(("firstcry", fc_price,
                           "firstcry" not in unconfirmed))
    for m in mkt:
        if mkt[m]["price"] != "" and m not in out_of_stock:
            contenders.append((m, mkt[m]["price"], m not in unconfirmed))
    named = {"own site", "myntra", "firstcry"} | set(mkt)
    if other_price != "" and not (out_of_stock - named):
        contenders.append((other_site or "other", other_price,
                           not (unconfirmed - named)))

    priced = [(name, float(p), conf) for name, p, conf in contenders
              if p not in (None, "")]
    lowest_price = lowest_source = lowest_confirmed = ""
    vs_lowest = None
    if priced:
        low = min(p for _, p, _ in priced)
        # A tie is not a winner: name every source sharing the low price, so
        # "cheapest" never implies an advantage that does not exist.
        winners = [(n, c) for n, p, c in priced if abs(p - low) < 0.005]
        lowest_price = low
        lowest_source = " / ".join(n for n, _ in winners)
        lowest_confirmed = "yes" if all(c for _, c in winners) else "no"
        if zp is not None:
            vs_lowest = round(float(zp) - low, 2)
        if any("like-for-like" in n for n in notes):
            lowest_confirmed = "no"

    return Row(
        zoddle_barcode=rec["zoddle_barcode"], design_code=rec["design_code"],
        design_name=rec.get("name"), brand=rec.get("brand"),
        size=rec.get("size"), zoddle_mrp=rec.get("mrp"), zoddle_price=zp,
        own_site=own_site, own_site_price=own_price, own_site_match=own_match,
        own_site_diff=own_diff,
        myntra_price=myn_price, myntra_status=myn_status,
        myntra_product=myn_product,
        firstcry_price=fc_price, firstcry_status=fc_status,
        amazon_price=mkt["amazon"]["price"],
        amazon_status=mkt["amazon"]["status"],
        flipkart_price=mkt["flipkart"]["price"],
        flipkart_status=mkt["flipkart"]["status"],
        other_site=other_site, other_price=other_price,
        lowest_price=lowest_price, lowest_source=lowest_source,
        lowest_confirmed=lowest_confirmed, zoddle_vs_lowest=vs_lowest,
        resolved_by=resolved, note="; ".join(notes),
        own_site_link=own_link, myntra_link=myn_link, firstcry_link=fc_link,
        amazon_link=mkt["amazon"]["link"],
        flipkart_link=mkt["flipkart"]["link"],
        zoddle_image=zoddle_image(rec),
        retailer_image=retailer_image or any_image)


def summarise(rows, fetched):
    """Health first: a channel that stopped answering must say so up front."""
    per_site = collections.defaultdict(lambda: {"rows": 0, "priced": 0})
    for r in rows:
        for site, price in ((r.own_site, r.own_site_price),
                            ("myntra.com" if r.myntra_status else "",
                             r.myntra_price),
                            ("firstcry.com" if r.firstcry_link else "",
                             r.firstcry_price),
                            ("amazon.in" if r.amazon_link else "",
                             r.amazon_price),
                            ("flipkart.com" if r.flipkart_link else "",
                             r.flipkart_price),
                            (r.other_site, r.other_price)):
            if not site:
                continue
            per_site[site]["rows"] += 1
            per_site[site]["priced"] += price != ""

    sites_health = []
    for site, d in sorted(per_site.items(), key=lambda kv: -kv[1]["rows"]):
        rate = (d["priced"] / d["rows"]) if d["rows"] else 0.0
        sites_health.append({"site": site, "rows": d["rows"],
                             "priced": d["priced"], "rate": rate,
                             "healthy": rate >= 0.8})

    reasons = collections.Counter(
        n for r in rows for n in (r.note.split("; ") if r.note else []) if n)
    # A row counts as priced when any RETAILER answered for it. Zoddle's
    # own price is an input, not a result, so a row nobody else priced is
    # not a priced row -- counting it would report a run that reached no
    # retailer at all as fully priced.
    priced = sum(1 for r in rows
                 if r.own_site_price != "" or r.myntra_price != ""
                 or r.firstcry_price != "" or r.amazon_price != ""
                 or r.flipkart_price != "" or r.other_price != "")

    return {
        "rows": len(rows),
        "priced": priced,
        "barcodes": len({r.zoddle_barcode for r in rows}),
        "designs": len({r.design_code for r in rows}),
        "urls_fetched": len(fetched),
        "own_priced": sum(1 for r in rows if r.own_site_price != ""),
        "myntra_priced": sum(1 for r in rows if r.myntra_price != ""),
        "firstcry_priced": sum(1 for r in rows if r.firstcry_price != ""),
        "amazon_priced": sum(1 for r in rows if r.amazon_price != ""),
        "flipkart_priced": sum(1 for r in rows if r.flipkart_price != ""),
        "no_price_anywhere": sum(1 for r in rows if not r.lowest_source),
        "matches": sum(1 for r in rows if r.own_site_match == "MATCH"),
        "mismatches": sum(1 for r in rows if r.own_site_match == "MISMATCH"),
        "unverified": sum(1 for r in rows if r.own_site_match == "UNVERIFIED"),
        "no_product_link": sum(1 for r in rows if r.own_site_match == "NPL"),
        "check_link": sum(1 for r in rows if r.own_site_match == "CHECK LINK"),
        "zoddle_lowest": sum(1 for r in rows if r.lowest_source == "zoddle"),
        "undercut": sum(1 for r in rows if r.lowest_source
                        and "zoddle" not in r.lowest_source),
        "cheapest_by_source": collections.Counter(
            r.lowest_source for r in rows if r.lowest_source).most_common(),
        "resolved_by": dict(collections.Counter(
            r.resolved_by for r in rows if r.resolved_by)),
        "sites": sites_health,
        "unhealthy": [s for s in sites_health if not s["healthy"]],
        "reasons": reasons.most_common(12),

        "firstcry": FIRSTCRY,
    }


def to_csv(rows):
    """The comparison as CSV text, in the column order above."""
    import csv, io
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


def main(argv=None):
    """Same polling run without the browser.

        python engine/compare.py <input.csv|.xlsx> [-o out.csv]

    The input is the link file the page takes -- see linkfile.py.
    """
    import argparse, pathlib, sys as _sys
    import linkfile

    ap = argparse.ArgumentParser(description="Compare one input file")
    ap.add_argument("file")
    ap.add_argument("-o", "--out", help="write the comparison CSV here")
    a = ap.parse_args(argv)

    p = pathlib.Path(a.file)
    records, issues, meta, _ = linkfile.parse_bytes(p.read_bytes(), p.name)
    if not records:
        first = next((i for i in issues if i.severity == "reject"), None)
        print("not a usable input file: %s"
              % (first.detail if first else "no rows"))
        return 1
    print("%s -- %d rows, %d designs, %s"
          % (p.name, len(records), len({r["design_code"] for r in records}),
             ", ".join(meta.get("brands", []))))

    def progress(done, tot, where):
        _sys.stderr.write("\r  fetching %d/%d %-34s" % (done, tot, where))
        if done == tot:
            _sys.stderr.write("\n")

    rows, s = run(records, progress=progress)
    au = s.get("audit") or {}
    print("  %d pages fetched; priced of %d rows -- own site %d, myntra %d, "
          "firstcry %d, amazon %d, flipkart %d"
          % (s["urls_fetched"], s["rows"], s["own_priced"], s["myntra_priced"],
             s["firstcry_priced"], s["amazon_priced"], s["flipkart_priced"]))
    print("  audit: %s (%d fail, %d warn)"
          % ("PASSED" if au.get("ok") else "FAILED",
             au.get("fail", 0), au.get("warn", 0)))
    print("  cheapest source: "
          + ", ".join("%s x%d" % kv for kv in s["cheapest_by_source"]))
    for h in s["sites"]:
        print("    %-26s %3d rows %3d priced  %3.0f%%%s"
              % (h["site"], h["rows"], h["priced"], h["rate"] * 100,
                 "" if h["healthy"] else "   LOW"))

    out = a.out or str(p.with_name(p.stem + "_live_prices.csv"))
    pathlib.Path(out).write_text(to_csv(rows), encoding="utf-8", newline="")
    print("  wrote %s" % out)
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    _s.exit(main())
