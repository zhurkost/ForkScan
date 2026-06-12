import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
TEAMS_FILE = DATA_DIR / "teams.json"


def load_teams() -> dict:
    if TEAMS_FILE.exists():
        return json.loads(TEAMS_FILE.read_text("utf-8"))
    return {"version": 1, "teams": {}}


def save_teams(data: dict) -> None:
    TEAMS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def _next_id(data: dict) -> str:
    existing = [int(k.split("_")[-1]) for k in data["teams"]]
    next_num = max(existing) + 1 if existing else 1
    return f"team_{next_num:05d}"


def add_team(
    canonical_name: str,
    sport: str,
    bookmaker: str,
    bookmaker_name: str,
) -> str:
    data = load_teams()
    team_id = _next_id(data)
    data["teams"][team_id] = {
        "id": team_id,
        "canonical_name": canonical_name,
        "sport": sport,
        "names": {
            "winline": None,
            "fonbet": None,
            "betera": None,
        },
    }
    data["teams"][team_id]["names"][bookmaker] = bookmaker_name
    save_teams(data)
    return team_id


def find_by_bookmaker(bookmaker: str, name: str) -> str | None:
    data = load_teams()
    name_clean = _clean(name)
    for team_id, team in data["teams"].items():
        bk_name = team["names"].get(bookmaker)
        if bk_name and _clean(bk_name) == name_clean:
            return team_id
    return None


def find_by_canonical(name: str, sport: str | None = None) -> str | None:
    data = load_teams()
    name_clean = _clean(name)
    for team_id, team in data["teams"].items():
        if _clean(team["canonical_name"]) == name_clean:
            if sport and team["sport"] != sport:
                continue
            return team_id
    return None


def set_bookmaker_name(team_id: str, bookmaker: str, name: str) -> None:
    data = load_teams()
    if team_id not in data["teams"]:
        raise KeyError(f"Team {team_id} not found")
    data["teams"][team_id]["names"][bookmaker] = name
    save_teams(data)


def get_team(team_id: str) -> dict | None:
    data = load_teams()
    return data["teams"].get(team_id)


def list_teams(sport: str | None = None) -> list[dict]:
    data = load_teams()
    teams = list(data["teams"].values())
    if sport:
        teams = [t for t in teams if t["sport"] == sport]
    return sorted(teams, key=lambda t: t["canonical_name"])


def export_mapping() -> dict:
    data = load_teams()
    mapping = {}
    for bookmaker in ["winline", "fonbet", "betera"]:
        mapping[bookmaker] = {}
        for team in data["teams"].values():
            name = team["names"].get(bookmaker)
            if name:
                mapping[bookmaker][_clean(name)] = team["id"]
    return mapping


def import_from_bookmaker(bookmaker: str, teams_data: list[dict]) -> dict:
    data = load_teams()
    stats = {"new": 0, "existing": 0, "cross_matched": 0}

    # Build lookup of all existing names by canonical
    name_to_id = {}
    for team_id, team in data["teams"].items():
        canonical_clean = _clean(team["canonical_name"])
        if canonical_clean not in name_to_id:
            name_to_id[canonical_clean] = team_id

    for t in teams_data:
        name = t["name"]
        name_clean = _clean(name)

        # 1: Already known for this bookmaker
        existing_id = find_by_bookmaker(bookmaker, name)
        if existing_id:
            stats["existing"] += 1
            continue

        # 2: Cross-match — same name exists from another bookmaker
        cross_id = name_to_id.get(name_clean)
        if cross_id and data["teams"][cross_id]["names"].get(bookmaker) is None:
            data["teams"][cross_id]["names"][bookmaker] = name
            stats["cross_matched"] += 1
            continue

        # 3: New team
        team_id = _next_id(data)
        data["teams"][team_id] = {
            "id": team_id,
            "canonical_name": name,
            "sport": t.get("sport", "unknown"),
            "notes": "",
            "names": {
                "winline": None,
                "fonbet": None,
                "betera": None,
            },
        }
        data["teams"][team_id]["names"][bookmaker] = name
        name_to_id[name_clean] = team_id
        stats["new"] += 1

    save_teams(data)
    return stats


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())
