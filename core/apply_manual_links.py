"""
Apply manually confirmed partial matches to teams.json.
Reads data/event_partial_matches.json, processes entries where result=true,
cross-links teams in teams.json.
Usage:
  python -m core.apply_manual_links
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.team_directory import load_teams, save_teams, _clean

PARTIAL_PATH = ROOT / "data" / "event_partial_matches.json"


def apply():
    if not PARTIAL_PATH.exists():
        print(f"File not found: {PARTIAL_PATH}")
        print("Run core/event_matcher.py first to generate partial matches.")
        return

    data = json.loads(PARTIAL_PATH.read_text("utf-8-sig"))
    partials = data.get("partials", [])
    confirmed = [p for p in partials if p.get("result") is True]

    if not confirmed:
        print(f"No entries with result=true. ({len(partials)} total partials)")
        return

    print(f"Applying {len(confirmed)} confirmed matches...")
    teams = load_teams()
    applied = 0
    merged = 0

    for pm in confirmed:
        sport = pm["sport"]
        for wl_name, fb_name in [
            (pm["wl_home"], pm["fb_home"]),
            (pm["wl_away"], pm["fb_away"]),
        ]:
            wl_clean = _clean(wl_name)
            fb_clean = _clean(fb_name)

            name_to_id = {}
            for team_id, team in teams["teams"].items():
                for bk, nm in team["names"].items():
                    if nm:
                        key = (bk, _clean(nm))
                        if key not in name_to_id:
                            name_to_id[key] = team_id

            wl_id = name_to_id.get(("winline", wl_clean))
            fb_id = name_to_id.get(("fonbet", fb_clean))

            if wl_id == fb_id and wl_id is not None:
                continue  # already linked

            if wl_id and fb_id and wl_id != fb_id:
                # Merge fb_id into wl_id
                teams["teams"][wl_id]["names"]["fonbet"] = fb_name
                for bk in ["winline", "fonbet", "betera"]:
                    if teams["teams"][fb_id]["names"].get(bk) and not teams["teams"][wl_id]["names"].get(bk):
                        teams["teams"][wl_id]["names"][bk] = teams["teams"][fb_id]["names"][bk]
                del teams["teams"][fb_id]
                merged += 1
                applied += 1
                continue

            if wl_id and not fb_id:
                teams["teams"][wl_id]["names"]["fonbet"] = fb_name
                applied += 1
                continue

            if fb_id and not wl_id:
                teams["teams"][fb_id]["names"]["winline"] = wl_name
                applied += 1
                continue

            # Neither exists
            from core.team_directory import _next_id
            team_id = _next_id(teams)
            teams["teams"][team_id] = {
                "id": team_id,
                "canonical_name": wl_name if len(wl_name) >= len(fb_name) else fb_name,
                "sport": sport,
                "notes": "",
                "names": {"winline": wl_name, "fonbet": fb_name, "betera": None},
            }
            applied += 1

    save_teams(teams)

    crosslinked = sum(1 for t in teams["teams"].values()
                      if t["names"].get("winline") and t["names"].get("fonbet"))
    print(f"  Applied: {applied} team links ({merged} merges)")
    print(f"  Teams cross-linked: {crosslinked}/{len(teams['teams'])}")

    # Mark applied entries as processed (result stays true, but add _applied_at)
    for pm in confirmed:
        pm["_applied_at"] = datetime.now(timezone.utc).isoformat()
    PARTIAL_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Updated: {PARTIAL_PATH}")


if __name__ == "__main__":
    apply()
