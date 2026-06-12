"""
Fonbet event parser.
Fetches /events/listBase and extracts football events with 1X2 odds.
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
OUTPUT_FILE = BASE_DIR / "events_parsed.json"

LINE_DOMAINS = [
    "https://line11.by0e87-resources.by",
    "https://line12.by0e87-resources.by",
    "https://line21.by0e87-resources.by",
    "https://line22.by0e87-resources.by",
]

LIST_BASE_PATH = "/events/listBase"
FOOTBALL_SPORT_ID = 1
FACTOR_1 = 921
FACTOR_X = 922
FACTOR_2 = 923

HEADERS = {
    "accept": "application/json",
    "accept-language": "ru-RU,ru;q=0.9",
    "origin": "https://fonbet.by",
    "referer": "https://fonbet.by/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/148.0.0.0 Safari/537.36",
}


def fetch_list_base(domain: str) -> dict | None:
    url = f"{domain}{LIST_BASE_PATH}?lang=ru&scopeMarket=700"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        print(f"  [{domain}] HTTP {resp.status_code}")
        return None
    except requests.RequestException as e:
        print(f"  [{domain}] Error: {e}")
        return None


def build_sport_tree(sports: list[dict]) -> dict[int, int]:
    tree = {}
    segment_to_root = {}
    for s in sports:
        if s.get("kind") == "sport":
            segment_to_root[s["id"]] = s["id"]
        elif s.get("kind") == "segment":
            parent = s.get("parentId")
            if parent and parent in segment_to_root:
                segment_to_root[s["id"]] = segment_to_root[parent]
            else:
                segment_to_root[s["id"]] = parent
    return segment_to_root


def parse_events(data: dict) -> list[dict]:
    sports = data.get("sports", [])
    events = data.get("events", [])
    custom_factors = {cf["e"]: cf for cf in data.get("customFactors", [])}

    segment_to_root = build_sport_tree(sports)

    result = []
    for event in events:
        if event.get("level") != 1:
            continue

        team1 = (event.get("team1") or "").strip()
        team2 = (event.get("team2") or "").strip()
        if not team1 or not team2:
            continue
        segment_id = event.get("sportId", 0)
        root_sport = segment_to_root.get(segment_id)
        if root_sport != FOOTBALL_SPORT_ID:
            continue

        cf = custom_factors.get(event["id"])
        if not cf:
            continue

        odds_1x2 = {}
        for f in cf.get("factors", []):
            fid = f["f"]
            if fid == FACTOR_1:
                odds_1x2["home"] = f["v"]
            elif fid == FACTOR_X:
                odds_1x2["draw"] = f["v"]
            elif fid == FACTOR_2:
                odds_1x2["away"] = f["v"]

        if len(odds_1x2) < 3:
            continue

        result.append({
            "event_id": event["id"],
            "sport_id": FOOTBALL_SPORT_ID,
            "segment_id": segment_id,
            "scheduled": event.get("startTime", 0),
            "competitors": [
                {"name": event.get("team1", ""), "qualifier": "home"},
                {"name": event.get("team2", ""), "qualifier": "away"},
            ],
            "market_1x2": {
                "market_id": 1,
                "outcomes": [
                    {"id": FACTOR_1, "odds": odds_1x2["home"]},
                    {"id": FACTOR_X, "odds": odds_1x2["draw"]},
                    {"id": FACTOR_2, "odds": odds_1x2["away"]},
                ],
            },
        })

    return result


def main():
    print("Fetching Fonbet events...")

    for domain in LINE_DOMAINS:
        data = fetch_list_base(domain)
        if data:
            events = parse_events(data)
            total_events = len(data.get("events", []))
            print(f"  {domain}: {total_events} total events, {len(events)} football with 1X2")
            break
    else:
        print("All domains failed!")
        return

    result = {
        "meta": {
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "source": "fonbet",
            "sport": "football",
        },
        "total": len(events),
        "events": events,
    }
    OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone: {len(events)} football events -> {OUTPUT_FILE}")

    for e in events[:5]:
        teams = " vs ".join(c["name"] for c in e["competitors"])
        odds = e["market_1x2"]["outcomes"]
        o_str = " / ".join(str(o["odds"]) for o in odds)
        print(f"  {e['event_id']}: {teams} [{o_str}]")

    teams_list = []
    for e in events:
        for c in e["competitors"]:
            if c["name"]:
                teams_list.append({"name": c["name"], "sport": "football"})
    stats = import_from_bookmaker("fonbet", teams_list)
    print(f"Teams: {stats['new']} new, {stats['cross_matched']} cross-matched, {stats['existing']} already known")


if __name__ == "__main__":
    main()
