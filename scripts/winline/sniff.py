"""
Winline API sniffer — v3.
Aggressive scroll to capture all event POSTs + raw cormache binary.
"""
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "bookmakers.json"
OUTPUT_PATH = Path(__file__).resolve().parent / "network_log.json"
COR_CACHE_PATH = Path(__file__).resolve().parent / "cormache_raw.bin"

INTERESTING = ["/sb/", "/cormache", "/api/init"]
INTERESTING_MIME = {"application/json", "application/octet-stream", "text/plain"}


def is_interesting(url, content_type):
    url_l = url.lower()
    if any(k in url_l for k in INTERESTING):
        return True
    if any(m in content_type.lower() for m in INTERESTING_MIME):
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


def sniff():
    config = json.loads(CONFIG_PATH.read_text("utf-8"))
    sport_url = config["bookmakers"]["winline"]["sport_url"]

    log = {
        "meta": {"url": sport_url, "ts": datetime.now(timezone.utc).isoformat()},
        "requests": [],
        "responses": [],
    }

    def on_request(request):
        if not is_interesting(request.url, ""):
            return
        log["requests"].append({
            "url": request.url,
            "method": request.method,
            "headers": dict(request.headers),
            "post_data": request.post_data,
            "resource_type": request.resource_type,
        })

    def on_response(response):
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

        if "/cormache/" in url and body and body.startswith("<binary"):
            raw = response.body()
            COR_CACHE_PATH.write_bytes(raw)
            entry["_cormache_saved"] = str(COR_CACHE_PATH)

        log["responses"].append(entry)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(locale="ru-RU", timezone_id="Europe/Minsk")
        page = ctx.new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        print(f"[sniff] Navigating to {sport_url}")
        page.goto(sport_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(10_000)

        print("[sniff] Endless scroll to load all events (max 60s)...")
        last_request_count = 0
        stuck = 0
        for i in range(40):
            page.evaluate("window.scrollBy(0, 1200)")
            time.sleep(1.5)
            current = len(log["requests"])
            if current == last_request_count:
                stuck += 1
                if stuck >= 6:
                    print(f"  no new requests after {i+1} scrolls, stopping")
                    break
            else:
                stuck = 0
            last_request_count = current
            if i % 5 == 0:
                print(f"  scroll {i+1}/40, requests so far: {current}")

        page.wait_for_timeout(10_000)

        browser.close()

    OUTPUT_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    sb = [r for r in log["requests"] if "/sb/" in r["url"]]
    n_actual = sum(1 for r in sb if "by/actual" in r["url"])
    print(f"[sniff] Done: {n_actual} /sb/api/by/actual requests, total {len(sb)}")
    if COR_CACHE_PATH.exists():
        print(f"[sniff] Cormache saved: {COR_CACHE_PATH.stat().st_size} bytes")

    event_ids = set()
    for r in log["requests"]:
        if r.get("post_data") and "by/actual" in r["url"]:
            try:
                eid = json.loads(r["post_data"])["query"]["id"]
                event_ids.add(eid)
            except Exception:
                pass
    print(f"[sniff] Unique event IDs captured: {len(event_ids)}")
    for eid in sorted(event_ids)[:20]:
        print(f"  {eid}")


if __name__ == "__main__":
    sniff()
