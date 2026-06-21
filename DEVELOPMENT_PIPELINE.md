# Development Pipeline

## Project Goal

Exploit pricing inefficiencies on Kalshi's World Cup prediction markets using a transition matrix model built from 94,792 club football matches. The system detects when Kalshi prices diverge from empirical probabilities on any of the 3 legs (favorite win / tie / underdog win) and generates real-time trading commentary via Gemma4 (vLLM on eGPU).

## Phase Status

### Phase 0: Foundation — COMPLETE

- [x] Project directory structure created
- [x] Kalshi API documentation scraped (210 pages → Obsidian vault)
- [x] Raw OpenAPI/AsyncAPI specs downloaded (4 YAML files)
- [x] Discovery brief written (market state, competitive landscape, API constraints)
- [x] Warehouse schema designed (PostgreSQL + Parquet + MongoDB)
- [x] Notes: `notes/01-discovery-brief.md`, `notes/02-warehouse-schema-design.md`

### Phase 1: Data Warehouse — COMPLETE

- [x] Kalshi API auth client (`kalshi_auth.py`) — RSA-PSS signing via `pass`
- [x] REST ingestion pipeline (`kalshi_ingest.py`) — paginated, rate-limited
- [x] MongoDB raw landing zone — Docker container `kalshi-mongo`
- [x] PostgreSQL ETL (`kalshi_etl.py`) — idempotent upserts, 400,891 events, 74,800 markets
- [x] WebSocket client (`kalshi_ws.py`) — unmetered real-time streaming
- [x] Historical backfill (`kalshi_backfill.py`)
- [x] Notes: `notes/02-warehouse-schema-design.md`

### Phase 2: Kalshi API Integration — COMPLETE

- [x] Markets, events, series, trades, candlesticks endpoints
- [x] Order placement, cancellation, position tracking
- [x] Rate limiting (15 reads/s, under 20/s Basic tier limit)
- [x] WebSocket reconnection (tech debt #4: _should_run flag)
- [x] Kalshi API pitfalls documented in skill `kalshi-wc-tie-edge`

### Phase 3A: Sentiment Analysis Pipeline — ABANDONED (2026-06-19)

Original plan: use LLM on eGPU to detect news events and assess their impact on Kalshi markets. Pivoted to WC TIE edge strategy after discovering a systematic, tradeable mispricing that doesn't require sentiment analysis. The warehouse infrastructure from Phases 0-2 remains and supports the WC strategy.

- [x] ~~LLM pipeline design~~ → Abandoned in favor of WC TIE edge
- [x] ~~Sentiment ingestion~~ → Not needed for WC strategy
- [x] ~~Fair price estimation~~ → Replaced by transition matrix model

### Phase 3B: WC TIE Edge Strategy — COMPLETE

- [x] **Edge discovery** (2026-06-19): Kalshi crashes TIE to 15-30% when underdog scores first in close games. Historical tie rate: 26.7% (4,157 games). Edge: +5-6pp after costs. See EDGE.md.
- [x] **WC transition matrix** (`data/wc_transition_matrix.json`): 850 WC matches, 1930-2022. 19 intervals × score states → P(home win), P(away win), P(tie).
- [x] **Combined transition matrix** (`data/combined_transition_matrix.json`): 94,792 club matches with goal minutes + pre-game odds. 4 odds classes × 19 intervals × 8+ score states → P(fav win), P(tie), P(und win), sample size. This is the primary model.
- [x] **Goal detection + BUY alerts** (`wc_goal_monitor.py`, cron 1 min)
- [x] **Equalizer detection + SELL/HOLD alerts** (`wc_equalizer_monitor.py`, cron 1 min)
- [x] **Auto-sell limit management** (`wc_auto_sell.py`, cron 1 min) — adjusts sell limits by game clock
- [x] **Vegas vs Kalshi snapshots** (`snapshot_vegas_kalshi.py`, cron 15 min)
- [x] **Trade #1**: TURPAR TIE — bought 69 shares at $0.285, sold at $0.27 for -$1.01. Lesson: Exit 2 (erosion sell) was missing. See EDGE.md for full trade history.
- [x] **Three-exit strategy**: Exit 1 (equalizer sell), Exit 2 (erosion sell), Exit 3 (hold to settlement). See EDGE.md.

### Phase 4: Real-Time Edge Detector + Gemma4 Commentary — COMPLETE (2026-06-20)

- [x] **Edge detector daemon** (`wc_live_edge.py`): Continuous loop, polls ESPN + Kalshi every 60s during live games, looks up combined matrix, computes edges on all 3 legs, calls Gemma4 every tick.
- [x] **Continuous analysis**: Gemma4 called every 60s (not just on threshold crossings). Trend data (prev tick deltas) and exit proximity included in every prompt.
- [x] **Telegram delivery cron** (`wc_live_edge_deliver.py`, cron 2 min): Tails commentary log, delivers new lines to Telegram. Silent when no new commentary.
- [x] **Prompt engineering**: Team names in edge lines (not abstract labels), per-leg probability labels, mismatch warnings, direction indicators (positive = overpriced = sell), hallucination guardrails.

### Phase 5: Web Cockpit + Accuracy Testing — COMPLETE (2026-06-20)

- [x] **Web cockpit** (`wc_cockpit.py`): FastAPI app at http://localhost:8877. Chat with Gemma4 (streaming, live data injected), live games panel with Kalshi odds table, account balance, daemon status, commentary feed.
- [x] **Live data injection**: Every chat message gets current ESPN + Kalshi + matrix data injected into the system prompt. Gemma4 answers with real numbers, not hallucinations.
- [x] **Accuracy test suite** (`test_gemma_accuracy.py`): 19 test cases, 68 checks, 8 categories. 100% accuracy (3 consecutive runs). Tests direction, numbers, action, fav/und, exit, mismatch, hallucination, score state.

### Phase 6: Automated Execution — NOT STARTED

- [ ] Auto-execute Buy orders when edge detected (currently manual)
- [ ] Auto-execute Exit 2 (erosion sell) in `wc_auto_sell.py` (detector recommends, doesn't execute)
- [ ] Position sizing automation (1/4 Kelly, hard cap 5%)
- [ ] Portfolio risk management (20% cap across simultaneous positions)
- [x] **Vegas-anchored calibration** (`vegas_calibrate()` in wc_live_edge.py and wc_cockpit.py): Log-ratio calibration that corrects matrix probabilities to match Vegas no-vig at the pre-game point. Fixes the coarse odds bucket problem (a -170 and -1050 favorite both land in heavy_fav). Mismatch warning covers all legs, not just TIE. Deployed 2026-06-21 after the Spain -1050 trade exposed a false +17pp edge.
- [x] **Tailscale dashboard access**: UFW port 8877 on your-vpn-interface, dashboard accessible from any machine on the tailnet.
- [ ] Outcome tracking and model calibration against trade results (Vegas calibration deployed; outcome-based calibration still pending)

## Trade History

| # | Date | Game | Side | Shares | Entry | Exit | P&L | Lesson |
|---|------|------|------|--------|-------|------|-----|--------|
| 1 | 2026-06-19 | TUR vs PAR (TURPAR) | TIE | 69 | $0.285 | $0.27 | -$1.01 | Exit 2 (erosion sell) was missing — held too long |

Balance: $XX.XX (started at ~$XXX).

## Key Lessons

1. **Entry edge was real**: Underdog scores first, close game, P(tie)=46.6%, bought at 28.5%. +18pp edge.
2. **Edge erodes with time**: By minute 60, P(tie) dropped to 21% but Kalshi still priced TIE at 30%. Should have sold (Exit 2).
3. **Held too long**: Sold at minute 73 for -$1.01. EV of holding was -$6.71 to -$9.72. Exit 2 would have sold at minute 60 for break-even.
4. **Maker orders save 75% on fees.** Always use limit orders.
5. **Kalshi overreacts to goals** — crashes TIE too far on 1-0, spikes TIE too far on 1-1. Both directions are tradeable.
6. **Prompt engineering matters**: Gemma4 initially confused edge direction (said "underpricing" when it meant "overpricing"). Fixed with explicit per-leg labels, team names, and direction indicators in the prompt.
7. **Live data injection prevents hallucinations**: Chat responses improved from fabricated numbers to 100% accuracy when game state was injected into every system prompt.
