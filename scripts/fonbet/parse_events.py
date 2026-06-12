"""
Fonbet event parser — v2.
Fetches /events/listBase and extracts events with 1X2 odds for ALL sports.
Usage:
  python parse_events.py                # all sports
  python parse_events.py --sport football  # specific sport (English slug)
  python parse_events.py --sport 1       # specific sport (Fonbet sport ID)
"""
import json
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.team_directory import import_from_bookmaker

BASE_DIR = Path(__file__).resolve().parent

LINE_DOMAINS = [
    "https://line11.by0e87-resources.by",
    "https://line12.by0e87-resources.by",
    "https://line21.by0e87-resources.by",
    "https://line22.by0e87-resources.by",
]

LIST_BASE_PATH = "/events/listBase"
FACTOR_1 = 921
FACTOR_X = 922
FACTOR_2 = 923

HEADERS = {
    "accept": "application/json",
    "accept-language": "ru-RU,ru;q=0.9",
    "origin": "https://fonbet.by",
    "referer": "https://fonbet.by/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


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


def fetch_sport_names_en(domain: str) -> dict[int, str]:
    url = f"{domain}{LIST_BASE_PATH}?lang=en&scopeMarket=700"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            return {s["id"]: s.get("name", "") for s in data.get("sports", []) if s.get("kind") == "sport"}
    except Exception:
        pass
    return {}


def build_sport_tree(sports: list[dict]) -> dict[int, int]:
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


def parse_events(data: dict, sport_filter: str | None = None) -> dict:
    sports = data.get("sports", [])
    events = data.get("events", [])
    custom_factors = {cf["e"]: cf for cf in data.get("customFactors", [])}

    domain = "https://line11.by0e87-resources.by"
    names_en = fetch_sport_names_en(domain)

    segment_to_root = build_sport_tree(sports)

    root_names_ru = {}
    for s in sports:
        if s.get("kind") == "sport":
            root_names_ru[s["id"]] = s.get("name", "")

    sport_buckets = {}

    for event in events:
        if event.get("level") != 1:
            continue
        segment_id = event.get("sportId", 0)
        root_sport = segment_to_root.get(segment_id)
        if not root_sport:
            continue

        sport_slug = slugify(names_en.get(root_sport, f"sport-{root_sport}"))

        if sport_filter and sport_filter != sport_slug and sport_filter != str(root_sport):
            continue

        team1 = (event.get("team1") or "").strip()
        team2 = (event.get("team2") or "").strip()
        if not team1 or not team2:
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

        if len(odds_1x2) < 2:
            continue

        # Build outcomes: always 3 slots, draw = None if missing
        outcomes = [
            {"id": FACTOR_1, "odds": odds_1x2.get("home")},
            {"id": FACTOR_X, "odds": odds_1x2.get("draw")},
            {"id": FACTOR_2, "odds": odds_1x2.get("away")},
        ]
        # Filter out None odds (sports without draws like darts, tennis)
        outcomes = [o for o in outcomes if o["odds"] is not None]

        parsed = {
            "event_id": event["id"],
            "sport_id": root_sport,
            "segment_id": segment_id,
            "scheduled": event.get("startTime", 0),
            "competitors": [
                {"name": team1, "qualifier": "home"},
                {"name": team2, "qualifier": "away"},
            ],
            "market_1x2": {
                "market_id": 1,
                "outcomes": outcomes,
            },
        }

        sport_buckets.setdefault(sport_slug, []).append(parsed)

    return {
        "buckets": sport_buckets,
        "names_ru": root_names_ru,
    }


def save_sport(sport_slug: str, events: list[dict], name_ru: str, sport_id: int) -> str:
    output = BASE_DIR / f"events_parsed_{sport_slug}.json"
    result = {
        "meta": {
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "source": "fonbet",
            "sport": sport_slug,
            "sport_id": sport_id,
            "sport_name_ru": name_ru,
        },
        "total": len(events),
        "events": events,
    }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(output)


def main():
    # Clean up old parsed events — each run is fresh
    for old in BASE_DIR.glob("events_parsed_*.json"):
        old.unlink()

    sport_filter = None
    if "--sport" in sys.argv:
        idx = sys.argv.index("--sport")
        if idx + 1 < len(sys.argv):
            sport_filter = sys.argv[idx + 1]

    print("Fetching Fonbet events...")

    data = None
    for domain in LINE_DOMAINS:
        data = fetch_list_base(domain)
        if data:
            total_events = len(data.get("events", []))
            print(f"  {domain}: {total_events} total events")
            break
    else:
        print("All domains failed!")
        return

    result = parse_events(data, sport_filter)
    buckets = result["buckets"]
    names_ru = result["names_ru"]

    if not buckets:
        print("No events found matching filter.")
        return

    total_parsed = sum(len(evs) for evs in buckets.values())
    print(f"\nParsed {total_parsed} events across {len(buckets)} sports:\n")

    for sport_slug in sorted(buckets.keys()):
        events = buckets[sport_slug]
        root_id = events[0]["sport_id"] if events else "?"
        name_ru = names_ru.get(int(root_id), sport_slug) if isinstance(root_id, int) else name_ru
        path = save_sport(sport_slug, events, name_ru, int(root_id) if isinstance(root_id, int) else 0)
        print(f"  {sport_slug:25s} ({name_ru:20s}) -> {len(events):4d} events  {path}")

        if sport_slug == "football" or len(buckets) <= 3:
            for e in events[:3]:
                teams = " vs ".join(c["name"] for c in e["competitors"])
                odds = e["market_1x2"]["outcomes"]
                o_str = " / ".join(str(o["odds"]) for o in odds)
                print(f"      {e['event_id']}: {teams} [{o_str}]")

    for sport_slug, events in buckets.items():
        teams_list = []
        for e in events:
            for c in e["competitors"]:
                if c["name"]:
                    teams_list.append({"name": c["name"], "sport": sport_slug})
        stats = import_from_bookmaker("fonbet", teams_list)
        print(f"  Teams ({sport_slug}): {stats['new']} new, {stats['cross_matched']} cross-matched, {stats['existing']} already known")


if __name__ == "__main__":
    main()
