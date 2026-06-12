"""
Fonbet API sniffer — v3. Playwright intercept, headless.
Usage:
  python sniff.py              # main page + scroll
  python sniff.py --headed     # show browser window
  python sniff.py --sport 1    # specific sport page
"""
import json
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR.parents[2] / "config" / "bookmakers.json"
OUTPUT = BASE_DIR / "network_log.json"
COR_CACHE = BASE_DIR / "fonbet_raw.bin"

INTERESTING_PREFIXES = ["/line", "/api", "/client", "/dictionary", "/event", "/sport", "/bets"]
INTERESTING_DOMAINS = ["by0e87-resources.by", "fonbet.by"]
INTERESTING_MIME = {"application/json", "application/octet-stream", "text/plain"}


def is_interesting(url, content_type=""):
    url_l = url.lower()
    ct_l = content_type.lower()
    if any(p in url_l for p in INTERESTING_PREFIXES):
        return True
    if any(m in ct_l for m in INTERESTING_MIME):
        return True
    return False


def try_body(response):
    try:
        b = response.body()
        if not b:
            return None
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary, {len(b)} bytes>"
    except Exception:
        return None


def load_sports():
    if not CONFIG_PATH.exists():
        return {}
    cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
    return cfg.get("bookmakers", {}).get("fonbet", {}).get("sports", {})


def sniff(filters=None, headless=True):
    # Clean up old network_log — each run is fresh
    if OUTPUT.exists():
        OUTPUT.unlink()

    log = {
        "meta": {
            "url": "https://fonbet.by",
            "ts": datetime.now(timezone.utc).isoformat(),
        },
        "requests": [],
        "responses": [],
    }

    def on_req(request):
        url = request.url
        if not is_interesting(url):
            return
        log["requests"].append({
            "url": url, "method": request.method,
            "headers": dict(request.headers),
            "post_data": request.post_data,
            "resource_type": request.resource_type,
        })

    def on_resp(response):
        url = response.request.url
        ct = response.headers.get("content-type", "")
        if not is_interesting(url, ct):
            return

        entry = {
            "url": url, "status": response.status,
            "content_type": ct, "resource_type": response.request.resource_type,
        }
        body = try_body(response)
        if body:
            entry["body"] = body
            if body.startswith("<binary") and len(body) > 1000:
                raw = response.body()
                COR_CACHE.write_bytes(raw)
                entry["_saved"] = str(COR_CACHE)

        log["responses"].append(entry)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(locale="ru-RU", timezone_id="Europe/Minsk")
        page = ctx.new_page()
        page.set_viewport_size({"width": 1280, "height": 1800})
        page.on("request", on_req)
        page.on("response", on_resp)

        print("[sniff] Navigating to https://fonbet.by")
        page.goto("https://fonbet.by", wait_until="domcontentloaded", timeout=30000)
        print("[sniff] Waiting for SPA to load (20s)...")
        page.wait_for_timeout(20_000)

        if filters:
            sports_cfg = load_sports()
            for filter_key in filters:
                sport_cfg = sports_cfg.get(filter_key)
                if not sport_cfg:
                    for sid, sc in sports_cfg.items():
                        if sc["name"] == filter_key:
                            sport_cfg = sc
                            filter_key = sid
                            break
                if not sport_cfg:
                    print(f"  Unknown sport: {filter_key}, skipping")
                    continue

                sport_url = f"https://fonbet.by/#!/sport/{filter_key}"
                print(f"[sniff] Navigating to {sport_cfg['name_ru']} -> {sport_url}")
                page.goto(sport_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(10_000)

                print(f"[sniff] Scrolling {sport_cfg['name_ru']}...")
                for i in range(8):
                    page.evaluate("window.scrollBy(0, 800)")
                    time.sleep(1.5)
                page.wait_for_timeout(5_000)
        else:
            print("[sniff] Navigating to football section...")
            try:
                sport_links = page.locator("a[href*='sport']").all()
                for link in sport_links[:3]:
                    href = link.get_attribute("href")
                    if href:
                        print(f"  -> {href}")
                        page.goto(f"https://fonbet.by{href}", wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(10_000)
                        break
            except Exception as e:
                print(f"  nav failed: {e}")

            print("[sniff] Scrolling to trigger API calls...")
            for i in range(8):
                page.evaluate("window.scrollBy(0, 800)")
                time.sleep(1.5)

        page.wait_for_timeout(10_000)
        browser.close()

    OUTPUT.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    api_urls = set()
    for r in log["responses"]:
        if any(d in r["url"] for d in INTERESTING_DOMAINS):
            api_urls.add(r["url"])
    print(f"\n[sniff] Done: {len(log['requests'])} requests, {len(log['responses'])} responses")
    print(f"[sniff] Interesting URLs:")
    for u in sorted(api_urls)[:40]:
        print(f"  {u}")
    if COR_CACHE.exists():
        print(f"[sniff] Binary saved: {COR_CACHE.stat().st_size} bytes")


if __name__ == "__main__":
    headless = "--headed" not in sys.argv
    filters = None
    if "--sport" in sys.argv:
        idx = sys.argv.index("--sport")
        if idx + 1 < len(sys.argv):
            filters = [s.strip() for s in sys.argv[idx + 1].split(",")]
    sniff(filters, headless=headless)
