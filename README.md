# World Cup Kalshi Opportunity Platform

A real-time prediction-market trading system that detects pricing inefficiencies on Kalshi's World Cup markets using a transition matrix built from 94,792 club football matches, with Vegas-anchored calibration and live LLM commentary.

## What It Does

The platform monitors live World Cup games minute-by-minute, compares Kalshi market prices to a probabilistic model, and identifies edges — legs where the market price diverges from the model's fair value by more than 6 percentage points. When edges are detected, it generates natural-language commentary via a local LLM (Gemma 4 31B on vLLM) and streams it to Telegram.

The core model is a **transition matrix** built from 94,792 club football matches (2003-2022, 18 leagues) with goal-minute data and pre-game odds. It gives the probability of each outcome (favorite win / tie / underdog win) for every score state at every 5-minute interval, split by 4 odds classes. A **Vegas-anchored calibration** layer corrects the matrix's coarse odds buckets to match real market-implied probabilities.

## Architecture

```
ESPN Scoreboard (free API)
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
│  8. Commentary → log → Telegram delivery              │
└──────────────────────────────────────────────────────┘
```

## The Model

### Transition Matrix

File: `data/combined_transition_matrix.json` (171 KB)

Built from 94,792 Kaggle-sourced club football matches with goal-minute data and pre-game odds. Organized by **favorite/underdog perspective** (not home/away):

- **4 odds classes**: heavy_fav (<1.6 decimal), moderate_fav (1.6-2.0), close (2.0-2.5), slight_fav (2.5+)
- **19 time intervals**: 0, 5, 10, ..., 90
- **8+ score states** per interval: 0-0, 1-0, 0-1, 1-1, 2-0, 0-2, 2-1, 1-2, etc.
- Each cell: P(fav win), P(tie), P(und win), sample size

Score states are from the favorite's perspective: "1-0" = favorite leads, "0-1" = underdog leads.

### Vegas-Anchored Calibration

The matrix's 4 odds buckets are coarse. A -170 favorite (63% win rate) and a -1050 favorite (91% win rate) both land in `heavy_fav`, which averages to 71% win rate. Without calibration, extreme favorites get incorrect probabilities — creating false edges of 15-20pp.

The calibration layer (`vegas_calibrate()` in both `wc_live_edge.py` and `wc_cockpit.py`) fixes this using log-ratio adjustment:

1. Compute Vegas no-vig probabilities from pre-game American odds
2. Get the matrix baseline (minute 0, score 0-0) for that odds class
3. Compute log-ratio deltas: `delta_i = ln(P_vegas_i / P_matrix_i(0,0))`
4. For any in-game state (minute m, score s): `P_calibrated_i = P_matrix_i(m,s) * exp(delta_i)`, then renormalize

The matrix's transition shape is preserved — how P(tie) rises with 0-0 at minute 70, how P(fav) drops when the underdog scores first — but absolute probabilities are anchored to Vegas reality. For normal favorites: 1-3pp correction. For extreme favorites (-400+): 15-20pp correction.

## The Trading Strategy

### The Edge

When the underdog scores first in a close World Cup game, Kalshi crashes the TIE (draw) price to 15-30%. Historical data (94,792 matches) shows the tie actually occurs 26.7% of the time in this scenario — a 5-6pp edge after costs. The edge is larger when the underdog scores first vs the favorite (+5-6pp gap, confirmed across 4,000-8,000 games per cell).

### Three-Exit Strategy

- **Exit 1 (equalizer spike)**: Sell on 1-1 equalizer. TIE price spikes when the favorite equalizes. Sell into the spike.
- **Exit 2 (erosion)**: Sell when P(tie) from the calibrated matrix drops below the Kalshi TIE bid. The edge erodes with time.
- **Exit 3 (settlement)**: Hold to resolution if P(tie) is still above market price. $1 if draw, $0 otherwise.

### Pre-Game Filter

Only trade close games. Proxy: favorite moneyline > -400. If the favorite is heavier than -400, the game is a mismatch — sample sizes are thin and the transition shape may not be reliable, even with calibration.

## Quick Start

### Prerequisites

- Python 3.10+
- `pass` (GPG password-store) for credential management
- Kalshi API account (production or demo)
- The Odds API key (free tier: 500 requests/month; paid: 20,000/month)
- Optional: vLLM server with Gemma 4 31B for live commentary
- Optional: PostgreSQL and MongoDB for the data warehouse

### Setup

```bash
# Clone
git clone https://github.com/Eric90403/worldcup-kalshi-opportunity-platform.git
cd worldcup-kalshi-opportunity-platform

# Install dependencies
pip install -r requirements.txt

# Store credentials in pass
pass insert kalshi              # Kalshi API Key ID
pass insert kalshi-private-key  # Kalshi RSA private key (PEM format)
pass insert oddsdotcom          # The Odds API key
```

### Run

```bash
# 1. Test Kalshi API authentication
python3 scripts/kalshi_auth.py

# 2. Start the edge detector daemon
python3 -u scripts/wc_live_edge.py --verbose

# 3. Start the web cockpit (optional)
python3 scripts/wc_cockpit.py
# Opens on http://localhost:8877

# 4. Run the accuracy test suite
python3 scripts/test_gemma_accuracy.py --verbose
```

## Components

### Edge Detector Daemon (`scripts/wc_live_edge.py`)

The core of the system. A continuous Python loop (not a cron job) that runs during World Cup game windows.

- Self-gates: 60s poll when games live, 300s when idle
- Pulls ESPN scoreboard + game summaries (free, no key)
- Pulls Kalshi market prices for all 3 legs (favorite / underdog / tie)
- Looks up the combined transition matrix, applies Vegas calibration
- Computes edge = Kalshi price - calibrated probability for each leg
- Calls a local LLM (Gemma 4 31B via vLLM) every 60s tick for natural-language commentary
- Tracks trends (probability deltas, price movements since last tick)
- Monitors exit proximity (gap to erosion sell trigger)

### Web Cockpit (`scripts/wc_cockpit.py`)

A FastAPI browser interface at `http://localhost:8877`.

- **Left panel**: Chat with the LLM (streaming responses, live game data injected into every prompt to prevent hallucinations)
- **Right panel**: Live games with Kalshi odds tables (price / bid-ask / model probability / edge per leg), 24h volume, odds class, sample size, Vegas-calibrated badge, account balance, daemon status, and a scrolling commentary feed

### Monitoring Cron Jobs

The system includes 5 scheduled jobs for automated monitoring:

| Job | Frequency | Purpose |
|-----|-----------|---------|
| Goal monitor | 1 min | ESPN goal detection → BUY signal alert |
| Equalizer monitor | 1 min | Equalizer / second-goal / settlement detection |
| Auto-sell | 1 min | Manages TIE sell limit orders by game clock |
| Vegas-Kalshi snapshot | 15 min | Vegas vs Kalshi price time series |
| Commentary delivery | 2 min | Tails commentary log → Telegram delivery |

### Accuracy Test Suite (`scripts/test_gemma_accuracy.py`)

19 test cases, 68 checks across 8 categories: edge direction, number accuracy, action correctness, favorite/underdog identification, exit recommendations, mismatch detection, hallucination prevention, score state handling. Verifies the LLM cites correct probabilities, identifies overpriced vs underpriced correctly, and recommends the right action.

## Documentation

| Document | Purpose |
|----------|---------|
| [EDGE.md](EDGE.md) | Trading strategy: edge thesis, historical data, three-exit strategy, Kelly sizing, fee schedule |
| [ARCHITECTURE.md](ARCHITECTURE.md) | System architecture: data flow, components, data warehouse schema |
| [MONITORING.md](MONITORING.md) | Operational workflow: deployment, cron jobs, troubleshooting, API audit |
| [DEVELOPMENT_PIPELINE.md](DEVELOPMENT_PIPELINE.md) | Development phases and status |

## Data Sources

- **Transition matrix**: 94,792 club football matches from Kaggle (goal minutes + pre-game odds, 18 leagues, 2003-2022). The processed matrix is included in `data/combined_transition_matrix.json`.
- **WC reference matrix**: 850 World Cup group stage matches, 1930-2022. Included in `data/wc_transition_matrix.json`.
- **ESPN**: Hidden API at `site.api.espn.com/apis/site/v2/sports/soccer/fifa.world` — free, no key. Returns live scores, clocks, and goal events.
- **Kalshi**: REST API at `external-api.kalshi.com/trade-api/v2`. RSA-PSS signature auth. Market data (prices, bid-ask, volume) for all 3 legs.
- **The Odds API**: Pre-game moneylines for odds classification and Vegas calibration. `api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds/`

## Credential Management

All credentials are stored in `pass` (GPG password-store) — no API keys are hardcoded in the codebase. The scripts load credentials at runtime via `subprocess.check_output(["pass", "show", "<key>"])`.

Required pass entries:

| Pass entry | Description |
|------------|-------------|
| `kalshi` | Kalshi API Key ID |
| `kalshi-private-key` | Kalshi RSA private key (PEM format) |
| `oddsdotcom` | The Odds API key |

## Kalshi Fee Structure

- **Taker (marketable)**: `0.07 × C × P × (1-P)` per share
- **Maker (resting limit)**: `0.0175 × C × P × (1-P)` — exactly 1/4 of taker
- No volume tiers, no breakpoints, no settlement fee
- The only fee break is maker vs taker — always use limit orders

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This software is for educational and research purposes. Prediction market trading involves risk. The authors are not responsible for any financial losses. Past performance does not guarantee future results. The transition matrix is built from historical club football data and may not reflect World Cup-specific dynamics.
