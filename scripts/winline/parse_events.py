"""
Winline event detail fetcher — v5.
Loads event IDs for a sport, POSTs to /sb/api/by/actual, parses odds.
Usage:
  python parse_events.py                      # auto-detect all event_ids_*.json files
  python parse_events.py --sport football     # parse specific sport by name
  python parse_events.py --sport 149          # parse specific sport by ID
  python parse_events.py --ids event_ids_football.json  # parse arbitrary IDs file
"""
import json
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

PARALLEL_WORKERS = 10  # change to adjust concurrency

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.team_directory import import_from_bookmaker

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "bookmakers.json"

API_URL = "https://winline.by/sb/api/by/actual"

HEADERS = {
    "accept": "application/json",
    "accept-language": "ru-RU,ru;q=0.9",
    "content-type": "application/json",
    "origin": "https://winline.by",
    "referer": "https://winline.by/sport/149",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
}


def fetch_event(session: requests.Session, event_id: int) -> dict | None:
    body = {
        "query": {"id": event_id},
        "with": ["match.venue", "markets", "competitors", "t_settings"],
        "withQuery": {},
        "lang": "ru",
        "fallbackLang": "en",
        "raw": False,
    }
    try:
        resp = session.post(API_URL, json=body, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, list):
            if not data:
                return None
            return data[0]
        return data
    except requests.RequestException:
        return None


def extract_markets(event_data: dict) -> list[dict]:
    markets = []
    for m in event_data.get("markets", []):
        outcomes = []
        for o in m.get("outcomes", []):
            if isinstance(o, dict):
                outcomes.append({
                    "id": o.get("id"),
                    "odds": o.get("odds"),
                    "type": o.get("type"),
                    "active": o.get("active"),
                })
        markets.append({
            "market_id": m.get("id"),
            "specifiers": m.get("specifiers", ""),
            "weight": m.get("weight"),
            "outcomes": outcomes,
        })
    return markets


def extract_competitors(event_data: dict) -> list[dict]:
    return [
        {
            "id": c.get("id"),
            "name": c.get("name", ""),
            "qualifier": c.get("qualifier", ""),
        }
        for c in event_data.get("competitors", [])
    ]


def load_sports_config():
    if not CONFIG_PATH.exists():
        return {}
    cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
    return cfg.get("bookmakers", {}).get("winline", {}).get("sports", {})


def resolve_sport_name(sports_config, sport_arg):
    if sport_arg.isdigit():
        sc = sports_config.get(sport_arg)
        if sc:
            return sc["name"]
    for sid, sc in sports_config.items():
        if sc["name"] == sport_arg or str(sid) == sport_arg:
            return sc["name"]
    return sport_arg


def find_ids_files(sports_config, filter_name=None):
    files = []
    for f in sorted(BASE_DIR.glob("event_ids_*.json")):
        stem = f.stem
        sport_name = stem.replace("event_ids_", "")
        if filter_name and sport_name != filter_name:
            continue
        if sports_config:
            match = any(sc["name"] == sport_name for sc in sports_config.values())
            if not match:
                continue
        files.append((f, sport_name))
    return files


def parse_sport(ids_file, sport_name, sport_id=None):
    ids_data = json.loads(ids_file.read_text("utf-8"))
    event_ids = ids_data["event_ids"]
    file_sport_id = ids_data.get("sport_id", sport_id)

    sport_label = sport_name
    if file_sport_id:
        sport_label = f"{sport_name} (id={file_sport_id})"

    print(f"Loading details for {len(event_ids)} [{sport_label}] events...")

    session = requests.Session()
    events = []
    failed = 0
    wrong_sport = 0

    for i, eid in enumerate(event_ids):
        data = fetch_event(session, eid)
        if not data:
            failed += 1
            if i % 30 == 0:
                print(f"  {i}/{len(event_ids)} (failed: {failed}, wrong sport: {wrong_sport})")
            continue

        # Check if event actually belongs to this sport
        if file_sport_id and data.get("sport_id") != file_sport_id:
            wrong_sport += 1
            if i % 30 == 0:
                print(f"  {i}/{len(event_ids)} (failed: {failed}, wrong sport: {wrong_sport})")
            continue

        competitors = extract_competitors(data)
        markets = extract_markets(data)
        m1x2 = next((m for m in markets if m["market_id"] == 1), None)

        events.append({
            "event_id": eid,
            "sport_id": data.get("sport_id"),
            "scheduled": data.get("scheduled"),
            "status": data.get("status"),
            "competitors": competitors,
            "market_1x2": m1x2,
            "all_market_ids": [m["market_id"] for m in markets],
        })

        if i % 50 == 0:
            print(f"  {i}/{len(event_ids)} (failed: {failed}, wrong sport: {wrong_sport})")

        time.sleep(0.12)

    output_file = BASE_DIR / f"events_parsed_{sport_name}.json"
    result = {
        "meta": {
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "source": "winline",
            "sport": sport_name,
            "sport_id": file_sport_id,
        },
        "total": len(events),
        "failed": failed,
        "wrong_sport": wrong_sport,
        "events": events,
    }
    output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone: {len(events)} events parsed, {failed} failed, {wrong_sport} wrong sport -> {output_file}")

    with_1x2 = sum(1 for e in events if e.get("market_1x2"))
    print(f"Events with 1X2 market: {with_1x2}")
    for e in events[:5]:
        comps = " vs ".join(c["name"] for c in e["competitors"])
        m1 = e.get("market_1x2")
        if m1:
            odds = " / ".join(str(o["odds"]) for o in m1["outcomes"])
            print(f"  {e['event_id']}: {comps} [{odds}]")

    teams_list = []
    for e in events:
        for c in e["competitors"]:
            if c["name"]:
                teams_list.append({"name": c["name"], "sport": sport_name})
    stats = import_from_bookmaker("winline", teams_list)
    print(f"Teams: {stats['new']} new, {stats['cross_matched']} cross-matched, {stats['existing']} already known")

    return len(events)


def main():
    # Clean up old parsed events — each run is fresh
    for old in BASE_DIR.glob("events_parsed_*.json"):
        old.unlink()

    sports_config = load_sports_config()

    filter_name = None

    if "--ids" in sys.argv:
        idx = sys.argv.index("--ids")
        if idx + 1 < len(sys.argv):
            ids_path = Path(sys.argv[idx + 1])
            if not ids_path.exists():
                print(f"File not found: {ids_path}")
                return
            sport_name = ids_path.stem.replace("event_ids_", "")
            parse_sport(ids_path, sport_name)
            return

    if "--sport" in sys.argv:
        idx = sys.argv.index("--sport")
        if idx + 1 < len(sys.argv):
            filter_name = resolve_sport_name(sports_config, sys.argv[idx + 1])

    files = find_ids_files(sports_config, filter_name)

    if not files:
        print("No event_ids_*.json files found to parse.")
        if filter_name:
            print(f"Check that event_ids_{filter_name}.json exists (run sniff.py first).")
        return

    print(f"Parsing {len(files)} sport(s), parallel={PARALLEL_WORKERS}")
    total_events = 0
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = {executor.submit(_parse_safe, ids_file, sport_name): sport_name
                   for ids_file, sport_name in files}
        for future in as_completed(futures):
            try:
                total_events += future.result()
            except Exception:
                pass

    print(f"\n[parser] TOTAL: {total_events} events across {len(files)} sports")


def _parse_safe(ids_file, sport_name):
    try:
        return parse_sport(ids_file, sport_name)
    except Exception as e:
        print(f"  [{sport_name}] FAILED: {e}")
        return 0


if __name__ == "__main__":
    main()
