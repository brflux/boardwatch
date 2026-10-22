# BoardWatch (MVP)

Live stock + price tracker for audio dev boards (Daisy, Teensy, Bela, ESP32, RP2350, Pi audio HATs).
Static site, no paid services. Data → JSON → static HTML.

## Layout
```
config.json                 site settings, sponsor slot, affiliate IDs (empty), newsletter form action (empty), checker UA/rate limit, FX for filters
data/boards.json            21 boards; every spec = {"v": value, "src": official URL}; unknowns omitted
data/vendors.json           12 vendors + how each is read (shopify / woocommerce / jsonld / sparkfun / reverb / pjrc / probe)
data/listings.json          34 board↔vendor product links
data/stock.json             latest snapshot (written by checker)
data/history.json           append-only price/stock points (price-change detection + charts)
data/changes.json           restocks / sold-outs / price moves since previous run (feed this to alert emails later)
checker/check_stock.py      the stock checker (robots.txt aware, ≥3 s per host, clear UA, never bypasses blocks)
generator/build_site.py     Jinja2 build → site/
templates/, static/         HTML templates, CSS (analog-synth theme), JS (filters, picker, history chart)
site/                       built output: index, compare, picker, about, boards/<id>.html, data/*.json
screenshot.py               Playwright full-page screenshots → screenshots/
.github/workflows/check-and-deploy.yml   scheduled check + GitHub Pages deploy (hourly; GitHub may skip some runs)
```

## Run
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt playwright
.venv/bin/python checker/check_stock.py     # ~100 s (polite delays)
.venv/bin/python generator/build_site.py
python3 -m http.server 8080 --directory site
```

## Add a board / vendor
Add the board to data/boards.json (specs only from official pages, with `src`), then a row in data/listings.json.
Shopify shops need `handle`; WooCommerce needs `product_id`; Reverb needs `query`/`must`/`exclude`.

## Affiliates & sponsor
Fill `config.json → affiliates.<vendor>.id` (+ `param` or `template`) and rebuild; links rewrite automatically.
Set `sponsor.name/url` to fill the "Sponsor BoardWatch" slot.

## Newsletter
`config.json → newsletter.form_action` empty = placeholder form (nothing is sent or stored). Paste a provider's
embed form action URL (Buttondown / Kit / Beehiiv) and rebuild.
