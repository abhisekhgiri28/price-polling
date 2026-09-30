"""Site adapters: a product URL in, today's selling price out.

One contract for every site. Plain requests first; a headless browser
(`browser.py`) only as the last resort for a page that shows its price
only once a browser has run it (see `_in_browser`).

    lst = fetch_listing(url)                  # one network request
    q   = pick(lst, ["1-2y", "12-18M"])       # pure, repeat per barcode

Fetching and size-picking are separate on purpose. A Shopify product JSON
already contains every variant, so a 49-row intake file needs one request, not
49. `fetch(url, size)` does both and exists for tests and one-offs.

Rules carried over from the handoff, each of which was a wrong number once:

  * An adapter returns ok=False rather than a plausible number. A blank beats
    a guess, and a blank is visible in the report.
  * Absent means unknown, not unavailable. Shopify's per-product JSON has no
    `available` key at all -- verified absent on all 21 stores in this
    corpus -- so `any(v.get("available"))` would stamp every listing
    out-of-stock. in_stock stays None unless the key is really there.
  * Filter prices by value, not truthiness. Shopify writes compare_at_price as
    "0.00" (peekaabookids.com), "" (kidsty.in), or a copy of the price
    (napchief.com) to mean "no discount". All three mean nothing and only one
    is falsy in Python.
  * MRP is never taken from a retailer. Retailers set a different nominal MRP
    per channel; only the selling price is comparable. A retailer's MRP is
    kept as `listed_mrp` for information and never compared.
  * If two different prices both answer to the requested size, report no
    price. That is the FirstCry per-size lesson applied generally.
  * Pace requests: one per host per second, enforced here rather than left to
    each caller. An unpaced ad-hoc checker earned 429s from two stores in the
    prior build, and that is the one trap the handoff left unfixed.
  * A refusal is not an answer. 429 and 5xx are retried with backoff, and a
    host that answers 429 has its pace widened for the rest of the run --
    hopscotch.in refused 11 of 57 product pages at one request per second and
    every one of them priced when asked more slowly. Recording that as a
    blank would say "this brand does not sell this design" when it means "we
    knocked too fast".
  * A storefront that is not Shopify is still a storefront. The Shopify JSON
    route is tried first, then the page's own schema.org Product, so a brand
    is priced on the evidence its shop publishes rather than on the platform
    it happens to run.

Robots is honoured per host, and a disallowed path is never fetched.
"""
import re, json, time, threading, collections, html, email.utils, datetime
import urllib.parse, urllib.robotparser

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# What any browser sends. This is a well-formed request, not a disguised
# one: the User-Agent is unchanged and nothing here claims to be a person.
#
# It is not cosmetic. Flipkart answers the two-header set with 403 on BOTH
# robots.txt and the PDP -- measured again 21 Sep 2026, alternating the two
# sets three times each, 403 every time on the thin set and 200 every time
# on this one. With robots.txt unreadable the engine could not even see that
# /p/ is allowed, so it recorded "robots.txt unavailable" and then a 403,
# and eight designs read as unpriceable when they were merely badly asked
# for. Brotli is safe to advertise: brotlicffi is installed (requirements).
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Connection": "keep-alive",
}
TIMEOUT = 25
PACE_SECONDS = 1.0

# Sites this engine does not price. Measured, not assumed -- first on
# 3 Sep 2026, re-measured 9 Sep 2026.
#
# Re-measuring mattered: two of these entries had gone stale and were steering
# decisions wrongly. Amazon was recorded as CAPTCHA-only and Flipkart as not
# serving robots.txt; on 9 Sep neither was true. A host stays in this table
# until an adapter can read it *honestly*, so "no adapter yet" and "cannot be
# read at all" both live here -- the note says which, because they call for
# completely different work.
UNSUPPORTED = {
    # FirstCry was here until 23 Sep 2026 and is now read by
    # `firstcry_listing`. Every note that used to sit here -- "124KB, empty
    # title, a client-rendered shell, no price" -- describes FirstCry's
    # DISCONTINUED-product page, which is exactly that. A live product page
    # is ~138KB and carries CurrentProductDetailJSON, one entry per size
    # with that size's MRP and discount. See the adapter.

    # Amazon was here until 23 Sep 2026 and is now read by `amazon_listing`.
    # The 3 Sep "CAPTCHA only" note was already wrong by 9 Sep; re-measured on
    # all 11 corpus links on 23 Sep, none served a CAPTCHA and 7 priced, the
    # other 4 being genuinely "Currently unavailable". See that adapter for
    # why every figure is anchored to the element that owns it.

    # Flipkart was here until 21 Sep 2026 and is now read by
    # `flipkart_listing`. It needed no adapter of its own -- only the
    # complete browser header set above. See that note.

    "meesho.com":   "returns HTTP 403",
    "tatacliq.com": "PDP is a client-rendered shell with no price in HTML",
}

# One offer on a page: a set of normalised size labels and its price.
Variant = collections.namedtuple("Variant", "labels price compare_at available image")

# One fetched page.
#
# `image` is the hero; `images` is EVERY view the page publishes, and the
# difference matters. Verifying that a link really is this design's page is
# done by photograph, and a brand's own shoot runs to several frames: design
# 2462's intake photograph scores 72.1 against its page's hero and 99.3
# against the next frame along -- same shoot, adjacent file numbers. Checking
# the hero alone would have called a correct link a mismatch.
#
# `sizes` and `colour` are the size and colourway the linked page itself is
# for, where the page says (Amazon and Flipkart give every size its own
# page); `siblings` are the same listing's other sizes as (size, colour, url),
# read from the page's own size selector. See `sibling_for`.
Listing = collections.namedtuple(
    "Listing", "ok site platform url product_name article_type listed_mrp "
               "per_size http_status note variants image images sizes colour "
               "siblings")

# One design-size's answer, derived from a Listing without touching the network.
Quote = collections.namedtuple(
    "Quote", "ok site platform url product_name sale_price listed_mrp "
             "in_stock size_matched article_type http_status note image "
             "resolved_by")


def _listing(url, site, platform, ok=False, note="", variants=(), **kw):
    f = {"product_name": None, "article_type": None, "listed_mrp": None,
         "per_size": True, "http_status": None, "image": None, "images": (),
         "sizes": frozenset(), "colour": "", "siblings": ()}
    f.update(kw)
    f["images"] = tuple(dict.fromkeys(u for u in (f.get("images") or ()) if u))
    f["sizes"] = frozenset(f.get("sizes") or ())
    f["siblings"] = tuple(f.get("siblings") or ())
    return Listing(ok=ok, site=site, platform=platform, url=url, note=note,
                   variants=list(variants), **f)


# ------------------------------------------------------------------ pacing --
# A host that answers 429 is telling us the pace is wrong, so the pace for
# that host widens for the rest of the run. One request per second is not
# universally safe: hopscotch.in refused 11 of 57 product pages at that rate,
# and every one of those URLs answered with a price when asked more slowly.
MAX_PACE_SECONDS = 8.0

_pace_lock = threading.Lock()
_last_hit = {}
_host_pace = {}


# A site that needs a slower pace from the FIRST request, not after it has
# refused us. Amazon shows its "continue shopping" check page to many
# requests in a row: at one a second, 35 pages of one run (26 Sep 2026);
# at one every 3 seconds, see AMAZON_PACE_SECONDS.
AMAZON_PACE_SECONDS = 3.0


def _base_pace(host):
    host = (host or "").lower()
    if host == "amazon.in" or host.endswith(".amazon.in"):
        return max(PACE_SECONDS, AMAZON_PACE_SECONDS)
    return PACE_SECONDS


def pace_for(host):
    with _pace_lock:
        return _host_pace.get(host, _base_pace(host))


def _slow_down(host):
    """Widen this host's gap after it refused us. Doubling, and it sticks."""
    with _pace_lock:
        cur = max(_host_pace.get(host, _base_pace(host)), _base_pace(host))
        _host_pace[host] = min(MAX_PACE_SECONDS, cur * 2.0)
        return _host_pace[host]


def _wait_turn(host):
    """One request per host per pace. Different hosts do not block."""
    while True:
        with _pace_lock:
            pace = _host_pace.get(host, _base_pace(host))
            now = time.monotonic()
            ready = _last_hit.get(host, 0.0) + pace
            if now >= ready:
                _last_hit[host] = now
                return
            gap = ready - now
        time.sleep(min(gap, pace))


# ----------------------------------------------------------------- fetching --
# "Ask again later" is not "there is no price here". Recording a rate limit
# as a blank is the same class of error as reporting a wrong number: the row
# reads as though the brand does not sell this design, when it means we
# knocked too fast. Every one of these is retried.
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 522, 524})
# A product that is genuinely gone. Retrying cannot change these, and naming
# the condition beats printing the number: a delisted product is an answer
# about the product, not a failure to reach the site.
GONE_STATUS = {404: "no such page on the retailer's site (HTTP 404)",
               410: "delisted by the retailer (HTTP 410)"}

RETRIES = 3                       # attempts after the first
BACKOFF = (3.0, 8.0, 20.0)        # seconds; a module constant so tests can zero it
MAX_RETRY_AFTER = 60.0            # never honour an absurd Retry-After


def _retry_after(resp):
    """The server's own Retry-After, in seconds, or None.

    Both spellings are allowed: a delay in seconds, or an HTTP date.
    """
    headers = getattr(resp, "headers", None) or {}
    try:
        val = headers.get("Retry-After")
    except Exception:
        return None
    if not val:
        return None
    try:
        return max(0.0, min(float(str(val).strip()), MAX_RETRY_AFTER))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(str(val))
        gap = (when - datetime.datetime.now(when.tzinfo)).total_seconds()
        return max(0.0, min(gap, MAX_RETRY_AFTER))
    except Exception:
        return None


def _strong_parts(path):
    """The parts of a path that name one product: with a digit (an id, an
    ASIN) or a hyphenated handle. "products", "p", "buy" name nothing."""
    path = re.sub(r"\.(?:json|js|html?)$", "", (path or "").lower())
    return [x for x in re.split(r"[/?&=]+", urllib.parse.unquote(path))
            if x and (re.search(r"\d", x) or (len(x) >= 5 and re.search(r"[-+_]", x)))]


def redirected_away(url, resp):
    """Did the site answer `url` with a DIFFERENT page? A product taken down
    is often sent on (301) to another product or to the home page, whose
    price then read as this product's (found 30 Sep 2026). Same page: the
    final address still carries the product's own id or handle."""
    final = getattr(resp, "url", None)
    if not isinstance(final, str) or not final:
        return False
    a, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(str(final))
    if a.path.rstrip("/") == b.path.rstrip("/"):
        return False
    if not b.path.strip("/") and a.path.strip("/"):
        return True                                  # sent to the home page
    mine = _strong_parts(a.path)
    there = urllib.parse.unquote(b.path.lower() + "?" + b.query.lower())
    return bool(mine) and not any(x in there for x in mine)


REDIRECTED_NOTE = "the site sent this link to another page (%s) -- the product has moved or is gone"


def _get(url, host=None, tries=None, headers=None, session=None,
         any_page=False):
    """One paced GET, retried while the answer means "ask again later".

    Returns (response, note). The response is handed back whenever the server
    answered at all, so a caller can still read its status; `note` is empty
    only on a 200. Nothing here raises.

    `headers` and `session` exist for one caller: Myntra's paginated gateway,
    which needs its own headers and the cookie a page visit sets. Routing it
    through here rather than around it is deliberate -- an enumeration is a
    hundred requests to one host, which is exactly the shape that earns a
    429, and this is where retry, backoff and pace widening live.
    """
    host = host or urllib.parse.urlsplit(url).netloc
    tries = (RETRIES + 1) if tries is None else tries
    getter = (session or requests).get
    sent = HEADERS if headers is None else headers
    resp, note = None, "no attempt made"
    for attempt in range(tries):
        _wait_turn(host)
        try:
            resp = getter(url, headers=sent, timeout=TIMEOUT)
        except Exception as ex:
            resp, note = None, "fetch failed (%s)" % type(ex).__name__
            if type(ex).__name__ == "TooManyRedirects":
                # A redirect loop is the address, not the moment: biba.in
                # loops on ".html.json", and four tries cost ~60s a page.
                return None, note
        else:
            code = getattr(resp, "status_code", None)
            if code == 200:
                if not any_page and redirected_away(url, resp):
                    return None, REDIRECTED_NOTE % getattr(resp, "url", "")
                return resp, ""
            if code in GONE_STATUS:
                return resp, GONE_STATUS[code]
            if code not in TRANSIENT_STATUS:
                return resp, "HTTP %s" % code
            note = "HTTP %s" % code
            if code == 429:
                _slow_down(host)
        if attempt < tries - 1:
            wait = _retry_after(resp)
            if wait is None:
                wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            if wait:
                time.sleep(wait)
    return resp, ("%s, still after %d attempts" % (note, tries))


# ------------------------------------------------------------------- robots --
_robots = {}
_robots_lock = threading.Lock()


ROBOTS_RETRY_SECONDS = 600     # an unreadable robots.txt is asked again


class _RobotRules:
    """robots.txt read the way Google and the RFC (9309) read it: `*` and
    `$` in paths, and the LONGEST matching rule wins (a tie goes to Allow).
    Python's robotparser supports neither and lets the first line win, so
    "Disallow: /products/*.json" allowed /products/x.json, and "Allow: /"
    above "Disallow: /products/" allowed /products/x (30 Sep 2026)."""

    def __init__(self, text, agent):
        groups, cur, in_agents = [], None, False
        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            k, v = (x.strip() for x in line.split(":", 1))
            k = k.lower()
            if k == "user-agent":
                if cur is None or not in_agents:
                    cur = {"agents": [], "rules": []}
                    groups.append(cur)
                cur["agents"].append(v.lower())
                in_agents = True
            elif k in ("allow", "disallow"):
                in_agents = False
                if cur is not None and v:
                    cur["rules"].append((k == "allow", v))
            else:
                in_agents = False
        me = agent.lower()
        mine = [g for g in groups if any(a and a != "*" and a in me
                                         for a in g["agents"])]
        pick = mine or [g for g in groups if "*" in g["agents"]]
        self.rules = [r for g in pick for r in g["rules"]]

    @staticmethod
    def _matches(pattern, path):
        end = pattern.endswith("$")
        rx = ".*".join(re.escape(x) for x in pattern.rstrip("$").split("*"))
        return re.match(rx + ("$" if end else ""), path) is not None

    def allows(self, url):
        parts = urllib.parse.urlsplit(url)
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        best = None
        for allow, pattern in self.rules:
            if self._matches(pattern, path):
                size = len(pattern)
                if best is None or size > best[0] or (size == best[0] and allow):
                    best = (size, allow)
        return True if best is None else best[1]


def robots_allows(url):
    """(allowed, reason). A missing robots.txt (4xx) allows everything; one
    that cannot be read (5xx, no answer) allows NOTHING for now and is asked
    again in ROBOTS_RETRY_SECONDS -- as Google treats it -- rather than
    being taken as permission for the rest of the app's life."""
    host = urllib.parse.urlsplit(url).netloc
    now = time.time()
    with _robots_lock:
        cached = _robots.get(host)
    if cached is None or (cached[2] is not None and now > cached[2]):
        rules, reason, until = None, "", None
        r, why = _get("https://%s/robots.txt" % host, host, any_page=True)
        code = getattr(r, "status_code", None)
        if code == 200:
            rules = _RobotRules(r.text, UA)
        elif code is not None and 400 <= code < 500:
            rules = _RobotRules("", UA)             # no robots.txt: all allowed
        else:
            reason = ("robots.txt could not be read (%s), so the page was not "
                      "fetched" % (why or "no response"))
            until = now + ROBOTS_RETRY_SECONDS
        cached = (rules, reason, until)
        with _robots_lock:
            _robots[host] = cached
    rules, reason, _until = cached
    if rules is None:
        return False, reason
    ok = rules.allows(url)
    return ok, ("" if ok else "disallowed by robots.txt")


# --------------------------------------------------------------- size logic --
def norm_size(s):
    """Compare '12-18M', '12 - 18 m' and '12-18 Months' as one token."""
    s = (s or "").strip().upper()
    if not s:
        return ""
    # Excel turns "3-4Y" into "3–4Y" as you type; every dash is a hyphen.
    s = re.sub("[\u2010-\u2015\u2212]", "-", s)
    for a, b in (("YEARS", "Y"), ("YEAR", "Y"), ("YRS", "Y"), ("YR", "Y"),
                 ("MONTHS", "M"), ("MONTH", "M"), ("MNTHS", "M"), ("MNTH", "M"),
                 ("MTHS", "M"), ("MOS", "M")):
        s = s.replace(a, b)
    s = re.sub(r"(?<=\d)\s*MO$", "M", s)             # "3-6 Mo"
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[^0-9A-Z\-/+]", "", s)
    s = re.sub(r"^(?:SIZE|AGE)(?=[0-9])", "", s)  # "Size 4" is 4 (suta.in)
    s = re.sub(r"(?<=\d)-(?=[YM]$)", "", s)       # "0-3-M" is 0-3M (kid1.co)
    # "2Y-3Y", "6M-12M" and "2TO3Y" are the same sizes as "2-3Y" and "6-12M".
    s = re.sub(r"^(\d+)TO(\d+)([YM])$", r"\1-\2\3", s)
    return re.sub(r"^(\d+)([YM])-(\d+)\2$", r"\1-\3\2", s)


# A size range inside a longer label. vastramay.com names its sizes
# "20 (2-3 Yrs)" -- a chest number, then the age -- which read as "202-3Y"
# and matched no row: 75 own-site rows were priced only because every size
# shared one price, and per-size stock was never read (found 26 Sep 2026).
_RANGE_IN = re.compile(r"\d+(?:\.\d+)?\s*(?:-|\u2013|to)\s*\d+(?:\.\d+)?\s*"
                       r"(?:years?|yrs?|y|months?|mths?|m)\b", re.I)


def _labels_of(*parts):
    """Variant labels arrive as '2-3Y / Green', 'Green / 2-3Y' or
    '20 (2-3 Yrs)'."""
    out = set()
    for p in parts:
        for piece in re.split(r"[/|,]", str(p or "")):
            inner = {norm_size(m.group(0)) for m in _RANGE_IN.finditer(piece)}
            # "16 (1Y)" -- a chest size, then the age in brackets
            # (thenesavu.com): the bracket is the size a row names.
            inner |= {norm_size(m.group(1))
                      for m in re.finditer(r"\(([^()]{1,15})\)", piece)
                      if re.search(r"\d", m.group(1))} - {""}
            n = norm_size(piece)
            # "20 (2-3 Yrs)" is 2-3Y; the run-together "202-3Y" is noise.
            out |= inner or ({n} if n else set())
            # "XL (14-15 Years)": the letter size outside the bracket is a
            # size a row may name too.
            outside = norm_size(re.sub(r"\([^()]*\)", "", piece))
            if inner and outside in _LETTER_SIZES:
                out.add(outside)
    return frozenset(out)


_LETTER_SIZES = {"XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL", "3XL",
                 "4XL", "5XL", "FREESIZE"}


def _months(label):
    """'1-2Y' -> (12, 24), '12-18M' -> (12, 18); None if not an age range."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)([YM])", label or "")
    if not m:
        return None
    k = 12.0 if m.group(3) == "Y" else 1.0
    return float(m.group(1)) * k, float(m.group(2)) * k


def _covering(labels, want):
    """The page's size labels that wholly contain the wanted age range.

    Brands and Zoddle cut ages differently: BownBee sells "1-2Y" where
    Zoddle has 12-18M and 18-24M, and "0-3M" where Zoddle has 1-3M. When
    exactly ONE page size covers the row's size, that is the size a shopper
    would buy; when none does, the page does not sell it.
    """
    w = _months(want)
    if not w:
        return set()
    out = set()
    for l in labels:
        r = _months(l)
        if r and l != want and r[0] <= w[0] and w[1] <= r[1]:
            out.add(l)
    return out


def wanted_sizes(want):
    """Accept one size or several, tried in order, de-duplicated.

    Intake carries three size columns in different vocabularies: `size` is
    Zoddle's ('12-18M'), `brand_size` is the brand's own ('1-2y'), and the
    storefront variant uses the brand's. Passing brand_size first is what
    makes a match possible at all -- matched sizes went from 12 to 18 of 23
    hosts when this was added.
    """
    if want is None:
        return []
    if isinstance(want, str):
        want = [want]
    out = []
    for s in want:
        n = norm_size(s)
        if n and n not in out:
            out.append(n)
    return out


def _num(x):
    """A price, or None. Zero is not a price -- see the "0.00" trap."""
    if x is None:
        return None
    if isinstance(x, bool):
        return None                    # True is not a price of 1
    if isinstance(x, (int, float)):
        v = float(x)
    else:
        t = str(x).strip().replace(",", "").replace("₹", "")
        if not t:
            return None
        try:
            v = float(t)
        except ValueError:
            return None
    return v if v > 0 and v == v and v != float("inf") else None


# ------------------------------------------------------------------ shopify --
# littlemuffet.com and aachho.co price in US dollars -- with or without an
# /en-us/ path -- and their "87.00" read as Rs 87 (found 26 Sep 2026). A
# price is compared only in rupees; any other currency is refused.
_CURRENCY_NOTE = "this site shows its prices in %s, not rupees -- not compared"


def _foreign_currency(codes):
    """The first currency code that is not INR, or ""."""
    for c in codes:
        c = str(c or "").strip().upper()
        if c and c not in ("INR", "RS", "\u20b9"):
            return c
    return ""


def shopify_listing(url):
    """21 of 23 brand storefronts in this corpus are Shopify. One adapter.

    `/products/<handle>.json` is the documented route and is robots-allowed.
    """
    site = urllib.parse.urlsplit(url).netloc
    clean = urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl()
    target = clean.rstrip("/") + ".json"

    allowed, why = robots_allows(target)
    if not allowed:
        return _listing(url, site, "shopify", note=why)
    r, why = _get(target)
    if r is None:
        return _listing(url, site, "shopify", note=why)
    if r.status_code != 200:
        return _listing(url, site, "shopify", http_status=r.status_code,
                        note=why)
    try:
        product = r.json()["product"]
    except Exception:
        product = None
    if not isinstance(product, dict) or not product:
        # {"product": null} too: not a product, so the page's own data is
        # read instead (it raised, and the fallback never ran).
        return _listing(url, site, "shopify", http_status=r.status_code,
                        note="response was not Shopify product JSON")
    cur = _foreign_currency(v.get("price_currency")
                            for v in product.get("variants") or [])
    if cur:
        return _listing(url, site, "shopify", http_status=r.status_code,
                        product_name=product.get("title"),
                        note=_CURRENCY_NOTE % cur)

    stock = _shopify_stock(clean)
    # A variant's own photograph is the only thing that separates two
    # colourways priced differently, so it is carried through.
    by_id = {im.get("id"): im.get("src") for im in product.get("images") or []}
    variants = []
    for v in product.get("variants") or []:
        variants.append(Variant(
            labels=_labels_of(v.get("title"), v.get("option1"),
                              v.get("option2"), v.get("option3")),
            price=_num(v.get("price")),
            compare_at=_num(v.get("compare_at_price")),
            # absent means unknown, and must not read as out-of-stock
            available=(v["available"] if "available" in v
                       else stock.get(v.get("id"))),
            image=by_id.get(v.get("image_id"))
            or (product.get("image") or {}).get("src")))   # "image": null (miniklub.in)
    if not variants:
        return _listing(url, site, "shopify", http_status=r.status_code,
                        product_name=product.get("title"), note="no variants")
    return _listing(url, site, "shopify", ok=True, http_status=r.status_code,
                    product_name=product.get("title"), variants=variants,
                    article_type=product.get("product_type") or None,
                    image=(product.get("image") or {}).get("src")
                          or next((im.get("src") for im in
                                   product.get("images") or []), None),
                    images=[im.get("src") for im in
                            product.get("images") or []])


def _shopify_stock(clean):
    """{variant id: True/False} from the store's /products/<handle>.js.

    The .json route carries prices but no stock at all -- which is why a
    brand site never showed "out of stock": BownBee 2248 was sold out in
    1-2Y, 2-3Y, 4-5Y and 5-6Y and read as unknown (26 Sep 2026). The .js
    route (Shopify's storefront API, robots-allowed) has `available` per
    variant. Any failure leaves stock unknown, never sold out.
    """
    target = clean.rstrip("/") + ".js"
    allowed, _ = robots_allows(target)
    if not allowed:
        return {}
    r, _ = _get(target, tries=1)
    if r is None or r.status_code != 200:
        return {}
    try:
        return {v.get("id"): bool(v["available"])
                for v in r.json().get("variants") or []
                if isinstance(v, dict) and "available" in v}
    except Exception:
        return {}


# ------------------------------------------------------------------- myntra --
def myntra_listing(url):
    """window.__myx carries name, articleType, mrp and the discounted price.

    articleType is the garment-type guard: a "Shirts" listing cannot be a
    clothing-set design. Myntra prices one product, not one size, so per_size
    is False -- an unmatched size costs stock information, not the price.
    """
    site = "myntra.com"
    allowed, why = robots_allows(url)
    if not allowed:
        return _listing(url, site, "myntra", note=why)
    r, why = _get(url)
    if r is None:
        return _listing(url, site, "myntra", note=why)
    if r.status_code != 200:
        return _listing(url, site, "myntra", http_status=r.status_code,
                        note=why)

    m = re.search(r"window\.__myx\s*=\s*(\{.*?\});?\s*</script>", r.text, re.S)
    if not m:
        return _listing(url, site, "myntra", http_status=r.status_code,
                        note="window.__myx not present -- page shape changed")
    try:
        pdp = json.loads(m.group(1)).get("pdpData") or {}
    except Exception:
        return _listing(url, site, "myntra", http_status=r.status_code,
                        note="window.__myx was not parseable JSON")

    price = _num((pdp.get("price") or {}).get("discounted")) \
        or _num((pdp.get("price") or {}).get("mrp"))
    if price is None:
        return _listing(url, site, "myntra", http_status=r.status_code,
                        product_name=pdp.get("name"), note="no price in payload")

    hero, shots = None, []
    for im in (pdp.get("media") or {}).get("albums") or []:
        for entry in im.get("images") or []:
            src = entry.get("secureSrc") or entry.get("src")
            if src:
                shots.append(src)
            hero = hero or src
    # Each size carries its own seller price (sizeSellerData), and it can
    # differ from the product's headline price; a size nobody is selling
    # has none and keeps the headline price, marked unavailable.
    # The seller's MRP comes with it, and it too can differ by size
    # (Vastramay 33061930: 1999 on some sizes, 2199 on others, 28 Sep 2026)
    # -- that size's MRP, beside the product-level listed_mrp.
    def size_offer(s):
        got = [(_num(x.get("discountedPrice")), _num(x.get("mrp")))
               for x in (s.get("sizeSellerData") or [])]
        got = [g for g in got if g[0] is not None]
        if not got:
            return price, None
        best = min(p for p, _m in got)
        mrps = {m for p, m in got if p == best and m}
        return best, (mrps.pop() if len(mrps) == 1 else None)
    variants = [Variant(labels=_labels_of(s.get("label")), price=p,
                        compare_at=m, available=s.get("available"),
                        image=hero)
                for s in (pdp.get("sizes") or [])
                for p, m in [size_offer(s)]]
    if not variants:
        variants = [Variant(frozenset(), price, None, None, hero)]
    return _listing(url, site, "myntra", ok=True, http_status=r.status_code,
                    product_name=pdp.get("name"), variants=variants,
                    per_size=False, listed_mrp=_num(pdp.get("mrp")),
                    images=shots,
                    article_type=(pdp.get("analytics") or {}).get("articleType"))


# ------------------------------------------------------------- schema.org --
# Not every storefront is Shopify, and the ones that are not still sell real
# designs. Both non-Shopify hosts in this corpus publish a schema.org Product
# on the page itself, so one reader serves them and Hopscotch alike.
def _ldjson_blobs(text):
    """Every ld+json payload on a page, parsed, in document order.

    Two shapes have to be tolerated or a live price reads as absent:

      * www.babyshop.in HTML-escapes the whole payload, so `&quot;` arrives
        where a quote belongs and a direct json.loads fails on all three of
        its blocks. Unescaping first is the difference between an unreadable
        page and a Product priced at 419.
      * schema.org allows a bare object, a list, or an @graph wrapper. All
        three appear in the wild, so all three are flattened here.
    """
    out = []
    for blob in re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            text, re.S | re.I):
        data = None
        for attempt in (blob, html.unescape(blob)):
            try:
                data = json.loads(attempt)
                break
            except Exception:
                continue
        if data is None:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                out.extend(g for g in graph if isinstance(g, dict))
            else:
                out.append(item)
    if not out and "self.__next_f" in text:
        out = _next_flight_ldjson(text)
    return out


def _next_flight_ldjson(text):
    """schema.org objects carried inside a Next.js page's streamed data
    (self.__next_f.push([1, "..."])) rather than in a <script> tag --
    jaypore.com publishes its Product (with price) only there."""
    out = []
    for lit in re.findall(r'self\.__next_f\.push\(\[\d+,("(?:[^"\\]|\\.)*")\]\)',
                          text):
        try:
            chunk = json.loads(lit)
        except Exception:
            continue
        for m in re.finditer(r'\{"@context"\s*:', chunk):
            try:
                data, _ = json.JSONDecoder().raw_decode(chunk[m.start():])
            except ValueError:
                continue
            for item in (data if isinstance(data, list) else [data]):
                if isinstance(item, dict):
                    graph = item.get("@graph")
                    out.extend(g for g in graph if isinstance(g, dict)) \
                        if isinstance(graph, list) else out.append(item)
    return out


def _is_product(d):
    """@type may be a string or a list, and case is not guaranteed."""
    t = d.get("@type")
    return any(str(x).strip().lower() == "product"
               for x in (t if isinstance(t, list) else [t]))


def _availability(text):
    """schema.org availability -> 1, 0 or None. Absent stays unknown."""
    low = re.sub(r"[^a-z]", "", str(text or "").lower())
    if not low:
        return None
    if "outofstock" in low or "soldout" in low or "discontinued" in low:
        return 0
    if "instock" in low or "onlineonly" in low or "limitedavailability" in low:
        return 1
    return None


# The MRP a one-price page states in its own markup when its schema.org
# tag leaves it out (28 Sep 2026): Salesforce's struck list price
# (biba.in: <span class="strike-through list"><span class="value"
# content="1999.00">) and Magento's price box (panashindia.com:
# "oldPrice":{"amount":4883 / data-price-type="oldPrice"
# data-price-amount="4883"). Information only, like every listed_mrp.
_PAGE_MRP = (
    re.compile(r'class="strike-through list">\s*<span class="value" '
               r'content="([0-9][0-9.]*)"'),
    re.compile(r'"oldPrice":\{"amount":([0-9][0-9.]*)'),
    re.compile(r'data-price-amount="([0-9][0-9.]*)"[^>]*'
               r'data-price-type="oldPrice"'),
    re.compile(r'data-price-type="oldPrice"[^>]*'
               r'data-price-amount="([0-9][0-9.]*)"'),
)


def _page_mrp(text, price):
    """The page's one stated MRP above `price`, or None -- two different
    ones (another product's box) and nothing is chosen. Pure."""
    found = {float(x) for rx in _PAGE_MRP for x in rx.findall(text or "")}
    if price is None or len(found) != 1:
        return None
    mrp = found.pop()
    return mrp if mrp > price else None


def _offer_price(offers):
    """(price, listed_mrp, in_stock) from a schema.org `offers` value.

    The price is not always where the obvious reader looks. boyzngalz.com
    publishes no `price` on the Offer at all -- it sits one level down in
    priceSpecification, which is perfectly valid schema.org. A flat
    offers["price"] read misses it and reports a live 599 product as
    unpriced, which is exactly the silent blank this engine exists to avoid.
    """
    price = mrp = stock = None
    for off in (offers if isinstance(offers, list) else [offers]):
        if not isinstance(off, dict):
            continue
        got = _availability(off.get("availability"))
        if got is not None:
            stock = got if stock is None else max(stock, got)
        here = _num(off.get("price"))
        specs = off.get("priceSpecification")
        for spec in (specs if isinstance(specs, list) else [specs]):
            if not isinstance(spec, dict):
                continue
            sp = _num(spec.get("price"))
            if sp is None:
                continue
            kind = str(spec.get("@type") or "").lower()
            # A struck-through or list price is the retailer's MRP, which is
            # recorded for information and never compared -- the same rule
            # Shopify's compare_at_price already gets.
            if "strikethrough" in kind or "listprice" in kind:
                mrp = sp if mrp is None else max(mrp, sp)
            elif here is None:
                here = sp
        if here is not None:
            price = here if price is None else min(price, here)
    return price, mrp, stock


def _first_image(value):
    got = _all_ldjson_images(value)
    return got[0] if got else None


def _all_ldjson_images(value):
    """Every image a schema.org Product publishes, in order.

    `image` is allowed to be a string, a list, an ImageObject or a list of
    them, and a brand's shoot is several frames -- which is exactly what
    verifying a link by photograph needs, since the hero is often not the
    frame the intake file carries.
    """
    out = []
    for v in (value if isinstance(value, list) else [value]):
        if isinstance(v, dict):
            v = v.get("url") or v.get("contentUrl")
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def ldjson_listing(url, platform="storefront", site=None, note="",
                   page_facts=None):
    """Price a page from its own schema.org Product.

    One price per product and no per-size breakdown, so per_size is False.
    That is honest rather than lazy: these pages publish a single price, and
    claiming a size match we never made would be the FirstCry per-size error
    wearing a different coat.
    """
    site = site or urllib.parse.urlsplit(url).netloc
    allowed, why = robots_allows(url)
    if not allowed:
        return _listing(url, site, platform, note=why)
    r, why = _get(url)
    if r is None:
        return _listing(url, site, platform, note=why)
    if r.status_code != 200:
        return _listing(url, site, platform, http_status=r.status_code, note=why)
    return _read_page(url, r, platform, site, note, page_facts)


def _read_page(url, r, platform, site, note="", page_facts=None):
    """Price a page that has been read: `r` has .text and .status_code.
    The same readers serve the page as sent and the page as a browser
    renders it (`_in_browser`)."""
    extra = page_facts(r.text) if page_facts else {}
    products = [d for d in _ldjson_blobs(r.text) if _is_product(d)]
    # Not in rupees, whichever reader would price it: the page's schema.org
    # offers (price specifications too) and its price tag name the currency.
    # WooCommerce's size list used to be read before this was looked at, so
    # a $45 dress read as Rs 45 (30 Sep 2026).
    cur = _foreign_currency(_page_currencies(products, r.text))
    if cur:
        return _listing(url, site, platform, http_status=r.status_code,
                        product_name=(products[0].get("name") if products
                                      else None),
                        note=_CURRENCY_NOTE % cur, **extra)
    live = _live_variants(r.text, url) if platform in (
        "storefront", "hopscotch") else None
    if live:
        # The site's own product data -- what its page shows a shopper. The
        # schema.org tag beside it can be stale (see _live_variants).
        name, variants = live
        first = products[0] if products else {}
        shots = _all_ldjson_images(first.get("image")) or             [v.image for v in variants if v.image][:1]
        return _listing(url, site, platform, ok=True,
                        http_status=r.status_code,
                        product_name=name or first.get("name"),
                        per_size=True, image=shots[0] if shots else None,
                        images=shots, variants=variants,
                        note="live price and stock from the site's own "
                             "product data", **extra)
    woo = _woo_variants(r.text) or _woo_store_variants(r.text, url)
    if woo:
        # WooCommerce lists every size with its own price and stock in the
        # add-to-cart form; its schema.org Product carries only one price.
        first = products[0] if products else {}
        shots = _all_ldjson_images(first.get("image"))
        return _listing(url, site, platform, ok=True,
                        http_status=r.status_code,
                        product_name=first.get("name") or _og(r.text, "og:title"),
                        per_size=True, image=shots[0] if shots else None,
                        images=shots, variants=woo,
                        note="per-size prices from the shop's own size list",
                        **extra)
    priced = [d for d in products
              if _offer_price(d.get("offers") or {})[0] is not None]
    if len({(str(d.get("name") or "").strip().lower(),
             _offer_price(d.get("offers") or {})[0]) for d in priced}) > 1:
        # Several products with prices (a related-products shelf, or the
        # page a gone product was sent on to): only the one this page is
        # about may be read -- named by its canonical address or its title.
        own = _own_product(priced, r.text, url)
        if own is None:
            return _listing(url, site, platform, http_status=r.status_code,
                            note="the page carries %d products and different "
                                 "prices match this link" % len(priced),
                            **extra)
        products = [own]
    span = _price_range(products)
    if span:
        # "Rs 499 - Rs 899" and no size list to say which size costs what:
        # one price for every size would be a guess.
        return _listing(url, site, platform, http_status=r.status_code,
                        product_name=(products[0].get("name") if products
                                      else None),
                        note="the page gives a price range (%s - %s), so "
                             "different prices match this link" % span,
                        **extra)
    for d in products:
        cur = _foreign_currency(
            off.get("priceCurrency") for off in
            (d.get("offers") if isinstance(d.get("offers"), list)
             else [d.get("offers")]) if isinstance(off, dict))
        if cur:
            return _listing(url, site, platform, http_status=r.status_code,
                            product_name=d.get("name"),
                            note=_CURRENCY_NOTE % cur, **extra)
        price, mrp, stock = _offer_price(d.get("offers") or {})
        if price is None:
            continue
        if mrp is None:
            mrp = _page_mrp(r.text, price)
        shots = _all_ldjson_images(d.get("image"))
        image = shots[0] if shots else None
        offers = _offer_variants(d.get("offers") or {}, image)
        if len({v.price for v in offers}) > 1:
            # Several offers at different prices: each is a variant, and
            # pick() chooses by size or refuses. Taking the minimum printed
            # the cheapest size's price against every size.
            return _listing(url, site, platform, ok=True,
                            http_status=r.status_code,
                            product_name=d.get("name"), per_size=True,
                            listed_mrp=mrp, image=image, images=shots,
                            variants=offers, note="", **extra)
        listed = _sfra_sizes(r.text)
        if len(listed) >= 2 and platform == "storefront":
            # One price, and the page's own size buttons say which sizes
            # it sells and which are sold out (biba.in).
            return _listing(url, site, platform, ok=True,
                            http_status=r.status_code,
                            product_name=d.get("name"), per_size=True,
                            listed_mrp=mrp, image=image, images=shots,
                            variants=[Variant(l, price, None, a, image)
                                      for l, a in listed],
                            note="one price for every size; each size's stock "
                                 "from the page's size buttons", **extra)
        labels = extra.get("sizes", frozenset()) | (
            {extra["colour"]} if extra.get("colour") else set())
        return _listing(url, site, platform, ok=True, http_status=r.status_code,
                        product_name=d.get("name"),
                        per_size=bool(extra.get("sizes")),
                        listed_mrp=mrp, image=image, images=shots,
                        variants=[Variant(frozenset(labels), price, None,
                                          stock, image)],
                        note=note or ("product-level price; this storefront "
                                      "does not publish per-size prices"),
                        **extra)
    og_price = _num(_og(r.text, "product:price:amount"))
    og_cur = _foreign_currency([_og(r.text, "product:price:currency")])
    if og_price and not og_cur and platform == "storefront":
        # No schema.org Product, but the shop's own sharing tags name the
        # price (Open Graph product:price) -- one price for the product.
        img = _og(r.text, "og:image") or None
        return _listing(url, site, platform, ok=True,
                        http_status=r.status_code,
                        product_name=_og(r.text, "og:title") or None,
                        per_size=False, image=img,
                        variants=[Variant(frozenset(), og_price, None, None,
                                          img)],
                        note="product-level price from the page's price tag")
    sold_out = platform == "flipkart" and "Notify Me" in r.text
    return _listing(url, site, platform, http_status=r.status_code,
                    note=("Flipkart is not selling this size right now (it "
                          "shows Notify Me)") if sold_out else NO_PRODUCT_NOTE,
                    **extra)


NO_PRODUCT_NOTE = "no schema.org Product offer with a price on the page"


def _offers_of(d):
    offers = d.get("offers") or []
    return [o for o in (offers if isinstance(offers, list) else [offers])
            if isinstance(o, dict)]


def _page_currencies(products, text):
    """Every currency the page states for its prices. Pure."""
    out = [_og(text, "product:price:currency")]
    for d in products:
        for off in _offers_of(d):
            out.append(off.get("priceCurrency"))
            specs = off.get("priceSpecification")
            for spec in (specs if isinstance(specs, list) else [specs]):
                if isinstance(spec, dict):
                    out.append(spec.get("priceCurrency"))
    return out


def _price_range(products):
    """(low, high) when a schema.org offer states a price range and no
    offer lists the sizes' own prices; else None. Pure."""
    for d in products:
        offers = _offers_of(d)
        if len({_num(o.get("price")) for o in offers} - {None}) > 1:
            return None                  # the offers price each size
        for o in offers:
            lo, hi = _num(o.get("lowPrice")), _num(o.get("highPrice"))
            if lo and hi and hi > lo:
                return ("%g" % lo, "%g" % hi)
    return None


def _own_product(priced, text, url):
    """The one Product among several that the page itself is: its url is
    the page's canonical address (or this link), or its name is the page's
    og:title. None when that does not single one out. Pure."""
    canon = _og(text, "og:url") or ""
    m = re.search(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', text or "")
    if m:
        canon = canon or m.group(1)
    here = {x.rstrip("/").lower() for x in (canon, url) if x}
    title = (_og(text, "og:title") or "").strip().lower()
    hits = [d for d in priced
            if str(d.get("url") or "").rstrip("/").lower() in here]
    if len(hits) != 1 and title:
        hits = [d for d in priced
                if str(d.get("name") or "").strip().lower() == title]
    return hits[0] if len(hits) == 1 else None


# ------------------------------------------------------------------ browser --
# Pages that build their price in the browser (thesouledstore.com: the page
# sent carries no price; its own script fetches it with a key only the page
# holds). `browser.py` loads such a page in headless Chromium as a shopper
# would and reads what it shows. Only when every reader above found nothing
# on a page that answered. validate.py turns it off: tests start no browser.
BROWSER_FALLBACK = True


class _Rendered:
    def __init__(self, text, status):
        self.text, self.status_code = text, status


def _wants_browser(lst):
    return (BROWSER_FALLBACK and not lst.ok and lst.http_status == 200
            and lst.note == NO_PRODUCT_NOTE)


def _in_browser(url, platform="storefront", site=None):
    """A Listing read from the page as a browser renders it."""
    import browser
    site = site or urllib.parse.urlsplit(url).netloc
    allowed, why = robots_allows(url)
    if not allowed:
        return _listing(url, site, platform, note=why)
    _wait_turn(urllib.parse.urlsplit(url).netloc)
    got = browser.render(url, robots_allows, user_agent=UA)
    return _from_render(url, got, platform, site)


def _from_render(url, got, platform, site):
    """The rendered page's answer as a Listing. Pure: tested offline."""
    if got.get("error"):
        return _listing(url, site, platform,
                        note="this page builds its price in the browser, and "
                             + got["error"])
    status = got.get("status")
    if status != 200:
        return _listing(url, site, platform, http_status=status,
                        note=GONE_STATUS.get(status)
                        or "HTTP %s (in the browser)" % status)
    via = " (read in a browser)"
    lst = _read_page(url, _Rendered(got.get("html") or "", 200), platform, site)
    if lst.ok or lst.note != NO_PRODUCT_NOTE:
        return lst._replace(note=(lst.note or "") + via)
    shown = got.get("shown") or {}
    blocked = got.get("blocked") or []
    why = (" -- robots.txt blocked %d of the page's own data requests"
           % len(blocked)) if blocked else ""
    if shown.get("foreign"):
        return _listing(url, site, platform, http_status=200,
                        product_name=shown.get("h1") or None,
                        note=_CURRENCY_NOTE % "another currency")
    sell = sorted({p for p in (_num(x) for x in shown.get("sell") or []) if p})
    if shown.get("gone") and not sell:
        return _listing(url, site, platform, http_status=200,
                        note="the page says this product does not exist -- "
                             "delisted by the retailer (read in a browser)")
    if not shown.get("h1"):
        return _listing(url, site, platform, http_status=200,
                        note="the page shows no product heading, even in a "
                             "browser" + why)
    if not sell:
        return _listing(url, site, platform, http_status=200,
                        product_name=shown.get("h1"),
                        note="the page shows no price, even in a browser" + why)
    if len(sell) > 1:
        return _listing(url, site, platform, http_status=200,
                        product_name=shown.get("h1"),
                        note="the page shows %d prices (%s) and does not say "
                             "which is this size -- no usable price%s"
                             % (len(sell), ", ".join("%g" % p for p in sell),
                                via))
    price = sell[0]
    struck = [p for p in (_num(x) for x in shown.get("struck") or [])
              if p and p > price]
    # A buy button says the PRODUCT can be bought, not that this size can:
    # stock stays unknown unless the whole product is sold out.
    sold = shown.get("sold_out")
    img = shown.get("og_image") or None
    sizes = [(_labels_of(s), bool(a)) for s, a in shown.get("sizes") or []
             if isinstance(s, str)]
    sizes = [(l, a) for l, a in sizes if l]
    if len(sizes) >= 2:
        # The page's size buttons: one price for them all (the page shows
        # one), and the stock each button shows. A size not among them is
        # a size the page does not sell.
        return _listing(url, site, platform, ok=True, http_status=200,
                        product_name=shown.get("h1"), per_size=True,
                        listed_mrp=max(struck) if struck else None, image=img,
                        images=[img] if img else [],
                        variants=[Variant(l, price, None, a and not sold, img)
                                  for l, a in sizes],
                        note="one price for every size, and each size's "
                             "stock, read from the page as a shopper sees it"
                             + via)
    return _listing(url, site, platform, ok=True, http_status=200,
                    product_name=shown.get("h1"), per_size=False,
                    listed_mrp=max(struck) if struck else None, image=img,
                    images=[img] if img else [],
                    variants=[Variant(frozenset(), price, None,
                                      False if sold else None, img)],
                    note="one price for the product, read from the page as a "
                         "shopper sees it" + via)


def _og(text, prop):
    """A <meta property="..."> value (Open Graph), or ""."""
    m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']'
                  r'([^"\']*)' % re.escape(prop), text or "", re.I) or \
        re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)='
                  r'["\']%s["\']' % re.escape(prop), text or "", re.I)
    return html.unescape(m.group(1)).strip() if m else ""


# Big retailers publish a schema.org tag that can be STALE, while their own
# product data (what the page renders) is live. Found 26 Sep 2026:
#   fabindia.com (SAP Commerce): tag Rs 2199 "InStock"; live data Rs 1100
#     after a 50% discount, and 4 of 5 sizes at stock 0.
#   jaypore.com (Aditya Birla platform): tag "InStock"; live Quantity 0.
#   pantaloons.com (same platform): tag Rs 1799; live SellingPrice 1259.
def _live_variants(text, url):
    """(product name, [Variant]) from a site's own product data, or None."""
    return (_sap_variants(text, url) or _abfrl_variants(text, url)
            or _landmark_variants(text, url) or _bwa_variants(text, url)
            or _hopscotch_variants(text, url) or _nushop_variants(text, url)
            or _wix_variants(text, url) or _fynd_variants(text, url))


def _next_data(text):
    """The page's __NEXT_DATA__ JSON (Next.js), or {}."""
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                  text or "", re.S)
    try:
        return json.loads(m.group(1)) if m else {}
    except Exception:
        return {}


def _landmark_variants(text, url):
    """Landmark Group sites -- maxfashion.in, lifestylestores.com (and their
    sister shops): __NEXT_DATA__ ... productPageReducer.data carries the
    price and, under each colour, every size with its stock level."""
    data = (((_next_data(text).get("props") or {}).get("initialState") or {})
            .get("productPageReducer") or {}).get("data") or {}
    if not isinstance(data, dict) or not data.get("variants"):
        return None
    price = _num((data.get("price") or {}).get("value"))
    if price is None or _foreign_currency([(data.get("price") or {})
                                           .get("currencyIso")]):
        return None
    codes = _url_codes(url)
    groups = [g for g in data["variants"] if isinstance(g, dict)]
    mine = [g for g in groups if str(g.get("code", "")).split("-")[0] in codes
            or any(str(s.get("code")) in codes for s in g.get("variants") or [])]
    group = (mine or [g for g in groups if g.get("isSelected")] or groups[:1])[0]
    colour = group.get("color") or ""
    out = []
    for s in group.get("variants") or []:
        st = (s.get("stock") or {})
        status = str(st.get("stockLevelStatus") or "").lower()
        level = st.get("stockLevel")
        avail = (False if status == "outofstock" else True
                 if status in ("instock", "lowstock") else
                 (level > 0) if isinstance(level, (int, float)) else None)
        out.append(Variant(_labels_of(s.get("size"), colour), price, None,
                           avail, None))
    return (data.get("name"), out) if out else None


def _bwa_variants(text, url):
    """Amazon's shop builder ("Buy with Amazon" storefronts, e.g.
    navtan.in): __NEXT_DATA__ pageProps.catalog.variantsDetails lists each
    variant with its size, colour, selling price and stock."""
    pp = (_next_data(text).get("props") or {}).get("pageProps") or {}
    details = ((pp.get("catalog") or {}).get("variantsDetails")) or []
    if not isinstance(details, list) or not details:
        return None
    out = []
    for d in details:
        attrs = (d or {}).get("variantAttributes") or {}
        if attrs.get("deactivated"):
            continue
        price = _num((attrs.get("price") or {}).get("sellingPrice"))
        labels = []
        for a in d.get("variantDimensionAttributes") or []:
            v = str(a.get("value") or "")
            labels.append(v.split("_", 1)[1] if v.startswith("#") and "_" in v
                          else v)            # "#0000FF_Blue" -> Blue
        avail = ((((attrs.get("buyingOptions") or {}).get("singlePurchase")
                   or {}).get("availability")) or {}).get("inStock")
        out.append(Variant(_labels_of(*labels), price,
                           _num((attrs.get("price") or {}).get("mrp")),
                           bool(avail) if avail is not None else None, None))
    name = (pp.get("catalog") or {}).get("name")
    return (name, out) if any(v.price for v in out) else None


def _enclosing(text, at, test, tries=400):
    """The JSON object around position `at` that passes `test`, or None:
    back from `at` to each opening brace in turn, decoding from there."""
    for _ in range(tries):
        at = text.rfind("{", 0, at)
        if at < 0:
            return None
        try:
            obj, _end = json.JSONDecoder().raw_decode(text[at:])
        except ValueError:
            continue
        if isinstance(obj, dict) and test(obj):
            return obj
    return None


def _fynd_variants(text, url):
    """Fynd shops (mothercare.in, 28 Sep 2026): the page's APP_DATA holds
    the product's "sizes" record -- every size with is_available and a
    stock quantity -- and its price (effective = selling, marked = MRP).
    Fynd gives the price as a min-max over sizes; only when both ends
    agree is it every size's price. Otherwise nothing is read here."""
    if '"sellable"' not in (text or "") or '"effective"' not in text:
        return None
    for m in re.finditer(r'"sellable"\s*:', text):
        rec = _enclosing(text, m.start(), lambda o: isinstance(
            o.get("sizes"), list) and isinstance(o.get("price"), dict)
            and "sellable" in o)
        if not rec:
            continue
        eff = (rec["price"].get("effective") or {})
        if _foreign_currency([eff.get("currency_code")]):
            return None
        lo, hi = _num(eff.get("min")), _num(eff.get("max"))
        if not lo or lo != hi:
            return None
        mrp = _num((rec["price"].get("marked") or {}).get("max"))
        out = []
        for s in rec["sizes"]:
            if not isinstance(s, dict):
                continue
            qty = s.get("quantity")
            avail = s.get("is_available")
            if isinstance(qty, (int, float)):
                avail = bool(avail) and qty > 0 if avail is not None else qty > 0
            out.append(Variant(_labels_of(s.get("value") or s.get("display")),
                               lo, mrp if mrp and mrp > lo else None,
                               avail if avail is not None else None, None))
        if out:
            return None, out
    return None


def _wix_variants(text, url):
    """Wix Stores (mymowgli.in, 28 Sep 2026): the page's warm-up data holds
    the product with "productItems" -- one per option choice, with its
    price and inventory -- and "options" naming each choice. Wix's own
    naming is back to front: with hasDiscount, "comparePrice" is the price
    the shopper pays (Rs 625 shown, "price" 1200 struck through)."""
    if '"productItems"' not in (text or "") or "wix" not in text[:300000].lower():
        return None
    slug = urllib.parse.urlsplit(url).path.rstrip("/").split("/")[-1]
    for m in re.finditer(r'"productItems"\s*:\s*\[', text):
        prod = _enclosing(text, m.start(), lambda o: isinstance(
            o.get("productItems"), list) and "options" in o)
        if not prod or (slug and prod.get("urlPart") and prod["urlPart"] != slug):
            continue
        if _foreign_currency([prod.get("currency")]):
            return None
        names = {}
        for opt in prod.get("options") or []:
            for sel in (opt or {}).get("selections") or []:
                if isinstance(sel, dict):
                    names[sel.get("id")] = sel.get("value") or sel.get("key")
        out = []
        for it in prod["productItems"]:
            if not isinstance(it, dict) or it.get("isVisible") is False:
                continue
            base = _num(it.get("price"))
            price = _num(it.get("comparePrice")) if it.get("hasDiscount") \
                else base
            status = str((it.get("inventory") or {}).get("status") or "").lower()
            avail = True if status == "in_stock" else False \
                if status == "out_of_stock" else None
            out.append(Variant(_labels_of(*[names.get(i) for i in
                                            it.get("optionsSelections") or []]),
                               price, base if price != base else None, avail,
                               None))
        if any(v.price for v in out):
            return prod.get("name"), out
    return None


def _nushop_variants(text, url):
    """Nushop shops (nusyl.com, 28 Sep 2026): the page's preloaded state
    has "customer_skus" -- every size with its own selling_price and a
    stock quantity (it varies by size: 6 to 14 on one tee, so it is a
    count). Links look like /<name>/catalogue/<product>/<size sku>; the
    list whose SKUs belong to the linked product is the one read."""
    if '"customer_skus"' not in (text or ""):
        return None
    parts = urllib.parse.urlsplit(url).path.split("/")
    prod = parts[parts.index("catalogue") + 1] if "catalogue" in parts[:-1] \
        else ""
    lists = []
    for m in re.finditer(r'"customer_skus"\s*:\s*\[', text):
        try:
            arr, _ = json.JSONDecoder().raw_decode(text[m.end() - 1:])
        except ValueError:
            continue
        arr = [s for s in arr if isinstance(s, dict)]
        if arr:
            lists.append(arr)
    mine = [a for a in lists if prod and any(
        "/catalogue/%s/" % prod in str(s.get("url_suffix") or "") for s in a)]
    uniq = {json.dumps(a, sort_keys=True) for a in lists}
    skus = mine[0] if mine else (lists[0] if len(uniq) == 1 else None)
    if not skus:
        return None
    out = []
    for s in skus:
        if s.get("is_active") is False:
            continue
        qty = s.get("quantity")
        out.append(Variant(_labels_of(s.get("size")),
                           _num(s.get("selling_price")), _num(s.get("mrp")),
                           (qty > 0) if isinstance(qty, (int, float)) else None,
                           None))
    name = _og(text, "og:title") or None
    return (name, out) if any(v.price for v in out) else None


def _hopscotch_variants(text, url):
    """hopscotch.in (28 Sep 2026): __NEXT_DATA__ pageProps.dehydratedState
    carries the ProductDetail query -- every size ("simpleSkus") with its
    own selling price (retailPrice), list price and availableQuantity.
    Sizes are priced separately: one party dress sells 6-7Y at Rs 1274 and
    every other size at Rs 1529, and its schema.org tag says 1529 for all.
    availableQuantity 0 is what the page shows as "Sold out"."""
    pp = (_next_data(text).get("props") or {}).get("pageProps") or {}
    queries = ((pp.get("dehydratedState") or {}).get("queries")) or []
    codes = _url_codes(url) | set(re.findall(
        r"/product/(\d+)", urllib.parse.urlsplit(url).path))
    for q in queries if isinstance(queries, list) else []:
        key = (q or {}).get("queryKey") or []
        data = ((q or {}).get("state") or {}).get("data") or {}
        if not (isinstance(key, list) and key and key[0] == "ProductDetail"
                and isinstance(data, dict) and data.get("simpleSkus")):
            continue
        # Only the linked product's own record, never another on the page.
        if codes and str(data.get("id")) not in codes and not any(
                str(k) in codes for k in key[1:]):
            continue
        imgs = [(i or {}).get("imgUrlLarge") or (i or {}).get("imgUrl")
                for i in data.get("imgurls") or [] if isinstance(i, dict)]
        out = []
        for s in data["simpleSkus"]:
            if not isinstance(s, dict):
                continue
            size = next((a.get("value") for a in s.get("attrs") or []
                         if isinstance(a, dict)
                         and str(a.get("name", "")).lower() == "size"),
                        (s.get("attributes") or {}).get("size"))
            labels = _labels_of(size)
            if not labels and s.get("fromAge") is not None and s.get("toAge"):
                labels = frozenset({"%s-%sM" % (s["fromAge"], s["toAge"])})
            qty = s.get("availableQuantity")
            out.append(Variant(labels, _num(s.get("retailPrice")),
                               _num(s.get("regularPrice")),
                               (qty > 0) if isinstance(qty, (int, float))
                               else None,
                               imgs[0] if imgs else None))
        if any(v.price for v in out):
            first = data["simpleSkus"][0] if isinstance(
                data["simpleSkus"][0], dict) else {}
            return first.get("productName") or data.get("crmProductName"), out
    return None


def _url_codes(url):
    return set(re.findall(r"\d{6,}", urllib.parse.urlsplit(url).path))


def _sap_variants(text, url):
    """SAP Commerce (Spartacus) pages: <script id="ng-state"> carries
    cx-state.product.details.entities[code].list.value -- the price after
    discount, and every size (variantMatrix) with its stock level."""
    m = re.search(r'<script[^>]+id=["\']ng-state["\'][^>]*>(.*?)</script>',
                  text or "", re.S)
    if not m:
        return None
    try:
        state = json.loads(m.group(1))
        entities = state["cx-state"]["product"]["details"]["entities"]
    except Exception:
        return None
    codes = _url_codes(url)
    keys = [k for k in entities if k in codes] or list(entities)
    for key in keys:
        prod = ((entities.get(key) or {}).get("list") or {}).get("value") or {}
        if not isinstance(prod, dict) or not prod.get("variantMatrix"):
            continue
        base = _num((prod.get("price") or {}).get("value"))
        sale = _num((prod.get("priceAfterDiscount") or {}).get("value"))
        if _foreign_currency([(prod.get("priceAfterDiscount") or
                               prod.get("price") or {}).get("currencyIso")]):
            return None
        # Each size's name is kept against its code wherever the entity
        # lists qualifiers (baseOptions); the matrix leaves carry the stock.
        size_of = {}
        for d in _dicts_with(entities.get(key), "variantOptionQualifiers"):
            for q in d.get("variantOptionQualifiers") or []:
                if str(q.get("name", "")).lower() == "size" and d.get("code"):
                    size_of.setdefault(d["code"], q.get("value"))
        out = []
        leaves = [el.get("variantOption") or {} for el in
                  _dicts_with(prod.get("variantMatrix"), "variantOption")
                  if el.get("isLeaf")]
        for opt in leaves:
            quals = opt.get("variantOptionQualifiers") or []
            size = next((q.get("value") for q in quals
                         if str(q.get("name", "")).lower() == "size"),
                        size_of.get(opt.get("code"), ""))
            listed = _num((opt.get("priceData") or {}).get("value"))
            # The discount is the product's; it applies where the size is
            # at the product's own list price. Any other size's discount is
            # unknown, so it gets no price rather than a guess.
            if sale and listed == base:
                price = sale
            elif sale:
                price = None
            else:
                price = listed
            stock = opt.get("stock") or {}
            status = str(stock.get("stockLevelStatus") or "").lower()
            level = stock.get("stockLevel")
            avail = (False if status == "outofstock" else True
                     if status in ("instock", "lowstock") else
                     (level > 0) if isinstance(level, (int, float)) else None)
            out.append(Variant(_labels_of(size), price, listed, avail, None))
        if out:
            return prod.get("name"), out
    return None


def _walk_key(o, key):
    """Every value stored under `key` anywhere inside a JSON value."""
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                yield v
            yield from _walk_key(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_key(v, key)


def _flight_values(text):
    """Every JSON value in a Next.js page's streamed data."""
    chunks = []
    for lit in re.findall(r'self\.__next_f\.push\(\[\d+,("(?:[^"\\]|\\.)*")\]\)',
                          text or ""):
        try:
            chunks.append(json.loads(lit))
        except Exception:
            continue
    for line in "".join(chunks).split("\n"):
        # "5:[...]" or a text chunk "16:T406,{...}"
        m = re.match(r"^[0-9a-f]+:(?:T[0-9a-f]+,)?(.*)$", line)
        if not m:
            continue
        s = m.group(1)
        at = min([i for i in (s.find("{"), s.find("[")) if i >= 0] or [-1])
        if at < 0:
            continue
        try:
            yield json.JSONDecoder().raw_decode(s[at:])[0]
        except Exception:
            continue


def _dicts_with(o, key):
    if isinstance(o, dict):
        if key in o:
            yield o
        for v in o.values():
            yield from _dicts_with(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from _dicts_with(v, key)


def _abfrl_variants(text, url):
    """The Aditya Birla (ABFRL) platform -- jaypore.com, pantaloons.com,
    allensolly.abfrl.in and sister brands: the product object in the page's
    Next.js data has "Sizes": [{Name, Quantity, SellingPrice}, ...]. The
    page also carries similar products shaped the same way, so the one
    whose own codes match the link is taken; failing that, only a lone
    candidate is trusted."""
    if "self.__next_f" not in (text or ""):
        return None
    codes = _url_codes(url)
    chunks = []
    for lit in re.findall(r'self\.__next_f\.push\(\[\d+,("(?:[^"\\]|\\.)*")\]\)',
                          text):
        try:
            chunks.append(json.loads(lit))
        except Exception:
            continue
    flight = "".join(chunks)
    # Every product record with a size list: from each "Sizes": back to
    # the brace that opens the record holding it. (Records may share a line
    # with a text chunk, so the stream is not read line by line.)
    cands = []
    for m in re.finditer(r'"Sizes"\s*:\s*\[', flight):
        at = m.start()
        for _ in range(400):
            at = flight.rfind("{", 0, at)
            if at < 0:
                break
            try:
                obj, _end = json.JSONDecoder().raw_decode(flight[at:])
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("Sizes"), list):
                sizes = obj["Sizes"]
                if sizes and all(isinstance(s, dict) and "SellingPrice" in s
                                 for s in sizes):
                    cands.append(obj)
                break
    if not cands:
        return None

    def own_id(obj):
        # Only the record's OWN id -- never "SimilarProducts" or colour
        # lists, which name other products' codes.
        ids = {str(obj.get(k)) for k in ("ProductID", "ProductId", "ID",
                                          "Code", "StyleCode") if obj.get(k)}
        sw = (obj.get("Media") or {}).get("Swatch") if isinstance(
            obj.get("Media"), dict) else ""
        if sw:
            ids.add(str(sw).split("-")[0])
        return ids
    mine = [o for o in cands if codes & own_id(o)]
    uniq = {json.dumps(o.get("Sizes"), sort_keys=True) for o in cands}
    best = mine[0] if mine else (cands[0] if len(uniq) == 1 else None)
    if not best:
        return None
    product_price = _num(best.get("SellingPrice")) or _num(best.get("Price"))
    out = []
    for s in best["Sizes"]:
        if not isinstance(s, dict):
            continue
        price = _num(s.get("SellingPrice")) or product_price
        qty = s.get("Quantity")
        avail = (qty > 0) if isinstance(qty, (int, float)) else None
        out.append(Variant(_labels_of(s.get("Name")), price,
                           _num(s.get("Price")), avail, None))
    return (best.get("Name"), out) if out else None


_WOO_WORDS = {"EXTRASMALL": "XS", "SMALL": "S", "MEDIUM": "M", "LARGE": "L",
              "EXTRALARGE": "XL", "XXLARGE": "XXL", "FREE": "FREESIZE",
              "XLARGE": "XL", "X-LARGE": "XL", "XSMALL": "XS", "X-SMALL": "XS",
              "XX-LARGE": "XXL", "XXX-LARGE": "XXXL", "XXXLARGE": "XXXL"}


def _woo_label(value):
    """A WooCommerce attribute value as a size label: '16y' -> 16Y,
    '2-3-years' -> 2-3Y, 'extra-small' -> XS, '%e2%9a%a1l' -> L."""
    v = urllib.parse.unquote(str(value or "")).lower()
    v = re.sub(r"[^a-z0-9\-\s]", "", v)
    v = re.sub(r"(?<=\d)-(?=[a-z])|(?<=[a-z])-(?=[a-z\d])", " ", v)
    # A size range inside the value wins: "c-6-9-months" (iris weaves puts
    # a sort letter first) is 6-9M.
    inner = _RANGE_IN.search(v)
    n = norm_size(inner.group(0) if inner else v)
    return _WOO_WORDS.get(n, n)


def _woo_variants(text):
    """Variants from a WooCommerce add-to-cart form's
    data-product_variations: each size's own price and stock. aanyasri.com
    sold out L and XL of one kurta while the product read "in stock"."""
    m = re.search(r'data-product_variations=["\']([^"\']+)["\']', text or "")
    if not m:
        return []
    try:
        data = json.loads(html.unescape(m.group(1)))
    except Exception:
        return []                 # "false": too many sizes to list inline
    out = []
    for v in data if isinstance(data, list) else []:
        if not isinstance(v, dict):
            continue
        price = _num(v.get("display_price"))
        if price is None:
            continue
        labels = frozenset(l for l in (_woo_label(a) for a in
                                       (v.get("attributes") or {}).values())
                           if l)
        img = (v.get("image") or {}).get("src") if isinstance(
            v.get("image"), dict) else None
        out.append(Variant(labels, price, _num(v.get("display_regular_price")),
                           bool(v["is_in_stock"]) if "is_in_stock" in v
                           else None, img))
    return out


def _sfra_sizes(text):
    """[(labels, in stock)] from a Salesforce (SFRA) page's size buttons:
    <a class="btn size-selector-box [disabled-size-button]"
    data-value="2-3Y">. biba.in, 28 Sep 2026: 2-3Y "2 Left", 4-5Y
    disabled (sold out)."""
    out, seen = [], set()
    for tag in re.findall(r'<a\b[^>]*\bsize-selector-box\b[^>]*>', text or ""):
        m = re.search(r'\bdata-value="([^"]+)"', tag)
        cls = re.search(r'\bclass="([^"]*)"', tag)
        if not m:
            continue
        labels = _labels_of(html.unescape(m.group(1)))
        if not labels or labels in seen:
            continue
        seen.add(labels)
        gone = bool(cls and re.search(r"disabled|unavailable|out-of-stock",
                                      cls.group(1)))
        out.append((labels, not gone))
    return out


_MONTHS = {m: i + 1 for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split())}


def _woo_store_label(value):
    """A size as the Store API gives it. boyzngalz.com's "1/32" (UK 1, EU
    32) is stored as "jan-32" and "9/27" as "Sep-27" -- a date, the way a
    spreadsheet mangles 1/32 -- so a month name is its number again."""
    v = str(value or "").strip()
    m = re.fullmatch(r"([A-Za-z]{3})-(\d{1,2})", v)
    if m and m.group(1).lower() in _MONTHS:
        return "%d/%s" % (_MONTHS[m.group(1).lower()], m.group(2))
    return v if "/" in v else (_woo_label(v) or v)


def _uk_eu_labels(name, value):
    """A 'UK Size / Euro Size' value "13/31" is also UK13, EU31 and EUR31,
    the ways a file writes a shoe size."""
    m = re.fullmatch(r"(\d{1,2}(?:\.\d)?)/(\d{2}(?:\.\d)?)", value)
    low = str(name or "").lower()
    if not m or "uk" not in low or "eu" not in low:
        return set()
    uk, eu = m.groups()
    return {"UK" + uk, "EU" + eu, "EUR" + eu}


def _woo_store_variants(text, url):
    """WooCommerce with many sizes leaves them out of the page
    (data-product_variations="false") and looks each one up only when a
    shopper picks it. Its public Store API lists every variation of the
    product -- price, stock, size and colour -- in one request
    (boyzngalz.com, 28 Sep 2026: one ballerina sells nine sizes at Rs 549
    and nine at Rs 599; the page's tag gave 549 for all)."""
    if 'data-product_variations="false"' not in (text or ""):
        return []
    m = re.search(r'data-product_id="(\d+)"[^>]*data-product_variations="false"',
                  text) or re.search(r'data-product_variations="false"[^>]*'
                                     r'data-product_id="(\d+)"', text)
    if not m:
        return []
    parts = urllib.parse.urlsplit(url)
    out = []
    for page in range(1, 4):
        api = ("%s://%s/wp-json/wc/store/v1/products?type=variation&parent=%s"
               "&per_page=100&page=%d" % (parts.scheme, parts.netloc,
                                          m.group(1), page))
        if not robots_allows(api)[0]:
            return []
        r, _ = _get(api, tries=2)
        try:
            got = r.json() if r is not None and r.status_code == 200 else None
        except Exception:
            got = None
        if not isinstance(got, list):
            return out if page > 1 else []
        for v in got:
            pr = (v or {}).get("prices") or {}
            if _foreign_currency([pr.get("currency_code")]):
                return []
            try:
                unit = 10 ** int(pr.get("currency_minor_unit", 2))
                price = _num(pr.get("price")) / unit if _num(pr.get("price")) \
                    else None
                reg = _num(pr.get("regular_price"))
                reg = reg / unit if reg else None
            except (TypeError, ValueError):
                continue
            labels = set()
            for pair in str(v.get("variation") or "").split(", "):
                if ": " in pair:
                    name, value = pair.split(": ", 1)
                    value = _woo_store_label(value)
                    labels |= _labels_of(value) | _uk_eu_labels(name, value)
            img = (v.get("images") or [{}])[0].get("src") if v.get("images") \
                else None
            out.append(Variant(frozenset(labels), price,
                               reg if reg and price and reg > price else None,
                               bool(v["is_in_stock"]) if "is_in_stock" in v
                               else None, img))
        if len(got) < 100:
            break
    return out if any(v.price for v in out) else []


def _offer_variants(offers, image):
    """One Variant per schema.org Offer that carries a price, labelled by
    whatever size or name the offer states."""
    out = []
    for off in (offers if isinstance(offers, list) else [offers]):
        if not isinstance(off, dict):
            continue
        p, _mrp, stock = _offer_price(off)
        if p is None:
            continue
        item = off.get("itemOffered") if isinstance(off.get("itemOffered"),
                                                    dict) else {}
        out.append(Variant(_labels_of(off.get("name"), off.get("size"),
                                      item.get("size"), item.get("name")),
                           p, None, stock, image))
    return out


# ---------------------------------------------------------------- hopscotch --
def hopscotch_listing(url):
    """Hopscotch: every size's own price and stock from the page's own data
    (`_hopscotch_variants`, 28 Sep 2026). Its schema.org tag, the old route
    and still the fallback, carries one price for the whole product.
    """
    lst = ldjson_listing(
        url, platform="hopscotch", site="hopscotch.in",
        note="product-level price; Hopscotch does not publish per-size prices")
    return _in_browser(url, "hopscotch", "hopscotch.in") \
        if _wants_browser(lst) else lst


# --------------------------------------------------------------- amazon --
# Amazon is the one page in this corpus where the wrong number is easiest to
# publish, because an unavailable product still shows a dozen OTHER products'
# prices in its recommendation rails. Measured 23 Sep 2026 on all 11 corpus
# links: the four that carry no buybox are "Currently unavailable", and each
# of them still has 8 to 37 rupee amounts on the page -- 689, 2999, 349 --
# every one belonging to something else. A regex over the body would price
# those four confidently and wrongly.
#
# So every figure here is anchored to the element that owns it:
#
#   selling price  the `pricetopay` accessibility label ("Rs 649.00 with 50
#                  percent savings"). NOT `a-price-whole`, which also appears
#                  in the carousels, and NOT the first `a-offscreen` in the
#                  price block -- that one is the struck-through MRP, which
#                  sits BEFORE the selling price in the markup.
#   MRP            the "M.R.P.:" label. Kept for information only, like every
#                  retailer MRP, and never compared.
#   availability   `id="availability"`, so "Only 3 left" is not read as gone.
#
# Measured the same day across all 11: 7 price (Rs 399 to Rs 1,189, each
# against a higher MRP), 4 are correctly blank with "currently unavailable".
# No CAPTCHA on any of the 11 -- but it is intermittent and WILL appear, so
# it is detected and answered with nothing rather than with a parse of the
# challenge page.
_AMZ_RUPEE = "₹"
_AMZ_PRICE = re.compile(r"pricetopay[^>]*>[^" + _AMZ_RUPEE + r"<]{0,120}"
                        + _AMZ_RUPEE + r"\s*([\d,]+(?:\.\d{1,2})?)", re.I | re.S)
_AMZ_MRP = re.compile(r"M\.R\.P\.?:?\s*(?:</span>\s*<span[^>]*>\s*)?"
                      + _AMZ_RUPEE + r"\s*([\d,]+(?:\.\d{1,2})?)", re.I)
_AMZ_AVAIL = re.compile(r'id="availability".{0,400}?<span[^>]*>\s*([^<]{3,80})', re.S)
_AMZ_TITLE = re.compile(r'id="productTitle"[^>]*>\s*([^<]{3,300})')
_AMZ_IMAGE = re.compile(r'id="landingImage"[^>]*\ssrc="([^"]+)"')
_AMZ_CAPTCHA = ("Enter the characters you see", "/errors/validateCaptcha",
                "Type the characters you see in this image")
# A softer check: a ~4KB page whose only content is "Click the button below
# to continue shopping". Measured 26 Sep 2026 when a run opened many size
# pages in a row -- it was being read as "no buybox price", and the same
# page a minute later carried a price. It passes with time, so it is waited
# out (with the host's pace widened) before it is reported.
_AMZ_SOFT_BLOCK = ("Click the button below to continue shopping",)
# How long it lasts, measured 28 Sep 2026 after ~90 pages of three brands
# in a row at one page per 3s: 5 to 8 minutes. The next run, minutes later,
# was blocked from its first page and stayed blocked through 30s + 90s +
# 180s of waiting (the run took 12 minutes and priced 1 of 43 Amazon
# cells) -- waiting inside a run does not beat it. So the waits stay short
# (Amazon's own queue only, compare._fetch_all), each wait doubles Amazon's
# pace, and the pop-up says to Refetch later (_RETRYABLE). The 3s pace is
# what prevents it: AJ Dezines (31 Amazon cells) and BownBee met none.
AMZ_BLOCK_WAITS = (30.0, 90.0)   # seconds; a module constant so tests zero it
# Hosts that kept the check page up through every wait. For the rest of the
# run their pages get one try each: waiting 40s per page on a host that has
# stopped answering turned a 3-minute run into an hour.
_amz_gave_up = set()


_active_runs = [0]


def new_run():
    """Forget one run's slow-downs before the next. A 429 widens a host's
    pace "for the rest of the run" -- in the long-lived app that used to
    mean until it was restarted. Not while another run is reading: a
    second brand's fetch, or a Refetch, wiped the first run's Amazon
    slow-downs mid-run and sent it back into the check page (30 Sep 2026)."""
    with _pace_lock:
        if _active_runs[0]:
            return
        _host_pace.clear()
    _amz_gave_up.clear()


def begin_run():
    """A run starts reading (compare.run): new_run, then count it."""
    new_run()
    with _pace_lock:
        _active_runs[0] += 1


def end_run():
    with _pace_lock:
        _active_runs[0] = max(0, _active_runs[0] - 1)


def amazon_listing(url):
    """Amazon India, read from the buybox and nothing else."""
    site = "amazon.in"
    allowed, why = robots_allows(url)
    if not allowed:
        return _listing(url, site, "amazon", note=why)
    host = urllib.parse.urlsplit(url).netloc
    waits = () if host in _amz_gave_up else AMZ_BLOCK_WAITS
    for wait in waits + (None,):
        r, why = _get(url)
        if r is None:
            return _listing(url, site, "amazon", note=why)
        if r.status_code != 200:
            return _listing(url, site, "amazon", http_status=r.status_code,
                            note=why)
        body = r.text
        if not any(sig in body for sig in _AMZ_SOFT_BLOCK):
            break
        if wait is None:
            _amz_gave_up.add(host)
            return _listing(url, site, "amazon", http_status=r.status_code,
                            note="Amazon asked us to slow down (its \"continue "
                                 "shopping\" check page) -- not priced; "
                                 "try again later")
        _slow_down(host)
        time.sleep(wait)

    # The challenge page is a 200 with a plausible shape. Parsing it would
    # produce a blank that reads like "Amazon does not sell this", which is a
    # different and wrong claim from "we were asked to prove we are human".
    if any(sig in body for sig in _AMZ_CAPTCHA):
        return _listing(url, site, "amazon", http_status=r.status_code,
                        note="Amazon served its CAPTCHA page, so nothing on "
                             "it is this product -- retry later")

    facts = _amazon_facts(body, url)
    title = _AMZ_TITLE.search(body)
    # Amazon's title arrives HTML-escaped ("SMT Men&#39;s Traditional"), and
    # it is printed on the page and written into the CSV, so it is unescaped
    # once here rather than in every consumer.
    name = html.unescape(title.group(1)).strip() if title else None
    image = _AMZ_IMAGE.search(body)
    avail = _AMZ_AVAIL.search(body)
    avail_text = re.sub(r"\s+", " ", avail.group(1)).strip() if avail else ""

    pm = _AMZ_PRICE.search(body)
    if not pm:
        # No buybox. Almost always "Currently unavailable" -- and the page is
        # still full of other products' prices, so the only safe answer is
        # none. Say which it is, because "unavailable" and "we could not read
        # it" call for completely different work.
        gone = "currently unavailable" in body.lower()
        return _listing(
            url, site, "amazon", http_status=r.status_code, product_name=name,
            **facts,
            image=image.group(1) if image else None,
            note=("Amazon lists this product but is not selling it right now "
                  "(%s)" % (avail_text or "currently unavailable")) if gone else
                 ("no buybox price on the page -- not priced rather than "
                  "priced wrongly, because the page carries other products' "
                  "prices in its rails"))

    price = _num(pm.group(1))
    mm = _AMZ_MRP.search(body)
    mrp = _num(mm.group(1)) if mm else None
    # A struck price below the selling price is not an MRP; it is a neighbour.
    if mrp is not None and price is not None and mrp < price:
        mrp = None

    in_stock = None
    if avail_text:
        low = avail_text.lower()
        if "unavailable" in low or "out of stock" in low:
            in_stock = False
        elif "in stock" in low or "only" in low or "left" in low:
            in_stock = True

    labels = facts["sizes"] | ({facts["colour"]} if facts["colour"] else set())
    return _listing(url, site, "amazon", ok=True, http_status=r.status_code,
                    product_name=name, listed_mrp=mrp,
                    per_size=bool(facts["sizes"]),
                    image=image.group(1) if image else None,
                    note=("price from the buybox of this size's own listing"
                          if facts["sizes"] else
                          "product-level price from the buybox; Amazon "
                          "publishes one price per listing"),
                    variants=[Variant(labels=frozenset(labels), price=price,
                                      compare_at=mrp, available=in_stock,
                                      image=image.group(1) if image else None)],
                    **facts)


# Amazon gives every size (and colour) of a garment its own ASIN, and the
# page names them all: "dimensions" says which value is the size, and
# "dimensionValuesDisplayData" maps each sibling ASIN to its values. A file's
# link is one of them -- AJ Dezines 489's is the 3-4Y Pink -- and its buybox
# is that size's price only. Measured 26 Sep 2026: one Vastramay listing
# sells Red at 929 and Yellow at 839.
_AMZ_DIMS = re.compile(r'"dimensions"\s*:\s*(\[[^\]]*\])')
_AMZ_DVDD = re.compile(r'"dimensionValuesDisplayData"\s*:\s*(\{[^{}]*\})')
_AMZ_CUR = re.compile(r'"currentAsin"\s*:\s*"([A-Z0-9]{10})"')


def _amazon_facts(body, url):
    """{sizes, colour, siblings} for an Amazon page; empty when it has no
    size dimension (or the markup is not there)."""
    none = {"sizes": frozenset(), "colour": "", "siblings": ()}
    try:
        dims = json.loads(_AMZ_DIMS.search(body).group(1))
        table = json.loads(_AMZ_DVDD.search(body).group(1))
        cur = _AMZ_CUR.search(body).group(1)
    except Exception:
        return none
    if "size_name" not in dims or cur not in table:
        return none
    si = dims.index("size_name")
    ci = dims.index("color_name") if "color_name" in dims else None

    def parts(vals):
        vals = vals if isinstance(vals, list) else [vals]
        size = norm_size(vals[si]) if si < len(vals) else ""
        colour = (norm_size(vals[ci]) if ci is not None and ci < len(vals)
                  else "")
        return size, colour
    size, colour = parts(table[cur])
    if not size:
        return none
    host = urllib.parse.urlsplit(url).netloc
    sibs = tuple((s, c, "https://%s/dp/%s" % (host, asin))
                 for asin, vals in table.items() if asin != cur
                 for s, c in [parts(vals)] if s)
    return {"sizes": frozenset({size}), "colour": colour, "siblings": sibs}


def flipkart_listing(url):
    """Flipkart, read from the page's own schema.org Product.

    No adapter of its own is needed or wanted. The PDP carries exactly one
    application/ld+json Product block, which `ldjson_listing` already reads,
    and that block is the only honest source on the page.

    Do NOT be tempted to regex the rendered HTML for a rupee sign. One real
    PDP shows 323 (the price), 699 (the MRP), a "Buy at" figure that is a
    bank offer nobody without that card pays, and an eight-digit junk label.
    Picking a number off that page is exactly how this engine would publish
    a believable wrong one.

    One price per listing and no per-size breakdown, so per_size is False --
    the same honesty the Hopscotch reader keeps.
    """
    body = {}

    def facts(text):
        body["text"] = text
        return _flipkart_facts(text, url)
    lst = ldjson_listing(
        url, platform="flipkart", site="flipkart.com",
        note="product-level price; Flipkart publishes one price per listing",
        page_facts=facts)
    if lst.ok and lst.listed_mrp is None and lst.variants:
        mrp = _flipkart_mrp(body.get("text", ""), lst.variants[0].price)
        if mrp:
            lst = lst._replace(listed_mrp=mrp)
    return lst


# Flipkart's schema.org Product has no MRP. The page's own pricing record
# does: "ppd":{"fsp":1344,"finalPrice":1424,"mrp":2999,...} (28 Sep 2026),
# fsp being the price the schema.org block states. Other products' cards
# can carry records of their own, so only a record whose fsp IS this price
# counts, and only when every such record names one MRP.
_FK_PPD = re.compile(r'"ppd":\{([^{}]*)\}')


def _flipkart_mrp(body, price):
    """The listing's MRP from its pricing record, or None. Pure."""
    found = set()
    for m in _FK_PPD.finditer(body or ""):
        fsp = re.search(r'"fsp":\s*([0-9.]+)', m.group(1))
        mrp = re.search(r'"mrp":\s*([0-9.]+)', m.group(1))
        if fsp and mrp and price is not None and \
                abs(float(fsp.group(1)) - price) < 0.01:
            found.add(float(mrp.group(1)))
    mrp = found.pop() if len(found) == 1 else None
    return mrp if mrp and mrp >= price else None


# Flipkart gives every size its own pid; a link without ?pid= opens on a
# default size (AJ Dezines 489: 9-10 Years), and some listings sell one size
# only (the Vastramay 447 link is 2-3 Years and nothing else). The page's
# size selector links each size's pid; the product's own size is the swatch
# pointing at its own pid, or failing that its "Size" specification.
_FK_PID = re.compile(r'"productId"\s*:\s*"([A-Z0-9]{10,20})"')
_FK_SWATCH = re.compile(r'<a[^>]*href="([^"]*[?&](?:amp;)?pid=([A-Z0-9]{10,20})'
                        r'[^"]*swatchAttr=size[^"]*)"[^>]*>(.*?)</a>', re.S)
_FK_SPEC = re.compile(r'\{"text":\["([^"]{1,40})"\]\}\},"label_1":\{"value":'
                      r'\{"text":\["[^"]*"\]\}\},"label_0":\{"value":'
                      r'\{"text":"Size"')
_SIZE_WORDS = re.compile(r"\d+(?:\.\d+)?\s*(?:-|to)\s*\d+(?:\.\d+)?\s*"
                         r"(?:years?|yrs?|months?|mths?|y|m)\b|"
                         r"\d+(?:\.\d+)?\s*(?:years?|yrs?|months?|y|m)\b|"
                         r"\b(?:xxs|xs|s|m|l|xl|xxl|xxxl|free size)\b", re.I)


def _size_text(text):
    """'3 - 4 Years Only 2 left' -> '3-4Y'. The size part of a label."""
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())
    m = _SIZE_WORDS.search(text)
    return norm_size(m.group(0) if m else (text if len(text) <= 12 else ""))


def _flipkart_facts(body, url):
    none = {"sizes": frozenset(), "colour": "", "siblings": ()}
    # The link's own ?pid= names the product; failing that, the page's first
    # productId (the page's own product comes before any carousel's).
    parts = urllib.parse.urlsplit(url)
    pid = (urllib.parse.parse_qs(parts.query).get("pid") or [""])[0]
    if not pid:
        m = _FK_PID.search(body)
        pid = m.group(1) if m else ""
    swatches = {}
    for sm in _FK_SWATCH.finditer(body):
        size = _size_text(sm.group(3))
        if size:
            swatches.setdefault(sm.group(2), size)
    size = swatches.get(pid, "")
    if not size:
        spec = _FK_SPEC.search(body)
        size = _size_text(spec.group(1)) if spec else ""
    if not size:
        return none
    sibs = tuple((s, "", "https://%s%s?pid=%s" % (parts.netloc, parts.path, p))
                 for p, s in swatches.items() if p != pid and s != size)
    return {"sizes": frozenset({size}), "colour": "", "siblings": sibs}


# ------------------------------------------------------------------ firstcry --
# FirstCry gives every SIZE its own product id, and the product page carries
# the whole style as `CurrentProductDetailJSON`: one entry per size id, with
# that size's label (`sz`), MRP (`mrp`), discount percent to two decimals
# (`Dis`), colour (`hashCols`) and group (`Gid`). The selling price is
# mrp * (100 - Dis) / 100. Measured 23 Sep 2026 against the listing pages'
# own "Sale price RS x and Regular price RS y" labels: 9 of 9 agree to the
# paisa across three brands -- 399.05, 548.96, 588.97, 720.00, 854.43.
#
# It is the retailer's OWN MRP and discount, so the product is FirstCry's
# price for that size, not an estimate from Zoddle's MRP -- the objection
# that kept this channel closed was to the latter, and still stands.
#
# Traps, each measured:
#   * One group holds several colours. The hounds-tooth ballerina has two ids
#     at EU 26, priced 548.96 and 588.97. Only the linked id's colour counts.
#   * Price varies by size within one colour: the same style is 548.96 at
#     EU 31 and 588.97 at EU 25. So a size FirstCry does not list is NOT
#     priced from its neighbour -- `pick` refuses it for this platform.
#   * `qty` is 1 on every Boyz N Galz size and 0 on every other brand's,
#     live products included. Its meaning is unproven, so stock is unknown,
#     not sold out.
#   * A discontinued product answers 200, ~124KB, with an empty <title> and
#     no JSON. That page is what "FirstCry has no price" was measured on.
#   * Sizes arrive as "EU 31"; intake says "EUR 31" or a bare "31".
#
# robots.txt: our agent matches `User-agent: *`, which allows
# /<brand>/<slug>/<pid>/product-detail and disallows /svc/. The
# "*/product-detail" Disallow in that file is bingbot's alone.
_FC_JSON = "CurrentProductDetailJSON="
_FC_IMG = "https://cdn.fcglcdn.com/brainbees/images/products/438x531/"
_FC_EU = re.compile(r"(?:EURO|EUR|EU)?(\d{1,2}(?:\.\d)?)")


def _fc_labels(sz):
    """'EU 31' also answers to 'EUR 31' and a bare '31' -- intake uses both."""
    n = norm_size(sz)
    out = {n} if n else set()
    m = _FC_EU.fullmatch(n)
    if m and n != m.group(1):
        num = m.group(1)
        out |= {"EU" + num, "EUR" + num, num}
    return frozenset(out)


def _fc_price(entry):
    """mrp * (100 - Dis) / 100, or None when either figure is not sane."""
    mrp = _num(entry.get("mrp"))
    try:
        dis = float(entry.get("Dis"))
    except (TypeError, ValueError):
        return None
    if mrp is None or not 0 <= dis < 100:
        return None
    return round(mrp * (100 - dis) / 100.0, 2)


def firstcry_listing(url):
    """FirstCry, read from its product page's per-size JSON."""
    site = "firstcry.com"
    clean = urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl()
    m = re.search(r"/(\d{5,10})/product-detail", clean)
    if not m:
        return _listing(url, site, "firstcry",
                        note="no FirstCry product id in the link")
    pid = m.group(1)
    allowed, why = robots_allows(clean)
    if not allowed:
        return _listing(url, site, "firstcry", note=why)
    r, why = _get(clean)
    if r is None:
        return _listing(url, site, "firstcry", note=why)
    if r.status_code != 200:
        return _listing(url, site, "firstcry", http_status=r.status_code,
                        note=why)

    body = r.text
    i = body.find(_FC_JSON)
    if i < 0:
        if "has been discontinued" in body:
            return _listing(url, site, "firstcry", http_status=r.status_code,
                            note="FirstCry has discontinued this product")
        return _listing(url, site, "firstcry", http_status=r.status_code,
                        note="no per-size price data on the page -- page "
                             "shape changed")
    try:
        group, _ = json.JSONDecoder().raw_decode(body[i + len(_FC_JSON):])
    except ValueError:
        return _listing(url, site, "firstcry", http_status=r.status_code,
                        note="per-size price data was not parseable JSON")

    mine = group.get(pid) if isinstance(group, dict) else None
    if not isinstance(mine, dict):
        return _listing(url, site, "firstcry", http_status=r.status_code,
                        note="the product id in the link is not on its own "
                             "page's size list")

    def colour(e):
        return tuple(e.get("hashCols") or ()) or e.get("col")

    same = [e for e in group.values()
            if isinstance(e, dict) and colour(e) == colour(mine)]
    images = [_FC_IMG + f for f in (mine.get("Img") or "").split(";")
              if f and "sz." not in f]          # "sz" is the size chart
    variants = [Variant(labels=_fc_labels(e.get("sz")), price=_fc_price(e),
                        compare_at=_num(e.get("mrp")), available=None,
                        image=images[0] if images else None)
                for e in same]
    if _fc_price(mine) is None:
        return _listing(url, site, "firstcry", http_status=r.status_code,
                        product_name=mine.get("pn"),
                        note="no usable MRP and discount for this product")
    return _listing(url, site, "firstcry", ok=True, http_status=r.status_code,
                    product_name=html.unescape(mine.get("pn") or "") or None,
                    variants=variants, images=images,
                    image=images[0] if images else None,
                    note="FirstCry's own MRP less its own discount, per size")


# --------------------------------------------------------------- storefront --
def storefront_listing(url):
    """A brand's own product page, whatever the shop is built on.

    21 of the 24 own-site hosts in this corpus are Shopify, so that route is
    tried first and stays the fast path: one JSON request carrying every
    variant and every size. The remaining hosts are not a rounding error to
    the brands that own them -- boyzngalz.com and www.babyshop.in price real
    designs, and both reported nothing at all while the only route tried was
    /products/<handle>.json, which is a 404 on a store that is not Shopify.

    So a failed Shopify read falls through to the page's own schema.org
    Product rather than giving up. A page that is genuinely gone short-
    circuits, because a second request cannot un-delist it.
    """
    if not urllib.parse.urlsplit(url).path.rstrip("/"):
        # A bare domain is no product page, and "brand.com" + ".json" would
        # ask a different host ("brand.com.json") -- go straight to the page.
        return ldjson_listing(url)
    lst = shopify_listing(url)
    # Only a 410 short-circuits. A 404 must NOT: that is precisely how a
    # store which is not Shopify answers /products/<handle>.json, so
    # treating it as "gone" would skip the fallback in the one case it
    # exists for.
    if lst.ok or lst.http_status == 410:
        return lst
    alt = ldjson_listing(url)
    if alt.ok:
        return alt
    if _wants_browser(alt):
        # The page answered but shows its price only once a browser has
        # run it: the last resort.
        return _in_browser(url)
    # Neither route answered. Report whichever actually reached the product
    # page: "no such page" about a Shopify JSON endpoint that was never going
    # to exist tells a reader nothing about the garment.
    # A store that is not Shopify often answers the .json address with an
    # ordinary page ("not Shopify product JSON"); the page's own reason is
    # the one worth reporting then too.
    return alt if (lst.http_status == 404 or lst.note.startswith(
        "response was not Shopify")) else lst


# ------------------------------------------------------- own-site discovery --
# A brand storefront publishes its whole catalogue at /products.json. That is
# Shopify's documented route, it is robots-allowed on every store measured,
# and it is the answer to a failure the file itself causes: image_url_3 is
# meant to carry the brand's own product page, and it arrives blank for whole
# files -- all 15 Googo Gaaga rows give only a CDN image, so there was no
# own-site channel to price and the column was empty for reasons no adapter
# could fix. Finding the page beats depending on it.
CATALOGUE_PAGE = 250
CATALOGUE_PAGES = 12          # 3,000 products; no storefront here is close


def _catalogue_product(p, host):
    """One Shopify product in the shape discover.rank already ranks."""
    images = [{"src": im.get("src")} for im in (p.get("images") or [])
              if im.get("src")]
    return {
        "productId": "shopify:%s" % p.get("id"),
        "productName": p.get("title") or "",
        "brand": p.get("vendor") or "",
        "gender": "",                       # Shopify does not publish one
        "articleType": {"typeName": p.get("product_type") or ""},
        "searchImage": images[0]["src"] if images else None,
        "images": images,
        "pageUrl": "https://%s/products/%s" % (host, p.get("handle") or ""),
    }


def shopify_catalogue(host):
    """Everything a storefront lists, as {productId: product}, plus meta.

    Paged rather than taken in one request because Shopify caps a page at 250.
    A page shorter than the cap is the last one.
    """
    target = "https://%s/products.json" % host
    allowed, why = robots_allows(target)
    if not allowed:
        return {}, {"host": host, "products": 0, "note": why}
    out, pages = {}, 0
    for page in range(1, CATALOGUE_PAGES + 1):
        url = "%s?limit=%d&page=%d" % (target, CATALOGUE_PAGE, page)
        r, why = _get(url, host)
        if r is None or r.status_code != 200:
            return out, {"host": host, "products": len(out), "pages": pages,
                         "note": "%s from /products.json" % (why or "no response")}
        try:
            got = r.json().get("products") or []
        except Exception:
            return out, {"host": host, "products": len(out), "pages": pages,
                         "note": "/products.json was not JSON"}
        pages += 1
        for prod in got:
            item = _catalogue_product(prod, host)
            out[item["productId"]] = item
        if len(got) < CATALOGUE_PAGE:
            break
    return out, {"host": host, "products": len(out), "pages": pages, "note": ""}


# ----------------------------------------------------------------- dispatch --
def platform_for(url):
    """Which adapter handles this URL, by host. Returns (name, callable|None)."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    for bad, why in UNSUPPORTED.items():
        if host == bad or host.endswith("." + bad):
            return "unsupported:" + bad, None
    if "myntra.com" in host:
        return "myntra", myntra_listing
    if "hopscotch.in" in host:
        return "hopscotch", hopscotch_listing
    if "flipkart.com" in host:
        return "flipkart", flipkart_listing
    if "amazon." in host:
        return "amazon", amazon_listing
    if "firstcry.com" in host:
        return "firstcry", firstcry_listing
    return "storefront", storefront_listing   # Shopify first, then ld+json


# Failures that say nothing about the product -- the site did not answer,
# refused for now, or asked us to slow down -- so fetching again can help.
# A "no price" that IS an answer (size not sold, sold out, delisted, 404)
# is not here. Each maps to the plain reason the page shows.
_RETRYABLE = (("slow down", "Amazon asked us to slow down -- Refetch in about "
                            "10 minutes"),
              ("CAPTCHA", "Amazon showed a security check"),
              ("fetch failed", "the site did not answer"),
              ("still after", "the site kept refusing"),
              ("HTTP 403", "the site refused"),
              ("adapter raised", "the page could not be read"))


# What an empty cell means (Abhisekh, 26 Sep 2026): "no price" is kept for
# "this site does not sell this size"; a size it lists but has sold out is
# "out of stock"; a page that could not be read is "not fetched".
_SOLD_OUT = ("not selling it right now", "not selling this size right now",
             "out of stock", "sold out", "notify me")
_NOT_SOLD = ("does not sell size", "does not list size", "discontinued",
             "delisted", "no such page", "no variant matches",
             "sent this link to another page")


def empty_kind(note):
    """What an empty cell says, from the reason the channel gave:

      "out of stock"  the size is listed but sold out
      "no price"      the site does not sell this size (or the product)
      "not in rupees" read, but priced in another currency
      "check page"    read, but two prices answer to this size
      "not allowed"   the site's robots.txt forbids the page
      "not fetched"   the page could not be read -- ALWAYS in the pop-up,
                      and Refetch retries it (Abhisekh, 26 Sep 2026: a
                      "not fetched" with no pop-up made no sense)
    """
    low = (note or "").lower()
    if any(k in low for k in _SOLD_OUT):
        return "out of stock"
    if any(k in low for k in _NOT_SOLD):
        return "no price"
    if "not rupees" in low:
        return "not in rupees"
    if "different prices match" in low or "no usable price" in low:
        return "check page"
    if "disallowed by robots" in low:
        return "not allowed"
    return "not fetched"


def retry_reason(lst):
    """Why this page is worth reading again, in plain words -- "" when
    its answer is a real one. Exactly the pages whose cells say "not
    fetched", so every such cell is in the pop-up."""
    if lst is None or lst.ok or empty_kind(lst.note) != "not fetched":
        return ""
    note = lst.note or ""
    return next((why for mark, why in _RETRYABLE if mark in note),
                "the page could not be read")


def fetch_listing(url):
    """One network request. Never raises; failure comes back as ok=False."""
    if not url or not url.startswith(("http://", "https://")):
        return _listing(url, "", "", note="not an http url")
    try:
        name, fn = platform_for(url)
    except ValueError:                 # "https://[shop.com/..." -- no address
        return _listing(url, "", "", note="not an http url")
    if fn is None:
        host = name.split(":", 1)[1]
        return _listing(url, host, name, note=UNSUPPORTED[host])
    try:
        return fn(url)
    except Exception as e:
        return _listing(url, urllib.parse.urlsplit(url).netloc, name,
                        note="adapter raised %s" % type(e).__name__)


PLATFORM_NAME = {"amazon": "Amazon", "flipkart": "Flipkart",
                 "myntra": "Myntra", "firstcry": "FirstCry"}


def sibling_for(lst, want=None, colour=None):
    """The URL of this listing's page for the wanted size, when the linked
    page is a different size of the same listing -- or None.

    This is the listing's own size selector, not a search: the file's link
    names the product, and choosing the size on it is what a shopper does.
    The colourway stays the linked page's own, unless the file's colour
    names another colour this listing sells.
    """
    wanted = wanted_sizes(want)
    if not wanted or not lst.sizes or not lst.siblings:
        return None
    colours = {c for _, c, _ in lst.siblings} | {lst.colour}
    pref = norm_size(colour)
    pref = pref if pref and pref in colours else lst.colour
    # The linked page already is this size -- and this colour, when the
    # file names one the listing sells (a Yellow barcode linked to the Red
    # 3-4Y page must move to the Yellow 3-4Y page, not keep Red's price).
    if any(w in lst.sizes or len(_covering(lst.sizes, w)) == 1
           for w in wanted) and (not pref or pref == lst.colour):
        return None
    for w in wanted:
        hits = [(c, url) for s, c, url in lst.siblings if s == w]
        if not hits:
            cover = _covering({s for s, _, _ in lst.siblings}, w)
            if len(cover) == 1:
                size = cover.pop()
                hits = [(c, url) for s, c, url in lst.siblings if s == size]
        if pref:
            hit = next((url for c, url in hits if c == pref or not c), None)
        else:
            # No colour to go by: only an unambiguous page will do.
            hit = hits[0][1] if len({c for c, _ in hits}) == 1 else None
        if hit:
            return hit
    return None


_SIZE_LABEL = re.compile(r"^(?:\d+(?:\.\d+)?-\d+(?:\.\d+)?[YM]|\d+(?:\.\d+)?[YM]"
                         r"|XXS|XS|S|M|L|XL|XXL|XXXL|\d?XL|FREESIZE"
                         r"|(?:EU|EUR|UK|US)\d+(?:\.\d)?"
                         # 30 Sep 2026: toddler 2T/3T, unit-less 1-2 / 2-3,
                         # and numbered sizes 22/24/26 are size lists too --
                         # a row asking another size is "no price" there.
                         r"|\d{1,2}T|\d{1,2}-\d{1,2}|\d{2})$")


def _is_size_label(label):
    """'2-3Y', '6-12M', '1Y', 'XL', 'EU31' -- a size, not a colour or a
    'Default Title'."""
    return bool(_SIZE_LABEL.match(label or ""))


def pick(lst, want=None, colour=None, chooser=None):
    """Resolve one barcode against an already-fetched Listing.

    Size alone is often not enough. peekaabookids.com prices a Tyohaar kurta
    set at 1199 in Blue and 1299 in Green at size 6-8Y, so a size-only match
    finds two prices and correctly refuses to pick. Two further signals close
    that, cheapest first:

      1. `colour` -- the intake `color` column against the variant's own Color
         option. Free, exact when the words agree, and it is right there in
         the file.
      2. `chooser(candidates)` -- an optional callback that compares the
         intake photograph against each candidate variant's photograph. It
         costs image downloads, so it is only ever consulted when colour has
         failed to narrow a genuine price disagreement.

    Only the chooser touches the network, and only through the caller. With
    neither signal available this still returns no price rather than guessing.
    """
    base = dict(site=lst.site, platform=lst.platform, url=lst.url,
                product_name=lst.product_name, listed_mrp=lst.listed_mrp,
                article_type=lst.article_type, http_status=lst.http_status)
    hero = lst.image or next((v.image for v in lst.variants if v.image), None)

    def bad(note, matched=None, image=None):
        return Quote(ok=False, sale_price=None, in_stock=None,
                     size_matched=matched, note=note,
                     image=image or hero, resolved_by="", **base)

    if not lst.ok:
        return bad(lst.note)

    wanted = wanted_sizes(want)
    picked, matched, used = lst.variants, None, None
    if wanted:
        matched = False
        for cand in wanted:
            hits = [v for v in lst.variants if cand in v.labels]
            if hits:
                picked, matched, used = hits, True, cand
                break
        if matched is False:
            every = {l for v in lst.variants for l in v.labels}
            for cand in wanted:
                cover = _covering(every, cand)
                if len(cover) == 1:
                    label = cover.pop()
                    picked = [v for v in lst.variants if label in v.labels]
                    matched, used = True, label
                    break

    # A page that is ONE size's listing (Amazon, Flipkart) prices that size
    # only. compare.run has already moved to the wanted size's own page
    # where the listing has one (sibling_for); reaching here means it has
    # none, so this size is not sold on this listing.
    if wanted and lst.sizes and not any(
            w in lst.sizes or len(_covering(lst.sizes, w)) == 1
            for w in wanted):
        sold = sorted(lst.sizes | {s for s, c, _ in lst.siblings
                                   if not lst.colour or not c
                                   or c == lst.colour})
        return bad("this %s listing does not sell size %s (it sells %s)"
                   % (PLATFORM_NAME.get(lst.platform, lst.site),
                      "/".join(wanted), ", ".join(sold)), False)

    # FirstCry prices each size separately and they differ within one colour
    # (548.96 at EU 31, 588.97 at EU 25), so a size it does not list has no
    # price there -- not its neighbour's, even when the rest happen to agree.
    if matched is False and lst.platform == "firstcry":
        listed = sorted({l for v in lst.variants for l in v.labels
                         if not l.isdigit() and not l.startswith("EUR")})
        return bad("FirstCry does not list size %s for this colour (it lists "
                   "%s)" % ("/".join(wanted), ", ".join(listed) or "none"),
                   matched)

    # A page that lists its sizes and not this one does not sell this size,
    # even when every size it does sell shares one price (Abhisekh, 26 Sep
    # 2026: "it should be no price"). Vastramay 4560 3-4Y..6-7Y used to show
    # 1079 from a page selling 6-12M..2-3Y. A page with no size list at all
    # (one product-level price) is still priced.
    if matched is False:
        listed = sorted({l for v in lst.variants for l in v.labels
                         if _is_size_label(l)})
        if listed:
            return bad("this page does not sell size %s (it sells %s)"
                       % ("/".join(wanted), ", ".join(listed)), matched)

    resolved_by = "size" if matched else ""
    extra = ""

    # The file's colour picks that colourway even when every colour shares
    # the price. The price cannot change -- they agree -- but the photograph
    # does: BownBee 2255 is the maroon set on a page whose first colour is
    # blue, and the thumbnail showed the blue one (24 Sep 2026).
    want_colour = norm_size(colour)
    if want_colour and len({v.price for v in picked} - {None}) == 1:
        by_colour = [v for v in picked if want_colour in v.labels]
        if by_colour:
            picked = by_colour

    # More than one price still answering to this size? Narrow it.
    if len({v.price for v in picked} - {None}) > 1:
        want_colour = norm_size(colour)
        if want_colour:
            by_colour = [v for v in picked if want_colour in v.labels]
            if by_colour and len({v.price for v in by_colour} - {None}) == 1:
                picked, resolved_by = by_colour, "colour"
                extra = "colourway %s chosen from the intake colour" % want_colour

        if len({v.price for v in picked} - {None}) > 1 and chooser:
            chosen, why = chooser(picked)
            if chosen:
                picked, resolved_by = chosen, "image"
                extra = why

    prices = {v.price for v in picked} - {None}
    if not prices:
        return bad("no usable price on the matching variants", matched)
    if len(prices) > 1:
        listed = ", ".join("%.0f" % p for p in sorted(prices))
        if not any(_is_size_label(l) for v in picked for l in v.labels):
            # Prices that no size name tells apart: two prices for one
            # link, not a size the page does not sell.
            return bad("%d different prices match this page and none of "
                       "them names a size (%s)" % (len(prices), listed), matched)
        return bad(("no variant matches %s, and the variants carry %d "
                    "different prices (%s)"
                    % ("/".join(wanted), len(prices), listed))
                   if matched is False else
                   ("%d different prices match size %s (%s) and neither the "
                    "intake colour nor the photograph separates them"
                    % (len(prices), used, listed)), matched)
    price = prices.pop()

    # Retailer MRP, information only. "0.00", "" and a copy of the price all
    # mean "no discount".
    listed_mrp = base["listed_mrp"]
    if listed_mrp is None:
        others = {v.compare_at for v in picked} - {None, price}
        listed_mrp = max(others) if others else None

    flags = [v.available for v in picked if v.available is not None]
    in_stock = None if not flags else (1 if any(flags) else 0)

    note = lst.note
    if matched is False:
        note = (("no variant matches %s; all %d variants share one price"
                 % ("/".join(wanted), len(lst.variants)))
                if lst.per_size and len(lst.variants) > 1 else
                ("size %s not listed; price is product-level"
                 % "/".join(wanted)) if not lst.per_size else
                "single-variant product")
    elif used and wanted and used != wanted[0]:
        note = "matched on %s rather than %s" % (used, wanted[0])
    if extra:
        note = "; ".join(x for x in (extra, note) if x)

    base["listed_mrp"] = listed_mrp
    return Quote(ok=True, sale_price=price, in_stock=in_stock,
                 size_matched=matched, note=note, resolved_by=resolved_by,
                 image=next((v.image for v in picked if v.image), hero), **base)


def fetch(url, want_size=None, colour=None, chooser=None):
    """Fetch and pick in one call. For tests and one-off checks."""
    return pick(fetch_listing(url), want_size, colour, chooser)
