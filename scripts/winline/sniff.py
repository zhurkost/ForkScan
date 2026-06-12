"""
Winline scroll sniffer — v5.
Multi-sport: scrolls each sport page, extracts event IDs from DOM.
Output: event_ids_{sport}.json (for parse_events.py POST-per-ID approach).
Usage:
  python sniff.py              # all sports, headless
  python sniff.py --headed     # show browser
  python sniff.py --sport 149  # single sport
"""
import json
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

PARALLEL_BROWSERS = 10  # change to adjust concurrency

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "bookmakers.json"
BASE_DIR = Path(__file__).resolve().parent

INTERESTING = ["/sb/", "/cormache", "/api/init"]


def is_interesting(url):
    return any(k in url.lower() for k in INTERESTING)


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


def dismiss_popups(page):
    for sel in ["[data-t='modal-close']", "[data-t='wl-modal-close']",
                ".wl-modal__close", "[data-t='cookie-accept']"]:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.click(timeout=3000)
                page.wait_for_timeout(1000)
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:
        pass


def extract_event_ids_from_dom(page, sport_name: str) -> set:
    js = """() => {
        const ids = new Set();
        function add(v) { const n = parseInt(v); if (n >= 1000000) ids.add(n); }
        document.querySelectorAll('[data-event-id], [data-id]').forEach(el => {
            add(el.getAttribute('data-event-id') || el.getAttribute('data-id'));
        });
        document.querySelectorAll('[id*="event"]').forEach(el => {
            const m = el.id.match(/(\\d{5,})/); if (m) add(m[1]);
        });
        document.querySelectorAll('a[href*="/event/"]').forEach(el => {
            const m = el.href.match(/\\/event\\/(\\d+)/); if (m) add(m[1]);
        });
        document.querySelectorAll('[class*="event"]').forEach(el => {
            for (const attr of ['id', 'data-event-id', 'data-id', 'data-key']) add(el.getAttribute(attr));
        });
        return Array.from(ids);
    }"""
    try:
        ids = page.evaluate(js)
        result = {int(x) for x in ids if isinstance(x, (int, float)) and x >= 1_000_000}
        return result
    except Exception as e:
        print(f"[sniff:{sport_name}] DOM extraction failed: {e}")
    return set()


def sniff_sport(sport_id: str, sport_cfg: dict, headless: bool):
    sport_name = sport_cfg["name"]
    sport_url = sport_cfg.get("url") or f"https://winline.by/sport/{sport_id}"
    name_ru = sport_cfg.get("name_ru", sport_name)

    try:
        return _sniff_sport(sport_id, sport_cfg, sport_name, sport_url, name_ru, headless)
    except Exception as e:
        print(f"[sniff:{sport_name}] FAILED: {e}")
        return 0


def _sniff_sport(sport_id, sport_cfg, sport_name, sport_url, name_ru, headless):
    print(f"[sniff:{sport_name}] {name_ru} -> {sport_url}")

    log = {"requests": [], "responses": []}

    def on_request(request):
        if is_interesting(request.url):
            log["requests"].append({
                "url": request.url, "method": request.method,
                "post_data": request.post_data, "resource_type": request.resource_type,
            })

    def on_response(response):
        url = response.request.url
        if is_interesting(url):
            ct = response.headers.get("content-type", "")
            entry = {"url": url, "status": response.status, "content_type": ct}
            body = try_body(response)
            if body:
                entry["body"] = body
            log["responses"].append(entry)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(locale="ru-RU", timezone_id="Europe/Minsk")
        page = ctx.new_page()
        page.set_viewport_size({"width": 1280, "height": 1800})
        page.on("request", on_request)
        page.on("response", on_response)

        page.goto(sport_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5_000)
        dismiss_popups(page)

        # Scroll to trigger lazy-load
        print(f"[sniff:{sport_name}] Scrolling...")
        last_count = 0
        stuck = 0
        bounced = False
        for i in range(60):
            page.evaluate("window.scrollBy(0, 800)")
            time.sleep(0.8)
            current = len(log["requests"])
            if current == last_count:
                stuck += 1
                if stuck >= 10:
                    if not bounced:
                        # Bounce up then down to trigger more lazy-load
                        page.evaluate("window.scrollBy(0, -2000)")
                        time.sleep(0.5)
                        page.evaluate("window.scrollBy(0, 2000)")
                        time.sleep(1.0)
                        stuck = 0
                        bounced = True
                    else:
                        break
            else:
                stuck = 0
                bounced = False
            last_count = current

        page.wait_for_timeout(3_000)

        # Extract DOM event IDs
        dom_ids = extract_event_ids_from_dom(page, sport_name)

        browser.close()

    # Network-based IDs (POST to /sb/api/by/actual)
    net_ids = set()
    for r in log["requests"]:
        if r.get("post_data") and "by/actual" in r["url"]:
            try:
                net_ids.add(json.loads(r["post_data"])["query"]["id"])
            except Exception:
                pass

    event_ids = dom_ids | net_ids
    print(f"[sniff:{sport_name}] IDs: {len(event_ids)} (DOM: {len(dom_ids)}, net: {len(net_ids)})")

    # Save
    ids_path = BASE_DIR / f"event_ids_{sport_name}.json"
    ids_data = {
        "sport_id": int(sport_id), "sport_name": sport_name,
        "count": len(event_ids), "event_ids": sorted(event_ids),
    }
    ids_path.write_text(json.dumps(ids_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[sniff:{sport_name}] Saved -> {ids_path.name}")
    return len(event_ids)


def get_sports(config, filter_ids=None):
    sports = config["bookmakers"]["winline"].get("sports", {})
    base_url = config["bookmakers"]["winline"]["sport_url"]
    return [(sid, {**sc, "url": f"{base_url}/{sid}"})
            for sid, sc in sports.items()
            if not filter_ids or sid in filter_ids]


def main():
    for old in BASE_DIR.glob("event_ids_*.json"):
        old.unlink()

    config = json.loads(CONFIG_PATH.read_text("utf-8"))

    headless = "--headed" not in sys.argv
    filter_ids = None
    if "--sport" in sys.argv:
        idx = sys.argv.index("--sport")
        if idx + 1 < len(sys.argv):
            filter_ids = set(sys.argv[idx + 1].split(","))

    sports = get_sports(config, filter_ids)
    if not sports:
        print("No sports found.")
        return

    print(f"Sniffing {len(sports)} sport(s), headless={headless}, parallel={PARALLEL_BROWSERS}")
    total_ids = 0
    with ThreadPoolExecutor(max_workers=PARALLEL_BROWSERS) as executor:
        futures = {executor.submit(sniff_sport, sid, cfg, headless): sid for sid, cfg in sports}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                total_ids += future.result()
            except Exception:
                pass  # logged inside sniff_sport

    print(f"\n[sniff] TOTAL: {total_ids} event IDs across {len(sports)} sports")


if __name__ == "__main__":
    main()
