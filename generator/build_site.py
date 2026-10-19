#!/usr/bin/env python3
"""Build the static BoardWatch site from data/*.json into ./site."""
import json, shutil, datetime as dt
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse, quote
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
DATA, OUT = ROOT / "data", ROOT / "site"
cfg = json.loads((ROOT / "config.json").read_text())
boards = json.loads((DATA / "boards.json").read_text())["boards"]
vendors = json.loads((DATA / "vendors.json").read_text())["vendors"]
stock = json.loads((DATA / "stock.json").read_text())
hist = json.loads((DATA / "history.json").read_text())["points"] if (DATA / "history.json").exists() else []
BY_ID = {b["id"]: b for b in boards}
FX = cfg.get("fx_to_usd", {"USD": 1})
SYM = {"USD": "$", "GBP": "£", "EUR": "€"}

SPEC_FIELDS = [("cpu", "CPU"), ("clock", "Clock"), ("ram", "RAM"), ("flash", "Flash"), ("codec", "Audio codec"),
               ("audio", "Bit depth / sample rate"), ("ch_in", "Audio in"), ("ch_out", "Audio out"), ("io", "Other I/O"),
               ("latency", "Latency notes"), ("toolchains", "Toolchains"), ("host", "Needs / host"), ("msrp", "Official price"), ("notes", "Notes")]


def aff(vendor, url):
    a = cfg.get("affiliates", {}).get(vendor) or {}
    if not a.get("id"):
        return url
    if a.get("template"):
        return a["template"].replace("{url}", quote(url, safe="")).replace("{id}", a["id"])
    if a.get("param"):
        u = urlparse(url); q = dict(parse_qsl(u.query)); q[a["param"]] = a["id"]
        return urlunparse(u._replace(query=urlencode(q)))
    return url


def money(price, cur):
    if price is None:
        return "—"
    return f"{SYM.get(cur, (cur or '') + ' ')}{price:,.2f}"


def fmt_time(iso):
    t = dt.datetime.fromisoformat(iso)
    ct = t.astimezone(ZoneInfo("America/Chicago"))
    return t.strftime("%Y-%m-%d %H:%M UTC"), ct.strftime("%b %-d, %Y %-I:%M %p CT")


STATUS_LABEL = {"in_stock": "In stock", "out_of_stock": "Sold out", "backorder": "Backorder", "blocked": "Not checked (blocked)",
                "error": "Check failed", "redirect": "Sold via SparkFun", "unknown": "Unknown"}

rows = []
for r in stock["results"]:
    b = BY_ID.get(r["board"])
    if not b:
        continue
    st = r.get("status", "unknown")
    if r.get("used_market"):
        st_label = f"{r.get('listing_count', 0)} listing(s)" if r.get("in_stock") else "No listings"
    else:
        st_label = STATUS_LABEL.get(st, st)
    usd = r["price"] * FX.get(r.get("currency") or "USD", 1) if r.get("price") is not None else None
    rows.append({**r, "b": b, "status": st, "status_label": st_label, "price_fmt": money(r.get("price"), r.get("currency")),
                 "usd": usd, "checked_short": dt.datetime.fromisoformat(r["checked_at"]).astimezone(ZoneInfo("America/Chicago")).strftime("%-I:%M %p CT"), "link": aff(r["vendor"], r.get("buy_url") or r["url"])})
order = {"in_stock": 0, "backorder": 1, "out_of_stock": 2, "redirect": 3, "unknown": 4, "error": 5, "blocked": 6}
rows.sort(key=lambda x: (x["b"]["family"], x["b"]["name"], order.get(x["status"], 9), x.get("usd") or 9e9))

for b in boards:
    b["offers"] = [r for r in rows if r["board"] == b["id"]]
    b["in_stock_any"] = any(o.get("in_stock") and not o.get("used_market") for o in b["offers"])
    real = [o for o in b["offers"] if o.get("price") is not None and not o.get("used_market") and o.get("in_stock")]
    b["best"] = min(real, key=lambda o: o["usd"]) if real else None
    b["spec_rows"] = [(label, b["specs"].get(k)) for k, label in SPEC_FIELDS]
    pts = [p for p in hist if p["board"] == b["id"] and p.get("price") is not None]
    series = {}
    for p in pts:
        series.setdefault((p["vendor"], p["url"]), []).append(p)
    b["history"] = [{"vendor": vendors[k[0]]["name"], "points": v, "currency": v[-1].get("currency")} for k, v in series.items()]

families = sorted({b["family"] for b in boards})
vendor_list = sorted({(r["vendor"], r["vendor_name"]) for r in rows}, key=lambda x: x[1])
utc_s, ct_s = fmt_time(stock["run_at"])
summary = stock["vendor_summary"]
ok_vendors = [vendors[v]["name"] for v, s in summary.items() if s["ok"]]
bad_vendors = [(vendors[v]["name"], s["errors"][0]) for v, s in summary.items() if not s["ok"]]
counts = {"in": sum(1 for r in rows if r["status"] == "in_stock" and not r.get("used_market")),
          "out": sum(1 for r in rows if r["status"] in ("out_of_stock", "backorder") and not r.get("used_market"))}

env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
env.filters["money"] = lambda p, c: money(p, c)
common = dict(cfg=cfg, site=cfg["site"], checked_utc=utc_s, checked_ct=ct_s, families=families, vendor_list=vendor_list,
              boards=boards, ok_vendors=ok_vendors, bad_vendors=bad_vendors, year=dt.date.today().year, counts=counts)

if OUT.exists():
    shutil.rmtree(OUT)
(OUT / "boards").mkdir(parents=True)
shutil.copytree(ROOT / "static", OUT / "static")
(OUT / "data").mkdir()
for f in ("stock.json", "history.json", "boards.json"):
    shutil.copy(DATA / f, OUT / "data" / f)


def render(tpl, path, depth=0, **kw):
    html = env.get_template(tpl).render(**common, root="../" * depth, page=path, **kw)
    (OUT / path).write_text(html)


render("index.html", "index.html", rows=rows)
render("compare.html", "compare.html", fields=SPEC_FIELDS)
import re as _re
def approx_usd(b):
    if b["best"]:
        return round(b["best"]["usd"], 2)
    m = _re.search(r"\$([0-9.]+)", (b["specs"].get("msrp") or {}).get("v", ""))
    return float(m.group(1)) if m else None
picker_data = [{"id": b["id"], "name": b["name"], "family": b["family"], "blurb": b["blurb"], "in_stock": b["in_stock_any"],
                "best": b["best"]["price_fmt"] + " at " + b["best"]["vendor_name"] if b["best"] else None,
                "discontinued": b.get("discontinued", False), "usd": approx_usd(b)} for b in boards]
render("picker.html", "picker.html", picker_json=json.dumps(picker_data))
render("about.html", "about.html")
for b in boards:
    render("board.html", f"boards/{b['id']}.html", depth=1, b=b, hist_json=json.dumps(b["history"]))
print(f"Built {len(boards)} board pages + 4 pages into {OUT}")
