# System Architecture

> See also: [EDGE.md](EDGE.md) (trading strategy), [MONITORING.md](MONITORING.md) (operational workflow), [DEVELOPMENT_PIPELINE.md](DEVELOPMENT_PIPELINE.md) (phase status), [AGENTS.md](AGENTS.md) (project conventions)

## Overview

The Kalshi WC TIE trading system detects pricing inefficiencies on Kalshi's World Cup prediction markets by comparing live market prices to a transition matrix model built from 94,792 club football matches. The system runs autonomously during WC game windows — polling ESPN for scores, pulling Kalshi prices on all 3 legs (favorite/underdog/tie), looking up matrix probabilities, and calling Gemma4 (vLLM on eGPU) for natural-language pricing commentary streamed to Telegram.

The project also includes a data warehouse (MongoDB + PostgreSQL) from the original build that warehouses Kalshi API data for historical analysis.

## Data Flow

```
                    ┌──────────────────┐
                    │  ESPN hidden API │  (free, no key)
                    │  Live scores +   │
                    │  goal events     │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  The Odds API    │  (20K req/month)
                    │  Pre-game odds →  │
                    │  odds class cache │
                    └────────┬─────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────┐
│           EDGE DETECTOR DAEMON (wc_live_edge.py)      │
│           Continuous loop, 60s poll during live games │
│                                                       │
│  1. ESPN scoreboard → live games?                     │
│  2. ESPN summary per game (score, clock, goals)       │
│  3. Kalshi /markets → all 3 legs (fav/und/tie)        │
│  4. Combined transition matrix lookup:                │
│     (odds_class, minute_bucket, score_state)          │
│     → P(fav win), P(tie), P(und win), sample size     │
│  5. Vegas-anchored calibration:                       │
│     log-ratio adjust to match Vegas no-vig at min 0   │
│     → calibrated P(fav), P(tie), P(und)               │
│  6. Edge = Kalshi price - calibrated prob per leg     │
│  7. Every tick: call Gemma4 with full game state      │
│     + trend data (prev tick deltas) + exit proximity  │
│  8. Commentary → data/live_edge_commentary.log        │
└──────────────────────┬───────────────────────────────┘
                       │
           ┌───────────┼───────────┐
           ▼           ▼           ▼
┌──────────────┐ ┌───────────┐ ┌─────────────────────┐
│  Commentary  │ │  Run log  │ │  Delivery cron      │
│  log →       │ │  (daemon  │ │  (every 2 min)      │
│  Telegram    │ │  state)   │ │  tails log → stdout │
│  via cron    │ │           │ │  → Telegram         │
└──────────────┘ └───────────┘ └─────────────────────┘

┌──────────────────────────────────────────────────────┐
│              WEB COCKPIT (wc_cockpit.py)              │
│              http://localhost:8877                     │
│                                                       │
│  LEFT PANEL: Chat with Gemma4                         │
│    - Streaming responses via vLLM SSE                 │
│    - Live game state injected into every prompt       │
│    - Preset questions for quick analysis              │
│                                                       │
│  RIGHT PANEL: Live Monitor                            │
│    - ESPN games with team colors + scores             │
│    - Kalshi odds table (price, bid/ask, model, edge)  │
│    - Account balance + positions                      │
│    - Daemon status (running/stopped)                  │
│    - Scrolling commentary feed                        │
└──────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────┐
│              5 HERMES CRON JOBS                       │
│                                                       │
│  wc-goal-monitor        1 min  → BUY signal alerts    │
│  wc-equalizer-monitor   1 min  → SELL/HOLD alerts     │
│  wc-auto-sell           1 min  → sell limit mgmt      │
│  kalshi-vegas-snapshot  15 min → price time series    │
│  wc-live-edge-deliver   2 min  → commentary→Telegram  │
└──────────────────────────────────────────────────────┘
```

## Components

### Edge Detector Daemon (`scripts/wc_live_edge.py`)

The core of the system. A continuous Python loop (NOT a cron job) that runs during WC game windows.

- **Self-gating**: 60s poll when games live, 300s when idle. Checks ESPN scoreboard for live games.
- **Pre-game classification**: The Odds API → 4 odds classes (heavy_fav, moderate_fav, close, slight_fav). Cached once per day, never overwritten once game is live.
- **Matrix lookup**: Combined transition matrix (94,792 matches) at (odds_class, minute_bucket, score_state from favorite's perspective) → P(fav win), P(tie), P(und win), sample size.
- **Vegas-anchored calibration**: After matrix lookup, probabilities are calibrated to match Vegas no-vig at the pre-game point using log-ratio adjustment (delta_i = ln(P_vegas_i / P_matrix_i(0,0))). Corrects the coarse odds bucket problem. For normal favorites: 1-3pp correction. For extreme favorites (-400+): 15-20pp correction.
- **Edge computation**: edge = Kalshi price - calibrated probability for each leg. Positive = overpriced (sell), negative = underpriced (buy).
- **Gemma4 calls**: Every 60s tick during live games. Prompt includes current game state, calibrated probs, Vegas anchor, Kalshi prices, edges, position, three-exit recommendation, exit proximity (gap to erosion sell trigger), and trend data (P(tie) delta, price movements since last tick).
- **Mismatch detection**: Heavy-favorite mismatches (fav_american <= -400) get a caution warning for ALL legs. Probabilities are Vegas-calibrated, but sample sizes are thin at extreme odds. The mismatch flag is a data quality warning, not a trade gate.
- **Position tracking**: trade_log.csv (aggregated TIE fills) + Kalshi /portfolio/positions (live confirmation).

### Web Cockpit (`scripts/wc_cockpit.py`)

FastAPI web app at http://localhost:8877 (accessible via Tailscale).

- **Chat panel**: Streaming Gemma4 responses via SSE. Live game state (ESPN + Kalshi + matrix) injected into every system prompt so Gemma4 answers with real data, not hallucinations.
- **Games panel**: All WC games with team colors, scores, Kalshi odds table (price/bid-ask/model prob/edge per leg), 24h volume, odds class, sample size, Vegas-calibrated badge, mismatch warnings. Games sort live > pre > post. Auto-refreshes every 30s.
- **Account panel**: Kalshi balance, position count, daemon status.
- **Commentary feed**: Scrolling view of data/live_edge_commentary.log. Auto-refreshes every 10s.

### Accuracy Test Suite (`scripts/test_gemma_accuracy.py`)

19 test cases, 68 checks across 8 categories. Generates synthetic game states with known ground truth, sends to Gemma4, parses responses, and verifies:

| Category | Checks | What it verifies |
|----------|--------|------------------|
| direction | 14 | Overpriced (positive edge = sell) vs underpriced (negative edge = buy) |
| numbers | 10 | Correct matrix probabilities and Kalshi prices cited |
| action | 12 | Buy/sell/hold recommendations match edge thresholds |
| fav_und | 7 | Correct team identified as favorite, team names used |
| exit | 8 | Three-exit strategy (erosion sell, hold, no position) |
| mismatch | 3 | Caution warning for heavy-favorite mismatches (Vegas-calibrated, small sample) |
| hallucination | 5 | No fabricated numbers, no placeholders |
| score_state | 9 | Handles 0-0, 1-0, 0-1, 1-1, 2-0, 0-2 states |

Current status: 100% (68/68 checks, verified across 3 consecutive runs).

### Data Warehouse (from original build)

- **kalshi_ingest.py**: REST polling → MongoDB (raw landing zone)
- **kalshi_etl.py**: MongoDB → PostgreSQL (metadata, upserts)
- **kalshi_ws.py**: WebSocket streaming (unmetered)
- **kalshi_backfill.py**: Historical data backfill

## Data Storage

```
┌─────────────────────────────────────────────────────────┐
│                    MongoDB 7.0                          │
│  Docker container: kalshi-mongo, port 27017             │
│  Database: kalshi_warehouse                             │
│  Collections: raw_markets, raw_series, raw_trades,      │
│  raw_candlesticks, raw_orderbooks, ws_messages          │
│  Every API response stored as immutable JSON document   │
└──────────────────────────────┬──────────────────────────┘
                               │ ETL (idempotent upserts)
                               ▼
┌─────────────────────────────────────────────────────────┐
│                   PostgreSQL 18.4                       │
│  systemd, port 5432, db=kalshi_warehouse                │
│  Tables: series, events, markets (74,800 markets,       │
│  400,891 events)                                        │
│  All upserts use INSERT ... ON CONFLICT DO UPDATE       │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│              Transition Matrix (JSON)                   │
│  data/combined_transition_matrix.json                   │
│  94,792 club matches, 4 odds classes × 19 minutes       │
│  × 8+ score states → P(fav win), P(tie), P(und win)     │
│  This IS the model — empirical probabilities,           │
│  directly comparable to Kalshi prices                   │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│              Operational Data Files                     │
│  data/trade_log.csv          — all executed trades      │
│  data/goal_alerts.csv        — goal detection alerts    │
│  data/equalizer_alerts.csv   — exit trigger alerts      │
│  data/vegas_kalshi_baseline.csv — price time series     │
│  data/live_edge_commentary.log — Gemma4 commentary      │
│  data/live_edge_run.log      — daemon run log           │
└─────────────────────────────────────────────────────────┘
```

## API Usage Audit

| Service | Limit | Current Usage | Status |
|---------|-------|---------------|--------|
| Kalshi API | 200 reads/s, 100 writes/s | ~10 reads/min (worst case) | <0.1% capacity |
| The Odds API | 20,000 req/month | ~98/day = ~2,940/month | 15% of quota |
| ESPN hidden API | Free, no key | ~11 calls/min (worst case) | Tolerated |
| vLLM (local) | No external limit | ~1 call/min during live games | GPU barely loaded |

Note: There is redundancy in API calls — 6 components independently pull the ESPN scoreboard during live games. A shared cache would eliminate this but is not a blocker at current volumes.

## Infrastructure

| Component | Specification |
|-----------|--------------|
| Kalshi API | Production, Basic tier. RSA-PSS auth. `pass show kalshi` |
| ESPN API | `site.api.espn.com/apis/site/v2/sports/soccer/fifa.world` (free) |
| The Odds API | `api.the-odds-api.com/v4/sports/soccer_fifa_world_cup` (`pass show oddsdotcom`) |
| vLLM | localhost:8765, model gemma4-31b, temp 0.3, max 200 tokens |
| MongoDB | Docker `kalshi-mongo`, port 27017 (NOT 8.0 — TCMalloc crash on kernel 7.0) |
| PostgreSQL | systemd, port 5432, user=kalshi, db=kalshi_warehouse |
| Python | 3.14. `pip3 install --break-system-packages` |
| eGPU | RTX Pro 5000 Blackwell 72GB via TB3 (Alpine Ridge, shared x4) |
| Hermes gateway | Telegram, polling, open access. systemd service. |
