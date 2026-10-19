#!/usr/bin/env python3
"""BoardWatch stock & price checker.

Reads data/listings.json + data/vendors.json, fetches each vendor product using the
most structured source available (Shopify .js, WooCommerce Store API, JSON-LD, public
Reverb API, HTML fallback), honours robots.txt, rate-limits per host, and writes:
  data/stock.json    latest snapshot
  data/history.json  append-only price/stock history (for change detection)
  data/changes.json  restocks / price changes since the previous run
Never bypasses blocks: a 403/429/challenge is recorded as "blocked".
"""
import json, re, time, html, sys, datetime as dt
import urllib.robotparser as robotparser
from urllib.parse import urlparse, quote_plus
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CFG = json.loads((ROOT / "config.json").read_text())["checker"]
UA = CFG["user_agent"]
DELAY = float(CFG.get("min_delay_s", 3))
TIMEOUT = CFG.get("timeout_s", 25)

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})
_last_hit, _robots = {}, {}


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class Blocked(Exception):
    pass


def robots_ok(url):
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            r = S.get(base + "/robots.txt", timeout=TIMEOUT)
            if r.status_code in (401, 403):
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(r.text.splitlines())
        except requests.RequestException:
            rp.allow_all = True  # unreachable robots => treat as no rules; fetch will likely fail anyway
        _robots[base] = rp
    return _robots[base].can_fetch("BoardWatchBot", url)


def polite_get(url, **kw):
    if not robots_ok(url):
        raise Blocked("disallowed by robots.txt")
    host = urlparse(url).netloc
    wait = DELAY - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()
    try:
        r = S.get(url, timeout=TIMEOUT, **kw)
    except requests.Timeout:
        raise Blocked("timed out (likely bot protection / tarpit)")
    except requests.RequestException as e:
        raise Blocked(f"network error: {e.__class__.__name__}")
    if r.status_code in (401, 403, 429, 503) or "Just a moment..." in r.text[:2000]:
        raise Blocked(f"HTTP {r.status_code} (bot protection)")
    r.raise_for_status()
    return r


# ---------------- parsers ----------------

def p_shopify(lst, vendor):
    u = urlparse(lst["url"])
    r = polite_get(f"{u.scheme}://{u.netloc}/products/{lst['handle']}.js")
    d = r.json()
    variants = []
    for v in d["variants"]:
        variants.append({"title": v.get("title"), "price": v["price"] / 100.0, "available": bool(v.get("available"))})
    # Optional listing["variant"] pins one Shopify option (e.g. Patch Submodule "65MB")
    # so a later cheaper option on the same product is not reported in its place.
    pool = variants
    want = (lst.get("variant") or "").strip().lower()
    if want:
        pool = [v for v in variants if (v.get("title") or "").strip().lower() == want]
        if not pool:
            raise ValueError(f"variant {lst['variant']!r} not on {lst['handle']}")
    avail = [v for v in pool if v["available"]]
    pick = min(avail or pool, key=lambda v: v["price"])
    return {"title": d["title"], "in_stock": bool(avail), "price": pick["price"],
            "variants": variants if len(variants) > 1 else None, "source": "shopify .js"}


def p_woocommerce(lst, vendor):
    u = urlparse(lst["url"])
    r = polite_get(f"{u.scheme}://{u.netloc}/wp-json/wc/store/v1/products/{lst['product_id']}")
    d = r.json()
    pr = d["prices"]
    price = int(pr["price"]) / (10 ** pr.get("currency_minor_unit", 2))
    return {"title": html.unescape(d["name"]), "in_stock": bool(d.get("is_in_stock")), "price": price,
            "currency": pr.get("currency_code"), "source": "WooCommerce Store API"}


def _jsonld_products(text):
    out = []
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', text, re.S):
        try:
            d = json.loads(m.group(1).strip())
        except Exception:
            continue
        items = d if isinstance(d, list) else d.get("@graph", [d])
        out += [i for i in items if isinstance(i, dict) and i.get("@type") == "Product"]
    return out


def p_jsonld(lst, vendor):
    r = polite_get(lst["url"])
    prods = _jsonld_products(r.text)
    if not prods:
        raise ValueError("no JSON-LD Product found")
    p = prods[0]
    offers = p.get("offers") or {}
    offers = offers if isinstance(offers, list) else [offers]
    o = offers[0]
    price = o.get("price")
    if price is None and o.get("priceSpecification"):
        ps = o["priceSpecification"]
        price = (ps[0] if isinstance(ps, list) else ps).get("price")
    avail = str(o.get("availability", ""))
    return {"title": html.unescape(p.get("name", "")), "in_stock": ("InStock" in avail) if avail else None,
            "price": float(price) if price is not None else None, "currency": o.get("priceCurrency"),
            "availability_raw": avail.rsplit("/", 1)[-1] or None, "source": "JSON-LD"}


def p_sparkfun(lst, vendor):
    r = polite_get(lst["url"])
    t = r.text
    res = {"source": "JSON-LD price + Magento stock block"}
    prods = _jsonld_products(t)
    if prods:
        p = prods[0]
        o = p.get("offers") or {}
        o = o[0] if isinstance(o, list) else o
        res.update(title=html.unescape(p.get("name", "")), price=float(o["price"]) if o.get("price") is not None else None,
                   currency=o.get("priceCurrency"))
    m = re.search(r'product-info-stock-sku.*?<div class="stock ([^"]*)"[^>]*>\s*<span>([^<]+)</span>', t, re.S)
    if not m:
        raise ValueError("stock block not found")
    label = m.group(2).strip()
    res["availability_raw"] = label
    res["in_stock"] = label.lower() == "in stock"
    if label.lower().startswith("backorder"):
        res["status"] = "backorder"
    return res


def p_pjrc(lst, vendor):
    r = polite_get(lst["url"])
    t = r.text
    if re.search(r"SparkFun is now manufacturing Teensy", t):
        return {"title": None, "in_stock": None, "price": None, "status": "redirect",
                "availability_raw": "PJRC page says: buy directly at SparkFun", "source": "page text"}
    return {"in_stock": None, "price": None, "status": "unknown", "source": "page text"}


def p_reverb(lst, vendor):
    url = f"https://api.reverb.com/api/listings?query={quote_plus(lst['query'])}&per_page=50"
    r = polite_get(url, headers={"Accept": "application/hal+json", "Accept-Version": "3.0"})
    d = r.json()
    must = [m.lower() for m in lst.get("must", [])]
    excl = [x.lower() for x in lst.get("exclude", [])]
    hits = []
    for l in d.get("listings", []):
        title = l.get("title", "")
        tl = title.lower()
        if all(m in tl for m in must) and not any(x in tl for x in excl):
            hits.append({"title": title, "price": float(l["price"]["amount"]), "currency": l["price"]["currency"],
                         "condition": (l.get("condition") or {}).get("display_name"), "url": l["_links"]["web"]["href"]})
    hits.sort(key=lambda h: h["price"])
    res = {"in_stock": bool(hits), "price": hits[0]["price"] if hits else None, "currency": hits[0]["currency"] if hits else "USD",
           "listing_count": len(hits), "cheapest": hits[0] if hits else None, "source": "Reverb public API (used/new marketplace)"}
    if hits:
        res["buy_url"] = hits[0]["url"]
    return res


def p_probe(lst, vendor):
    polite_get(lst["url"])
    raise ValueError("reachable but no parser yet (needs official API)")


PARSERS = {"shopify": p_shopify, "woocommerce": p_woocommerce, "jsonld": p_jsonld, "sparkfun": p_sparkfun,
           "pjrc": p_pjrc, "reverb": p_reverb, "probe": p_probe}


def main():
    vendors = json.loads((DATA / "vendors.json").read_text())["vendors"]
    listings = json.loads((DATA / "listings.json").read_text())["listings"]
    prev = {}
    if (DATA / "stock.json").exists():
        for r in json.loads((DATA / "stock.json").read_text()).get("results", []):
            prev[(r["board"], r["vendor"], r["url"])] = r
    run_at = now()
    results, vstat = [], {}
    for lst in listings:
        v = vendors[lst["vendor"]]
        rec = {"board": lst["board"], "vendor": lst["vendor"], "vendor_name": v["name"], "url": lst["url"],
               "variant_label": lst.get("variant_label"), "currency": v.get("currency"), "price_note": v.get("price_note"),
               "used_market": bool(v.get("used")), "checked_at": now()}
        try:
            out = PARSERS[v["method"]](lst, v)
            out = {k: val for k, val in out.items() if val is not None or k in ("in_stock", "price")}
            rec.update(out)
            rec.setdefault("status", "in_stock" if rec.get("in_stock") else ("out_of_stock" if rec.get("in_stock") is False else "unknown"))
            rec["ok"] = True
            vstat.setdefault(lst["vendor"], {"ok": 0, "fail": 0, "errors": []})["ok"] += 1
        except Blocked as e:
            rec.update(ok=False, status="blocked", error=str(e), in_stock=None, price=None)
            vs = vstat.setdefault(lst["vendor"], {"ok": 0, "fail": 0, "errors": []}); vs["fail"] += 1; vs["errors"].append(str(e))
        except Exception as e:
            rec.update(ok=False, status="error", error=f"{e.__class__.__name__}: {e}"[:200], in_stock=None, price=None)
            vs = vstat.setdefault(lst["vendor"], {"ok": 0, "fail": 0, "errors": []}); vs["fail"] += 1; vs["errors"].append(rec["error"])
        p = prev.get((rec["board"], rec["vendor"], rec["url"]))
        if p and p.get("in_stock") is not None and rec.get("in_stock") is not None and p["in_stock"] != rec["in_stock"]:
            rec["changed"] = "restock" if rec["in_stock"] else "sold_out"
        elif p and p.get("price") and rec.get("price") and abs(p["price"] - rec["price"]) > 0.005:
            rec["changed"] = "price_up" if rec["price"] > p["price"] else "price_down"
            rec["prev_price"] = p["price"]
        results.append(rec)
        print(f"[{rec['status']:>12}] {rec['vendor']:<12} {rec['board']:<22} {rec.get('price')} {rec.get('error','')}", flush=True)

    snap = {"run_at": run_at, "user_agent": UA, "vendor_summary": vstat, "results": results}
    (DATA / "stock.json").write_text(json.dumps(snap, indent=1, ensure_ascii=False))

    hist_path = DATA / "history.json"
    hist = json.loads(hist_path.read_text()) if hist_path.exists() else {"points": []}
    for r in results:
        if r.get("ok") and (r.get("price") is not None or r.get("in_stock") is not None):
            hist["points"].append({"t": r["checked_at"], "board": r["board"], "vendor": r["vendor"], "url": r["url"],
                                   "price": r.get("price"), "currency": r.get("currency"), "in_stock": r.get("in_stock")})
    hist_path.write_text(json.dumps(hist, indent=0, ensure_ascii=False))

    changes = [r for r in results if r.get("changed")]
    (DATA / "changes.json").write_text(json.dumps({"run_at": run_at, "changes": changes}, indent=1, ensure_ascii=False))
    ok = sum(1 for r in results if r.get("ok"))
    print(f"\n{ok}/{len(results)} listings OK; {len(changes)} changes. Wrote data/stock.json, history.json, changes.json")


if __name__ == "__main__":
    sys.exit(main())
