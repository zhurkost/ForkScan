"""
Cross-bookmaker event matcher + arb finder.
Matches events by (team_id_1, team_id_2, date) and finds 1X2 arbs.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.team_directory import load_teams, _clean
from datetime import datetime, timezone

DATE_WINDOW_SECONDS = 24 * 3600  # match events within 24h


def _team_to_id(team_name: str, bk: str) -> str | None:
    teams = load_teams()
    name_clean = _clean(team_name)
    for team_id, team in teams["teams"].items():
        bk_name = team["names"].get(bk)
        if bk_name and _clean(bk_name) == name_clean:
            return team_id
    return None


def _mk_key(home_id: str | None, away_id: str | None, ts: int) -> str | None:
    if not home_id or not away_id:
        return None
    # Round to day to handle minor time differences
    day = ts // DATE_WINDOW_SECONDS
    return f"{home_id}|{away_id}|{day}"


def load_events(bk: str) -> list[dict]:
    path = ROOT / "scripts" / bk / "events_parsed.json"
    if not path.exists():
        print(f"  File not found: {path}")
        return []
    data = json.loads(path.read_text("utf-8"))
    return data.get("events", [])


def match_and_find_arbs() -> dict:
    print("Loading events...")
    wl_events = load_events("winline")
    fb_events = load_events("fonbet")
    print(f"  Winline: {len(wl_events)} events")
    print(f"  Fonbet: {len(fb_events)} events")

    # Index Fonbet events by key
    fb_index = {}
    for ev in fb_events:
        mkt = ev.get("market_1x2")
        if not mkt or len(mkt.get("outcomes", [])) < 3:
            continue
        comps = ev.get("competitors", [])
        if len(comps) < 2:
            continue
        home = comps[0]["name"]
        away = comps[1]["name"]
        home_id = _team_to_id(home, "fonbet")
        away_id = _team_to_id(away, "fonbet")
        ts = ev.get("scheduled", 0)
        if isinstance(ts, float):
            ts = int(ts)
        if ts > 1_000_000_000_000:
            ts = ts // 1000  # ms -> seconds
        key = _mk_key(home_id, away_id, ts)
        if key:
            fb_index[key] = ev

    print(f"  Fonbet indexed: {len(fb_index)} unique keys")

    # Match Winline events against Fonbet index
    arbs = []
    matched = 0
    unmatched = 0

    for ev in wl_events:
        mkt = ev.get("market_1x2")
        if not mkt or len(mkt.get("outcomes", [])) < 3:
            continue
        comps = ev.get("competitors", [])
        if len(comps) < 2:
            continue
        home = comps[0]["name"]
        away = comps[1]["name"]
        home_id = _team_to_id(home, "winline")
        away_id = _team_to_id(away, "winline")
        ts = ev.get("scheduled", 0)
        if isinstance(ts, float):
            ts = int(ts)
        if ts > 1_000_000_000_000:
            ts = ts // 1000  # ms -> seconds
        key = _mk_key(home_id, away_id, ts)
        if not key:
            unmatched += 1
            continue

        fb_ev = fb_index.get(key)
        if not fb_ev:
            unmatched += 1
            continue

        matched += 1

        # Calculate arb yield: best combo of 1X2 odds across both BKs
        # For 1X2: outcomes are [home, draw, away] in order
        wl_odds = [o["odds"] for o in mkt["outcomes"]]
        fb_odds = [o["odds"] for o in fb_ev["market_1x2"]["outcomes"]]

        if len(wl_odds) < 3 or len(fb_odds) < 3:
            continue

        max_home = max(wl_odds[0], fb_odds[0])
        max_draw = max(wl_odds[1], fb_odds[1])
        max_away = max(wl_odds[2], fb_odds[2])

        if max_home <= 0 or max_draw <= 0 or max_away <= 0:
            continue

        yield_pct = round((1 - (1 / max_home + 1 / max_draw + 1 / max_away)) * 100, 2)

        arbs.append({
            "event_key": key,
            "team_home": home,
            "team_away": away,
            "scheduled": ts,
            "winline_odds": {"1": wl_odds[0], "x": wl_odds[1], "2": wl_odds[2]},
            "fonbet_odds": {"1": fb_odds[0], "x": fb_odds[1], "2": fb_odds[2]},
            "best_odds": {"1": max_home, "x": max_draw, "2": max_away},
            "yield_pct": yield_pct,
            "best_sources": {
                "1": "winline" if wl_odds[0] >= fb_odds[0] else "fonbet",
                "x": "winline" if wl_odds[1] >= fb_odds[1] else "fonbet",
                "2": "winline" if wl_odds[2] >= fb_odds[2] else "fonbet",
            },
        })

    # Sort by yield descending (best arbs first)
    arbs.sort(key=lambda a: a["yield_pct"], reverse=True)

    result = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sources": ["winline", "fonbet"],
        },
        "stats": {
            "matched_events": matched,
            "unmatched_events": unmatched,
            "total_arbs": len(arbs),
            "positive_yield": sum(1 for a in arbs if a["yield_pct"] > 0),
        },
        "arbs": arbs,
    }

    output_path = ROOT / "data" / "arbs.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Matched: {matched}, Unmatched: {unmatched}")
    print(f"  Arbs found: {len(arbs)} ({sum(1 for a in arbs if a['yield_pct'] > 0)} with positive yield)")
    print(f"  Saved to: {output_path}")

    if arbs:
        print(f"\n  Top 10 arbs:")
        for a in arbs[:10]:
            print(f"    {a['team_home']} vs {a['team_away']} | yield={a['yield_pct']}% | "
                  f"best: {a['best_odds']['1']}/{a['best_odds']['x']}/{a['best_odds']['2']} | "
                  f"sources: {a['best_sources']['1']}/{a['best_sources']['x']}/{a['best_sources']['2']}")

    return result


if __name__ == "__main__":
    match_and_find_arbs()
