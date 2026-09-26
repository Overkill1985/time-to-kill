# Tooling: Skills, MCP servers and fallbacks

Inventory taken 2026-09-26 in the Claude Code desktop environment used to build
this project. Only tools that were actually present and probed are listed.
**The application never depends on any of these at runtime** - they are
development, research and integration aids. Runtime data flows through the
provider adapters in `src/ttk/providers/`.

## MCP servers

| Tool | Provider | Purpose | Project use | Auth | R/W | Status at inventory | Fallback |
|---|---|---|---|---|---|---|---|
| `propline` | PropLine | Multi-book odds (27 books incl. Pinnacle, DraftKings, FanDuel), odds history, closing lines, movement, CLV grading, scores | Design and verify the odds adapter; capture real fixtures; spot-check ingestion | Configured in the MCP; app use needs its own PropLine key | Read (plus a `create_free_api_key` tool - account creation is the user's call, never Claude's) | **Working.** Returned 15 NFL games, 13 spread lines each, Pinnacle | The Odds API adapter |
| `sports-hub` | Aggregator (ESPN, NCAA, SportsDB, ...) | Schedules, scoreboards, standings, rosters | Schedule/results adapter design; fixtures | None for the ESPN/NCAA providers | Read | **Working.** ESPN CFB scoreboard returned (very large payloads - always pass `limit`) | `nfl-data` / `cfb-data` / `nba-data` / `cbb-data` skills, ESPN site API directly |
| `olympus-bets` | Olympus Bets Analytics | Third-party Monte Carlo projections, track record | **Benchmark only** - compare our calibration against an outside model. Never a model input | Public tools anonymous; premium needs a paid token | Read | **Working.** NFL `current`, `in_season` | None needed |
| `sports-betting` | Third-party picks service | "AI picks", confidence scores | **Not used.** Reports confidence scores, not calibrated probabilities; no NFL/CFB | API key on their side | Read + `log_pick` write | **Failing** (HTTP 403) | n/a |
| `definite` | Definite | Data platform | Not needed | - | - | **Failed to connect** (endpoint not found) | n/a |
| Notion, Slack, Linear, Figma, BigQuery, Hex, Asana, Atlassian, ... | various | - | Not needed | **Needs user authorization** in claude.ai connector settings | - | Unauthenticated | n/a |
| `Claude_Browser` / `claude-in-chrome` | Anthropic | Browser automation | Read provider docs, check the running app | - | - | Available | WebFetch |
| `scheduled-tasks` | Anthropic | Scheduled local runs | Possible future: scheduled `ttk ingest-odds` | - | - | Available | OS scheduler |

**No GitHub MCP is installed.** Source control uses local `git` over SSH
(`git@github.com:Overkill1985/time-to-kill.git`) and the GitHub CLI.
- The CLI is `gh` 2.101.0, installed 2026-09-26 via winget at `C:\Program Files\GitHub CLI\gh.exe`.
- It's logged in as Overkill1985 with the `repo`, `read:org` and `gist` scopes, using SSH for git operations.
- Use it for PRs, CI run status (`gh run list`) and issues.

## Skills

| Skill | Use in this project |
|---|---|
| `betting` | Cross-check de-vig/Kelly/parlay math when writing tests |
| `nfl-data`, `cfb-data`, `nba-data`, `cbb-data` | Explore ESPN / nflverse / NCAA data shapes when building stats providers (Phase 4+) |
| `data:statistical-analysis`, `data:validate-data` | Model validation and data-quality work (Phases 4, 10) |
| `dataviz` | Calibration curves, performance charts (Phase 10) |
| `code-review`, `security-review`, `simplify` | Review before merging substantial changes |
| `artifact-design` | Only for published reports, not the app UI |

Not relevant: `betting-app` (pari-mutuel wallets - a different product),
`sports-betting-analyzer` (heuristic trends), the prediction-market skills
(`kalshi`, `polymarket`) except as optional extra price sources.

## Capability map

| Capability | Preferred | Fallback |
|---|---|---|
| Repository | local git + `gh` CLI (no GitHub MCP available) | GitHub web UI |
| Odds research / fixtures | PropLine MCP | The Odds API via `ttk ingest-odds` |
| Odds at runtime | `TheOddsApiProvider` (needs `TTK_ODDS_API_KEY`) | PropLine REST adapter (not built; needs a PropLine key) |
| Schedules / scores research | sports-hub MCP, sport data skills | ESPN site API |
| Library docs | WebFetch / browser on official docs | - |
| Database inspection | `sqlite3` / SQLAlchemy session | - |
| Tests / lint / types | `pytest`, `ruff`, `mypy` in `.venv` | - |
