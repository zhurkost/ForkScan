"""
Cross-bookmaker event matcher + arb finder — v4.
Matches events by (sport, team_id_1, team_id_2, date_day) and finds 1X2 arbs.
Team cross-referencing happens during parsing (parse_events.py → teams.json).
Usage:
  python -m core.arb_finder              # all sports
  python -m core.arb_finder --sport football
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.team_directory import load_teams, _clean

DATE_WINDOW_SECONDS = 24 * 3600

# ---- sport mapping (Winline name → Fonbet name) -----------------------------

def _build_sport_map() -> dict[str, str | None]:
    cfg_path = ROOT / "config" / "bookmakers.json"
    if not cfg_path.exists():
        return {}
    cfg = json.loads(cfg_path.read_text("utf-8"))
    wl_sports = cfg["bookmakers"]["winline"].get("sports", {})
    fb_sports = cfg["bookmakers"]["fonbet"].get("sports", {})

    fb_by_name = {sc["name"]: sc["name"] for sc in fb_sports.values()}
    fb_by_ru = {sc["name_ru"]: sc["name"] for sc in fb_sports.values()}

    mapping = {}
    for sc in wl_sports.values():
        wl_name = sc["name"]
        wl_ru = sc["name_ru"]
        if wl_name in fb_by_name:
            mapping[wl_name] = wl_name
        elif wl_ru in fb_by_ru:
            mapping[wl_name] = fb_by_ru[wl_ru]
        else:
            for fb_name in fb_by_name:
                if wl_name in fb_name or fb_name in wl_name:
                    mapping[wl_name] = fb_name
                    break
            else:
                mapping[wl_name] = None
    return mapping

SPORT_MAP = _build_sport_map()

# Explicit overrides for name mismatches (auto-detection can't resolve)
SPORT_OVERRIDES = {
    "mma": "mix-fights",           # Winline "ММА" vs Fonbet "Единоборства"
}
for k, v in SPORT_OVERRIDES.items():
    SPORT_MAP[k] = v


# ---- event loading ----------------------------------------------------------

def _find_event_files(bk: str, sport_filter: str | None = None) -> list[Path]:
    base = ROOT / "scripts" / bk
    files = sorted(base.glob("events_parsed_*.json"))
    if sport_filter:
        files = [f for f in files if f.stem == f"events_parsed_{sport_filter}"]
    if not files:
        old = base / "events_parsed.json"
        if old.exists():
            files = [old]
    return files


def load_events(bk: str, sport_filter: str | None = None) -> list[dict]:
    events = []
    for path in _find_event_files(bk, sport_filter):
        if not path.exists():
            print(f"  File not found: {path}")
            continue
        raw = path.read_bytes()
        text = None
        for encoding in ("utf-8-sig", "utf-8", "utf-16"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            print(f"  Failed to decode: {path}")
            continue
        for i, ch in enumerate(text):
            if ch in "{[0123456789\"":
                if i > 0:
                    text = text[i:]
                break
        if not text.strip():
            print(f"  Empty file: {path}")
            continue
        data = json.loads(text)
        file_sport = data.get("meta", {}).get("sport")
        for ev in data.get("events", []):
            ev["_file_sport"] = file_sport
            events.append(ev)
    return events


# ---- key construction -------------------------------------------------------

def _team_to_id(team_name: str, bk: str) -> str | None:
    return _team_lookup.get((bk, _clean(team_name)))


def _build_team_lookup() -> dict:
    """Build lookup: (bookmaker, cleaned_name) -> team_id. Cached, not reloaded per call."""
    teams = load_teams()
    lookup = {}
    for team_id, team in teams["teams"].items():
        for bk in ["winline", "fonbet"]:
            bk_name = team["names"].get(bk)
            if bk_name:
                lookup[(bk, _clean(bk_name))] = team_id
    return lookup


_team_lookup = _build_team_lookup()


def _mk_key(home_id: str | None, away_id: str | None, ts: int, sport: str) -> str | None:
    if not home_id or not away_id:
        return None
    day = ts // DATE_WINDOW_SECONDS
    return f"{sport}|{home_id}|{away_id}|{day}"


def _normalize_ts(ts) -> int:
    if isinstance(ts, float):
        ts = int(ts)
    if ts > 1_000_000_000_000:
        ts = ts // 1000
    return ts


def _get_competitors(ev: dict) -> tuple[str, str] | None:
    comps = ev.get("competitors", [])
    if len(comps) < 2:
        return None
    return comps[0]["name"], comps[1]["name"]


# ---- main matching logic ----------------------------------------------------

def match_and_find_arbs(sport_filter: str | None = None):
    # Clean up old arb outputs
    for old in (ROOT / "data").glob("arbs*.json"):
        old.unlink()

    print("Loading events...")
    wl_events = load_events("winline", sport_filter)
    fb_events = load_events("fonbet", sport_filter)
    print(f"  Winline: {len(wl_events)} events")
    print(f"  Fonbet: {len(fb_events)} events")

    if sport_filter:
        fb_sport = SPORT_MAP.get(sport_filter)
        print(f"  Sport mapping: Winline '{sport_filter}' -> Fonbet '{fb_sport}'")
    else:
        print(f"  Sport map: {len(SPORT_MAP)} sports mapped between bookmakers")

    # ---- Index Fonbet events ----

    fb_index = {}
    fb_skipped_no_market = 0
    fb_skipped_no_comps = 0
    fb_skipped_no_team = 0

    for ev in fb_events:
        mkt = ev.get("market_1x2")
        if not mkt or len(mkt.get("outcomes", [])) < 2:
            fb_skipped_no_market += 1
            continue
        comps = _get_competitors(ev)
        if not comps:
            fb_skipped_no_comps += 1
            continue
        home, away = comps
        ts = _normalize_ts(ev.get("scheduled", 0))
        sport = ev.get("_file_sport", "")

        # Sport filter for Fonbet
        if sport_filter:
            fb_sport_name = SPORT_MAP.get(sport_filter)
            if fb_sport_name and sport and sport != fb_sport_name:
                continue

        home_id = _team_to_id(home, "fonbet")
        away_id = _team_to_id(away, "fonbet")
        key = _mk_key(home_id, away_id, ts, sport)
        if key:
            fb_index[key] = ev
        else:
            fb_skipped_no_team += 1

    print(f"  Fonbet indexed: {len(fb_index)} events "
          f"[skipped: {fb_skipped_no_market} no market, "
          f"{fb_skipped_no_comps} no comps, {fb_skipped_no_team} no team ID]")

    # ---- Match Winline events ----

    arbs = []
    matched = 0
    unmatched_no_team = 0
    unmatched_not_in_fb = 0
    wl_skipped_no_market = 0
    wl_skipped_no_comps = 0

    for ev in wl_events:
        mkt = ev.get("market_1x2")
        if not mkt or len(mkt.get("outcomes", [])) < 2:
            wl_skipped_no_market += 1
            continue
        comps = _get_competitors(ev)
        if not comps:
            wl_skipped_no_comps += 1
            continue
        home, away = comps
        ts = _normalize_ts(ev.get("scheduled", 0))
        sport = ev.get("_file_sport", "")

        home_id = _team_to_id(home, "winline")
        away_id = _team_to_id(away, "winline")
        key = _mk_key(home_id, away_id, ts, sport)

        if not key:
            unmatched_no_team += 1
            continue

        fb_ev = fb_index.get(key)
        if not fb_ev:
            unmatched_not_in_fb += 1
            continue

        matched += 1

        wl_odds = [o["odds"] for o in mkt["outcomes"]]
        fb_odds = [o["odds"] for o in fb_ev["market_1x2"]["outcomes"]]

        # Match requires same number of outcomes (both 2-outcome or both 3-outcome)
        if len(wl_odds) != len(fb_odds):
            continue

        is_1x2 = len(wl_odds) == 3

        max_home = max(wl_odds[0], fb_odds[0])
        max_away = max(wl_odds[-1], fb_odds[-1])

        if is_1x2:
            max_draw = max(wl_odds[1], fb_odds[1])
            if max_home <= 0 or max_draw <= 0 or max_away <= 0:
                continue
            yield_pct = round((1 - (1 / max_home + 1 / max_draw + 1 / max_away)) * 100, 2)
            wl_draw = wl_odds[1]
            fb_draw = fb_odds[1]
            draw_best = "winline" if wl_draw >= fb_draw else "fonbet"
        else:
            if max_home <= 0 or max_away <= 0:
                continue
            yield_pct = round((1 - (1 / max_home + 1 / max_away)) * 100, 2)
            max_draw = None
            wl_draw = None
            fb_draw = None
            draw_best = None

        arbs.append({
            "event_key": key,
            "sport": sport,
            "team_home": home,
            "team_away": away,
            "scheduled": ts,
            "market_type": "1X2" if is_1x2 else "12",
            "winline_odds": {"1": wl_odds[0], "x": wl_draw, "2": wl_odds[-1]},
            "fonbet_odds": {"1": fb_odds[0], "x": fb_draw, "2": fb_odds[-1]},
            "best_odds": {"1": max_home, "x": max_draw, "2": max_away},
            "yield_pct": yield_pct,
            "best_sources": {
                "1": "winline" if wl_odds[0] >= fb_odds[0] else "fonbet",
                "x": draw_best,
                "2": "winline" if wl_odds[-1] >= fb_odds[-1] else "fonbet",
            },
        })

    arbs.sort(key=lambda a: a["yield_pct"], reverse=True)

    # ---- per-sport breakdown ----
    wl_by_sport = defaultdict(lambda: {"total": 0, "with_market": 0, "matched": 0})
    for ev in wl_events:
        sport = ev.get("_file_sport", "?")
        mkt = ev.get("market_1x2")
        has_market = mkt and len(mkt.get("outcomes", [])) >= 2
        wl_by_sport[sport]["total"] += 1
        if has_market:
            wl_by_sport[sport]["with_market"] += 1
    for a in arbs:
        wl_by_sport[a["sport"]]["matched"] += 1

    print(f"\n  Per-sport breakdown:")
    for sport in sorted(wl_by_sport):
        s = wl_by_sport[sport]
        rate = round(s["matched"] / max(s["with_market"], 1) * 100)
        print(f"    {sport:25s}  total={s['total']:>4d}  market={s['with_market']:>4d}  matched={s['matched']:>4d}  rate={rate}%")

    # ---- output ----

    suffix = f"_{sport_filter}" if sport_filter else ""
    output_path = ROOT / "data" / f"arbs{suffix}.json"

    total_wl = matched + unmatched_no_team + unmatched_not_in_fb + wl_skipped_no_market + wl_skipped_no_comps

    result = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sources": ["winline", "fonbet"],
            "sport": sport_filter,
        },
        "stats": {
            "winline_total": total_wl,
            "winline_skipped_no_market": wl_skipped_no_market,
            "winline_skipped_no_comps": wl_skipped_no_comps,
            "fonbet_indexed": len(fb_index),
            "fonbet_skipped_no_team": fb_skipped_no_team,
            "matched_events": matched,
            "unmatched_no_team": unmatched_no_team,
            "unmatched_not_in_fb": unmatched_not_in_fb,
            "total_arbs": len(arbs),
            "positive_yield": sum(1 for a in arbs if a["yield_pct"] > 0),
        },
        "arbs": arbs,
    }

    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  Results:")
    print(f"  Winline: {total_wl} total ({wl_skipped_no_market} no 1X2 market, {wl_skipped_no_comps} no competitors)")
    print(f"  Fonbet indexed: {len(fb_index)} ({fb_skipped_no_team} missing team IDs)")
    print(f"  Matched: {matched} | No team ID: {unmatched_no_team} | Not in FB: {unmatched_not_in_fb}")
    print(f"  match rate: {matched}/{matched + unmatched_not_in_fb} = {round(matched/(matched+unmatched_not_in_fb)*100) if (matched+unmatched_not_in_fb) else 0}%")
    print(f"  Arbs: {len(arbs)} ({sum(1 for a in arbs if a['yield_pct'] > 0)} positive yield)")
    print(f"  Saved to: {output_path}")

    if arbs:
        print(f"\n  Top 10 arbs:")
        for a in arbs[:10]:
            from datetime import datetime as dt_d, timedelta
            local_tz = timezone(timedelta(hours=3))
            date_str = dt_d.fromtimestamp(a['scheduled'], tz=local_tz).strftime('%d%m%y %H:%M')
            src = a['best_sources']
            odds = a['best_odds']
            if odds['x'] is not None:
                kefs = f"{odds['1']}({src['1'][:2]})/{odds['x']}({src['x'][:2]})/{odds['2']}({src['2'][:2]})"
            else:
                kefs = f"{odds['1']}({src['1'][:2]})/{odds['2']}({src['2'][:2]})"
            print(f"    [{a['sport']}] {date_str} {a['team_home']} vs {a['team_away']} | {a['yield_pct']:+.1f}% | {kefs}")

    return result


if __name__ == "__main__":
    sport = None
    if "--sport" in sys.argv:
        idx = sys.argv.index("--sport")
        if idx + 1 < len(sys.argv):
            sport = sys.argv[idx + 1]
    match_and_find_arbs(sport)
