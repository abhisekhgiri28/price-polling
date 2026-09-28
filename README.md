# Price Polling

**Give it a product link and it reports today's selling price.**

You upload a link file, or pick a brand you saved earlier. For every
barcode, the engine reads the price of that size on each channel the file
has a link for, then names the cheapest one. The channels are:

- the brand's own website
- Myntra
- FirstCry
- Amazon
- Flipkart

It never searches, ranks or guesses. Every price it shows comes from a link
a person put in the input file.

---

## Setup

Python 3.13 on Windows 11 (other platforms should work, but only Windows is
tested).

```
pip install -r requirements.txt
python -m playwright install chromium      # once; only needed for the headless browser
```

| Package | Used for |
| --- | --- |
| `requests` | Fetching product pages |
| `openpyxl` | Reading `.xlsx` input files; the brand catalogue |
| `playwright` | Optional. Used as a last resort for sites that show no price until a browser runs them, and by the spot-check and weekly check |

The web app itself uses only the standard library. Nothing in the engine
calls an AI model.

## Running it

```
python engine/serve.py                   # opens http://127.0.0.1:8770 in the browser
python engine/serve.py --log             # same, printing each request
python engine/serve.py --port 8771 --no-open   # a throwaway test server
python engine/compare.py <file.csv|.xlsx> [-o out.csv]   # no browser, CSV out
python engine/validate.py                # 329 offline tests (~100 s)
python engine/spotcheck.py               # the app's MRP vs the MRP a shopper sees
python engine/spotcheck.py --recheck spotcheck_<date>.csv   # only the misses
python engine/weekly.py                  # the weekly health check, by hand
```

The app binds to `127.0.0.1` only. It accepts uploads and makes outbound
requests, so it is never exposed to a network.

### The page

- **Three panels:** Upload a file, Fetch a saved brand (you can list
  design codes, separated by commas), and Template.
- **Results** appear under the panels on the same page, so another fetch
  can start straight away. They come in two views, **List** and
  **Thumbnails**, with **Excel** and **CSV** downloads.
- **List columns:** Barcode, Design, Size, Zoddle, one column per
  marketplace, Lowest price, Lowest source. The own-website column is
  headed with the brand's name.
- **Each marketplace cell** shows one of these, and each one links to its
  page, with the reason in a tooltip:

| Cell | Meaning |
| --- | --- |
| a price | The price for that size |
| **out of stock** | The site lists the size but it is sold out (shown beside its price when the page gives one) |
| **no price** | The site does not sell this size, or the product is delisted |
| **not fetched** | The page could not be read. It always appears in the pop-up, and Refetch retries it |
| not in rupees / check page / not allowed | Priced in another currency / two prices the colour cannot separate / disallowed by robots.txt |

- **Lowest always names a source.** When no marketplace price comes back,
  Zoddle is the lowest.
- **Skipped links pop up** after a run, with the reason for each.
  **Refetch** re-reads only those links; **Ignore** closes the pop-up.

### Downloads

The columns are, in order:

1. `design_code`, `zoddle_barcode`, `size`, `mrp`, `zoddle_price`
2. the file's other columns, with each link replaced by its price
3. Lowest price, Lowest source

Wherever there is no price, the cell shows `-`. The Excel file keeps prices
as numbers and centres the `-`.

## The input file

The file can be `.csv` or `.xlsx` (`/template.csv` on the page gives a blank
one).

- **Required columns:** `zoddle_barcode`, `design_code`, `size`,
  `zoddle_price`, `image`.
- **Optional columns:** `mrp`, design name, `brand`, `colour`, brand size.
- **Links:** one link column per marketplace. The heading names the
  marketplace, and a link under the wrong heading is skipped with a
  reason.
- **One link per barcode per marketplace.** That link is the listing; it
  is never second-guessed by photograph or by page title.
- **Colour matters.** When one page sells several colours, `colour`
  picks the right colour's price and photo.

Every upload is saved to `data/brand_catalogue.xlsx`, one sheet per brand, so
a saved brand can be fetched again from the dropdown with no file. The merge
works per design code:

- A new file's link replaces the old one.
- A marketplace the new file leaves blank keeps its old link.
- New designs and barcodes are added.
- If Excel has the workbook open, the save is refused rather than lost.

## How each channel is read

| Channel | How |
| --- | --- |
| Shopify brand sites | `/products/<handle>.json` for per-size prices, plus `.js` for per-size stock |
| Other brand sites | The site's own live page data first, then schema.org `ld+json`, then Open Graph, then (last resort) a headless browser. See below |
| Myntra | `window.__myx`: each size's seller price and MRP |
| FirstCry | The product page's per-size JSON: price = mrp × (100 − Dis) / 100 |
| Amazon | The buybox only. Every size and colour is its own page |
| Flipkart | Its schema.org Product. Every size is its own page. The MRP comes from the page's own pricing record |

**Other brand-site platforms:**

| Platform | How it is read |
| --- | --- |
| WooCommerce | `data-product_variations`, or the public Store API when the page leaves it out |
| SAP Commerce (Fabindia) | The page's live `ng-state` data |
| Aditya Birla (Jaypore, Pantaloons, Allen Solly) | Next.js live data |
| Landmark (Max, Lifestyle) | `__NEXT_DATA__` |
| Amazon's shop builder (Navtan) | `__NEXT_DATA__` catalog variants |
| Hopscotch | Its own page data |
| Nushop (NUSYL) | Customer SKUs in the page state |
| Wix Stores | `productItems` in the page data |
| Salesforce SFRA (Biba) | schema.org price plus the size buttons |
| Fynd (Mothercare) | APP_DATA `sizes` |
| Magento and custom sites | schema.org |
| Pages that show no price until a browser runs them (The Souled Store) | `engine/browser.py` |

**A size is priced from its own page.** Amazon and Flipkart link a
listing's other sizes, and the engine follows the listing's own size
selector to the row's size. Because age cuts differ between brands, a page
size that wholly covers the row's size counts as that size (for example,
1-2Y covers 12-18M).

## Hard rules

1. **Polling only.** A price must always trace back to a URL in the input
   file. Anything that searches for a listing does not belong here.
2. **Return nothing rather than something plausible.** Every trap met so
   far produced a believable wrong number, not an error.
3. **A blank is a result.** Every barcode gets a row for every channel it
   has a link for, and an empty price always carries its reason.
4. **MRP for comparing prices always comes from Zoddle.** A retailer's MRP
   is recorded for information only.
5. **robots.txt is honoured per host.**
6. **One request per host per second** (Amazon: one every 3 s). A host that
   answers 429 has its pace doubled, up to 8 s.
7. **Never ship a run whose audit fails.** `engine/audit.py` runs on every
   comparison.
8. **Localhost only.**
9. **Run `python engine/validate.py` after touching any reader.**
10. **Nothing is disguised to get past a site that refuses us.** Adidas,
    H&M and Ajio answer 403 and stay unread.

## The checks

### Spot-check (`engine/spotcheck.py`)

Compares **the MRP the app reads** with **the MRP a shopper sees** on the
page, which it loads in a headless browser. It covers every saved brand's
website links and the links in `spotcheck_links.csv` (optional).

- **What counts as the page's MRP:** a price that is struck through or
  labelled "MRP". If neither is shown, there is no discount, so the price
  shown is the MRP.
- **Price ranges are not compared.** On such pages a size's MRP shows only
  once a size is chosen, so the verdict is "can't tell".
- **Verdicts:** MATCH, DIFFERENT, APP MISSED, CAN'T TELL, BOTH EMPTY.
- **Output:** writes `spotcheck_<date>.csv`.

The selling price is no longer compared here. The earlier selling-price
comparison is in this repository's first commit (`engine/spotcheck.py`).

### Weekly health check (`engine/weekly.py`)

This is meant to run every Monday at 11 am from Windows Task Scheduler. For
every saved brand and every channel it links, it takes 2 random links (a
different pair each week):

- **Each link must read.** A product that is gone counts as an answer, and
  another link is tried in its place.
- **Each link that reads has its MRP spot-checked,** on every channel:
  - Amazon, Flipkart and FirstCry from their own price box in a headless
    browser;
  - Myntra from its page data, because it refuses the headless browser.
- **Failures are retried** after 15 minutes.

It opens `weekly_check_issues_<date>.html` **only when something is wrong**.
It keeps its results in `checks/` and never writes to the catalogue.

## Files

| File | What it does |
| --- | --- |
| `engine/serve.py` | The web page: upload, saved-brand dropdown, template, results, downloads |
| `engine/linkfile.py` | Reads the input file, checks each link's heading, builds the Excel/CSV downloads |
| `engine/catalogue.py` | Saves uploads to `data/brand_catalogue.xlsx`, one sheet per brand |
| `engine/sites.py` | Visits each link and reads the price for the size and colour; pacing, retries, robots.txt |
| `engine/compare.py` | One row per barcode with every channel's price, the lowest price and its source |
| `engine/audit.py` | Hard checks on a run's numbers before they are trusted |
| `engine/browser.py` | The headless-browser reader (last resort), plus the page scripts the checks use |
| `engine/spotcheck.py` | The MRP spot-check |
| `engine/weekly.py` | The weekly health check |
| `engine/validate.py` | 329 offline tests |
| `data/brand_catalogue.xlsx` | The saved brands (one sheet per brand): barcodes, Zoddle prices, MRPs, each marketplace's link |

## Updates

**29 Sep 2026**

- **This repository** (private) holds the engine, `requirements.txt`,
  this README and the brand catalogue (`data/`).

**28 Sep 2026**

- **The spot-check and the weekly check compare the MRP, not the selling
  price.** Runs still report selling prices. Along the way:
  - The app now reads the MRP for Flipkart (from its pricing record), for
    Myntra per size (sizes can differ, e.g. 1999 and 2199), and for Biba
    and Panash (from their own page markup).
  - The page script now picks up an MRP labelled "MRP", and Flipkart's
    struck MRP, which it prints with no rupee sign.
  - **Measured:** 72 of 77 website links MATCH with none wrong, and every
    link that read in a weekly trial matched, on all five channels.
- **Weekly health check added:** `engine/weekly.py`, with a mandatory
  spot-check on every channel. It reports only when something fails.
- **Spot-check added** (`engine/spotcheck.py`). Stale schema.org tags had
  been the biggest source of wrong website prices (Fabindia, Jaypore,
  Pantaloons).
- **Headless-browser last resort** (`engine/browser.py`). It is paced,
  follows robots.txt, reads rupees only, and refuses rather than guesses.
- **New readers:**
  - Hopscotch, now per size again;
  - Nushop, Wix, the WooCommerce Store API, Salesforce size buttons and
    Fynd (Mothercare).
- **Prices not in rupees are refused.** Little Muffet and Aachho price in
  USD.
- **A page that says "Page Not Found"** but is sent as a normal page is
  read as delisted.
- **Amazon:**
  - Amazon is paced at one page every 3 s.
  - A run reads its sites side by side, so the slower pace does not make
    it longer.
  - Amazon's "slow down" page is waited on briefly; after that, Refetch.

**26 Sep 2026**

- **A stress test fixed 17 bugs.** The biggest: Amazon and Flipkart give
  every size its own page, and one size's price had been shown on every
  size.
- **Every cell says one of four things:** a price, out of stock, no price
  or not fetched.
- **The own website shows sold-out sizes,** read from Shopify's `.js`.
- **Pop-up for skipped links,** with Refetch and Ignore.
- **A size the page does not sell is "no price",** with the covering-size
  rule for different age cuts.

**24 Sep 2026**

- **The page was cut down to** the inputs and the results: List and
  Thumbnails, every price a link, Lowest price and Lowest source.
- **Downloads** became the input file with prices filled in, `-` for none,
  and an Excel copy.
- **Photograph matching, name matching and the old intake format were
  removed.** The file's link is the listing.
- **Several design codes** can be listed, separated by commas.

**23 Sep 2026**

- **Split out of the ZOCS Price Watch project** to do polling only.
- **A new input file** with one link column per marketplace, and Amazon
  and Flipkart in their own columns.
- **FirstCry priced** from its product page.
- **The brand catalogue:** uploads are saved per brand and can be fetched
  again with no file.
