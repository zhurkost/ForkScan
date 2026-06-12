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
from playwright.sync_api import sync_playwright

SCROLL_PASSES = 40        # max scroll iterations
SCROLL_SLEEP = 1.0      # seconds between scrolls
SCROLL_STUCK_LIMIT = 6   # consecutive no-new-requests before giving up

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

        # Scroll to trigger lazy-load — go to absolute bottom each time
        print(f"[sniff:{sport_name}] Scrolling...")
        last_height = 0
        stuck = 0
        for i in range(SCROLL_PASSES):
            height_before = page.evaluate("document.body.scrollHeight")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(SCROLL_SLEEP)
            height_after = page.evaluate("document.body.scrollHeight")

            if height_after == last_height:
                stuck += 1
                if stuck >= SCROLL_STUCK_LIMIT:
                    break
            else:
                stuck = 0
            last_height = height_after

            if i % 10 == 0:
                print(f"  scroll {i+1}/{SCROLL_PASSES}, height={height_after}, "
                      f"requests={len(log['requests'])}")

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

    print(f"Sniffing {len(sports)} sport(s), headless={headless}")
    total_ids = 0
    for sid, cfg in sports:
        total_ids += sniff_sport(sid, cfg, headless)

    print(f"\n[sniff] TOTAL: {total_ids} event IDs across {len(sports)} sports")


if __name__ == "__main__":
    main()
