# MONITORING.md — WC TIE Edge Monitoring Workflow

The monitoring system runs autonomously during World Cup game windows. It detects goals, equalizers, and pricing edges; generates Gemma4 commentary; and delivers it to Eric's Telegram. Eric does not prompt the system — it watches the games and tells him what to do in plain English.

## Architecture

```
                     ESPN hidden API (free, no key)
                     site.api.espn.com/.../fifa.world
                              │
                              ▼
┌─────────────────────────────────────────────────────┐
│              5 CRON JOBS (Hermes scheduler)          │
│                                                      │
│  wc-goal-monitor        1 min  no_agent              │
│  wc-equalizer-monitor   1 min  no_agent              │
│  wc-auto-sell           1 min  no_agent              │
│  kalshi-vegas-snapshot  15 min no_agent              │
│  wc-live-edge-deliver   2 min  no_agent              │
└─────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────┐
│         EDGE DETECTOR DAEMON (background loop)       │
│         wc_live_edge.py (NOT cron — continuous)      │
│                                                      │
│  ESPN scoreboard → live games?                       │
│    No  → sleep 300s, recheck                         │
│    Yes → every 60s per live game:                    │
│      1. ESPN summary (score, clock, goals)           │
│      2. Kalshi all-3-legs prices (fav/und/tie)       │
│      3. Combined matrix lookup (odds_class,          │
│         minute_bucket, score_state)                  │
│      4. Edge = Kalshi price − matrix prob per leg    │
│      5. If |edge|>6pp or state change →              │
│         call Gemma4 (vLLM localhost:8765)            │
│      6. Commentary → data/live_edge_commentary.log   │
└─────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────┐
│  wc-live-edge-deliver cron (every 2 min)             │
│  Tails live_edge_commentary.log                      │
│  New lines → Telegram (via Hermes gateway)           │
│  No new lines → silent                               │
└─────────────────────────────────────────────────────┘
```

## Components

### 1. Edge Detector Daemon (wc_live_edge.py)

The core of the system. A continuous Python loop (NOT a cron job) that runs during WC game windows.

- Self-gates: polls ESPN scoreboard. If no games are live, sleeps 300s. If any game is live, polls every 60s.
- Pre-game classifications cached from The Odds API once per day (4 odds classes: heavy_fav, moderate_fav, close, slight_fav). Now also captures draw odds for 3-way no-vig calibration. Never overwritten once a game goes live — live odds contaminate the classification.
- For each live game: looks up the combined transition matrix at (odds_class, minute_bucket, score_state from favorite's perspective) → P(fav win), P(tie), P(und win), sample size. Then applies Vegas-anchored calibration (log-ratio adjustment to match Vegas no-vig at pre-game point).
- Computes edge = Kalshi price - calibrated probability for all 3 legs (fav, und, tie).
- Calls Gemma4 (vLLM, localhost:8765, model gemma4-31b, temp 0.3, max 200 tokens) when:
  - Any |edge| > 6pp (material inefficiency), OR
  - A state change occurs (kickoff, goal, equalizer, second goal, full-time)
- Cooldown: 180s between repeat EDGE alerts (state changes bypass cooldown).
- Gemma4 generates 2-4 sentences of direct pricing dialogue: states the edge, the action, and the reasoning. No filler.
- Three-exit recommendations embedded in the Gemma4 prompt:
  - Exit 1 (equalizer sell): sell if Kalshi TIE bid > P(tie holds) + 3pp
  - Exit 2 (erosion sell): sell if Kalshi TIE bid > P(tie) + 3pp (edge has flipped)
  - Exit 3 (hold to settlement): hold if P(tie) still above market price
- Position tracking: trade_log.csv (aggregated TIE fills) + Kalshi /portfolio/positions (live confirmation).

Files:
- scripts/wc_live_edge.py — the daemon
- data/live_edge_run.log — run log (startup banner, verbose state, errors)
- data/live_edge_commentary.log — commentary delivery queue (Gemma4 output)

### 2. Goal Monitor (wc_goal_monitor.py, cron 1 min)

ESPN goal detection. On first goal in a close game: pulls Kalshi TIE price, computes edge against historical tie rates (underdog-first vs favorite-first), alerts if edge > 6pp. Logs to data/goal_alerts.csv. Pre-game classifications cached before kickoff.

### 3. Equalizer Monitor (wc_equalizer_monitor.py, cron 1 min)

Detects equalizer (1-0 → 1-1) → SELL/HOLD recommendation based on P(tie holds | 1-1 at minute X). Detects second goals (2-0/0-2) → CUT LOSS alert. Tracks settlement (game end → P&L). Logs to data/equalizer_alerts.csv.

### 4. Auto-Sell Manager (wc_auto_sell.py, cron 1 min)

Auto-adjusts TIE sell limit orders based on game clock. Cancels and replaces orders when optimal price moves > 2pp. Auto-fills on equalizer, auto-cuts-loss on second goal. Needs Exit 2 (erosion sell) auto-execution added — the edge detector recommends it, but wc_auto_sell.py doesn't auto-execute yet.

### 5. Vegas Snapshot (snapshot_vegas_kalshi.py, cron 15 min)

The Odds API vs Kalshi price comparison + edge tracking. Builds a time series in data/vegas_kalshi_baseline.csv for post-hoc analysis.

### 6. Delivery Cron (wc_live_edge_deliver.py, cron 2 min)

Tails data/live_edge_commentary.log from the last-delivered offset. New lines → stdout → Telegram via Hermes gateway. No new lines → silent (empty stdout). Offset stored in /tmp/wc_live_edge_deliver_offset.

### 7. Web Cockpit (wc_cockpit.py, on-demand)

FastAPI web app at http://localhost:8877 (accessible via Tailscale at http://your-tailnet-hostname:8877). Not a cron job — start manually when you want the browser UI.

- **Chat panel**: Streaming Gemma4 responses via SSE. Live game state (ESPN + Kalshi + matrix) injected into every system prompt so Gemma4 answers with real data, not hallucinations. Preset question buttons for quick analysis.
- **Games panel**: All WC games with team colors, scores, Kalshi odds table (price/bid-ask/model prob/edge per leg), 24h volume, odds class, sample size, Vegas-calibrated badge, mismatch warnings. Games sort live > pre > post. Auto-refreshes every 30s.
- **Account panel**: Kalshi balance, position count, daemon status (running/stopped with PID). Auto-refreshes every 60s.
- **Commentary feed**: Scrolling view of data/live_edge_commentary.log. Auto-refreshes every 10s.

### 8. Accuracy Test Suite (test_gemma_accuracy.py, on-demand)

19 test cases, 68 checks across 8 categories. Generates synthetic game states with known ground truth, sends to Gemma4, parses responses, and verifies accuracy. Run after any prompt change to catch regressions.

Categories: direction (14 checks), numbers (10), action (12), fav_und (7), exit (8), mismatch (3), hallucination (5), score_state (9).

Current status: 100% (68/68 checks, verified across 3 consecutive runs).

## Deployment

### Start the edge detector daemon

```bash
cd /path/to/worldcup-kalshi-opportunity-platform
python3 -u scripts/wc_live_edge.py --verbose >> data/live_edge_run.log 2>&1 &
```

Do NOT use `exec` with Hermes terminal background=true — it breaks pipe-based output capture. Use plain `python3 -u` with file redirect instead.

### Start the web cockpit

```bash
cd /path/to/worldcup-kalshi-opportunity-platform
python3 scripts/wc_cockpit.py &
# Opens on http://localhost:8877
# Access via Tailscale: http://your-tailnet-hostname:8877
```

### Run the accuracy test suite

```bash
cd /path/to/worldcup-kalshi-opportunity-platform
python3 scripts/test_gemma_accuracy.py --verbose          # full suite (19 tests, ~3 min)
python3 scripts/test_gemma_accuracy.py --category direction  # specific category
python3 scripts/test_gemma_accuracy.py --dry-run          # show prompts without calling API
```

Run this after any prompt change to verify accuracy hasn't regressed. Current baseline: 100% (68/68 checks).

### Verify it's running

```bash
ps aux | grep wc_live_edge | grep -v grep
tail -f data/live_edge_run.log
```

Expected startup output:
```
WC LIVE EDGE DETECTOR — started 2026-06-20 11:07 ET
Polling ESPN every 60s (live) / 300s (idle). Edge threshold 6.0pp. Gemma4 model gemma4-31b.
Commentary log: .../data/live_edge_commentary.log
No live games (3 total events).
No live games — sleeping 300s
```

### Stop the daemon

```bash
kill $(pgrep -f wc_live_edge.py)
```

### Test modes

```bash
# Dry-run: shows the Gemma4 prompt without calling the API (validates matrix lookup + Kalshi pull + edge computation)
python3 scripts/wc_live_edge.py --dry-run --simulate --once --verbose

# Simulate: injects a fake live game (NED 0-1 SWE at min 63) and calls Gemma4 for real
python3 scripts/wc_live_edge.py --simulate --once --verbose

# Once: single tick against real ESPN data, then exit
python3 scripts/wc_live_edge.py --once --verbose
```

### Hermes cron jobs

All 5 crons are managed by the Hermes scheduler. Wrappers live in ~/.hermes/scripts/. The edge detector daemon is NOT a cron job — it's a long-running background process.

```bash
# List all cron jobs
# (via Hermes: cronjob action=list)

# Cron wrappers:
# ~/.hermes/scripts/wc_goal_monitor.sh
# ~/.hermes/scripts/wc_equalizer_monitor.sh
# ~/.hermes/scripts/wc_auto_sell.sh
# ~/.hermes/scripts/kalshi_vegas_snapshot.sh
# ~/.hermes/scripts/wc_live_edge_deliver.sh
```

## Monitoring the Monitor

### Is the daemon alive?

```bash
ps aux | grep wc_live_edge.py | grep -v grep
tail -20 data/live_edge_run.log
```

### Is commentary flowing?

```bash
tail -f data/live_edge_commentary.log
```

### Is delivery cron working?

The cron runs every 2 min. If commentary log has new lines but Telegram isn't getting them:
- Check /tmp/wc_live_edge_deliver_offset (should match the byte offset of the last delivered line)
- Reset delivery: `rm /tmp/wc_live_edge_deliver_offset` (will re-deliver from start of log)
- Check Hermes cron status: cronjob action=list, find wc-live-edge-deliver, check last_status

### Is Gemma4 responding?

```bash
curl -s localhost:8765/v1/models | python3 -m json.tool
# Should show model "gemma4-31b"
```

If Gemma4 is down, the edge detector prints `[Gemma4 error: ...]` to the commentary log but continues running. It will resume calling Gemma4 once it's back.

### Is vLLM running?

```bash
systemctl --user status vllm-tailscale-proxy  # the socat proxy on 8765
# or check the vLLM process directly
ps aux | grep vllm | grep -v grep
```

## Data Files

| File | Purpose |
|------|---------|
| data/live_edge_run.log | Daemon run log (startup, verbose state, errors) |
| data/live_edge_commentary.log | Commentary delivery queue (Gemma4 output, tailed by delivery cron) |
| data/goal_alerts.csv | Goal detection alerts with edge calculations |
| data/equalizer_alerts.csv | Equalizer/second-goal/settlement alerts |
| data/trade_log.csv | All executed trades with entry/exit/fees/P&L |
| data/vegas_kalshi_baseline.csv | Vegas vs Kalshi price time series (15-min snapshots) |
| data/combined_transition_matrix.json | The model: 94,792 matches, 4 odds classes × 19 minutes × score states |
| data/wc_transition_matrix.json | WC-only matrix (850 matches, 1930-2022) — secondary reference |
| /tmp/wc_live_edge_state.json | Daemon state (classifications, per-game score tracking, cooldown timestamps) |
| /tmp/wc_live_edge_deliver_offset | Delivery cron byte offset into commentary log |

## What Eric Sees on Telegram

During a live WC game, Eric receives 2-4 sentence pricing dialogues like:

> [KICKOFF] Netherlands vs Sweden | min 0 | 0-0 | max edge +2.1pp | exit: NO_POSITION
>
> Netherlands vs Sweden kickoff. Matrix says P(fav)=58%, P(tie)=24%, P(und)=21%. Kalshi prices are within 2pp of fair value on all legs. No edge yet — stand aside.

Then when the underdog scores first and Kalshi crashes TIE:

> [GOAL] Netherlands vs Sweden | min 3 | 0-1 | max edge +15.2pp | exit: NO_POSITION
>
> Sweden (underdog) scored at 2'. TIE crashed to 19% but the matrix says 28% for a 0-1 close game at minute 5. That's a +9pp edge on TIE. Buy TIE at the ask if you're not positioned. The equalizer window is wide open — P(equalizer in next 30 min) is ~40%.

And when the edge erodes (Exit 2):

> [EDGE] Netherlands vs Sweden | min 58 | 0-1 | max edge +8.3pp | exit: EXIT_2_EROSION_SELL
>
> TIE is overpriced at 28% — the matrix says 22% for 0-1 at minute 60. If you're holding TIE, this is the erosion exit. Sell at market. The equalizer window is closing — P(equalizer in next 15 min) is only 15%. Stand aside if not positioned.

## Status (2026-06-20)

- Edge detector: DEPLOYED (background daemon, pid managed manually)
- Web cockpit: DEPLOYED at http://localhost:8877
- 5 Hermes crons: ALL RUNNING
- Gemma4 (vLLM): RUNNING on localhost:8765, model gemma4-31b
- Continuous analysis: calls Gemma4 every 60s during live games (not just on threshold crossings)
- Trend tracking: previous-tick data (P(tie) delta, price movements, time elapsed) passed to each prompt
- Exit proximity: shows gap to erosion sell trigger, warns when within 3pp
- Accuracy test suite: 100% (68/68 checks, 3 consecutive runs)
- Live data injection: chat responses use real ESPN + Kalshi + matrix data, not hallucinations
- Balance: $XX.XX (after Trade #1 TURPAR -$1.01)
- No active positions
- Next games: NED-SWE 17:00 UTC, GER-CIV 20:00 UTC, ECU-CUW 00:00 UTC (Jun 21)

## API Usage Audit

| Service | Limit | Current Usage | Status |
|---------|-------|---------------|--------|
| Kalshi API | 200 reads/s, 100 writes/s | ~10 reads/min (worst case) | <0.1% capacity |
| The Odds API | 20,000 req/month | ~98/day = ~2,940/month | 15% of quota |
| ESPN hidden API | Free, no key | ~11 calls/min (worst case) | Tolerated |
| vLLM (local) | No external limit | ~1 call/min during live games | GPU barely loaded |

Note: There is redundancy in API calls — 6 components independently pull the ESPN scoreboard during live games. A shared cache would eliminate this but is not a blocker at current volumes.
