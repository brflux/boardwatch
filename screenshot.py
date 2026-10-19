#!/usr/bin/env python3
"""Full-page screenshots of the locally served site (python -m http.server 8080 --directory site)."""
import sys
from playwright.sync_api import sync_playwright
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
    pg.emulate_media(reduced_motion="reduce")
    pg.goto(BASE + "/index.html", wait_until="networkidle"); pg.screenshot(path="screenshots/home.png", full_page=True)
    pg.goto(BASE + "/picker.html", wait_until="networkidle"); pg.screenshot(path="screenshots/picker.png", full_page=True)
    # picker with answers filled (installation + Pd + in/out + $150 + quality)
    for q, v in [("q1", "install"), ("q2", "patch"), ("q3", "inout"), ("q4", "150"), ("q5", "quality")]:
        pg.check(f'input[name="{q}"][value="{v}"]')
    pg.click("#go"); pg.wait_for_timeout(400)
    pg.screenshot(path="screenshots/picker-results.png", full_page=True)
    pg.goto(BASE + "/boards/daisy-pod.html", wait_until="networkidle"); pg.screenshot(path="screenshots/board-daisy-pod.png", full_page=True)
    pg.goto(BASE + "/compare.html", wait_until="networkidle"); pg.screenshot(path="screenshots/compare.png", full_page=False)
    m = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True)
    m.emulate_media(reduced_motion="reduce")
    m.goto(BASE + "/index.html", wait_until="networkidle"); m.screenshot(path="screenshots/home-mobile.png", full_page=True)
    b.close()
print("screenshots saved")
