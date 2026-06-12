# ForkScan — Project Flow

Arbitrage scanner: Winline ↔ Fonbet event matching and odds comparison.

## Directory Structure

```
ForkScan/
├── config/
│   └── bookmakers.json          # Bookmaker config (sports, URLs, IDs)
├── core/
│   ├── event_matcher.py         # Matches Winline↔Fonbet events, auto-crosslinks teams
│   ├── apply_manual_links.py    # Reads AI-resolved partials, links teams
│   ├── arb_finder.py            # Finds arbitrage opportunities
│   └── team_directory.py        # Teams.json CRUD + import_from_bookmaker
├── scripts/
│   ├── winline/
│   │   ├── sniff.py             # Scroll sniffer → event_ids_{sport}.json
│   │   ├── sniff_cormache.py    # Binary sniffer → cormache_raw.bin (debug only)
│   │   └── parse_events.py      # POST-per-ID → events_parsed_{sport}.json
│   └── fonbet/
│       ├── sniff.py             # API sniffer → network_log.json
│       └── parse_events.py      # Parses Fonbet events → events_parsed_{sport}.json
├── partial_ai_resolver.py       # Qwen2.5-VL evaluates partial matches (pair-by-pair)
├── data/                        # Runtime outputs (gitignored except .gitkeep)
│   ├── teams.json               # Persistent team cross-reference (cumulative)
│   ├── event_matches.json       # Full matches from event_matcher
│   ├── event_partial_matches.json  # Partial matches → AI resolver → manual_links
│   └── arbs.json                # Arbitrage results
└── config/bookmakers.json       # Sports definitions
```

## Pipeline Flow

```
┌─────────────────────────────────────────────────────────────┐
│ 1. SNIFFERS (data collection)                               │
├─────────────────────────────────────────────────────────────┤
│ Winline:  sniff.py → event_ids_{sport}.json (scroll+DOM)    │
│           sniff_cormache.py → cormache_raw.bin (debug)      │
│ Fonbet:   sniff.py → network_log.json                       │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ 2. PARSERS (event details)                                  │
├─────────────────────────────────────────────────────────────┤
│ Winline:  parse_events.py → POST /sb/api/by/actual per ID   │
│           → events_parsed_{sport}.json                      │
│ Fonbet:   parse_events.py → events_parsed_{sport}.json      │
│ Both also call import_from_bookmaker() → populates teams    │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ 3. EVENT MATCHER (cross-referencing)                        │
├─────────────────────────────────────────────────────────────┤
│ Groups by (sport, exact timestamp), skips past events       │
│ Phase 1: exact name match → auto cross-link teams           │
│ Phase 2: 1 exact + 1 partial substring → auto-resolve       │
│ Phase 3: remainder → event_partial_matches.json             │
│ Output:  event_matches.json + event_partial_matches.json    │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ 4. AI RESOLVER (team name disambiguation)                   │
├─────────────────────────────────────────────────────────────┤
│ partial_ai_resolver.py — Qwen2.5-VL-7B-Instruct (local GPU) │
│ Evaluates EACH pair (wl_home↔fb_home, wl_away↔fb_away)     │
│ Sets result=true if BOTH pairs are same team                │
│ Output:  updates event_partial_matches.json in-place        │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ 5. APPLY MANUAL LINKS (teams.json update)                   │
├─────────────────────────────────────────────────────────────┤
│ apply_manual_links.py                                       │
│ Reads result=true entries, cross-links teams in teams.json  │
│ Tags applied entries with _applied_at timestamp             │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ 6. ARB FINDER (odds comparison)                             │
├─────────────────────────────────────────────────────────────┤
│ arb_finder.py                                               │
│ Matches Winline events to Fonbet odds via teams.json        │
│ Output:  arbs.json                                          │
└─────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

### Auto-cleanup
Each stage deletes its own old outputs on fresh run:

| Stage | Deletes |
|-------|---------|
| `sniff.py` (Winline) | `event_ids_*.json` |
| `parse_events.py` (Winline) | `events_parsed_*.json` |
| `sniff.py` (Fonbet) | `network_log.json` |
| `parse_events.py` (Fonbet) | `events_parsed_*.json` |
| `event_matcher.py` | `event_matches.json`, `event_partial_matches.json` |
| `arb_finder.py` | `arbs*.json` |

### teams.json — Cumulative
`teams.json` is the ONLY persistent runtime file. It accumulates team name mappings across all runs:
- Parsers call `import_from_bookmaker()` → adds new team names
- `event_matcher` cross-links exact matches
- `apply_manual_links` cross-links AI-confirmed matches

The more teams accumulated, the fewer partials reach the AI resolver — eventually approaching zero.

### AI Resolver (partial_ai_resolver.py)
- Uses local Qwen2.5-VL-7B-Instruct (4-bit quantized) via HuggingFace transformers
- Model cache: `M:\hf_cache`
- Evaluates pairs ONE BY ONE (not batched)
- Determines if two team names refer to the same team (transliteration, typos, abbreviations)
- Result=true only if BOTH home pair AND away pair match
- No resume/backup — always runs fresh from 0 (if interrupted, just re-run)

### Time Filter
`event_matcher` skips events with `scheduled < time.time()` — only future events are processed.

### Two Winline Sniffers
- **sniff.py**: scrolls the page, extracts event IDs from DOM + network → `event_ids_*.json`
- **sniff_cormache.py**: captures raw cormache binary → `cormache_raw.bin` (debug/analysis only)
- For production, use `sniff.py` + `parse_events.py`

## Running the Pipeline

```bash
# 1. Sniff events
cd scripts/winline && python sniff.py
cd scripts/fonbet && python sniff.py

# 2. Parse event details
cd scripts/winline && python parse_events.py
cd scripts/fonbet && python parse_events.py

# 3. Match events across bookmakers
python -m core.event_matcher

# 4. AI-resolve ambiguous team names
python partial_ai_resolver.py

# 5. Apply confirmed links to teams.json
python -m core.apply_manual_links

# 6. Find arbs
python -m core.arb_finder
```

### Single-sport mode (append --sport to sniffer/parser/matcher):
```bash
python sniff.py --sport 149          # football only
python parse_events.py --sport football
python -m core.event_matcher --sport football
```

## Dependencies

- Python 3.10+ with torch (CUDA), transformers, bitsandbytes, accelerate, playwright
- Node.js 22+ (for Fonbet sniffer only)
- Qwen2.5-VL-7B-Instruct model cached in `M:\hf_cache`
- RTX 5060 8GB VRAM (for AI resolver)
