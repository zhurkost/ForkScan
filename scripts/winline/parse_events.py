"""
Winline event detail fetcher.
Loads football event IDs, POSTs to /sb/api/by/actual, parses odds.
"""
import json
import time
import sys
from pathlib import Path
from datetime import datetime, timezone
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.team_directory import import_from_bookmaker

BASE_DIR = Path(__file__).resolve().parent
IDS_FILE = BASE_DIR / "event_ids_football.json"
OUTPUT_FILE = BASE_DIR / "events_parsed.json"

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


def main():
    ids_data = json.loads(IDS_FILE.read_text("utf-8"))
    event_ids = ids_data["event_ids"]
    print(f"Loading details for {len(event_ids)} football events...")

    session = requests.Session()
    events = []
    failed = 0

    for i, eid in enumerate(event_ids):
        data = fetch_event(session, eid)
        if not data:
            failed += 1
            if i % 30 == 0:
                print(f"  {i}/{len(event_ids)} (failed: {failed})")
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
            print(f"  {i}/{len(event_ids)} (failed: {failed})")

        time.sleep(0.12)

    result = {
        "meta": {
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "source": "winline",
            "sport": "football",
        },
        "total": len(events),
        "failed": failed,
        "events": events,
    }
    OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone: {len(events)} events parsed, {failed} failed -> {OUTPUT_FILE}")

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
                teams_list.append({"name": c["name"], "sport": "football"})
    stats = import_from_bookmaker("winline", teams_list)
    print(f"Teams: {stats['new']} new, {stats['cross_matched']} cross-matched, {stats['existing']} already known")


if __name__ == "__main__":
    main()
