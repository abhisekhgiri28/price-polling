"""Local app: upload a link file, get today's prices back as a list and as
thumbnails, and download them.

The input is linkfile.py's format -- barcode, design code, size, Zoddle price,
an image link, and one column per marketplace named in its heading. The ZOCS
intake CSV the page used to take is retired.

Every upload is saved into the brand catalogue (catalogue.py:
data/brand_catalogue.xlsx, one sheet per brand), so a brand -- or some of its
designs -- can be re-polled later from a dropdown without uploading anything.
The runs themselves (the result pages) still live only in memory: restarting
the server clears them, never the catalogue.

Standard library only for serving (`cgi` was removed in Python 3.13, so the
multipart body is split by hand below). `requests` is used by the adapters.

Bound to 127.0.0.1: it accepts uploads and makes outbound requests, so it is
not something to expose on a network interface.

    python engine/serve.py --open        -> http://127.0.0.1:8770
"""
import sys, os, re, io, json, html, uuid, pathlib, argparse, threading
import urllib.parse
import collections
import webbrowser, datetime, time, subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import linkfile, compare, sites, catalogue, daily

MAX_UPLOAD = 32 * 1024 * 1024
JOBS = {}
JOBS_LOCK = threading.Lock()
KEEP_RUNS = 30


# ---------------------------------------------------------------- multipart --
def parse_multipart(body, content_type):
    """(field, filename, bytes) for each part. Minimal but strict: with no
    boundary it returns nothing rather than guessing."""
    m = re.search(r'boundary=("?)([^";]+)\1', content_type or "", re.I)
    if not m:
        return []
    sep = b"--" + m.group(2).encode("latin-1")
    out = []
    for part in body.split(sep):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        head, marker, data = part.partition(b"\r\n\r\n")
        if not marker:
            continue
        disp = ""
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-disposition:"):
                # Browsers send the file name as UTF-8; read as latin-1,
                # "Vastramay – list.xlsx" came back "Vastramay â list".
                try:
                    disp = line.decode("utf-8")
                except UnicodeDecodeError:
                    disp = line.decode("latin-1", "replace")
        name = re.search(r'\bname="([^"]*)"', disp)
        fname = re.search(r'\bfilename="([^"]*)"', disp)
        if not name:
            continue
        out.append((name.group(1), fname.group(1) if fname else None,
                    data[:-2] if data.endswith(b"\r\n") else data))
    return out


# --------------------------------------------------------------------- jobs --
def start_job(filename, raw, brand=""):
    """An upload: read it, save it into its brand's sheet, then poll it.

    Reading and saving happen now, because their errors belong to the upload.
    Fetching happens in a thread (slow, paced at one request per host per
    second). `brand` is the form's Brand field; a `brand` column in the file
    wins row by row.
    """
    records, issues, meta, enc = linkfile.parse_bytes(raw, filename)
    brand = (brand or "").strip()
    error, saved = None, {}
    if records:
        for rec in records:
            rec["brand"] = rec.get("brand") or brand or None
        unbranded = sum(1 for r in records if not r["brand"])
        if unbranded:
            error = ("choose the brand these products belong to -- %d %s "
                     "no brand, and every product is saved under its "
                     "brand's sheet" % (unbranded, "row has" if unbranded == 1
                                        else "rows have"))
        else:
            by_brand = collections.OrderedDict()
            for rec in records:
                by_brand.setdefault(rec["brand"], []).append(rec)
            try:
                for b, recs in by_brand.items():
                    saved[catalogue.sheet_name(b)] = catalogue.merge(b, recs)
            except catalogue.CatalogueLocked as ex:
                error = str(ex)
    job = start_run(filename, records if not error else [], issues, meta,
                    error=error, table=linkfile.read_table(raw, filename),
                    brand=brand)
    job["encoding"], job["saved"] = enc, saved
    return job


def start_run(label, records, issues=(), meta=None, error=None, table=None,
              brand=""):
    """Poll these records in a thread and return the job at once.

    `table` is the file's own rows (or the saved sheet's), kept so the CSV
    download can hand the same file back with prices in place of links."""
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id, "filename": label, "encoding": None,
        "records": records, "issues": list(issues), "meta": meta or {},
        "saved": {},
        "created": datetime.datetime.now().strftime("%H:%M:%S"),
        "urls": len({u for r in records for _, u in compare.channels_for(r)}),
        "done": 0, "total": 0, "where": "", "rows": None, "summary": None,
        "started": time.monotonic(), "finished": None,
        "state": "queued" if records and not error else "rejected",
        "error": error, "table": table, "brand": brand,
        # Every page read, kept so Refetch reads again only the ones that
        # failed for a passing reason; and whether the pop-up was ignored.
        "fetched": {}, "dismissed": False,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
        # Runs live in memory: keep the newest KEEP_RUNS, so a day of
        # fetching does not hold every page ever read (a running one stays).
        done = [k for k, j in JOBS.items()
                if j["state"] not in ("queued", "running")]
        for k in done[:max(0, len(JOBS) - KEEP_RUNS)]:
            del JOBS[k]
    if job["state"] == "rejected":
        if not job["error"]:
            first = next((i for i in job["issues"]
                          if i.severity == "reject"), None)
            job["error"] = first.detail if first else "nothing to compare"
        return job

    launch(job)
    return job


def launch(job):
    """Run (or re-run) a job's polling in a thread. A re-run reuses the
    pages already read and fetches only those that failed for a passing
    reason -- the pop-up's Refetch."""
    job.update(state="running", done=0, total=0, where="",
               started=time.monotonic(), finished=None, dismissed=False,
               plan={}, shown_pct=0, eta=None)

    def work():
        try:
            def progress(done, total, where):
                job["done"], job["total"], job["where"] = done, total, where
            job["rows"], job["summary"] = compare.run(
                job["records"], progress=progress, fetched=job["fetched"],
                plan=job["plan"])
            job["finished"] = time.monotonic()
            job["fetched_on"] = datetime.date.today()   # the download's name
            job["state"] = "done"
        except Exception as e:
            job["state"] = "failed"
            job["error"] = "%s: %s" % (type(e).__name__, e)

    threading.Thread(target=work, daemon=True).start()


def start_saved(brand, design_text=""):
    """Re-poll a saved brand -- all of it, or the design codes typed in."""
    wanted = list(collections.OrderedDict.fromkeys(
        d for d in re.split(r"[\s,;]+", design_text or "") if d))
    brand = catalogue.saved_name(brand)
    if brand not in {b for b, _d, _n in catalogue.brands()}:
        return start_run(brand, [], error="no brand called %s is saved -- "
                         "upload its file first" % brand, brand=brand)
    records, issues, meta = catalogue.records_for(brand, wanted or None)
    label = "%s -- %s" % (catalogue.sheet_name(brand),
                          ("designs " + ", ".join(wanted)) if wanted
                          else "all saved products")
    error = None
    if wanted and records:
        found = {r["design_code"] for r in records}
        missing = [d for d in wanted if d not in found]
        if missing:
            issues = list(issues) + [linkfile.Issue(
                0, None, "warn", "design_not_saved",
                "not in the %s sheet: %s" % (catalogue.sheet_name(brand),
                                              ", ".join(missing)))]
    elif wanted and not records:
        error = ("none of those design codes are saved for %s"
                 % catalogue.sheet_name(brand))
    return start_run(label, records, issues, meta, error=error,
                     table=catalogue.table_for(brand, wanted or None),
                     brand=catalogue.sheet_name(brand))


# -------------------------------------------------------------------- pages --
def e(x):
    return html.escape("" if x is None else str(x))


def money(v):
    return "&mdash;" if v in (None, "") else "&#8377;%s" % ("%.0f" % float(v))


CSS = """
:root{--ground:#eef1f3;--panel:#fff;--panel-2:#f6f8f9;--band:#f0f3f7;
 --ink:#101821;--ink-soft:#2f3d4a;--muted:#5b6975;--faint:#8a97a2;
 --line:#dce2e7;--line-2:#c6ced6;--accent:#28418c;--accent-ink:#1d3271;
 --accent-soft:#e8ecf7;--good:#136b3f;--good-soft:#e3f0e8;--bad:#a81f28;
 --bad-soft:#fbe9ea;--warn:#8a5a00;--warn-soft:#fdf1d8;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--ground:#0e1319;--panel:#161d25;
 --panel-2:#1b232c;--band:#1a232e;--ink:#e6ecf2;--ink-soft:#c2ccd6;
 --muted:#93a0ac;--faint:#6f7d89;--line:#252f3a;--line-2:#36424f;
 --accent:#8aa4e8;--accent-ink:#a9bef2;--accent-soft:#18203a;--good:#5cc286;
 --good-soft:#11241a;--bad:#f0787f;--bad-soft:#261416;--warn:#dfb45c;
 --warn-soft:#231c0d;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
 font:400 15px/1.55 -apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:0 22px 80px}
header{padding:32px 0 18px;border-bottom:2px solid var(--ink);margin-bottom:24px}
.eyebrow{font:500 11px/1 ui-monospace,Consolas,monospace;letter-spacing:.16em;
 text-transform:uppercase;color:var(--accent-ink);margin:0 0 9px}
h1{font:800 clamp(23px,4vw,34px)/1.05 "Helvetica Neue",Arial,sans-serif;
 letter-spacing:-.02em;margin:0 0 7px}
h2{font:700 19px/1.2 "Helvetica Neue",Arial,sans-serif;margin:32px 0 9px}
p.lede{color:var(--muted);margin:0;max-width:70ch}
a{color:var(--accent-ink)}
.figs{display:grid;grid-template-columns:repeat(2,1fr);gap:1px;
 background:var(--line);border:1px solid var(--line);border-radius:3px;
 overflow:hidden;margin:22px 0 0}
@media(min-width:760px){.figs{grid-template-columns:repeat(5,1fr)}}
.fig{background:var(--panel);padding:13px 15px}
.fig b{display:block;font:800 25px/1 "Helvetica Neue",Arial,sans-serif;
 margin-bottom:3px;font-variant-numeric:tabular-nums}
.fig span{font:500 10px/1.3 ui-monospace,Consolas,monospace;letter-spacing:.06em;
 text-transform:uppercase;color:var(--muted)}
.fig.bad b{color:var(--bad)} .fig.good b{color:var(--good)}
.drop{border:2px dashed var(--line-2);border-radius:6px;background:var(--panel);
 padding:24px;text-align:center}
.drop.over{border-color:var(--accent);background:var(--accent-soft)}
.drop input[type=file]{display:block;margin:12px auto}
button,.btn{font:600 14px/1 inherit;padding:11px 18px;border-radius:4px;
 border:1px solid var(--accent);background:var(--accent);color:#fff;
 cursor:pointer;text-decoration:none;display:inline-block}
.btn.ghost{background:transparent;color:var(--accent-ink)}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:4px;
 background:var(--panel);margin:0 0 18px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
thead th{font:500 10px/1 ui-monospace,Consolas,monospace;letter-spacing:.08em;
 text-transform:uppercase;color:var(--muted);text-align:left;padding:9px 11px;
 background:var(--panel-2);border-bottom:1px solid var(--line);white-space:nowrap;
 position:sticky;top:0}
tbody td{padding:8px 11px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.pill{font:500 10px/1 ui-monospace,Consolas,monospace;letter-spacing:.05em;
 text-transform:uppercase;padding:4px 6px;border-radius:2px;white-space:nowrap}
.pill.MATCH{color:var(--good);background:var(--good-soft);border:1px solid var(--good)}
.pill.MISMATCH{color:var(--bad);background:var(--bad-soft);border:1px solid var(--bad)}
.pill.none{color:var(--muted);background:var(--panel-2);border:1px solid var(--line-2)}
.pill.CANDIDATE{color:var(--warn);background:var(--warn-soft);border:1px solid var(--warn)}
.card.cand{border-left:3px solid var(--warn)}
.rank{font:600 11px/1 ui-monospace,Consolas,monospace;color:var(--warn)}
.pill.reject{color:var(--bad);background:var(--bad-soft);border:1px solid var(--bad)}
.pill.warn{color:var(--warn);background:var(--warn-soft);border:1px solid var(--warn)}
.pill.oos{color:var(--bad);background:var(--bad-soft);border:1px solid var(--bad)}
.pill.note{color:var(--muted);background:var(--panel-2);border:1px solid var(--line-2)}
.banner{border-radius:4px;padding:14px 17px;margin:0 0 18px;border:1px solid}
.banner.ok{border-color:var(--good);background:var(--good-soft)}
.banner.bad{border-color:var(--bad);background:var(--bad-soft)}
.banner.info{border-color:var(--accent);background:var(--accent-soft)}
.banner.warnb{border-color:var(--warn);background:var(--warn-soft)}
.banner b{display:block;margin-bottom:3px}
.banner p{margin:0;font-size:14px;color:var(--ink-soft)}
.bar{height:8px;background:var(--band);border-radius:99px;overflow:hidden;
 margin:10px 0 0;border:1px solid var(--line)}
.bar i{display:block;height:100%;background:var(--accent);
 transition:width .9s cubic-bezier(.4,0,.2,1)}
.progress{margin:26px 0 18px}
.pline{display:flex;align-items:baseline;justify-content:space-between;gap:16px}
.pline b{font-size:30px;font-variant-numeric:tabular-nums;letter-spacing:-.5px}
.pline span{font-size:14px;color:var(--ink-soft)}
.progress .bar{height:10px;margin-top:12px}
.task{margin:12px 0 0}
.task code{font-size:13px;color:var(--ink-soft);background:none;padding:0;
 word-break:break-all}
.right{float:right}
.tabs{display:flex;gap:0;margin:22px 0 4px;border-bottom:1px solid var(--line)}
.tabs a{padding:9px 15px;text-decoration:none;color:var(--muted);
 font-weight:600;font-size:14px;border:1px solid transparent;border-bottom:0;
 border-radius:4px 4px 0 0;margin-bottom:-1px}
.tabs a.on{background:var(--panel);color:var(--ink);border-color:var(--line);
 border-bottom:1px solid var(--panel)}
.cards{display:grid;grid-template-columns:1fr;gap:14px;margin:0 0 18px}
@media(min-width:700px){.cards{grid-template-columns:repeat(2,1fr)}}
@media(min-width:1080px){.cards{grid-template-columns:repeat(3,1fr)}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:5px;
 overflow:hidden;display:flex;flex-direction:column}
.card.flag{border-color:var(--warn)}
.card .shots{display:grid;grid-template-columns:1fr 1fr;gap:1px;
 background:var(--line)}
.card figure{margin:0;background:var(--panel-2);position:relative}
.card figure img{display:block;width:100%;aspect-ratio:3/4;object-fit:cover;
 background:var(--band)}
.card figure figcaption{position:absolute;left:0;bottom:0;
 font:500 9.5px/1 ui-monospace,Consolas,monospace;letter-spacing:.07em;
 text-transform:uppercase;background:rgba(16,24,33,.76);color:#fff;
 padding:4px 6px;border-radius:0 3px 0 0}
.card figure.miss{display:flex;align-items:center;justify-content:center;
 aspect-ratio:3/4;color:var(--faint);font-size:11px;text-align:center;padding:8px}
.card .body{padding:12px 13px;display:flex;flex-direction:column;gap:8px;flex:1}
.card h3{margin:0;font:600 14.5px/1.3 -apple-system,"Segoe UI",sans-serif}
.card .sub{font:500 10.5px/1.4 ui-monospace,Consolas,monospace;color:var(--muted)}
.card table{font-size:12.5px}
.card thead th{padding:6px 8px;position:static}
.card tbody td{padding:5px 8px}
.card .why{font-size:11.5px;color:var(--ink-soft);background:var(--band);
 border-radius:3px;padding:7px 9px;margin:0}
.card .why b{font:500 9.5px/1 ui-monospace,Consolas,monospace;letter-spacing:.07em;
 text-transform:uppercase;color:var(--accent-ink);display:block;margin-bottom:4px}
.fmt{display:grid;grid-template-columns:1fr;gap:14px;margin:18px 0 0}
@media(min-width:860px){.fmt{grid-template-columns:3fr 2fr}}
.fmt .box{background:var(--panel);border:1px solid var(--line);border-radius:5px;
 padding:14px 16px}
.fmt h3{margin:0 0 8px;font:600 14px/1.3 -apple-system,"Segoe UI",sans-serif}
.fmt ul{margin:0;padding-left:18px;font-size:13.5px;color:var(--ink-soft)}
.fmt li{margin:3px 0}
.fmt code,.example code{font-size:12px;background:var(--band);padding:1px 5px;
 border-radius:3px}
.req{font:600 9.5px/1 ui-monospace,Consolas,monospace;letter-spacing:.06em;
 text-transform:uppercase;color:var(--bad);margin-left:4px}
.opt{font:600 9.5px/1 ui-monospace,Consolas,monospace;letter-spacing:.06em;
 text-transform:uppercase;color:var(--faint);margin-left:4px}
.example{overflow-x:auto;margin:14px 0 0}
.example table{font-size:12px;background:var(--panel);border:1px solid var(--line)}
.example th{position:static}
.drop .hint{font-size:13px;color:var(--muted);margin:8px 0 12px}
.card .links{display:flex;flex-wrap:wrap;gap:6px}
.card .links a{font:500 11px/1 ui-monospace,Consolas,monospace;padding:5px 7px;
 border:1px solid var(--line-2);border-radius:3px;text-decoration:none}
.thumb{width:44px;height:58px;object-fit:cover;border-radius:3px;
 background:var(--band);display:block}
.brandf{display:inline-flex;gap:8px;align-items:center;font-weight:600;
 font-size:14px;margin:4px 0 6px}
.brandf input,.fetchbox input,.fetchbox select{font:inherit;padding:8px 10px;
 border:1px solid var(--line-2);border-radius:4px;background:var(--panel);
 color:var(--ink);min-width:220px}
.fetchbox{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;
 background:var(--panel);border:1px solid var(--line);border-radius:5px;
 padding:14px 16px}
.fetchbox label{display:flex;flex-direction:column;gap:5px;font-weight:600;
 font-size:13.5px}
.start{display:grid;grid-template-columns:1fr;gap:14px}
@media(min-width:860px){.start{grid-template-columns:2fr 1.5fr 1.2fr}}
.rbar{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;
 flex-wrap:wrap;margin:0 0 12px}
.rbar .tabs{flex:1;margin:0}
.start .drop{height:100%;box-sizing:border-box}
.start .fetchbox{flex-direction:column;align-items:stretch;justify-content:center;
 gap:10px;height:100%;box-sizing:border-box}
.start .fetchbox .hint{font-size:13px;color:var(--muted);margin:0}
.start .fetchbox select,.start .fetchbox input{min-width:0;width:100%;
 box-sizing:border-box}
button:disabled{opacity:.45;cursor:not-allowed}
a.plink{font-weight:600;text-decoration:underline;text-underline-offset:2px}
a.plink::after{content:" \\2197";font-size:.8em;display:inline-block;
 margin-left:1px}
td.low{background:var(--good-soft);box-shadow:inset 0 0 0 2px var(--good);
 color:var(--good);font-weight:700}
td.low a{color:var(--good)}
.modal{position:fixed;inset:0;background:rgba(10,15,20,.45);display:flex;
 align-items:center;justify-content:center;padding:16px;z-index:10}
.dialog{background:var(--panel);color:var(--ink);border-radius:8px;
 max-width:560px;width:100%;padding:20px 22px;border:1px solid var(--line);
 box-shadow:0 12px 40px rgba(0,0,0,.25);max-height:85vh;overflow:auto}
.dialog h3{margin:0 0 8px;font:700 17px/1.3 "Helvetica Neue",Arial,sans-serif}
.dialog ul{margin:0 0 12px;padding-left:18px;font-size:14px}
.dialog .hint{font-size:13px;color:var(--muted);margin:10px 0 6px}
.dialog .actions{display:flex;gap:10px;justify-content:flex-end;margin-top:14px}
.sysfix{position:fixed;top:14px;right:16px;z-index:50;font:800 15px/1
 "Helvetica Neue",Arial,sans-serif;padding:12px 18px;border-radius:5px;
 background:#c62828;color:#fff;border:2px solid #7f1414;text-decoration:none;
 box-shadow:0 2px 10px rgba(0,0,0,.25);animation:sysfix 1s steps(1) infinite}
@keyframes sysfix{50%{background:#fff;color:#c62828}}
@media (prefers-reduced-motion:reduce){.sysfix{animation:none}}
.verified{position:fixed;top:14px;right:16px;z-index:50;display:flex;
 align-items:center;gap:8px;padding:8px 14px;border-radius:5px;
 background:var(--good-soft);color:var(--good);border:1px solid var(--good);
 text-decoration:none;font:700 14px/1.2 "Helvetica Neue",Arial,sans-serif}
.verified .tick{font-size:18px}
.verified small{display:block;font:500 11px/1.2 inherit;color:var(--muted)}
.fixlist{list-style:none;padding:0;margin:0 0 18px}
.fixlist li{background:var(--panel);border:1px solid var(--line);
 border-left:4px solid var(--bad);border-radius:4px;padding:11px 14px;
 margin:0 0 8px}
.fixlist .when{color:var(--muted);font-size:13px}
footer{margin-top:40px;padding-top:15px;border-top:1px solid var(--line);
 font:400 12px/1.6 ui-monospace,Consolas,monospace;color:var(--faint)}
"""

SHELL = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s</title>%(head)s<style>%(css)s</style></head><body>
<div class="wrap"><header><h1>%(h1)s</h1>%(lede)s</header>%(body)s
</div>%(script)s</body></html>"""


def duration(seconds):
    """A rough duration a person can read: "40 seconds", "6 min", "1 h 12 min"."""
    s = int(max(0, seconds))
    if s < 90:
        return "1 second" if s == 1 else "%d seconds" % s
    if s < 5400:
        return "%d min" % round(s / 60.0)
    return "%d h %d min" % (s // 3600, (s % 3600) // 60)


def estimate(job):
    """(elapsed, remaining_or_None) for a running job.

    Site by site (compare.remaining_seconds): each site's pages left at
    that site's own time per page, the slowest site deciding -- Amazon is
    read at 3s a page while a brand's website takes about 1s, so a single
    "X% in Y minutes" rate raced ahead and then fell back (Abhisekh, 30 Sep
    2026). The number shown moves a third of the way toward each new
    figure and otherwise counts down, so it does not jump about.
    None in the first seconds, before any site has been timed.
    """
    now = time.monotonic()
    elapsed = now - job["started"]
    raw = compare.remaining_seconds(job.get("plan"), now)
    if raw is None or elapsed < 3:
        return elapsed, None
    prev = job.get("eta")
    if prev:
        at, value = prev
        guess = max(0.0, value - (now - at))
        raw = guess + (raw - guess) / 3.0
    job["eta"] = (now, raw)
    return elapsed, max(0.0, raw)


def progress_pct(job):
    """The bar's percentage; it never goes back (the size pages' count
    replaces a forecast mid-run and may be a few more)."""
    total = job["total"] or job["urls"] or 1
    pct = min(100, int(100 * job["done"] / total))
    job["shown_pct"] = max(job.get("shown_pct") or 0, pct)
    return job["shown_pct"]


def page(title, h1, lede, body, script="", head=""):
    lede = '<p class="lede">%s</p>' % lede if lede else ""
    return SHELL % {"title": title, "css": CSS, "h1": h1, "lede": lede,
                    "body": body, "script": script, "head": head}


DROP_JS = """<script>
const d=document.getElementById('drop'),f=document.getElementById('file');
if(d&&f){['dragenter','dragover'].forEach(k=>d.addEventListener(k,ev=>{
  ev.preventDefault();d.classList.add('over')}));
['dragleave','drop'].forEach(k=>d.addEventListener(k,ev=>{
  ev.preventDefault();d.classList.remove('over')}));
d.addEventListener('drop',ev=>{if(ev.dataTransfer.files.length){
  f.files=ev.dataTransfer.files;const b=document.querySelector(
  '#form input[name=brand]');if(!b||b.value.trim())
  document.getElementById('form').submit();else b.focus()}});
f.addEventListener('change',()=>{const b=document.querySelector(
  '#form input[name=brand]');if(f.files.length&&(!b||b.value.trim()))
  document.getElementById('form').submit()});}
</script>"""


def home(flash="", job=None, view="table"):
    """The one page: a heading, the three ways in -- upload a file, fetch a
    saved brand, the template -- and, once a run is chosen, its result
    underneath, so another fetch can be started from the same screen.

    Abhisekh, 24 Sep 2026: "the input page should only consist of the upload
    file section, brand dropdown section and the template section and a
    heading ... except the result section all other data is of no use to
    me." So there is no summary, audit banner, per-site table, notes or
    findings here -- the audit still runs on every comparison (compare.run)
    and the CSV and /api/runs still carry it.
    """
    saved = catalogue.brands()
    brand_opts = "".join(
        '<option value="%s"%s>%s &mdash; %d design%s, %d barcode%s</option>'
        % (e(b), " selected" if job and job.get("brand") == b else "", e(b),
           nd, "" if nd == 1 else "s", nb, "" if nb == 1 else "s")
        for b, nd, nb in saved)
    brand_list = "".join('<option value="%s">' % e(b) for b, _, _ in saved)
    if saved:
        opts = ('<option value="" disabled%s>Choose a brand&hellip;</option>'
                % ("" if job and job.get("brand") in
                   {b for b, _, _ in saved} else " selected")) + brand_opts
        off = ""
    else:
        opts = ('<option value="">No brand saved yet &mdash; upload a file '
                'first</option>')
        off = " disabled"

    body = system_fix_button() + flash + """
<div class="start">
<form id="form" method="POST" action="/upload" enctype="multipart/form-data">
  <div class="drop" id="drop">
    <strong>Upload a file</strong>
    <p class="hint">.csv or .xlsx &middot; one row per Zoddle barcode</p>
    <label class="brandf">Brand
      <input type="text" name="brand" list="brands" required
        placeholder="e.g. Vastramay" autocomplete="off"></label>
    <datalist id="brands">%s</datalist>
    <input type="file" id="file" name="file"
      accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet">
    <button type="submit">Get live prices</button>
  </div>
</form>
<form method="POST" action="/fetch" class="fetchbox">
  <strong>Fetch a saved brand</strong>
  <label>Brand
    <select name="brand" required%s>%s</select></label>
  <label>Design codes <span class="opt">optional &middot; separate with commas</span>
    <input type="text" name="designs" placeholder="e.g. 2248, 2249, 2264 -- blank for all"%s>
  </label>
  <button type="submit"%s>Fetch live prices</button>
</form>
<div class="fetchbox">
  <strong>Template</strong>
  <p class="hint">zoddle_barcode, design_code, size, zoddle_price, image,
    then one column per marketplace: Website, Myntra, FirstCry, Amazon,
    Flipkart.</p>
  <a class="btn ghost" href="/template.csv">Download template (.csv)</a>
</div>
</div>""" % (brand_list, off, opts, off, off)

    section, script = result_section(job, view)
    return page("Live price polling", "Live marketplace prices", "",
                body + section, DROP_JS + SYSFIX_JS + script)


def _shot(url, caption):
    if not url:
        return ('<figure class="miss"><span>no %s image</span></figure>'
                % e(caption))
    return ('<figure><img src="%s" alt="%s" loading="lazy" '
            'referrerpolicy="no-referrer"><figcaption>%s</figcaption>'
            '</figure>' % (e(url), e(caption), e(caption)))


def _price_cell(price, link, site=""):
    """A price, linked to the page it came from."""
    if price in (None, ""):
        return "&mdash;"
    body = money(price)
    if link:
        return ('<a class="plink" href="%s" target="_blank" rel="noopener" '
                'title="open on %s">%s</a>' % (e(link), e(site or link), body))
    return body


# The marketplaces, in the order they are shown. Each is
# (heading, price field, link field, status field or None). "Website" is
# shown under the brand's own name -- see brand_of.
CHANNEL_VIEW = (
    ("Website", "own_site_price", "own_site_link", None),
    ("Myntra", "myntra_price", "myntra_link", "myntra_status"),
    ("FirstCry", "firstcry_price", "firstcry_link", "firstcry_status"),
    ("Amazon", "amazon_price", "amazon_link", "amazon_status"),
    ("Flipkart", "flipkart_price", "flipkart_link", "flipkart_status"),
)
# linkfile's channel name for each Row price field, for the CSV download.
CHANNEL_FIELD = {"own site": "own_site_price", "myntra": "myntra_price",
                 "firstcry": "firstcry_price", "amazon": "amazon_price",
                 "flipkart": "flipkart_price"}


def brand_of(job):
    """The heading for the brand's own-website column: the brand's name.
    A file mixing brands gets "Brand website"."""
    names = collections.Counter(r.brand for r in (job.get("rows") or [])
                                if r.brand)
    if len(names) == 1:
        return next(iter(names))
    if names:
        return "Brand website"
    return job.get("brand") or "Website"


def channels_in(rows):
    """Only the marketplaces this file actually linked, so no empty columns."""
    out = [c for c in CHANNEL_VIEW if any(getattr(r, c[2]) for r in rows)]
    if any(r.other_price != "" for r in rows):
        out.append(("Other", "other_price", None, None))
    return out


def _heading(chan, brand):
    return brand if chan[0] == "Website" else chan[0]


def cell_state(row, chan):
    """(kind, reason) for one marketplace cell. kind is "price", "out of
    stock" (priced or not), "no price" (the site does not sell this size)
    or "not fetched" (the page could not be read); "" when the file gave
    no link. Abhisekh, 26 Sep 2026: "no price" means only that the site
    does not sell the size."""
    name, pf, lf, sf = chan
    price, link = getattr(row, pf), (getattr(row, lf) if lf else "")
    status = str(getattr(row, sf) if sf else "") or ""
    if name == "Website":
        # The own site says out of stock in its verdict, not a status.
        status = ("OUT OF STOCK" if row.own_site_match == "OUT OF STOCK"
                  else status)
    if price not in (None, ""):
        if status.upper().startswith("OUT OF STOCK"):
            return "out of stock", status
        return "price", ""
    if not link:
        return "", ""
    why = status or (_own_note(row) if name == "Website" else "") \
        or "no price on the page"
    return sites.empty_kind(why), why


def _own_note(row):
    """The own site's part of the row's note. The note joins every
    channel's ("vastramay.com: ...; myntra.com: out of stock ..."), and
    reading all of it let Myntra's out-of-stock label the website cell."""
    parts = [p for p in str(row.note or "").split("; ") if p]
    host = str(row.own_site or "")
    mine = [p for p in parts if host and p.startswith(host + ":")]
    if mine:
        return "; ".join(mine)
    others = ("myntra.com:", "firstcry.com:", "amazon.in:", "flipkart.com:")
    return "; ".join(p for p in parts if not p.startswith(others))


def _chan_cell(row, chan):
    """A marketplace's price linked to its page; or "out of stock", "no
    price" or "not fetched", each linked to the page with the reason in its
    tooltip. No verification tags: the file's link is the listing."""
    name, pf, lf, sf = chan
    price, link = getattr(row, pf), (getattr(row, lf) if lf else "")
    if name == "Other":
        return ("%s %s" % (money(price), e(row.other_site))
                if price != "" else "&mdash;")
    kind, why = cell_state(row, chan)
    if kind == "price":
        return _price_cell(price, link, name)
    if kind == "out of stock" and price not in (None, ""):
        return (_price_cell(price, link, name)
                + ' <span class="pill oos">out of stock</span>')
    if kind:
        # Clickable, like a price: it opens the page (Abhisekh, 26 Sep).
        return ('<a class="plink" href="%s" target="_blank" rel="noopener" '
                'title="%s"><span class="pill %s">%s</span></a>'
                % (e(link), e(why), {"out of stock": "oos",
                                     "not fetched": "warn"}.get(kind, "none"),
                   kind))
    return "&mdash;"


def lowest_headings(row):
    """The column headings (Zoddle, Website, Myntra, ...) of the cheapest
    source(s). compare.py names them "zoddle", "myntra", ..., the own site by
    its host, and joins a tie with " / "."""
    named = {"zoddle": "Zoddle", "myntra": "Myntra", "firstcry": "FirstCry",
             "amazon": "Amazon", "flipkart": "Flipkart"}
    out = set()
    for n in str(row.lowest_source or "").split(" / "):
        n = n.strip()
        if not n:
            continue
        if n in named:
            out.add(named[n])
        elif row.other_price != "" and n in (row.other_site, "other"):
            out.add("Other")
        else:
            out.add("Website")
    return out


def _lowest_price_cell(row):
    """The cheapest price, linked to its page when one marketplace holds it
    alone. Zoddle's own price, or a tie, has no single page to open."""
    if row.lowest_price in (None, ""):
        return "&mdash;"
    return "<strong>%s</strong>" % _price_cell(
        row.lowest_price, lowest_link(row), row.lowest_source)


def lowest_link(row):
    """The page of the cheapest price, when one marketplace holds it alone;
    "" for Zoddle's own price or a tie (no single page to open)."""
    heads = lowest_headings(row)
    if len(heads) != 1 or row.lowest_price in (None, ""):
        return ""
    chan = next((c for c in CHANNEL_VIEW if c[0] in heads), None)
    return getattr(row, chan[2]) if chan and chan[2] else ""


_ORDER = ["Zoddle"] + [c[0] for c in CHANNEL_VIEW] + ["Other"]


def lowest_source_name(row, brand):
    """Which source is cheapest, named as its column is -- the brand's name
    for its own website. A tie names every source sharing the price."""
    return " / ".join(brand if h == "Website" else h
                      for h in sorted(lowest_headings(row), key=_ORDER.index))


def _lowest_source_cell(row, brand):
    return e(lowest_source_name(row, brand)) or "&mdash;"


def tabs(job_id, view):
    return ('<div class="tabs"><a class="%s" href="/job/%s#results">List</a>'
            '<a class="%s" href="/job/%s?view=cards#results">Thumbnails</a></div>'
            % ("on" if view != "cards" else "", job_id,
               "on" if view == "cards" else "", job_id))


def table_view(rows, brand="Website"):
    """The list: one row per barcode, every marketplace's price in that row.
    No photographs, no gap, no note -- prices only."""
    chans = channels_in(rows)
    trs = "".join(
        """<tr><td class="mono">%s</td><td class="mono">%s</td>
           <td class="mono">%s</td><td class="n"><strong>%s</strong></td>%s
           <td class="n">%s</td><td>%s</td></tr>""" % (
            e(r.zoddle_barcode), e(r.design_code), e(r.size),
            money(r.zoddle_price),
            "".join('<td class="n">%s</td>' % _chan_cell(r, c) for c in chans),
            _lowest_price_cell(r), _lowest_source_cell(r, brand))
        for r in rows)
    return ('<div class="scroll"><table><thead><tr>'
            "<th>Barcode</th><th>Design</th><th>Size</th>"
            "<th>Zoddle</th>%s<th>Lowest price</th><th>Lowest source</th>"
            "</tr></thead><tbody>%s</tbody></table></div>"
            % ("".join("<th>%s</th>" % e(_heading(c, brand)) for c in chans),
               trs))


def cards_view(rows, brand="Website"):
    """Thumbnails: one card per design -- your photograph beside the
    marketplace's, and every size's price on every marketplace underneath,
    the cheapest of each size highlighted. No warnings, no notes.
    """
    chans = channels_in(rows)
    groups = collections.OrderedDict()
    for r in rows:
        groups.setdefault(r.design_code, []).append(r)

    # The cheapest cell of every size row is highlighted -- Zoddle's own
    # price included, since "us" can be the cheapest.
    def low(r, heading):
        if heading in lowest_headings(r):
            return ' low" title="lowest price for this size'
        return ""

    cards = []
    for design, rs in groups.items():
        head = rs[0]
        sizes = "".join(
            '<tr><td class="mono">%s</td><td class="n%s">%s</td>%s'
            '<td class="n">%s</td><td>%s</td></tr>'
            % (e(r.size or "any"), low(r, "Zoddle"), money(r.zoddle_price),
               "".join('<td class="n%s">%s</td>' % (low(r, c[0]),
                                                    _chan_cell(r, c))
                       for c in chans),
               _lowest_price_cell(r), _lowest_source_cell(r, brand))
            for r in rs)
        links = "".join(
            '<a href="%s" target="_blank" rel="noopener">%s &rarr;</a>'
            % (e(getattr(head, c[2])), e(_heading(c, brand)))
            for c in chans if c[2] and getattr(head, c[2]))

        cards.append(
            """<div class="card">
  <div class="shots">%s%s</div>
  <div class="body">
    <h3>%s</h3>
    <p class="sub">design %s &middot; %d size%s</p>
    <div class="scroll" style="margin:0"><table><thead><tr><th>Size</th>
      <th>Zoddle</th>%s<th>Lowest</th><th>Source</th></tr></thead>
      <tbody>%s</tbody></table></div>
    <div class="links">%s</div>
  </div>
</div>""" % (
                _shot(head.zoddle_image, "your photo"),
                _shot(head.retailer_image, "marketplace"),
                e(head.design_name or "Design %s" % design),
                e(design), len(rs), "" if len(rs) == 1 else "s",
                "".join("<th>%s</th>" % e(_heading(c, brand)) for c in chans),
                sizes, links))

    return '<div class="cards">%s</div>' % "".join(cards)


def result_section(job, view="table"):
    """(html, script) for the part of the page under the inputs: nothing, a
    progress bar, why nothing could be fetched, or the results."""
    if not job:
        return "", ""
    title = '<h2 id="results">%s</h2>' % e(job["filename"])

    if job["state"] in ("queued", "running"):
        pct = progress_pct(job)
        _, remaining = estimate(job)
        html_ = title + """<div class="progress">
  <div class="pline"><b id="pct">%d%%</b><span id="eta">%s</span></div>
  <div class="bar"><i id="fill" style="width:%d%%"></i></div>
  <p class="task"><code id="task">%s</code></p>
</div>""" % (pct,
             ("about %s remaining" % e(duration(remaining)))
             if remaining is not None else "estimating&hellip;",
             pct, e(job["where"] or "starting&hellip;"))
        # Polled rather than meta-refreshed, so the bar travels to its new
        # width. When the run is done the page reloads into the results. If
        # the server has stopped (the run lived in its memory), say so.
        script = """<script>
(function(){
  var id=%s, fill=document.getElementById('fill'),
      pct=document.getElementById('pct'), eta=document.getElementById('eta'),
      task=document.getElementById('task'), misses=0;
  function tick(){
    fetch('/job/'+id+'/progress').then(function(r){
      if(!r.ok)throw new Error(r.status);return r.json()}).then(function(p){
      misses=0;
      if(p.state!=='running'&&p.state!=='queued'){location.reload();return}
      fill.style.width=p.pct+'%%'; pct.textContent=p.pct+'%%';
      eta.textContent=p.remaining?('about '+p.remaining+' remaining')
                                 :'estimating\\u2026';
      task.textContent=p.where||'\\u2026';
      setTimeout(tick,2000);
    }).catch(function(){
      if(++misses>=4){
        eta.textContent='';
        task.textContent='The app has stopped, so this run was lost. '+
          'Start it again and pick the brand from the '+
          '\\u201cfetch a saved brand\\u201d dropdown \\u2014 it was saved.';
        return}
      setTimeout(tick,4000)});
  }
  setTimeout(tick,2000);
})();</script>""" % json.dumps(job["id"])
        return html_, script

    if job["state"] in ("rejected", "failed"):
        return (title + '<div class="banner bad"><b>No prices fetched</b>'
                "<p>%s</p></div>" % e(job["error"] or ""), "")

    rows, brand = job["rows"], brand_of(job)
    # Design codes typed into the saved-brand box that the sheet does not
    # hold. One line, because a code silently missing from the results
    # reads as "fetched, nothing found" when it was never fetched.
    missing = "".join(
        '<div class="banner warnb"><p>%s</p></div>' % e(i.detail)
        for i in job["issues"] if i.code == "design_not_saved")
    pop, pop_js = popup(job)
    return (title + missing + pop
            + '<div class="rbar">%s<span><a class="btn" href="/job/%s/xlsx">'
              'Download Excel</a> <a class="btn ghost" href="/job/%s/csv">'
              "Download CSV</a></span></div>"
              % (tabs(job["id"], view), job["id"], job["id"])
            + (cards_view(rows, brand) if view == "cards"
               else table_view(rows, brand)), pop_js)


def job_page(job, view="table"):
    return home("", job, view)


def price_map(rows):
    """(barcode, channel) -> what the download shows: the live price, or
    "out of stock" / "not fetched" (a plain "no price" stays "-")."""
    out = {}
    view = {c[1]: c for c in CHANNEL_VIEW}
    for r in rows:
        for ch, f in CHANNEL_FIELD.items():
            kind, _ = cell_state(r, view[f])
            out[(r.zoddle_barcode, ch)] = (
                kind if kind not in ("price", "no price", "")
                else getattr(r, f))
        # Any other marketplace shares Row.other_price; it goes only under
        # the channel whose hosts the price came from.
        if r.other_price != "":
            host = str(r.other_site or "").lower()
            for ch, _, hosts in linkfile.MARKETPLACES:
                if ch not in CHANNEL_FIELD and any(
                        host == h or host.endswith("." + h) for h in hosts):
                    out[(r.zoddle_barcode, ch)] = r.other_price
    return out


def link_map(rows):
    """(barcode, channel) -> the page each download cell was read from --
    the same page the app's cell links to (for Amazon and Flipkart, the
    row's own size's page)."""
    out = {}
    view = {c[1]: c for c in CHANNEL_VIEW}
    for r in rows:
        for ch, f in CHANNEL_FIELD.items():
            link = getattr(r, view[f][2])
            if link:
                out[(r.zoddle_barcode, ch)] = link
    return out


def download_name(job):
    """The download's file name without extension: the brand's name and
    the day of the live fetch, "Bhama_30-09-2026" (Abhisekh, 30 Sep 2026).
    A file mixing brands keeps its own name in place of the brand's."""
    names = {r.brand for r in (job.get("rows") or []) if r.brand}
    brand = (next(iter(names)) if len(names) == 1 else
             "" if names else job.get("brand") or "")
    if not brand:
        brand = re.sub(r"\.(csv|xlsx|xlsm)$", "", job["filename"], flags=re.I)
    brand = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", brand)
    brand = re.sub(r"\s+", " ", brand).strip(" .") or "prices"
    day = job.get("fetched_on") or datetime.date.today()
    return "%s_%s" % (brand, day.strftime("%d-%m-%Y"))


def attachment(name):
    """A Content-Disposition header for any file name. HTTP headers are
    latin-1, so a name with "–" or Hindi in it could not be sent as-is:
    an ASCII fallback plus the RFC 5987 UTF-8 spelling browsers prefer."""
    plain = re.sub(r'[^A-Za-z0-9._() -]+', "_", name).strip() or "download"
    return ('attachment; filename="%s"; filename*=UTF-8\'\'%s'
            % (plain, urllib.parse.quote(name, safe="")))


def popup(job):
    """(html, script) for the pop-up after a run: how many links could not
    be fetched and why, with Refetch (read only those again, then show the
    whole page) and Ignore (close it; nothing else changes). Rows the file
    itself got wrong are named too -- refetching cannot mend those.
    Abhisekh, 26 Sep 2026."""
    if job.get("dismissed"):
        return "", ""
    retry = (job.get("summary") or {}).get("retry") or []
    rows = [i for i in job["issues"] if i.severity == "reject" and i.row_no > 1]
    links = [i for i in job["issues"]
             if i.code in ("link_not_a_url", "link_wrong_marketplace")]
    if not (retry or rows or links):
        return "", ""
    parts = []
    if retry:
        groups = collections.Counter(
            (re.sub(r"^www\.", "", urllib.parse.urlsplit(u).netloc), why)
            for u, why in retry)
        parts.append("<h3>%d link%s skipped during fetching</h3><ul>%s</ul>" % (
            len(retry), "" if len(retry) == 1 else "s", "".join(
                "<li>%s: %d &mdash; %s</li>" % (e(host), n, e(why))
                for (host, why), n in groups.most_common())))
    if rows or links:
        parts.append("<p class=\"hint\">Not fetched because of the file "
                     "(fix these in the file; Refetch cannot):</p>"
                     + skipped_notice(job["issues"]))
    buttons = ""
    if retry:
        buttons += ('<form method="POST" action="/job/%s/refetch" '
                    'style="display:inline"><button type="submit">Refetch'
                    '</button></form> ' % e(job["id"]))
    buttons += ('<button type="button" class="btn ghost" id="ignore">'
                'Ignore</button>')
    html_ = ('<div class="modal" id="popup"><div class="dialog">%s'
             '<div class="actions">%s</div></div></div>'
             % ("".join(parts), buttons))
    script = """<script>
(function(){var b=document.getElementById('ignore');if(!b)return;
b.addEventListener('click',function(){
  fetch('/job/'+%s+'/ignore',{method:'POST'}).catch(function(){});
  var p=document.getElementById('popup');if(p)p.remove();});})();
</script>""" % json.dumps(job["id"])
    return html_, script


def skipped_notice(issues):
    """One line naming the rows that were not fetched and the links that
    were skipped, and why. Without it a refused row simply vanished from
    the results, which reads as "fetched, nothing found"."""
    rows = [i for i in issues if i.severity == "reject" and i.row_no > 1]
    links = [i for i in issues
             if i.code in ("link_not_a_url", "link_wrong_marketplace")]

    def listed(items, what):
        shown = "; ".join("row %d: %s" % (i.row_no, i.detail)
                          for i in items[:6])
        more = " (and %d more)" % (len(items) - 6) if len(items) > 6 else ""
        return "<p><b>%d %s</b> %s%s</p>" % (len(items), what, e(shown), more)

    parts = []
    if rows:
        parts.append(listed(rows, "row%s not fetched:" % (
            "" if len(rows) == 1 else "s")))
    if links:
        parts.append(listed(links, "link%s skipped:" % (
            "" if len(links) == 1 else "s")))
    return ('<div class="banner warnb">%s</div>' % "".join(parts)
            if parts else "")


# ------------------------------------------------------------------ handler --
# ------------------------------------------------------------- System Fix --
# Abhisekh, 29 Sep 2026: instead of the daily check opening a report page,
# a flashing "System Fix !" button on this page; clicking it opens the
# details; it stays until the problem is fixed AND a check has passed on it.
# The open problems live in checks/status.json (daily.save_status).

RECHECK_LOCK = threading.Lock()
RECHECK_STARTED = [0.0]
RECHECK_PROC = [None]          # the last re-check's process


def recheck_log():
    return daily.CHECKS / "recheck.log"


def recheck_failed():
    """The last line of what the last re-check printed, when it stopped
    with an error (a change that broke the code: it died before it could
    write a word, and the page only showed the old problems again)."""
    proc = RECHECK_PROC[0]
    if proc is None or proc.poll() in (None, 0):
        return ""
    try:
        lines = recheck_log().read_text(encoding="utf-8",
                                        errors="replace").strip().splitlines()
    except OSError:
        lines = []
    return lines[-1] if lines else "exit code %s" % proc.returncode


# A page left open shows (or drops) the button by itself: it asks every
# 30 seconds whether a problem is open. No reload needed.
SYSFIX_JS = """<script>
setInterval(()=>{fetch('/system-fix/state').then(r=>r.json()).then(s=>{
  const b=document.getElementById('wkstate');
  if(b&&b.outerHTML===s.html)return;
  if(b)b.remove();
  if(s.html)document.body.insertAdjacentHTML('beforeend',s.html);
  }).catch(()=>{})},30000);
</script>"""


def system_fix_button():
    """The daily check's mark, top right: the flashing "System Fix !"
    while a problem is open; else, once a check has passed, a green tick
    "Verified" with the time of that check -- it stays until the next
    check (Abhisekh, 30 Sep 2026: "we don't have any clue whether it ran").
    "" before any check has run."""
    st = daily.load_status()
    if st["issues"]:
        return ('<a class="sysfix" id="wkstate" href="/system-fix" '
                'title="The daily check found a problem -- click for the '
                'details">System Fix !</a>')
    if not st.get("checked"):
        return ""
    return ('<a class="verified" id="wkstate" href="/system-fix" title="The '
            'last daily check passed: %s%s"><span class="tick">&#10004;'
            '</span><span>Verified<small>%s</small></span></a>'
            % (e(st.get("what") or "daily check"),
               (" -- " + e(st["stats"])) if st.get("stats") else "",
               e(st["checked"])))


def start_recheck():
    """Run `daily.py --recheck` in its own process, so it finishes even if
    this app is closed. False when a check is already running."""
    with RECHECK_LOCK:
        if (not daily.load_status()["issues"] or daily.running_since()
                or time.time() - RECHECK_STARTED[0] < 30):
            return False
        RECHECK_STARTED[0] = time.time()
        daily.CHECKS.mkdir(exist_ok=True)
        with open(recheck_log(), "w", encoding="utf-8") as log:
            RECHECK_PROC[0] = subprocess.Popen(
                [sys.executable, str(HERE / "daily.py"), "--recheck"],
                cwd=str(HERE.parent), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True


def system_fix_page():
    st = daily.load_status()
    issues = st["issues"]
    since = daily.running_since() or (
        "just now" if time.time() - RECHECK_STARTED[0] < 30 else None)
    head = ('<meta http-equiv="refresh" content="20">' if since else "")
    last = ("Last check: %s (%s)." % (e(st.get("checked") or "?"),
                                      e(st.get("what") or "daily"))
            if st.get("checked") else "")
    if since:
        action = ('<p><b>A check is running</b> (started %s). This page '
                  'refreshes by itself; the button goes away once the check '
                  'passes.</p>' % e(since))
    elif issues:
        broke = recheck_failed()
        action = (('<p class="bad"><b>The last re-check stopped with an '
                   'error</b> before it could check anything: %s -- see '
                   'checks/recheck.log. Fix the code, then run it again.</p>'
                   % e(broke)) if broke else "") + (
                  '<form method="POST" action="/system-fix/recheck">'
                  '<button type="submit">Run the check again</button></form>'
                  '<p class="hint">Once it is fixed: this runs the code\'s '
                  'own tests, then checks EVERY brand and channel, so a new '
                  'problem the change caused shows here too (about 6 '
                  'minutes; a link that still fails is tried again 15 '
                  'minutes later). It uses the code as it is now, and keeps '
                  'running even if you close the app.</p>')
    else:
        action = ""
    if not issues:
        body = ('<p>Nothing to fix &mdash; every brand and channel passed '
                'the last check. %s</p><p><a class="btn ghost" href="/">'
                'Back to prices</a></p>' % last) + action
        return page("System Fix", "System Fix", "", body, head=head)
    by = collections.OrderedDict()
    for i in issues:
        by.setdefault(i.get("kind") or "Problem", []).append(i)
    parts = []
    for kind, items in by.items():
        lis = "".join(
            '<li><b>%s</b>%s &mdash; %s%s<div class="when">found %s</div></li>'
            % (e(i.get("brand") or "The daily check"),
               (" &middot; " + e(i["channel"])) if i.get("channel") else "",
               e(i.get("detail") or ""),
               (' &nbsp;<a href="%s" target="_blank" rel="noopener">open '
                'page &#8599;</a>' % e(i["link"])) if i.get("link") else "",
               e(i.get("found") or "?"))
            for i in items)
        parts.append('<h2>%s (%d)</h2><ul class="fixlist">%s</ul>'
                     % (e(kind), len(items), lis))
    body = ('<p>%s Only what could not be read correctly is listed; '
            'everything else passed.</p>%s%s<p><a class="btn ghost" href="/">'
            'Back to prices</a></p>') % (last, "".join(parts), action)
    n = len(issues)
    return page("System Fix", "System Fix: %d problem%s" % (n, "" if n == 1
                                                            else "s"),
                "", body, head=head)


class Handler(BaseHTTPRequestHandler):
    server_version = "ZocsPriceWatch/3.0"

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=()):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _job(self, job_id):
        with JOBS_LOCK:
            return JOBS.get(job_id)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                self._send(200, home())
                return
            m = re.fullmatch(r"/job/([0-9a-f]{6,32})(/csv|/xlsx|/progress)?",
                             path)
            if m:
                job = self._job(m.group(1))
                if not job:
                    self._send(404, home(
                        '<div class="banner warnb"><b>That run is no longer '
                        "in memory</b><p>Runs do not survive a restart. Pick "
                        "the brand from the dropdown to fetch it again."
                        "</p></div>"))
                    return
                if m.group(2) == "/progress":
                    # The running page polls this instead of reloading, so
                    # the bar can travel to its new width rather than jump.
                    elapsed, remaining = estimate(job)
                    self._send(200, json.dumps({
                        "state": job["state"],
                        "pct": progress_pct(job),
                        "where": job["where"] or "",
                        "remaining": (duration(remaining)
                                      if remaining is not None else None),
                    }), "application/json")
                    return
                if m.group(2):
                    if job["state"] != "done":
                        self._send(409, "run is not finished",
                                   "text/plain; charset=utf-8")
                        return
                    brand = brand_of(job)
                    lowest = {r.zoddle_barcode: (r.lowest_price,
                                                 lowest_source_name(r, brand),
                                                 lowest_link(r))
                              for r in job["rows"]}
                    stem = download_name(job)
                    if m.group(2) == "/xlsx" and job.get("table"):
                        name = stem + ".xlsx"
                        ctype = ("application/vnd.openxmlformats-"
                                 "officedocument.spreadsheetml.sheet")
                        body = linkfile.priced_xlsx(
                            job["table"], price_map(job["rows"]), lowest,
                            links=link_map(job["rows"]))
                    else:
                        name, ctype = stem + ".csv", "text/csv; charset=utf-8"
                        body = (linkfile.priced_csv(job["table"],
                                                    price_map(job["rows"]),
                                                    lowest)
                                if job.get("table")
                                else compare.to_csv(job["rows"]))
                    self._send(200, body, ctype,
                               [("Content-Disposition",
                                 attachment(os.path.basename(name)))])
                    return
                view = "cards" if "view=cards" in self.path else "table"
                self._send(200, job_page(job, view))
                return
            if path == "/system-fix":
                self._send(200, system_fix_page())
                return
            if path == "/system-fix/state":
                self._send(200, json.dumps(
                    {"open": len(daily.load_status()["issues"]),
                     "html": system_fix_button()}),
                    "application/json")
                return
            if path == "/template.csv":
                self._send(200, linkfile.template_csv(),
                           "text/csv; charset=utf-8",
                           [("Content-Disposition",
                             'attachment; filename="price_polling_input_'
                             'template.csv"')])
                return
            if path == "/api/runs":
                with JOBS_LOCK:
                    out = [{"id": j["id"], "filename": j["filename"],
                            "state": j["state"], "rows": len(j["records"]),
                            "summary": j["summary"]} for j in JOBS.values()]
                self._send(200, json.dumps(out, indent=1, default=str),
                           "application/json")
                return
            self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception as ex:
            self._send(500, "error: %r" % (ex,), "text/plain; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def _redirect(self, where):
        self.send_response(303)
        self.send_header("Location", where)
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        m = re.fullmatch(r"/job/([0-9a-f]{6,32})/(refetch|ignore)", path)
        if m:
            job = self._job(m.group(1))
            if not job:
                self._redirect("/")
                return
            if m.group(2) == "ignore":
                job["dismissed"] = True
                self._send(204, b"", "text/plain; charset=utf-8")
                return
            with JOBS_LOCK:
                # Checked and claimed at once: a double click started two
                # runs of one job, every page read twice (30 Sep 2026).
                go = job["state"] == "done" and bool(job["records"])
                if go:
                    job["state"] = "running"
            if go:
                launch(job)
            self._redirect("/job/%s" % job["id"])
            return
        if path == "/system-fix/recheck":
            start_recheck()
            self._redirect("/system-fix")
            return
        if path == "/fetch":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(
                    self.rfile.read(length).decode("utf-8", "replace"))
                brand = (form.get("brand") or [""])[0]
                if not brand:
                    self._redirect("/")
                    return
                job = start_saved(brand, (form.get("designs") or [""])[0])
                self._redirect("/job/%s" % job["id"])
            except Exception as ex:
                self._send(500, "error: %r" % (ex,), "text/plain; charset=utf-8")
            return
        if path != "/upload":
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_UPLOAD:
                self._send(413, "upload must be 1 byte to %d MB"
                           % (MAX_UPLOAD // 1048576),
                           "text/plain; charset=utf-8")
                return
            body = self.rfile.read(length)
            fields = parse_multipart(body, self.headers.get("Content-Type"))
            parts = [p for p in fields if p[1]]
            brand = next((d.decode("utf-8", "replace").strip()
                          for n, fn, d in fields if n == "brand" and not fn), "")
            if not parts:
                self._send(200, home('<div class="banner warn"><b>No file '
                                     'was chosen</b><p>Choose a .csv or .xlsx '
                                     'file, then press upload.</p></div>'))
                return
            jobs = [start_job(os.path.basename(fn.replace("\\", "/")), data,
                              brand)
                    for _, fn, data in parts]
            if len(jobs) == 1:
                self.send_response(303)
                self.send_header("Location", "/job/%s" % jobs[0]["id"])
                self.end_headers()
                return
            flash = "".join(
                '<div class="banner info"><b>%s</b><p><a href="/job/%s">'
                "open the run</a></p></div>" % (e(j["filename"]), j["id"])
                for j in jobs)
            self._send(200, home(flash))
        except Exception as ex:
            self._send(500, "error: %r" % (ex,), "text/plain; charset=utf-8")

    # Silent by default (Abhisekh, 24 Sep 2026: "after that nothing should
    # be shown or mentioned in the cmd window"). --log turns it back on.
    LOG = False

    def log_message(self, fmt, *a):
        if self.LOG:
            sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % a))


class Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR (which http.server turns on) lets a SECOND
    # copy bind a port already in use, and the two then split the requests
    # between them -- measured: two processes LISTENING on one port. Off, a
    # second launch fails to bind and opens the running app instead.
    allow_reuse_address = False


def main(argv=None):
    """Serve and open the page in the browser, printing nothing.

        python engine/serve.py              -> opens http://127.0.0.1:8770
        python engine/serve.py --no-open    -> serve only
        python engine/serve.py --log        -> print each request (a real
                                               browser shows GET /favicon.ico)
    """
    ap = argparse.ArgumentParser(description="Local ZOCS price watch")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--open", action="store_true",
                    help="open the browser (the default; kept for run.cmd)")
    ap.add_argument("--no-open", action="store_true",
                    help="do not open the browser")
    ap.add_argument("--log", action="store_true",
                    help="print the address and each request")
    a = ap.parse_args(argv)
    url = "http://127.0.0.1:%d/" % a.port
    Handler.LOG = a.log
    if a.log:
        print("serving at %s   (Ctrl+C to stop)" % url)
    else:
        import warnings
        warnings.simplefilter("ignore")      # e.g. openpyxl on an .xlsx
    try:
        server = Server(("127.0.0.1", a.port), Handler)
    except OSError:
        # Almost always: the app is already running on this port. Open that
        # one rather than print an error and leave.
        if not a.no_open:
            webbrowser.open(url)
        return 0
    if not a.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
