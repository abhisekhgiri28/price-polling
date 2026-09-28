"""The last resort: read a page the way a shopper's browser shows it.

Some brand sites build their price in the browser: the page the server
sends carries no price, and the product data behind it comes from the
site's own API with a key only its page script holds (thesouledstore.com,
28 Sep 2026). Such a key is never forged or copied. Instead a real
(headless) Chromium loads the page as a shopper would, the site's own
script fetches its own data, and the rendered page is read.

It is used ONLY when every cheaper reader in `sites.py` returned nothing
from a page that did answer, and it keeps the same rules:

  * robots.txt: every page and data request the page makes to the site's
    own hosts is checked, and a disallowed one is blocked, not fetched.
  * pace: the page load waits its turn like any other request.
  * rupees only, and no price rather than a guess -- see `SHOWN_JS`.

    shown = render(url, allows)   # dict, or {"error": "..."}

`allows(url)` is `sites.robots_allows`. Playwright is optional: without it
(or without its Chromium) `available()` is False and the page is reported
as needing a browser.
"""
import threading, urllib.parse

try:
    from playwright.sync_api import sync_playwright
except Exception:                       # not installed
    sync_playwright = None

LOAD_TIMEOUT_MS = 30000     # the page itself
IDLE_TIMEOUT_MS = 25000     # waiting for the price to show and settle
SETTLE_MS = 1500            # a last beat for late price widgets

# One browser at a time: Chromium is ~150MB of memory, and this is a
# fallback, not the main road.
_lock = threading.Lock()

# What the page shows beside its title, read in the page. The price block
# is found from the product's own heading (<h1>) outwards: the first
# enclosing block that shows a rupee amount is the product's, which keeps
# the "similar products" rails -- dozens of other prices further down --
# out of it (the Amazon trap). Lines about offers, savings, EMIs and
# coupons are not the selling price and are dropped. A struck-through
# amount is the site's MRP. More than one remaining amount means the page
# shows a range or several prices, and nothing is chosen.
SHOWN_JS = r"""
() => {
  const RUPEE = /(?:₹|\bRs\.?|\bINR)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)/gi;
  const FOREIGN = /(?:US\$|\$|€|£|\bUSD|\bAED|\bEUR|\bGBP)\s*[0-9]/;
  // Words before an amount that make it something other than the price:
  // "You save", "Best price", "M.R.P.", "or Pay Rs 373 now" (pay-later,
  // vastramay.com), "SHOP ABOVE Rs 2,499" (ajdezines.com), "as low as Rs
  // 712" (nusyl.com), "orders between Rs 500 and Rs 4999" (pantaloons.com).
  const NOT_PRICE = /\bsave|saving|\boff\b|\bemi\b|coupon|cashback|best price|reward|points|member|exclusive|\bclub\b|free (?:shipping|delivery)|orders?|minimum|use code|\bbank\b|wallet|\bM\.?R\.?P\b|\bpay\b|\babove\b|\bover\b|\bbelow\b|\bunder\b|\bbetween\b|as low as|\bupto\b|\bup to\b|\bworth\b|\bget\b|\bflat\b/i;
  // "Rs 500 and Rs 4999", "Rs 500 - Rs 999": an amount joined to an
  // excluded one by a short connector is excluded with it.
  // A plain space is NOT a join: "MRP Rs 1,279 Rs 799" (bownbee.com) is
  // an MRP, then the price.
  const JOIN = /^\s*(?:-|–|to|and|&)\s*$/i;
  // ...and after it: "Rs 150 OFF", "Shop Rs 2,499+ & save big"
  // (ajdezines.com), "Rs 999 onwards", and a number run into a percentage
  // ("Rs 1799" then "30% off" read as "179930%", pantaloons.com).
  const AFTER_NOT_PRICE = /^(?:\s*(?:off\b|discount|saved?\b|cashback|\/\s*month|per month|onwards|and above|& above)|\s*\+|%)/i;
  const SOLD = /sold out|out of stock|notify me|currently unavailable/i;
  // A size button's whole text: 2-3Y, 6-12 months, XS, 3XL, EU 31, 32.
  const SIZE = /^(?:\d{1,2}(?:\.\d)?\s*-\s*\d{1,2}(?:\.\d)?\s*(?:y|yrs?|years?|m|mths?|months?)|\d{1,2}\s*(?:y|yrs?|years?|m|mths?|months?)|xxs|xs|s|m|l|xl|xxl|xxxl|[2-6]xl|free\s*size|(?:eu|uk|us)\s*\d{1,2}(?:\.\d)?|\d{2})$/i;
  const GONE = /out.?of.?stock|outstock|sold|disabled|unavailable|strike|oos\b|not-available|inactive/i;
  const BUY = /add to (?:cart|bag|basket)|buy now/i;
  // The product's heading: its <h1>, or -- on pages with none (nusyl.com,
  // pantaloons.com) -- the largest short text that the page's own title
  // names, which is the product's name as displayed.
  const titled = () => {
    const title = (document.title || "").toLowerCase();
    let best = null, size = 0;
    for (const el of document.body.querySelectorAll("*")) {
      if (el.children.length > 2) continue;
      const t = (el.innerText || "").trim().toLowerCase();
      if (t.length < 8 || t.length > 200 || !title.includes(t)) continue;
      const r = el.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) continue;
      const fs = parseFloat(getComputedStyle(el).fontSize) || 0;
      if (fs > size) { best = el; size = fs; }
    }
    return best;
  };
  // The <h1> a shopper can SEE: amazon.in's first <h1> is a hidden
  // "Product summary presents key product information" for screen readers.
  const seen = e => { const r = e.getBoundingClientRect();
                      return e.innerText.trim() && r.width > 2 && r.height > 2; };
  const h1 = [...document.querySelectorAll('h1')].find(seen) || titled();
  const out = {title: document.title, h1: h1 ? h1.innerText.trim() : "",
               og_image: (document.querySelector('meta[property="og:image"]') || {}).content || "",
               sell: [], struck: [], foreign: false, sold_out: null, level: -1,
               sizes: [], gone: false};
  // A shop that answers a removed product with an ordinary page saying
  // so (nusyl.com, 28 Sep 2026: "Page Not Found -- The page you're
  // looking for does not exist", sent as a success).
  const GONE_PAGE = /page not found|page (?:you(?:'|’)re|you are) looking for (?:does not|doesn't) exist|no longer available|product (?:is )?(?:not found|unavailable)|\b404\b/i;
  out.gone = GONE_PAGE.test(((document.body && document.body.innerText) || "").slice(0, 4000));
  if (!h1) return out;
  // Other products' shelves ("related", "similar", "you may also like")
  // and text meant only for screen readers ("Original price was: Rs
  // 1,199") are not what the shopper reads as this product's price:
  // hidden in this headless tab before anything is read (boyzngalz.com's
  // related shelf sits in the same block as its price).
  for (const el of document.querySelectorAll(
      '.related, .upsells, .cross-sells, ul.products, [class*="related"], ' +
      '[class*="similar"], [class*="recommend"], [class*="also-like"], ' +
      '.screen-reader-text, .sr-only, .visually-hidden')) {
    if (!el.contains(h1)) el.style.setProperty("display", "none", "important");
  }
  const amounts = text => {
    const got = [];
    text = text || "";
    let last = 0, dropped = false;
    for (const m of text.matchAll(RUPEE)) {
      // The words just before an amount say what it is: "You save Rs 63",
      // "Best price Rs 1466", "MRP Rs 999" (on the line above it, on
      // fabindia.com), "EMI from Rs 200". Never reaching back past the
      // amount before it.
      const before = text.slice(Math.max(last, m.index - 30), m.index);
      const joined = last > 0 && JOIN.test(text.slice(last, m.index));
      last = m.index + m[0].length;
      if (NOT_PRICE.test(before) || (dropped && joined)) { dropped = true; continue; }
      // ...or just after it: "Rs 150 OFF" (thesouledstore.com).
      if (AFTER_NOT_PRICE.test(text.slice(last, last + 20))) { dropped = true; continue; }
      dropped = false;
      const v = parseFloat(m[1].replace(/,/g, ""));
      if (v > 0) got.push(v);
    }
    return got;
  };
  const struckIn = root => {
    const got = [];
    for (const el of root.querySelectorAll("*")) {
      const st = getComputedStyle(el);
      if (["DEL", "S", "STRIKE"].includes(el.tagName) ||
          st.textDecorationLine.includes("line-through"))
        got.push(...amounts(el.innerText));
    }
    return got;
  };
  const shows = el => {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== "hidden";
  };
  // The size buttons near the price: each one's label, and whether the
  // page marks it gone (disabled, "out of stock"/"sold" classes, struck
  // through). Fewer than two is no size list.
  const sizesNear = box => {
    let scope = box;
    for (let up = 0; up < 4 && scope && scope !== document.body; up++) {
      const found = new Map();
      for (const el of scope.querySelectorAll("*")) {
        if (el.children.length > 1 || !shows(el)) continue;
        const t = (el.innerText || "").trim();
        if (!t || t.length > 16 || !SIZE.test(t) || found.has(t)) continue;
        let gone = false;
        // Up through the size's own wrappers only (span, label, li), never
        // into the list that holds every size.
        for (let e = el, i = 0; e && i < 4 && e !== scope &&
             (e.innerText || "").trim() === t; e = e.parentElement, i++) {
          const cls = (e.className && e.className.toString()) || "";
          if (GONE.test(cls) || e.disabled || e.getAttribute("aria-disabled") === "true" ||
              getComputedStyle(e).textDecorationLine.includes("line-through") ||
              [...e.querySelectorAll("input,button")].some(x => x.disabled)) {
            gone = true; break;
          }
        }
        found.set(t, !gone);
      }
      if (found.size >= 2) return [...found.entries()];
      scope = scope.parentElement;
    }
    return [];
  };
  let box = h1;
  for (let level = 0; level < 7 && box && box !== document.body; level++) {
    const text = box.innerText || "";
    if (text.length > 6000) break;           // grown into the whole page
    const all = amounts(text);
    if (all.length) {
      const struck = struckIn(box);
      const sell = [...all];
      for (const s of struck) {
        const i = sell.indexOf(s);
        if (i >= 0) sell.splice(i, 1);
      }
      out.sell = [...new Set(sell)];
      out.struck = [...new Set(struck)];
      out.level = level;
      out.foreign = FOREIGN.test(text);
      // Stock, from the same block and the one around it (where the
      // buttons usually sit): a sold-out word with no buy button.
      const around = (box.parentElement || box).innerText || "";
      if (SOLD.test(around) && !BUY.test(around)) out.sold_out = true;
      else if (BUY.test(around)) out.sold_out = false;
      out.sizes = sizesNear(box);
      return out;
    }
    if (FOREIGN.test(text)) { out.foreign = true; out.level = level; return out; }
    box = box.parentElement;
  }
  return out;
}
"""


# A marketplace's own price box, as a shopper sees it (28 Sep 2026). Used by
# the spot-check and the weekly check, never by a run: the app reads these
# sites from their page data; this is the second, independent look.
# Each answers in SHOWN_JS's shape: {h1, sell: [...], struck: [...]}.
MARKET_JS = {
    # amazon.in: the main price box at the top of the page.
    "amazon": r"""
() => {
  const R = /(?:₹|\bRs\.?)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)/;
  const first = el => { const m = el && (el.innerText || "").match(R);
                        return m ? parseFloat(m[1].replace(/,/g, "")) : 0; };
  const title = (document.querySelector('#productTitle') || {}).innerText || "";
  const box = document.querySelector('#corePriceDisplay_desktop_feature_div')
           || document.querySelector('#corePrice_feature_div')
           || document.querySelector('#corePrice_desktop');
  const out = {h1: title.trim(), sell: [], struck: [], sizes: []};
  if (!box) return out;
  const pay = first(box.querySelector('.priceToPay') ||
                    box.querySelector('.a-price:not(.a-text-price)'));
  const mrp = first(box.querySelector('.a-text-price'));
  if (pay > 0) out.sell = [pay];
  if (mrp > pay) out.struck = [mrp];
  return out;
}""",
    # firstcry.com: its "discounted price" box; the paise are a raised
    # <sup> after the rupees ("1423" and "93" = Rs 1,423.93).
    "firstcry": r"""
() => {
  const h1 = document.querySelector('h1');
  const out = {h1: h1 ? h1.innerText.trim() : "", sell: [], struck: [], sizes: []};
  const el = document.querySelector('.th-discounted-price .prod-price') ||
             document.querySelector('.prod-price');
  if (!el) return out;
  const sup = el.querySelector('sup');
  const rupees = [...el.childNodes].filter(n => n.nodeType === 3)
                   .map(n => n.textContent).join("").replace(/[^0-9]/g, "");
  const paise = sup ? (sup.innerText || "").replace(/[^0-9]/g, "") : "";
  const v = parseFloat(rupees + (paise ? "." + paise : ""));
  if (v > 0) out.sell = [v];
  const mrp = document.querySelector('.th-discounted-price del');
  const m = mrp ? parseFloat((mrp.innerText || "").replace(/[^0-9.]/g, "")) : 0;
  if (m > v) out.struck = [m];
  return out;
}""",
    # flipkart.com: class names are scrambled and change, so the price is
    # the LARGEST rupee amount on the first screen -- where a shopper's eye
    # goes -- leaving out offers ("Buy at", "off") and struck MRPs. Two
    # different amounts at that size: nothing is chosen.
    "flipkart": r"""
() => {
  const R = /(?:₹|\bRs\.?)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)/;
  const h1 = document.querySelector('h1');
  const out = {h1: h1 ? h1.innerText.trim() : "", sell: [], struck: [], sizes: []};
  let best = 0, found = [];
  for (const el of document.querySelectorAll('body *')) {
    const t = (el.innerText || "").trim();
    if (!t || t.length > 20 || !R.test(t)) continue;
    if ([...el.children].some(c => R.test(c.innerText || ""))) continue;
    if (/off|buy at|save|emi|bank|coupon/i.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.top > 1000) continue;
    const st = getComputedStyle(el);
    if (st.textDecorationLine.includes('line-through')) continue;
    const fs = parseFloat(st.fontSize) || 0, v = parseFloat(t.match(R)[1].replace(/,/g, ""));
    if (fs > best + 0.5) { best = fs; found = [v]; }
    else if (Math.abs(fs - best) <= 0.5 && !found.includes(v)) found.push(v);
  }
  out.sell = found;
  return out;
}""",
}


def market_probe(url):
    """The MARKET_JS page script for this marketplace link, or None."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    return next((js for key, js in MARKET_JS.items() if key + "." in host), None)


def available():
    return sync_playwright is not None


def _same_site(a, b):
    """api.thesouledstore.com and www.thesouledstore.com are one site."""
    ta, tb = a.lower().split(".")[-2:], b.lower().split(".")[-2:]
    return ta == tb


def _settled(page, js=None):
    """SHOWN_JS once the page has settled: the same answer twice in a row,
    SETTLE_MS apart, after a price has appeared -- or whatever it shows
    when IDLE_TIMEOUT_MS runs out. Waiting for the network to go quiet
    instead cost 30s a page (a chatty page never goes quiet)."""
    waited, last = 0, None
    while True:
        try:
            now = page.evaluate(js or SHOWN_JS)
        except Exception:
            now = None                     # navigating; try again
        if now and (now.get("sell") or now.get("foreign")) and now == last:
            return now
        if waited >= IDLE_TIMEOUT_MS:
            return now or {}
        last = now
        page.wait_for_timeout(SETTLE_MS)
        waited += SETTLE_MS


def render(url, allows, user_agent=None, js=None):
    """Load `url` in headless Chromium and read what it shows.

    Returns a dict: status, html (the rendered page), shown (SHOWN_JS's
    answer, or `js`'s -- a MARKET_JS script), blocked (requests robots.txt
    refused) -- or {"error": why}.
    Never raises.
    """
    if not available():
        return {"error": "the browser reader is not installed"}
    host = urllib.parse.urlsplit(url).netloc
    blocked = []

    def route(r):
        req = r.request
        try:
            if req.resource_type in ("document", "xhr", "fetch") and \
                    _same_site(urllib.parse.urlsplit(req.url).netloc, host):
                ok, _ = allows(req.url)
                if not ok:
                    blocked.append(req.url)
                    return r.abort()
            return r.continue_()
        except Exception:
            try:
                return r.abort()
            except Exception:
                return None

    with _lock:
        try:
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                try:
                    ctx = b.new_context(user_agent=user_agent, locale="en-IN",
                                        viewport={"width": 1300, "height": 1000})
                    page = ctx.new_page()
                    page.route("**/*", route)
                    # "commit": the page has started to arrive. flipkart.com
                    # never reached "domcontentloaded" within 30s, though
                    # it had long since shown its price; _settled waits for
                    # the price itself instead.
                    resp = page.goto(url, wait_until="commit",
                                     timeout=LOAD_TIMEOUT_MS)
                    shown = _settled(page, js)
                    try:                  # requests still in flight: let go
                        page.unroute_all(behavior="ignoreErrors")
                    except Exception:
                        pass
                    return {"status": resp.status if resp else None,
                            "html": page.content(), "shown": shown,
                            "blocked": blocked}
                finally:
                    b.close()
        except Exception as e:
            msg = str(e).splitlines()[0][:120] if str(e) else ""
            if "Executable doesn't exist" in msg:
                return {"error": "the browser reader is not installed "
                                 "(run: python -m playwright install chromium)"}
            return {"error": "the browser could not load the page (%s)"
                             % (type(e).__name__)}
