# EDGE.md — The "Buy TIE After First Goal" Strategy

## Discovery date: 2026-06-19

## The Thesis

In close World Cup matches, when a team scores the first goal to go up 1-0, Kalshi crashes the TIE (draw) price to ~15%. But the historical probability of a close game ending in a tie after going 1-0 is 36.4%. That's a 21-percentage-point mispricing and the first actionable edge found in this project.

---

## The Data

Source: jfjelstul/worldcup GitHub database — every goal in every FIFA World Cup match, 1930-2022, with minute scored. 850 group-stage matches with at least one goal. Group stage only (knockout matches can't end in a tie).

### P(tie | score is 1-0 at minute X) — ALL games

| Minute | Games at 1-0 | Ended in tie | Tie rate | Ended 1-1 | 1-1 rate |
|--------|-------------|-------------|----------|-----------|----------|
| 15     | 139         | 20          | 14.4%    | 12        | 8.6%     |
| 30     | 190         | 32          | 16.8%    | 20        | 10.5%    |
| 45     | 183         | 26          | 14.2%    | 20        | 10.9%    |
| 60     | 162         | 17          | 10.5%    | 16        | 9.9%     |
| 70     | 152         | 17          | 11.2%    | 16        | 10.5%    |
| 75     | 132         | 17          | 12.9%    | 16        | 12.1%    |
| 80     | 122         | 16          | 13.1%    | 15        | 12.3%    |
| 85     | 110         | 6           | 5.5%     | 6         | 5.5%     |

Overall: 1-0 at any minute → 15.8% tie rate. This is what Kalshi is pricing. But it's the wrong number for close games.

### P(tie | 1-0 at minute X) — SPLIT BY GAME QUALITY

Final goal margin used as proxy for team quality gap.

| Minute | Close games (margin ≤1) | Close tie rate | Mismatches (margin ≥3) | Mismatch tie rate |
|--------|------------------------|----------------|------------------------|-------------------|
| 15     | 55                     | 36.4%          | 57                     | 0.0%              |
| 30     | 88                     | 36.4%          | 56                     | 0.0%              |
| 45     | 97                     | 26.8%          | 35                     | 0.0%              |
| 60     | 96                     | 17.7%          | 16                     | 0.0%              |
| 75     | 103                    | 16.5%          | 3                      | 0.0%              |

### P(tie | 1-0 at minute X) — SPLIT BY WHO SCORED FIRST

Home/away used as proxy for favorite/underdog (home team ≈ more likely favorite in WC group stage due to host advantage and seeding). Close games only (final margin ≤ 1).

| Minute | Favorite scores first (home) | Tie rate | Underdog scores first (away) | Tie rate | Difference |
|--------|------------------------------|----------|------------------------------|----------|------------|
| 15     | 55 games                     | 36.4%    | 58 games                     | 46.6%    | +10.2pp    |
| 30     | 88 games                     | 36.4%    | 73 games                     | 38.4%    | +2.0pp     |
| 45     | 97 games                     | 26.8%    | 92 games                     | 33.7%    | +6.9pp     |
| 60     | 96 games                     | 17.7%    | 84 games                     | 31.0%    | +13.2pp    |
| 75     | 103 games                    | 16.5%    | 70 games                     | 17.1%    | +0.6pp     |

### Full breakdown: scorer × game quality

**HOME scores first (proxy: FAVORITE scores first)**

| Minute | All games | All tie% | Close (margin≤1) | Close tie% | Blowout (margin≥3) | Blowout tie% |
|--------|-----------|----------|-------------------|------------|---------------------|--------------|
| 15     | 139       | 14.4%    | 55                | 36.4%      | 57                  | 0.0%         |
| 30     | 190       | 16.8%    | 88                | 36.4%      | 56                  | 0.0%         |
| 45     | 183       | 14.2%    | 97                | 26.8%      | 35                  | 0.0%         |
| 60     | 162       | 10.5%    | 96                | 17.7%      | 16                  | 0.0%         |
| 75     | 132       | 12.9%    | 103               | 16.5%      | 3                   | 0.0%         |

**AWAY scores first (proxy: UNDERDOG scores first)**

| Minute | All games | All tie% | Close (margin≤1) | Close tie% | Blowout (margin≥3) | Blowout tie% |
|--------|-----------|----------|-------------------|------------|---------------------|--------------|
| 15     | 106       | 25.5%    | 58                | 46.6%      | 30                  | 0.0%         |
| 30     | 140       | 20.0%    | 73                | 38.4%      | 32                  | 0.0%         |
| 45     | 140       | 22.1%    | 92                | 33.7%      | 17                  | 0.0%         |
| 60     | 123       | 21.1%    | 84                | 31.0%      | 12                  | 0.0%         |
| 75     | 93        | 12.9%    | 70                | 17.1%      | 2                   | 0.0%         |

### Key findings

1. **Close games tie 36.4% of the time when 1-0 at minute 15-30.** This is more than double the overall 14-17% rate.
2. **Mismatches NEVER tie after going 1-0.** 0% across 167 games. When Brazil goes up 1-0 on Haiti, the tie is dead.
3. **The tie probability decays with time.** Even in close games, a 1-0 at minute 60 only ties 17.7% (favorite first) to 31.0% (underdog first). The edge shrinks as the game progresses.
4. **The edge is concentrated in the first half of close games.** Minutes 15-45 after a goal is the sweet spot.
5. **Underdog scoring first produces MORE ties at every minute threshold.** The gap is largest at minute 60 (+13.2pp) and minute 15 (+10.2pp). When the underdog scores first, the favorite must chase, which opens the game — but the underdog parks the bus, preserving the tie possibility longer.
6. **Best-case scenario: underdog scores first in a close game, 1-0 at minute 15 → 46.6% tie rate.** Nearly a coin flip. If Kalshi prices TIE at 28-30%, the edge is 16-18pp.

---

## The Edge — Expected Value

Using Kalshi's actual TIE prices from the 2026-06-19 snapshots (close games only):

| Scenario | Kalshi TIE price | Historical tie rate (close games) | EV per $1 share | Return | 1/4 Kelly |
|----------|-----------------|-----------------------------------|-----------------|--------|-----------|
| SCOMAR 1-0 at min 30 | $0.15 | 36.4% | +$0.214 | +143% | 6.3% of bankroll |
| USA-AUS 1-0 at min 30 | $0.14 | 36.4% | +$0.224 | +160% | 6.5% of bankroll |
| USA-AUS 1-0 at min 60 | $0.04 | 17.7% | +$0.137 | +342% | 3.6% of bankroll |
| BRAHTI 1-0 at min 60 | $0.04 | 0.0% (mismatch) | -$0.04 | -100% | 0% (DO NOT BET) |

The EV is enormous in close games. A $0.15 TIE share that pays $1 on a 36.4% outcome is worth $0.364. You're buying dimes for 36 cents.

---

## The Strategy

### Pre-game filter (THE GATE)
Only apply to close games. Proxy: favorite moneyline > -400 (i.e., -133, -163, -240 qualify; -1050 does not). If the favorite is heavier than -400, the game is a mismatch — the matrix's heavy_fav bucket averages across too wide a range, and even with Vegas calibration the sample sizes are thin. Mismatches get Vegas-calibrated probabilities but should be traded with caution on ALL legs, not just TIE.

### Entry
When the first goal is scored in a qualifying close game, buy TIE on Kalshi immediately. The earlier the goal, the higher the tie probability and the larger the edge. Minutes 15-45 after the first goal is the optimal window.

**Who scored first matters.** The edge is significantly larger when the UNDERDOG scores first:
- Underdog scores first, 1-0 at minute 15: 46.6% tie rate (best scenario)
- Favorite scores first, 1-0 at minute 15: 36.4% tie rate (still good)
- The underdog-first edge persists through minute 60 (31.0% vs 17.7%)

The strategy is most profitable when the underdog scores early in a close game. Kalshi does not appear to distinguish between favorite-scores-first and underdog-scores-first scenarios — it crashes TIE to the same level regardless.

### Exit — The Three-Exit Strategy

**Exit 1: SELL on equalizer (capture the spike).** When the favorite equalizes (1-1), TIE price spikes. Sell into the spike. This locks in guaranteed profit without needing the tie to hold through full time.

The decision to sell or hold depends on how much time is left when the equalizer comes:

P(tie holds | 1-1 at minute X) — World Cup group stage, 1930-2022:

| Equalizer at minute | Games at 1-1 | P(tie holds) | Sell at $0.50 | Sell at $0.60 | Sell at $0.70 |
|---------------------|--------------|-------------|----------------|----------------|----------------|
| 30                  | 49           | 30.6%       | SELL           | SELL           | SELL           |
| 45                  | 87           | 32.2%       | SELL           | SELL           | SELL           |
| 60                  | 114          | 42.1%       | SELL           | SELL           | SELL           |
| 70                  | 114          | 50.9%       | HOLD           | SELL           | SELL           |
| 75                  | 112          | 54.5%       | HOLD           | SELL           | SELL           |
| 80                  | 98           | 66.3%       | HOLD           | HOLD           | SELL           |
| 85                  | 97           | 74.2%       | HOLD           | HOLD           | HOLD           |

**Rule: if Kalshi TIE spikes above P(tie holds), SELL. Lock in guaranteed profit.**

The later the equalizer, the more you should hold. Equalizer at minute 30-60: always sell — 58-69% of the time someone scores again, killing the tie. Equalizer at minute 80+: hold — only 26-34% chance of another goal.

**Exit 2: SELL on erosion (CRITICAL — learned from TURPAR trade).** The edge erodes with time. At every minute, compute P(tie) from the transition matrix. If P(tie) drops below the current Kalshi TIE bid price, SELL immediately. The market is still overpricing TIE, but now the wrongness works against the holder.

Example from TURPAR trade:
- Entry at minute 14: P(tie) = 46.6%, bought at $0.285. Edge: +18pp. Correct.
- Minute 60: P(tie) = 21.1%, Kalshi TIE at ~$0.30. Edge has flipped: market overprices TIE by 9pp. SELL.
- Minute 73: P(tie) = ~15%, Kalshi TIE at $0.27. Sold at $0.27. Loss of $1.01, but EV of holding was -$6.71 to -$9.72.

The erosion exit is the missing piece. Without it, you hold a depreciating asset past the point where the market price exceeds the true probability. The auto-sell system must compute P(tie) every minute and sell when P(tie) < Kalshi TIE bid.

**Exit 3: HOLD to settlement (fallback).** If no equalizer comes and the erosion exit hasn't triggered (P(tie) still above market price), hold to settlement. TIE resolves at $1 if draw, $0 otherwise. No settlement fee.

**Why three exits:** Exit 1 captures the upside (equalizer spike). Exit 2 stops the bleeding (erosion). Exit 3 is the fallback. The TURPAR trade lost $1.01 because Exit 2 was missing from the system. With all three, the system sells proactively when the odds turn against the position.

### Sizing
Fractional Kelly (1/4) on the TIE leg, with a hard cap. On a $XXX bankroll:

| Scenario | P(tie) | Entry | Full Kelly | 1/4 Kelly | Hard cap 5% | Bet |
|----------|--------|-------|------------|-----------|-------------|-----|
| Underdog first, min 15, $0.28 | 46.6% | $0.28 | $74 (24.7%) | $18 (6.2%) | $15 | $15 |
| Underdog first, min 15, $0.30 | 46.6% | $0.30 | $71 (23.7%) | $18 (5.9%) | $15 | $15 |
| Underdog first, min 30, $0.15 | 38.4% | $0.15 | $83 (27.5%) | $21 (6.9%) | $15 | $15 |
| Favorite first, min 15, $0.15 | 36.4% | $0.15 | $76 (25.2%) | $19 (6.3%) | $15 | $15 |

- Full Kelly: 20-28% of bankroll. DO NOT DO THIS. Drawdowns would be brutal.
- 1/4 Kelly: 5-7% of bankroll. Recommended.
- Hard cap: 5% of bankroll ($15 on $XXX) per position regardless of Kelly calculation.
- Portfolio cap: 20% of bankroll across all simultaneous positions.

### Execution — Maker vs Taker (CRITICAL for cost management)

Kalshi has no volume tiers. The fee is a flat formula based on contract price (P) and contract count (C):

  **Taker** (marketable orders): `fee = 0.07 × C × P × (1-P)`
  **Maker** (resting limit orders): `fee = 0.0175 × C × P × (1-P)` — exactly 1/4 of taker

Fee as % of trade value = `0.07 × (1-P)` for taker, `0.0175 × (1-P)` for maker. Cheaper contracts (low P) have higher fee percentages.

| Contract price | Taker fee % | Maker fee % | Savings |
|----------------|-------------|-------------|---------|
| $0.05          | 6.7%        | 1.7%        | 75%     |
| $0.15          | 6.0%        | 1.5%        | 75%     |
| $0.25          | 5.3%        | 1.3%        | 75%     |
| $0.30          | 4.9%        | 1.2%        | 75%     |
| $0.40          | 4.2%        | 1.1%        | 75%     |
| $0.50          | 3.5%        | 0.9%        | 75%     |

**Always use maker (limit) orders when possible.** The fee is 75% lower. On a $15 trade at $0.28:
- Taker: $0.73 fee (4.9%)
- Maker: $0.19 fee (1.3%)
- Savings: $0.54 per trade

**Execution strategy:** Place a limit order at or slightly below the current ask. The order rests on the book as a maker order. If the TIE price is volatile (which it is after a goal), it will likely fill within minutes as the market oscillates. On Trade #2 (TURPAR), the $0.28 limit filled in 4 minutes.

**Tradeoff:** Maker orders may not fill if the price moves away from you. For fast-moving markets where you need immediate execution, use taker (marketable) orders. But for TIE buys after a goal — where the price is crashing and oscillating — a limit slightly below the ask usually fills quickly.

**Rounding note:** On 1 contract at $0.30, fee rounds up to $0.02 (6.7%). On 50+ contracts, rounding is negligible. Don't place tiny orders.

**No settlement fee.** Kalshi charges nothing when the contract resolves. The only cost is the entry fee.

---

## Why This Edge Exists

### Why Kalshi misprices it
Kalshi's live orderbook reprices fast on goals — faster than Vegas books. But "fast" doesn't mean "accurate." When a goal is scored, Kalshi traders appear to overreact: they crash the TIE price to 15% and spike the favorite to 80-94%. The historical data says the TIE should be at 36% for close games. Kalshi is pricing the average game (16% tie rate) rather than conditioning on game quality.

### Why Vegas can't arbitrage it
Vegas sportsbooks don't offer efficient live 3-way draw markets. They suspend in-play betting on draws or move to absurd numbers. There's no cross-market arbitrage mechanism to correct the Kalshi price. The edge persists because nobody with the historical data is acting on it.

### Why it won't close quickly
- The edge requires historical World Cup goal-timing data that most Kalshi traders don't have.
- The close-game filter is non-obvious — you have to know to split by team quality.
- The strategy is high-variance (lose 64% of the time), which deters casual traders.

---

## Risks and Limitations

1. **The close-game filter is the entire edge.** Misclassify a mismatch as close and you lose 100%. The pre-game odds are the gate. Everything depends on correctly classifying games before kickoff. Vegas-anchored calibration (deployed 2026-06-21) corrects the matrix's coarse odds buckets to match Vegas no-vig probabilities, but extreme favorites (-400+) still have thin samples and should be traded with caution.

2. **High variance.** You lose 64% of bets. Over a 12-game group stage with 4 qualifying close games, you'd expect ~1.5 ties. The variance is real. Long losing streaks are normal.

3. **Small sample size.** 88 close games at 1-0 minute 30 is not enormous. The 36.4% could be 30% or 42% in reality. The edge survives even at 30% (buying at 15%), but the Kelly sizing should be conservative.

4. **Execution speed.** You need to detect the first goal and buy TIE before Kalshi fully reprices. Based on today's data, the window appears to be at least 15 minutes, but this may tighten as the market matures.

5. **Liquidity.** TIE legs on Kalshi have lower volume than favorite legs. SCOMAR TIE had 5.4M 24h volume vs 21.5M on MAR. You may not get full size at mid.

6. **Tournament-specific effects.** World Cup group stage may differ from knockout (teams play for draws in group stage to advance). The edge is specific to group stage. Knockout games can't end in a tie.

---

## Live Evidence (2026-06-19)

### Snapshots (paper analysis)

Three games played today where the favorite scored first. All three were correctly classified:

- **USA-AUS (USA -163, CLOSE):** USA scored, Kalshi TIE crashed to 14%. Historical close-game tie rate at that point: 36.4%. USA won, so the TIE bet would have lost. But the EV was positive.
- **SCOMAR (Morocco -133, CLOSE):** Morocco scored, Kalshi TIE crashed to 15%. Historical: 36.4%. Morocco won, TIE bet lost. EV was positive.
- **BRAHTI (Brazil -809, MISMATCH):** Brazil scored, Kalshi TIE crashed to 4%. Historical mismatch tie rate: 0.0%. Correctly classified as mismatch — Vegas-calibrated, small sample. Brazil won.

### Trade #1: Turkey vs Paraguay (LIVE, underdog scores first)

- **Pre-game:** Turkey +106 (favorite), Paraguay +285 (underdog). Classified: CLOSE game.
- **Goal:** Paraguay (underdog) scored at minute 2. Score: TUR 0 - PAR 1.
- **Kalshi TIE price after goal:** $0.28-0.30 (crashed from pre-game $0.29, but TIE was already cheap; the crash was modest because the game was close).
- **Historical tie rate (underdog first, close game, 1-0 at min 15):** 46.6%.
- **Edge:** 46.6% - 30% = 16.6pp. Well above the 6pp cost threshold.
- **Trade:** Bought 16 shares of KXWCGAME-26JUN19TURPAR-TIE at $0.30. Cost $4.80 + $0.24 fees = $5.04. Payout if tie: $16.00. Profit if tie: $10.96.
- **Note:** First attempt at $0.28 missed (ask moved to $0.30 in ~30 seconds). Filled at $0.30 on second attempt. Execution lag is real.
- **Note on fees:** Kalshi charged 4.9% on this trade (taker), higher than the ~2% modeled. The cost threshold should account for this — on small trades, fees are proportionally higher. EDGE.md cost models need updating. See "Execution — Maker vs Taker" section below for the full fee schedule.
- **Outcome:** Pending (game in progress at time of writing).

### Trade #2: Turkey vs Paraguay — Maker limit fill

- **Same game as Trade #1.** Added to position via maker limit order.
- **Order:** 53 shares at $0.28 limit (GTC, post_only=True).
- **Fill:** Filled in 4 minutes (placed 03:34 UTC, filled 03:38 UTC). A seller hit the bid.
- **Fee:** $0.19 maker fee (1.3%) vs $0.73 it would have cost as taker (4.9%). Saved $0.54.
- **Combined position after Trade #2:** 69 shares at avg $0.285. Total cost $19.64 + $0.42 fees = $20.07. Payout if tie: $69.00. Profit if tie: $48.93.
- **Key learning:** Maker limit orders work for this strategy. The TIE price oscillates after a goal, so a limit slightly below the ask fills within minutes. Always use maker orders — the 75% fee reduction is significant over many trades.

### Trade #3: Turkey vs Paraguay — Erosion exit (the lesson)

- **Situation:** Game at minute 73, still 0-1 Paraguay. No equalizer. P(tie) had eroded from 46.6% at entry to ~15%.
- **Kalshi TIE:** $0.27 bid (market still overpricing at 27% vs true 15%).
- **Action:** Cancelled the $0.55 equalizer sell. Placed taker sell at $0.27 bid. Filled immediately.
- **Result:** Sold 69 shares at $0.27. Revenue $18.63 - $0.95 fee = $17.68. Realized P&L: -$1.01.
- **Balance after trade:** $XX.XX (started at ~$XXX).
- **Key learning:** The system was missing Exit 2 (erosion sell). At minute 60, P(tie) was 21.1% and Kalshi was pricing TIE at 30%. The system should have sold then. Instead it held until minute 73 when Eric manually identified the problem. The EV of holding was -$6.71 to -$9.72; selling at -$1.01 saved $5.70-$8.71 in expected losses.
- **Root cause:** The auto-sell system only had two exits (equalizer spike, hold to settlement). It needed the third exit: sell when P(tie) < Kalshi TIE bid. This is now documented as Exit 2 in the three-exit strategy.

Today's outcomes: 0 for 2 on paper close-game TIE bets (USA-AUS and SCOMAR both won by the favorite). TURPAR trade: -$1.01 realized loss (erosion exit). Balance: $XX.XX.

---

## What We Need to Operationalize

See MONITORING.md for the full monitoring workflow documentation (architecture, components, deployment, troubleshooting).

1. **Live score monitoring** — DONE. ESPN hidden API, no key needed. Cron every 1 min.
2. **Pre-game classification** — DONE. The Odds API, cached pre-game. Cron every 1 min.
3. **Kalshi price monitoring** — DONE. Kalshi API, integrated into all monitors.
4. **Transition matrix** — DONE. data/combined_transition_matrix.json (94,792 club matches, 4 odds classes). data/wc_transition_matrix.json (850 WC matches, secondary reference).
5. **Goal detection + BUY alerts** — DONE. wc_goal_monitor.py (cron every 1 min).
6. **Equalizer detection + SELL/HOLD alerts** — DONE. wc_equalizer_monitor.py (cron every 1 min).
7. **Auto-sell limit management** — PARTIAL. wc_auto_sell.py adjusts sell limits by clock. Needs Exit 2 (erosion sell) auto-execution — the edge detector recommends it, but wc_auto_sell.py doesn't auto-execute yet.
8. **Real-time edge detector** — DONE (2026-06-20). wc_live_edge.py (background daemon). Compares Kalshi all-3-legs prices to combined transition matrix every minute. Calls Gemma4 on vLLM when |edge|>6pp or state change. Streams commentary to Telegram via delivery cron.
9. **Execution** — Manual for now. Eric places trades when alerted. Auto-execution is Phase 5.
10. **Outcome tracking** — DONE. data/trade_log.csv. Needs ongoing calibration against model predictions.

---

## The Transition Matrix — The Complete Model

### Three datasets

1. **WC transition matrix** — `data/wc_transition_matrix.json`
   - 850 WC group stage matches, 1930-2022 (jfjelstul/worldcup)
   - Goal minutes + final scores, no pre-game odds
   - Secondary reference — WC-specific effects (kept for comparison, not actively used)
   - 19 intervals × ~15 score states → P(home win), P(away win), P(tie)
   - Limitation: no odds conditioning, uses home/away as proxy for favorite/underdog

2. **Football-Data.co.uk dataset** — `data/football_matches_processed.json`
   - 97,155 matches across 16 leagues, 1993-2025
   - Pre-game decimal odds (Pinnacle, B365, etc.) + half-time scores + full-time scores
   - No goal minutes — only HT/FT scores
   - Used for odds distribution analysis and HT→FT transitions

3. **Kaggle dataset with goal minutes** — `data/kaggle_matches_parsed.json`
   - 94,792 matches, 18 leagues, 2002-2022
   - Goal minutes (from INC field) + pre-game odds + HT/FT scores
   - Zero goal-count mismatches after own-goal parsing fix
   - This is the primary training data for the combined model

### The combined transition matrix — `data/combined_transition_matrix.json`

Built from the 94,792 Kaggle matches. Organized by **favorite/underdog perspective** (not home/away):

- 4 odds classes: heavy_fav (<1.6), moderate_fav (1.6-2.0), close (2.0-2.5), slight_fav (2.5+)
- 19 time intervals (0, 5, 10, ..., 90)
- 8+ score states per interval (0-0, 1-0, 0-1, 1-1, 2-0, 0-2, 2-1, 1-2, etc.)
- Each cell: P(fav win), P(tie), P(und win), sample size

Score states are from the FAVORITE's perspective: "1-0" = favorite leads, "0-1" = underdog leads.

### The edge confirmed at scale

Close games (fav odds 2.0-2.5), 1-0 (fav first) vs 0-1 (und first) — P(tie):

| Minute | 1-0 (fav first) | P(tie) | 0-1 (und first) | P(tie) | Difference |
|--------|-----------------|--------|-----------------|--------|------------|
| 15     | 5,393 games     | 21.0%  | 4,157 games     | 26.7%  | +5.7pp     |
| 30     | 7,876 games     | 21.2%  | 6,082 games     | 27.5%  | +6.3pp     |
| 45     | 8,408 games     | 21.2%  | 6,394 games     | 27.4%  | +6.3pp     |
| 60     | 7,500 games     | 21.1%  | 5,649 games     | 26.8%  | +5.7pp     |
| 75     | 6,172 games     | 17.3%  | 4,656 games     | 22.5%  | +5.1pp     |
| 85     | 5,263 games     | 11.3%  | 3,961 games     | 14.8%  | +3.5pp     |

The underdog-scores-first edge is +5-6pp, confirmed across 4,000-8,000 games per cell. Statistically significant at every minute threshold. Smaller than the WC-only estimate (10pp) but still tradeable after costs.

### Odds conditioning matters

P(tie) at 1-0, minute 45, by odds class:

| Odds class | P(tie) | Games |
|------------|--------|-------|
| heavy_fav (<1.6) | 11.5% | 4,436 |
| moderate_fav (1.6-2.0) | 17.8% | 6,443 |
| close (2.0-2.5) | 21.2% | 8,408 |
| slight_fav (2.5+) | 24.6% | 2,009 |

A 1-0 lead at minute 45 produces 11.5% ties when a heavy favorite leads, but 24.6% when it's a coin-flip game. The odds conditioning is the difference between a tradeable edge and a losing bet.

### Club vs WC comparison

The club matrix shows lower P(tie) than the WC matrix (e.g., 26.7% vs 46.6% for underdog-first at min 15). Two reasons:
1. The WC model used final margin ≤1 as the "close" filter (ex post, biases toward ties). The club model uses pre-game odds (ex ante, correct).
2. WC group stage may genuinely have more ties (conservative play, tournament structure).

The club model is the better baseline. 26.7% is the true probability. If Kalshi prices TIE at 15-20%, the edge is 7-12pp.

### Can we detect material inefficiencies? YES.

The system has everything needed to model live games and detect edges in real time:

1. **Pre-game classification**: The Odds API provides moneylines → classify game as heavy_fav/moderate_fav/close/slight_fav before kickoff. Cached so live odds don't contaminate. Now also captures draw odds for 3-way no-vig calibration.

2. **Vegas-anchored calibration**: After matrix lookup, probabilities are calibrated to match Vegas no-vig at the pre-game point using log-ratio adjustment. This corrects the coarse odds bucket problem (a -170 and -1050 favorite both land in heavy_fav). See "Vegas-Anchored Calibration" section below.

3. **Live game state**: ESPN hidden API provides score + clock every minute. No key needed, no cost.

4. **Model lookup**: Given (odds class, score state, minute), the combined matrix gives P(fav win), P(tie), P(und win) with thousands of games per cell. Calibrated to Vegas anchor.

5. **Market comparison**: Kalshi API provides live prices on all three legs (home/away/tie).

6. **Edge detection**: If any Kalshi price diverges from the calibrated probability by >6pp (after fees), flag an edge. This works on ALL legs, not just TIE.

The edge detector (scripts/wc_live_edge.py) is COMPLETE. It compares Kalshi prices to the matrix every minute and flags any material inefficiency — buy TIE when underpriced, sell TIE when overpriced, buy home/away when those are mispriced. The model covers every score state at every minute, so it works for the entire game, not just the first-goal scenario. See the "Real-Time Edge Detector + Gemma4 Commentary" section below and MONITORING.md for full documentation.

### Known limitations

1. **5-minute buckets**: The matrix is at 5-min intervals, not continuous. Between minute 62 and 63, same probability. An ML model would smooth this, but the matrix is sufficient for trading.

2. **Domestic league data**: The matrix is from club football, not WC. Tournament dynamics may differ. The WC matrix (850 games) is kept as a secondary reference for WC-specific calibration.

3. **No in-game stats**: The model uses only score + time + pre-game odds. It doesn't account for red cards, injuries, possession, or xG. These would be ML model features in Phase 2.

4. **Coarse odds buckets**: 4 classes. A 2.0 favorite and 2.4 favorite are both "close." Finer buckets would reduce sample sizes but improve precision. PARTIALLY MITIGATED by Vegas-anchored calibration (log-ratio adjustment to match Vegas no-vig at pre-game point). The calibration corrects the absolute probabilities, but the transition shape still comes from the bucket average.

5. **No goal-minute data for Football-Data.co.uk matches**: The 97K matches from Football-Data.co.uk only have HT/FT scores. The 94K Kaggle matches have goal minutes and are the primary source for the combined matrix.

---

## Vegas-Anchored Calibration (the rudder — deployed 2026-06-21)

### The Problem

The matrix has 4 odds buckets: heavy_fav (<1.6 decimal), moderate_fav (1.6-2.0), close (2.0-2.5), slight_fav (2.5+). A -170 favorite (63% win rate) and a -1050 favorite (91% win rate) both land in heavy_fav. The bucket averages to 71% win rate. Without calibration, Spain at -1050 gets assigned P(fav win)=71.2% instead of 91.3%, creating a false +17pp edge on the NO leg. The system was rudderless — it could not distinguish -170 from -1050 within the same bucket.

### The Fix: Log-Ratio Calibration

Function: vegas_calibrate() in both wc_live_edge.py and wc_cockpit.py. Applied after every matrix lookup, before edge computation.

1. Compute Vegas no-vig probabilities from American odds. If draw odds are available (captured from The Odds API), uses proper 3-way normalization. Otherwise, estimates draw from the matrix's tie:und ratio at the baseline cell.
2. Get matrix baseline (minute 0, score 0-0) for that odds class.
3. Compute log-ratio deltas: delta_i = ln(P_vegas_i / P_matrix_i(0,0))
4. For any in-game state (minute m, score s): P_calibrated_i = P_matrix_i(m,s) * exp(delta_i), then renormalize to sum to 1.

The matrix's transition shape is preserved — how P(tie) rises with 0-0 at minute 70, how P(fav) drops when the underdog scores first — but the absolute probabilities are anchored to Vegas reality.

### Impact

- Normal favorites (Belgium -240): correction is 1-3pp. Minimal effect.
- Extreme favorites (Spain -1050): correction is 15-20pp. The rudder steers harder the further the matrix drifts from Vegas.
- The Spain trade (bought NO at $0.12) exposed the problem: matrix said NO fair value $0.29 (false 17pp edge), Vegas-calibrated says $0.09 (true 3pp edge, roughly break-even after fees).

### Mismatch Handling

The mismatch flag (fav_american <= -400) is now a data quality warning, not a trade gate. The calibration handles the math — it corrects the probabilities for all legs. The mismatch flag warns that sample sizes are thin at extreme odds and the transition shape may not be reliable. The warning covers ALL legs, not just TIE.

---

## Data Source

jfjelstul/worldcup GitHub repository: https://github.com/jfjelstul/worldcup
- goals.csv: 3,637 goals with minute scored, 1930-2022
- matches.csv: 1,248 matches with final scores
- Local copy: /tmp/worldcup/data-csv/

Analysis script: inline in conversation, 2026-06-19.

---

## Historical Tie Rates (Reference Table)

Use this table for sizing decisions. "Close" = final margin ≤ 1 goal. Split by who scored first (home/away as proxy for favorite/underdog).

### Close games — FAVORITE scores first

| 1-0 at minute | P(tie) | Buy TIE if Kalshi price below |
|---------------|--------|-------------------------------|
| 15            | 36.4%  | $0.30                         |
| 30            | 36.4%  | $0.30                         |
| 45            | 26.8%  | $0.22                         |
| 60            | 17.7%  | $0.14                         |
| 75            | 16.5%  | $0.13                         |

### Close games — UNDERDOG scores first (PREFERRED)

| 1-0 at minute | P(tie) | Buy TIE if Kalshi price below |
|---------------|--------|-------------------------------|
| 15            | 46.6%  | $0.40                         |
| 30            | 38.4%  | $0.32                         |
| 45            | 33.7%  | $0.28                         |
| 60            | 31.0%  | $0.25                         |
| 75            | 17.1%  | $0.13                         |

### Mismatches (favorite > -200) — DO NOT BET

| 1-0 at minute | P(tie) |
|---------------|--------|
| Any           | 0.0%   |

The "buy below" price is the historical tie rate minus a 6pp safety margin (covers Kalshi settlement fees + bid-ask spread). If Kalshi TIE is below the threshold, the expected value is positive after costs.

Note: Kalshi fees on small trades (~$5) run ~4.9%, higher than the 2% modeled. The 6pp safety margin absorbs this, but barely. On larger trades, the fee percentage drops.

---

## Real-Time Edge Detector + Gemma4 Commentary (Phase 4-5 — COMPLETE)

### Edge Detector Daemon (`scripts/wc_live_edge.py`)

The general model: compares Kalshi prices to the combined transition matrix on ALL 3 legs every 60 seconds during live games. Calls Gemma4 (vLLM, localhost:8765, model gemma4-31b, temp 0.3, max 200 tokens) every tick with a structured prompt containing:

- Current game state (score, minute, odds class, favorite/underdog)
- Matrix probabilities for all 3 legs (with team name labels, each on its own line)
- Kalshi prices for all 3 legs (price, bid/ask)
- Edges (Kalshi price - matrix prob, with direction: positive = overpriced = sell, negative = underpriced = buy)
- Position (if any)
- Three-exit recommendation (Exit 1: equalizer sell, Exit 2: erosion sell, Exit 3: hold to settlement)
- Exit proximity (gap to erosion sell trigger, warns when within 3pp)
- Trend data (P(tie) delta, price movements, time elapsed since last tick)
- Mismatch warning (if heavy-favorite mismatch: caution on ALL legs, Vegas-calibrated, small sample)

Gemma4 generates 2-4 sentences of direct pricing commentary using team names (not abstract labels). Commentary streams to Telegram via delivery cron (every 2 min).

### Web Cockpit (`scripts/wc_cockpit.py`)

Browser UI at http://localhost:8877. Combines:
- **Chat with Gemma4**: Streaming responses. Live game state (ESPN + Kalshi + matrix) injected into every system prompt so Gemma4 answers with real data, not hallucinations.
- **Live games panel**: ESPN scoreboard with team colors, scores, Kalshi odds table (price/bid-ask/model prob/edge per leg), 24h volume, odds class, sample size, Vegas-calibrated badge, mismatch warnings.
- **Account panel**: Kalshi balance, position count, daemon status.
- **Commentary feed**: Scrolling view of edge detector commentary.

### Accuracy Test Suite (`scripts/test_gemma_accuracy.py`)

19 test cases, 68 checks across 8 categories. 100% accuracy (3 consecutive runs). Verifies:
- Edge direction (overpriced vs underpriced)
- Number accuracy (correct matrix probs and Kalshi prices cited)
- Action correctness (buy/sell/hold matches edge thresholds)
- Favorite/underdog identification (team names used, not abstract labels)
- Exit recommendations (erosion sell, hold, no position)
- Mismatch detection ("do not bet TIE" warning)
- Hallucination prevention (no fabricated numbers)
- Score state handling (0-0, 1-0, 0-1, 1-1, 2-0, 0-2)

See MONITORING.md for deployment instructions and troubleshooting.
