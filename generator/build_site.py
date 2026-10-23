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


def aff_rel(vendor):
    """noopener nofollow, plus sponsored only once an affiliate template and id are set."""
    a = cfg.get("affiliates", {}).get(vendor) or {}
    rel = "noopener nofollow"
    if (a.get("template") or "").strip() and (a.get("id") or "").strip():
        rel += " sponsored"
    return rel


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
# Buyer-facing names. DE is Daisy's EU storefront, not a Germany-only shop.
MARKET_BUYER = {"US": "US", "CA": "Canada", "GB": "UK", "DE": "EU", "AU": "Australia"}
MARKET_PLACE = {"US": "the US", "CA": "Canada", "GB": "the UK", "DE": "the EU", "AU": "Australia"}


def _join_names(names):
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + " and " + names[-1]


def market_note(r):
    """Extra line when other Shopify countries disagree with the primary store."""
    markets = r.get("markets") or {}
    primary = r.get("market")
    if not markets or not primary or r.get("in_stock") is None:
        return None
    if r.get("in_stock"):
        differ = [c for c, ok in markets.items() if c != primary and ok is False]
        if not differ:
            return None
        return f"Sold out for {_join_names(MARKET_BUYER.get(c, c) for c in differ)} buyers"
    differ = [c for c, ok in markets.items() if c != primary and ok is True]
    if not differ:
        return None
    return f"In stock for {_join_names(MARKET_BUYER.get(c, c) for c in differ)} buyers"


def status_label(r, st):
    if r.get("used_market"):
        n = r.get("listing_count") or 0
        if n and r.get("price") is not None:
            return f"{n} used from {money(r.get('price'), r.get('currency'))}"
        if st in ("blocked", "error"):
            return STATUS_LABEL.get(st, st)
        return "No used listings"
    if r.get("market") and st in ("in_stock", "out_of_stock"):
        word = "In stock" if st == "in_stock" else "Sold out"
        return f"{word} ({r['market']} store)"
    return STATUS_LABEL.get(st, st)


def buyable(r):
    """True when a non-used listing is in stock in the primary store or any checked country."""
    if r.get("used_market") or r.get("status") not in ("in_stock", "out_of_stock", "backorder"):
        return False
    if any((r.get("markets") or {}).values()):
        return True
    return r.get("status") == "in_stock"


def stock_where(offers):
    """Hero line. 'In stock somewhere' overstated a US-only Shopify result."""
    places, other = [], False
    for o in offers:
        if o.get("used_market"):
            continue
        markets = o.get("markets") or {}
        if markets:
            for code, ok in markets.items():
                if ok:
                    places.append(MARKET_PLACE.get(code, code))
        elif o.get("in_stock"):
            other = True
    seen = []
    for p in places:
        if p not in seen:
            seen.append(p)
    if not seen and not other:
        return None
    if seen and not other:
        return "In stock in " + _join_names(seen)
    if seen and other:
        return "In stock in " + _join_names(seen) + " and at other tracked shops"
    return "In stock at tracked shops"


rows = []
for r in stock["results"]:
    b = BY_ID.get(r["board"])
    if not b:
        continue
    st = r.get("status", "unknown")
    usd = r["price"] * FX.get(r.get("currency") or "USD", 1) if r.get("price") is not None else None
    rows.append({**r, "b": b, "status": st, "status_label": status_label(r, st), "market_note": market_note(r),
                 "buyable": buyable(r), "price_fmt": money(r.get("price"), r.get("currency")),
                 "usd": usd, "checked_short": dt.datetime.fromisoformat(r["checked_at"]).astimezone(ZoneInfo("America/Chicago")).strftime("%-I:%M %p CT"),
                 "link": aff(r["vendor"], r.get("buy_url") or r["url"]), "link_rel": aff_rel(r["vendor"])})
order = {"in_stock": 0, "backorder": 1, "out_of_stock": 2, "redirect": 3, "unknown": 4, "error": 5, "blocked": 6,
         "used_listings": 7, "no_listings": 8}
rows.sort(key=lambda x: (x["b"]["family"], x["b"]["name"], order.get(x["status"], 9), x.get("usd") or 9e9))

def reverb_panel(o):
    if not o:
        return None
    cur = o.get("currency") or "USD"
    items = []
    for it in o.get("listings") or []:
        icur = it.get("currency") or cur
        ship = it.get("shipping_us")
        items.append({**it,
                      "price_fmt": money(it.get("price"), icur),
                      "unit_fmt": money(it.get("unit_price"), icur),
                      "ship_fmt": money(ship, "USD") if ship is not None else None,
                      "link": aff("reverb", it.get("url") or o["url"])})
    return {"status": o.get("status"), "error": o.get("error"), "ok": o.get("ok"),
            "count": o.get("listing_count") or 0,
            "from_fmt": money(o.get("price"), cur) if o.get("price") is not None else None,
            "head": items[0] if items else None, "hits": items,
            "search": aff("reverb", o["url"]), "rel": aff_rel("reverb")}


for b in boards:
    b["offers"] = [r for r in rows if r["board"] == b["id"]]
    b["shop_offers"] = [o for o in b["offers"] if not o.get("used_market")]
    b["reverb"] = reverb_panel(next((o for o in b["offers"] if o.get("used_market")), None))
    b["in_stock_any"] = any(o.get("buyable") for o in b["offers"])
    b["stock_where"] = stock_where(b["offers"]) if b["in_stock_any"] else None
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
counts = {"in": sum(1 for r in rows if r.get("buyable")),
          "out": sum(1 for r in rows if r["status"] in ("out_of_stock", "backorder") and not r.get("used_market") and not r.get("buyable"))}

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
                "where": b.get("stock_where"),
                "best": b["best"]["price_fmt"] + " at " + b["best"]["vendor_name"] if b["best"] else None,
                "discontinued": b.get("discontinued", False), "usd": approx_usd(b)} for b in boards]
render("picker.html", "picker.html", picker_json=json.dumps(picker_data))
render("about.html", "about.html")
for b in boards:
    render("board.html", f"boards/{b['id']}.html", depth=1, b=b, hist_json=json.dumps(b["history"]))
print(f"Built {len(boards)} board pages + 4 pages into {OUT}")
