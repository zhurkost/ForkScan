"""
Winline cormache binary sniffer.
Captures cormache_raw.bin for debugging/analysis only.
For production event parsing, use sniff.py (scroll) + parse_events.py (POST-per-ID).
Usage:
  python sniff_cormache.py              # all sports, headless
  python sniff_cormache.py --sport 149  # single sport
"""
import json
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parents[1]
CONFIG_PATH = ROOT / "config" / "bookmakers.json"


def is_cormache(url, content_type):
    return "/cormache/" in url.lower() and "octet-stream" in content_type.lower()


def sniff_sport(sport_id: str, sport_cfg: dict, headless: bool):
    sport_name = sport_cfg["name"]
    sport_url = sport_cfg.get("url") or f"https://winline.by/sport/{sport_id}"

    print(f"[sniff:{sport_name}] -> {sport_url}")

    cormache_bin = BASE_DIR / "cormache_raw.bin"
    captured = False

    def on_response(response):
        nonlocal captured
        if captured:
            return
        if is_cormache(response.request.url, response.headers.get("content-type", "")):
            try:
                raw = response.body()
                cormache_bin.write_bytes(raw)
                captured = True
                print(f"[sniff:{sport_name}] Cormache: {len(raw)} bytes")
            except Exception as e:
                print(f"[sniff:{sport_name}] Save failed: {e}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(locale="ru-RU", timezone_id="Europe/Minsk")
        page = ctx.new_page()
        page.on("response", on_response)

        page.goto(sport_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(10_000)

        for i in range(20):
            if captured:
                break
            time.sleep(1)

        browser.close()

    if not captured:
        print(f"[sniff:{sport_name}] No cormache captured")


def get_sports(config, filter_ids=None):
    sports = config["bookmakers"]["winline"].get("sports", {})
    base_url = config["bookmakers"]["winline"]["sport_url"]
    return [(sid, {**sc, "url": f"{base_url}/{sid}"})
            for sid, sc in sports.items()
            if not filter_ids or sid in filter_ids]


def main():
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
    for sport_id, sport_cfg in sports:
        sniff_sport(sport_id, sport_cfg, headless)


if __name__ == "__main__":
    main()
