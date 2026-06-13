"""
Event-based team cross-referencer — v2.
Groups events by (sport, timestamp), matches Winline<->Fonbet.
Full matches -> auto cross-link teams. Unmatched -> partial_matches.json (manual review).
Usage:
  python -m core.event_matcher               # all sports
  python -m core.event_matcher --sport football
"""
import json
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.team_directory import load_teams, save_teams, _clean

DATE_WINDOW_SECONDS = 24 * 3600


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
            continue
        for i, ch in enumerate(text):
            if ch in "{[0123456789\"":
                if i > 0:
                    text = text[i:]
                break
        if not text.strip():
            continue
        data = json.loads(text)
        file_sport = data.get("meta", {}).get("sport")
        for ev in data.get("events", []):
            ev["_file_sport"] = file_sport
            events.append(ev)
    return events


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


# ---- team name matching ----------------------------------------------------

def _names_match(a: str, b: str) -> bool:
    return _clean(a) == _clean(b)


def _partial_match(a: str, b: str) -> bool:
    """Substring or word-subset match (>=2 words in shorter)."""
    ca = _clean(a)
    cb = _clean(b)
    if ca in cb or cb in ca:
        return True
    words_a = set(ca.split())
    words_b = set(cb.split())
    shorter = min(words_a, words_b, key=len)
    if len(shorter) >= 2:
        return shorter.issubset(words_a if shorter is words_b else words_b)
    return False


def _match_type(wl_home: str, wl_away: str, fb_home: str, fb_away: str) -> str:
    """Returns 'full' if both teams match, 'partial' if exactly one, 'none' if neither."""
    hm = _names_match(wl_home, fb_home)
    am = _names_match(wl_away, fb_away)
    if hm and am:
        return "full"
    if hm != am:
        return "partial"
    return "none"


# ---- main matching ---------------------------------------------------------

def match_and_crosslink(sport_filter: str | None = None):
    # Clean up old outputs — each run is fresh
    for old in [ROOT / "data" / "event_matches.json", ROOT / "data" / "event_partial_matches.json"]:
        if old.exists():
            old.unlink()

    print("Loading events...")
    wl_events = load_events("winline", sport_filter)
    fb_events = load_events("fonbet", sport_filter)
    print(f"  Winline: {len(wl_events)} events")
    print(f"  Fonbet: {len(fb_events)} events")

    teams = load_teams()

    now_ts = int(time.time())

    # ---- Group events by (sport, timestamp), skip past ----
    groups = defaultdict(lambda: {"wl": [], "fb": []})
    wl_skipped_past = 0
    fb_skipped_past = 0

    for ev in wl_events:
        comps = _get_competitors(ev)
        if not comps:
            continue
        sport = ev.get("_file_sport", "")
        ts = _normalize_ts(ev.get("scheduled", 0))
        if ts < now_ts:
            wl_skipped_past += 1
            continue
        groups[(sport, ts)]["wl"].append({
            "event_id": ev.get("event_id"),
            "home": comps[0],
            "away": comps[1],
        })
    for ev in fb_events:
        comps = _get_competitors(ev)
        if not comps:
            continue
        sport = ev.get("_file_sport", "")
        ts = _normalize_ts(ev.get("scheduled", 0))
        if ts < now_ts:
            fb_skipped_past += 1
            continue
        groups[(sport, ts)]["fb"].append({
            "event_id": ev.get("event_id"),
            "home": comps[0],
            "away": comps[1],
        })

    print(f"  Skipped past events: WL={wl_skipped_past} FB={fb_skipped_past}")
    print(f"  Time groups: {len(groups)}")

    full_matches = []
    auto_resolved = 0
    partial_matches = []

    for (sport, ts), group in groups.items():
        wl_list = group["wl"]
        fb_list = group["fb"]

        # Track which events are already matched (to exclude from partials)
        wl_matched = set()
        fb_matched = set()

        # --- Phase 1: full matches (both teams exact match) ---
        for wi, wl_ev in enumerate(wl_list):
            if wi in wl_matched:
                continue
            for fi, fb_ev in enumerate(fb_list):
                if fi in fb_matched:
                    continue
                if _match_type(wl_ev["home"], wl_ev["away"],
                               fb_ev["home"], fb_ev["away"]) == "full":
                    full_matches.append({
                        "sport": sport, "scheduled": ts,
                        "wl_event_id": wl_ev["event_id"],
                        "fb_event_id": fb_ev["event_id"],
                        "teams": [
                            {"wl": wl_ev["home"], "fb": fb_ev["home"]},
                            {"wl": wl_ev["away"], "fb": fb_ev["away"]},
                        ],
                    })
                    for pair in [{"wl": wl_ev["home"], "fb": fb_ev["home"]},
                                 {"wl": wl_ev["away"], "fb": fb_ev["away"]}]:
                        _crosslink_teams(teams, pair["wl"], pair["fb"], sport)
                    wl_matched.add(wi)
                    fb_matched.add(fi)
                    break

        # --- Phase 2: auto-resolve (1 team exact, other is substring/word-subset) ---
        for wi, wl_ev in enumerate(wl_list):
            if wi in wl_matched:
                continue
            for fi, fb_ev in enumerate(fb_list):
                if fi in fb_matched:
                    continue
                hm = _names_match(wl_ev["home"], fb_ev["home"])
                am = _names_match(wl_ev["away"], fb_ev["away"])
                if hm != am:  # exactly one matches
                    if (not hm and _partial_match(wl_ev["home"], fb_ev["home"])) or \
                       (not am and _partial_match(wl_ev["away"], fb_ev["away"])):
                        auto_resolved += 1
                        for pair in [{"wl": wl_ev["home"], "fb": fb_ev["home"]},
                                     {"wl": wl_ev["away"], "fb": fb_ev["away"]}]:
                            _crosslink_teams(teams, pair["wl"], pair["fb"], sport)
                        wl_matched.add(wi)
                        fb_matched.add(fi)
                        break

        # --- Phase 3: partials (all remaining unmatched pairs, full combinatorics) ---
        unmatched_wl = [w for i, w in enumerate(wl_list) if i not in wl_matched]
        unmatched_fb = [f for i, f in enumerate(fb_list) if i not in fb_matched]

        for wl_ev in unmatched_wl:
            for fb_ev in unmatched_fb:
                hm = _names_match(wl_ev["home"], fb_ev["home"])
                am = _names_match(wl_ev["away"], fb_ev["away"])
                partial_matches.append({
                    "result": "",
                    "sport": sport,
                    "scheduled": ts,
                    "wl_event_id": wl_ev["event_id"],
                    "fb_event_id": fb_ev["event_id"],
                    "home_match": hm,
                    "away_match": am,
                    "wl_home": wl_ev["home"],
                    "wl_away": wl_ev["away"],
                    "fb_home": fb_ev["home"],
                    "fb_away": fb_ev["away"],
                })

    # Save updated teams.json
    save_teams(teams)

    # ---- output ----
    matches_path = ROOT / "data" / "event_matches.json"
    partial_path = ROOT / "data" / "event_partial_matches.json"

    matches_result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "full_matches": len(full_matches),
            "auto_resolved": auto_resolved,
            "partial_matches": len(partial_matches),
            "teams_total": len(teams["teams"]),
            "teams_crosslinked": sum(
                1 for t in teams["teams"].values()
                if t["names"].get("winline") and t["names"].get("fonbet")
            ),
        },
        "matches": full_matches,
    }
    matches_path.write_text(json.dumps(matches_result, ensure_ascii=False, indent=2), encoding="utf-8")

    partial_result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "Set 'result' to true for pairs that are the same event (different team names). "
                "Then run: python -m core.apply_manual_links",
        "count": len(partial_matches),
        "partials": partial_matches,
    }
    partial_path.write_text(json.dumps(partial_result, ensure_ascii=False, indent=2), encoding="utf-8")

    # Print
    crosslinked = matches_result["stats"]["teams_crosslinked"]
    total = matches_result["stats"]["teams_total"]
    full_comb_groups = sum(1 for (s, ts), g in groups.items()
                           if (len(g["wl"]) + len(g["fb"])) >= 10)
    print(f"\n  Full matches:     {len(full_matches)} events -> teams cross-linked")
    print(f"  Auto-resolved:    {auto_resolved} partials resolved automatically")
    print(f"  Partial matches:  {len(partial_matches)} entries -> manual review")
    print(f"  Teams: {crosslinked}/{total} cross-linked "
          f"({round(crosslinked / max(total, 1) * 100, 1)}%)")
    print(f"  Saved: {matches_path}")
    print(f"  Saved: {partial_path}")

    if partial_matches:
        print(f"\n  First 5 partials for review:")
        for pm in partial_matches[:5]:
            print(f"    [{pm['sport']}] {pm['wl_home']} vs {pm['wl_away']}  <->  "
                  f"{pm['fb_home']} vs {pm['fb_away']}")
            print(f"      home={pm['home_match']} away={pm['away_match']}")


# ---- team cross-linking -----------------------------------------------------

def _crosslink_teams(teams: dict, wl_name: str, fb_name: str, sport: str) -> None:
    name_to_id = {}
    for team_id, team in teams["teams"].items():
        for bk, nm in team["names"].items():
            if nm:
                key = (bk, _clean(nm))
                if key not in name_to_id:
                    name_to_id[key] = team_id

    wl_clean = _clean(wl_name)
    fb_clean = _clean(fb_name)
    wl_id = name_to_id.get(("winline", wl_clean))
    fb_id = name_to_id.get(("fonbet", fb_clean))

    if wl_id == fb_id and wl_id is not None:
        return

    if wl_id and fb_id and wl_id != fb_id:
        teams["teams"][wl_id]["names"]["fonbet"] = fb_name
        for bk in ["winline", "fonbet", "fonbetRU", "betera"]:
            if teams["teams"][fb_id]["names"].get(bk) and not teams["teams"][wl_id]["names"].get(bk):
                teams["teams"][wl_id]["names"][bk] = teams["teams"][fb_id]["names"][bk]
        if len(fb_name) > len(teams["teams"][wl_id]["canonical_name"]):
            teams["teams"][wl_id]["canonical_name"] = fb_name
        del teams["teams"][fb_id]
        return

    if wl_id and not fb_id:
        teams["teams"][wl_id]["names"]["fonbet"] = fb_name
        if len(fb_name) > len(teams["teams"][wl_id]["canonical_name"]):
            teams["teams"][wl_id]["canonical_name"] = fb_name
        return

    if fb_id and not wl_id:
        teams["teams"][fb_id]["names"]["winline"] = wl_name
        if len(wl_name) > len(teams["teams"][fb_id]["canonical_name"]):
            teams["teams"][fb_id]["canonical_name"] = wl_name
        return

    from core.team_directory import _next_id
    team_id = _next_id(teams)
    teams["teams"][team_id] = {
        "id": team_id,
        "canonical_name": wl_name if len(wl_name) >= len(fb_name) else fb_name,
        "sport": sport,
        "notes": "",
        "names": {"winline": wl_name, "fonbet": fb_name, "fonbetRU": None, "betera": None},
    }


if __name__ == "__main__":
    sport = None
    if "--sport" in sys.argv:
        idx = sys.argv.index("--sport")
        if idx + 1 < len(sys.argv):
            sport = sys.argv[idx + 1]
    match_and_crosslink(sport)
