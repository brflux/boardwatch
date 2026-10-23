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
import json, os, re, time, html, sys, datetime as dt
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

def _shopify_host(lst, vendor):
    """Product JSON host. electro-smith.com 301s to daisy.audio and can drop ?country=."""
    u = urlparse(lst["url"])
    host = (u.netloc or "").lower()
    home = urlparse(vendor.get("home") or "")
    if home.netloc and host.endswith(("electro-smith.com", "electrosmith.com")):
        host = home.netloc
    return u.scheme or "https", host or home.netloc


def _shopify_pick(lst, d):
    variants = []
    for v in d.get("variants") or []:
        variants.append({"title": v.get("title"), "price": v["price"] / 100.0, "available": bool(v.get("available"))})
    if not variants:
        raise ValueError(f"no variants on {lst.get('handle')}")
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
    return {"title": d.get("title"), "in_stock": bool(avail), "price": pick["price"],
            "variants": variants if len(variants) > 1 else None}


def _shopify_fetch(lst, vendor, country=None):
    scheme, host = _shopify_host(lst, vendor)
    url = f"{scheme}://{host}/products/{lst['handle']}.js"
    if country:
        url += "?country=" + quote_plus(country)
    return polite_get(url).json()


def p_shopify(lst, vendor):
    markets = [m for m in (vendor.get("markets") or []) if m]
    primary = vendor.get("primary_market") or (markets[0] if markets else None)
    d = _shopify_fetch(lst, vendor, primary)
    out = _shopify_pick(lst, d)
    out["source"] = "shopify .js"
    if not primary:
        return out
    # Price and the headline in_stock flag are the primary store. Other countries
    # are availability only; a failed fetch is omitted instead of failing the listing.
    found = {}
    for code in ([primary] + [m for m in markets if m != primary]):
        if code == primary:
            found[code] = out["in_stock"]
            continue
        try:
            found[code] = _shopify_pick(lst, _shopify_fetch(lst, vendor, code))["in_stock"]
        except Exception:
            continue
    out["market"] = primary
    out["markets"] = found
    return out


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


_BOARDS = None
# Reverb publishes continental-US shipping as US_CON. The header value "US" drops
# listings that only have a US_CON rate, including live board lots.
_REVERB_HEADERS = {"Accept": "application/hal+json", "Accept-Version": "3.0",
                   "X-Display-Currency": "USD", "X-Shipping-Region": "US_CON"}
_QTY_RES = (
    re.compile(r"(?<![a-z0-9])(?:lot|pack|set|bundle)\s+of\s+(\d+)(?![a-z0-9])", re.I),
    re.compile(r"(?<![a-z0-9])(\d+)\s*pcs(?![a-z0-9])", re.I),
    re.compile(r"(?<![a-z0-9])(\d+)\s*-?\s*pack(?![a-z0-9])", re.I),
    re.compile(r"(?<![a-z0-9])x\s*(\d+)(?![a-z0-9])", re.I),
)


def _boards():
    global _BOARDS
    if _BOARDS is None:
        _BOARDS = {b["id"]: b for b in json.loads((DATA / "boards.json").read_text())["boards"]}
    return _BOARDS


def _term_in(hay, term):
    term = (term or "").lower().strip()
    if not term:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", hay) is not None


def _norm_make(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _money_amount(obj):
    if not isinstance(obj, dict) or obj.get("amount") is None:
        return None, None
    try:
        return float(obj["amount"]), obj.get("currency")
    except (TypeError, ValueError):
        return None, None


def _pack_qty(title):
    for rx in _QTY_RES:
        m = rx.search(title or "")
        if not m:
            continue
        n = int(m.group(1))
        if 2 <= n <= 100:
            return n
    return 1


def _reverb_excludes(lst, vendor):
    b = _boards().get(lst.get("board")) or {}
    kind = (b.get("kind") or "").lower()
    name = (b.get("name") or "").lower()
    is_kit = "kit" in kind or "kit" in name
    is_euro_kit = is_kit and "eurorack" in name
    skip = set()
    if is_kit:
        skip.add("pedal kit")
    if is_euro_kit:
        skip.update(("module", "eurorack"))
    out = []
    for term in list(vendor.get("exclude_default") or []) + list(lst.get("exclude") or []):
        if (term or "").lower() in skip:
            continue
        out.append(term)
    return out


def _reverb_keep(lst, vendor, raw):
    """Live listings only. Haystack is title + make + model."""
    state = ((raw.get("state") or {}).get("slug") or "").lower()
    if raw.get("draft") is True or (state and state != "live") or raw.get("live") is False:
        return None
    title = raw.get("title") or ""
    make = raw.get("make") or ""
    model = raw.get("model") or ""
    hay = f"{title} {make} {model}".lower()
    must = lst.get("must") or []
    any_of = lst.get("any") or []
    if any(not _term_in(hay, t) for t in must):
        return None
    if any_of and not any(_term_in(hay, t) for t in any_of):
        return None
    if any(_term_in(hay, t) for t in _reverb_excludes(lst, vendor)):
        return None
    allowed = [_norm_make(m) for m in (lst.get("make") or []) if _norm_make(m)]
    if allowed and _norm_make(make) not in allowed:
        return None
    price, cur = _money_amount(raw.get("buyer_price"))
    if price is None:
        price, cur = _money_amount(raw.get("price"))
    if price is None:
        return None
    qty = _pack_qty(title)
    unit = round(price / qty, 2)
    ship = None
    for rate in ((raw.get("shipping") or {}).get("rates") or []):
        if rate.get("region_code") == "US_CON":
            ship, _cur = _money_amount(rate.get("rate"))
            break
    photo = None
    photos = raw.get("photos") or []
    if photos:
        links = photos[0].get("_links") or {}
        for key in ("small_crop", "thumbnail", "large_crop", "full"):
            href = (links.get(key) or {}).get("href")
            if href:
                photo = href
                break
    if not photo:
        photo = ((raw.get("_links") or {}).get("photo") or {}).get("href")
    web = ((raw.get("_links") or {}).get("web") or {}).get("href")
    return {"id": raw.get("id"), "title": title, "url": web, "condition": (raw.get("condition") or {}).get("display_name"),
            "price": price, "currency": cur or "USD", "shipping_us": ship, "qty": qty, "unit_price": unit,
            "offers_enabled": bool(raw.get("offers_enabled")), "shop_name": raw.get("shop_name"),
            "photo": photo, "published_at": raw.get("published_at")}


def _reverb_queries(lst):
    queries = [q for q in (lst.get("queries") or []) if q]
    if not queries and lst.get("query"):
        queries = [lst["query"]]
    return queries


def p_reverb(lst, vendor):
    queries = _reverb_queries(lst)
    if not queries:
        raise ValueError("reverb listing has no queries")
    headers = dict(_REVERB_HEADERS)
    token = (os.environ.get("REVERB_API_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    found, errors = {}, []
    for q in queries:
        try:
            d = polite_get("https://api.reverb.com/api/listings", params={"query": q, "per_page": 50}, headers=headers).json()
        except Exception as e:
            errors.append(str(e))
            continue
        for raw in d.get("listings") or []:
            rid = raw.get("id")
            if rid is None or rid in found:
                continue
            hit = _reverb_keep(lst, vendor, raw)
            if hit:
                found[rid] = hit
    if not found and errors:
        msg = errors[0]
        if msg.startswith(("disallowed", "timed out", "HTTP ", "network error")):
            raise Blocked(msg)
        raise ValueError(msg)
    hits = sorted(found.values(), key=lambda h: (h["unit_price"], h["price"], h.get("id") or 0))
    head = hits[0] if hits else None
    res = {"in_stock": bool(hits), "price": head["price"] if head else None,
           "currency": head["currency"] if head else "USD", "listing_count": len(hits),
           "listings": hits[:5], "listing_ids": [h["id"] for h in hits],
           "min_price": min((h["price"] for h in hits), default=None),
           "min_unit_price": min((h["unit_price"] for h in hits), default=None),
           "status": "used_listings" if hits else "no_listings",
           "source": "Reverb public API", "_matches": hits}
    if head and head.get("url"):
        res["buy_url"] = head["url"]
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
        matches = rec.pop("_matches", None)
        if rec.get("used_market"):
            # Used rows never raise restock / price_down. A new listing id is the only alert.
            prev_ids = set()
            if p:
                prev_ids = set(p.get("listing_ids") or [])
                if not prev_ids:
                    prev_ids = {x.get("id") for x in (p.get("listings") or []) if x.get("id") is not None}
            fresh = [m for m in (matches or []) if m.get("id") not in prev_ids]
            if fresh:
                rec["changed"] = "new_used_listing"
                lines = []
                for m in fresh:
                    amt, cur = m.get("price"), m.get("currency") or "USD"
                    shown = f"${amt:,.2f}" if cur == "USD" and amt is not None else (f"{cur} {amt:.2f}" if amt is not None else "")
                    lines.append(f"New on Reverb: {m.get('title')} for {shown}".rstrip())
                rec["report_lines"] = lines
                rec["report_line"] = lines[0]
        elif p and p.get("in_stock") is not None and rec.get("in_stock") is not None and p["in_stock"] != rec["in_stock"]:
            rec["changed"] = "restock" if rec["in_stock"] else "sold_out"
        elif p and p.get("price") and rec.get("price") and abs(p["price"] - rec["price"]) > 0.005:
            rec["changed"] = "price_up" if rec["price"] > p["price"] else "price_down"
            rec["prev_price"] = p["price"]
        results.append(rec)
        mk = rec.get("markets") or {}
        mk_s = " ".join(f"{k}{'+' if v else '-'}" for k, v in mk.items())
        used_n = f"n={rec.get('listing_count')}" if rec.get("used_market") else ""
        print(f"[{rec['status']:>14}] {rec['vendor']:<12} {rec['board']:<22} {rec.get('price')} {mk_s} {used_n} {rec.get('error','')}", flush=True)

    snap = {"run_at": run_at, "user_agent": UA, "vendor_summary": vstat, "results": results}
    (DATA / "stock.json").write_text(json.dumps(snap, indent=1, ensure_ascii=False))

    hist_path = DATA / "history.json"
    hist = json.loads(hist_path.read_text()) if hist_path.exists() else {"points": []}
    for r in results:
        if r.get("ok") and (r.get("price") is not None or r.get("in_stock") is not None):
            point = {"t": r["checked_at"], "board": r["board"], "vendor": r["vendor"], "url": r["url"],
                     "price": r.get("price"), "currency": r.get("currency"), "in_stock": r.get("in_stock")}
            if r.get("market"):
                point["market"] = r["market"]
            if r.get("markets"):
                point["markets"] = r["markets"]
            hist["points"].append(point)
    hist_path.write_text(json.dumps(hist, indent=0, ensure_ascii=False))

    changes = [r for r in results if r.get("changed")]
    (DATA / "changes.json").write_text(json.dumps({"run_at": run_at, "changes": changes}, indent=1, ensure_ascii=False))
    ok = sum(1 for r in results if r.get("ok"))
    print(f"\n{ok}/{len(results)} listings OK; {len(changes)} changes. Wrote data/stock.json, history.json, changes.json")


if __name__ == "__main__":
    sys.exit(main())
