#!/usr/bin/env python3
"""
wc_live_edge.py — Real-time World Cup edge detector with Gemma4 streaming commentary.

Runs as a CONTINUOUS LOOP during live WC games (not a cron job). Every 60 seconds it:
  1. Pulls ESPN scoreboard for live games
  2. For each live game: pulls ESPN summary (score, clock, goals)
  3. Pulls Kalshi prices on all 3 legs (home/away/tie)
  4. Looks up the combined transition matrix: (odds class, score state, minute) ->
     P(fav win), P(tie), P(und win)
  5. Computes edge = Kalshi price - matrix probability for each leg
  6. If any |edge| > 6pp OR a state change (goal, equalizer, kickoff, settlement),
     sends the observation to Gemma4 (vLLM, localhost:8765) for natural-language
     pricing dialogue.
  7. Prints commentary to stdout AND appends to data/live_edge_commentary.log
     (the log is the delivery queue for the Telegram cron).

Self-gates: if no games are live, sleeps 5 minutes and rechecks.

Usage:
  python3 wc_live_edge.py                  # continuous loop (production)
  python3 wc_live_edge.py --dry-run        # show Gemma4 prompt, don't call the API
  python3 wc_live_edge.py --once           # single tick, then exit
  python3 wc_live_edge.py --simulate       # inject a fake live game (testing)
  python3 wc_live_edge.py --verbose        # print state every tick, not just alerts
"""
import os, sys, json, csv, subprocess, urllib.request, time, argparse, re, math
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.join(SCRIPT_DIR, '..')
MATRIX_FILE = os.path.join(PROJECT_DIR, 'data', 'combined_transition_matrix.json')
COMMENTARY_LOG = os.path.join(PROJECT_DIR, 'data', 'live_edge_commentary.log')
STATE_FILE = '/tmp/wc_live_edge_state.json'
ET = timezone(timedelta(hours=-4))  # Kalshi event tickers use ET dates

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
EDGE_THRESHOLD_PP = 6.0        # highlight in header if any |edge| exceeds this
COMMENTARY_INTERVAL = 60       # call Gemma4 every tick during live games (every 60s)
COOLDOWN_SECONDS = 60          # min seconds between calls for same game (prevents double-fire)
POLL_INTERVAL_LIVE = 60        # seconds between ticks when games are live
POLL_INTERVAL_IDLE = 300       # seconds between ticks when no games live
VLLM_URL = "http://localhost:8765/v1/chat/completions"
VLLM_MODEL = "gemma4-31b"
VLLM_TIMEOUT = 45

# ---------------------------------------------------------------------------
# Odds-class classification (matches the combined matrix: 4 classes)
#   heavy_fav:     fav decimal odds < 1.6
#   moderate_fav:  1.6 .. 2.0
#   close:         2.0 .. 2.5
#   slight_fav:    2.5+   (underdog-leaning, "slight favorite" = coin-flip-or-upset)
# ---------------------------------------------------------------------------
def american_to_decimal(american):
    if american is None:
        return None
    a = int(american)
    if a > 0:
        return round((a / 100.0) + 1.0, 3)
    elif a < 0:
        return round((100.0 / abs(a)) + 1.0, 3)
    return 2.0  # +100 / -100 → 2.0

def classify_odds_class(fav_decimal):
    """Map favorite's decimal odds to one of the 4 matrix odds classes."""
    if fav_decimal is None:
        return 'close'  # safe default — the matrix's most-populated class
    if fav_decimal < 1.6:
        return 'heavy_fav'
    elif fav_decimal < 2.0:
        return 'moderate_fav'
    elif fav_decimal <= 2.5:
        return 'close'
    else:
        return 'slight_fav'

# ---------------------------------------------------------------------------
# Vegas-anchored calibration — the rudder
# ---------------------------------------------------------------------------
def american_to_implied(american):
    """Convert American odds to vigged implied probability."""
    if american is None:
        return None
    a = int(american)
    if a > 0:
        return 100.0 / (a + 100.0)
    elif a < 0:
        return abs(a) / (abs(a) + 100.0)
    return 0.5  # +100 / -100

def vegas_calibrate(matrix_probs, matrix, odds_class, fav_american, und_american, draw_american=None):
    """Vegas-anchored calibration of matrix probabilities.

    Shifts the matrix probability surface so the pre-game point matches Vegas
    no-vig probabilities, while preserving the matrix's transition shape.

    Uses log-ratio calibration: for each outcome i,
        adjusted_p_i = matrix_p_i(m,s) * exp(delta_i)
    where delta_i = ln(p_vegas_i / p_matrix_i(0,0))
    Then renormalizes to sum to 1.

    If draw_american is available, uses proper 3-way no-vig.
    Otherwise, estimates draw from matrix's tie:und ratio.

    Returns calibrated dict (copy of matrix_probs with adjusted fav_win/tie/und_win)
    or the original matrix_probs if calibration is not possible.
    """
    if not fav_american or not und_american:
        return matrix_probs

    # Get matrix baseline (minute 0, score 0-0)
    cls = matrix.get(odds_class) or matrix.get('close')
    if not cls or '0' not in cls or '0-0' not in cls['0']:
        return matrix_probs
    baseline = cls['0']['0-0']
    p_fav_m0 = baseline.get('fav_win', 0)
    p_tie_m0 = baseline.get('tie', 0)
    p_und_m0 = baseline.get('und_win', 0)
    if p_fav_m0 <= 0 or p_tie_m0 <= 0 or p_und_m0 <= 0:
        return matrix_probs

    # Compute Vegas no-vig probabilities
    p_fav_v = american_to_implied(fav_american)
    p_und_v = american_to_implied(und_american)
    if not p_fav_v or not p_und_v:
        return matrix_probs

    if draw_american is not None:
        # Proper 3-way no-vig
        p_tie_v = american_to_implied(draw_american)
        if p_tie_v:
            total = p_fav_v + p_tie_v + p_und_v
            p_fav_vegas = p_fav_v / total
            p_tie_vegas = p_tie_v / total
            p_und_vegas = p_und_v / total
        else:
            draw_american = None  # fall through to ratio method

    if draw_american is None:
        # Estimate draw from matrix tie:und ratio
        # Use p_fav_v directly (vig on favorite is small, 1-2pp for extremes)
        p_fav_vegas = p_fav_v
        remainder = 1.0 - p_fav_vegas
        tie_und_ratio = p_tie_m0 / (p_tie_m0 + p_und_m0)
        p_tie_vegas = remainder * tie_und_ratio
        p_und_vegas = remainder * (1.0 - tie_und_ratio)

    # Compute log-ratio corrections
    delta_fav = math.log(p_fav_vegas / p_fav_m0)
    delta_tie = math.log(p_tie_vegas / p_tie_m0)
    delta_und = math.log(p_und_vegas / p_und_m0)

    # Apply to current matrix probs
    raw_fav = matrix_probs['fav_win'] * math.exp(delta_fav)
    raw_tie = matrix_probs['tie'] * math.exp(delta_tie)
    raw_und = matrix_probs['und_win'] * math.exp(delta_und)
    total = raw_fav + raw_tie + raw_und
    if total <= 0:
        return matrix_probs

    calibrated = dict(matrix_probs)  # preserve sample, state_used, minute_bucket
    calibrated['fav_win'] = raw_fav / total
    calibrated['tie'] = raw_tie / total
    calibrated['und_win'] = raw_und / total
    calibrated['calibrated'] = True
    calibrated['vegas_fav'] = p_fav_vegas
    calibrated['vegas_tie'] = p_tie_vegas
    calibrated['vegas_und'] = p_und_vegas
    calibrated['delta_fav'] = delta_fav
    calibrated['delta_tie'] = delta_tie
    calibrated['delta_und'] = delta_und
    return calibrated

# ---------------------------------------------------------------------------
# Team name normalization (ESPN ↔ The Odds API)
# ---------------------------------------------------------------------------
def normalize_team_name(name):
    name = (name or '').strip()
    repl = {
        'Türkiye': 'Turkey', 'Türki̇ye': 'Turkey',
        'Republic of Korea': 'South Korea', 'Korea Republic': 'South Korea',
        'IR Iran': 'Iran', 'Czechia': 'Czech Republic',
        "Côte d'Ivoire": 'Ivory Coast', 'United States': 'USA',
    }
    return repl.get(name, name)

# ---------------------------------------------------------------------------
# ESPN hidden API
# ---------------------------------------------------------------------------
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event={}"

def http_get_json(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)

def get_espn_scoreboard():
    return http_get_json(ESPN_SCOREBOARD)

def get_espn_summary(event_id):
    return http_get_json(ESPN_SUMMARY.format(event_id), timeout=15)

def parse_clock_minute(clock_str):
    """Parse "66'" or "45'+3'" → integer minute (regulation + stoppage)."""
    if not clock_str:
        return 0
    clean = clock_str.replace("'", "").replace("+", " ").strip()
    parts = clean.split()
    try:
        m = int(parts[0])
        if len(parts) > 1:
            m += int(parts[1])
        return m
    except (ValueError, IndexError):
        return 0

def parse_game_state(summary):
    c = summary['header']['competitions'][0]
    comps = c.get('competitors', [])
    home = [t for t in comps if t.get('homeAway') == 'home']
    away = [t for t in comps if t.get('homeAway') == 'away']
    clock_str = c.get('status', {}).get('displayClock', '0')
    goals = []
    for g in c.get('details', []):
        if g.get('scoringPlay'):
            minute_val = g.get('clock', {}).get('value', 0)
            goals.append({
                'minute': g.get('clock', {}).get('displayValue', '?'),
                'minute_reg': int(minute_val) // 60 if minute_val else 0,
                'team': g.get('team', {}).get('displayName', '?'),
                'team_abbr': g.get('team', {}).get('abbreviation', '?'),
                'is_home': g.get('home_team') == '1',
                'scorer': g.get('participants', [{}])[0].get('athlete', {}).get('displayName', '?'),
            })
    return {
        'event_id': str(summary['header']['id']),
        'home_team': home[0]['team']['displayName'] if home else '?',
        'away_team': away[0]['team']['displayName'] if away else '?',
        'home_abbr': home[0]['team'].get('abbreviation', '?') if home else '?',
        'away_abbr': away[0]['team'].get('abbreviation', '?') if away else '?',
        'home_score': int(home[0]['score']) if home else 0,
        'away_score': int(away[0]['score']) if away else 0,
        'clock': clock_str,
        'minute': parse_clock_minute(clock_str),
        'status': c.get('status', {}).get('type', {}).get('description', '?'),
        'state': c.get('status', {}).get('type', {}).get('state', '?'),
        'start_time': summary['header']['competitions'][0].get('date', ''),
        'goals': goals,
    }

# ---------------------------------------------------------------------------
# The Odds API (pre-game classification — cached, never overwritten once live)
# ---------------------------------------------------------------------------
def get_odds_key():
    return subprocess.check_output(["pass", "show", "oddsdotcom"]).decode().strip()

def pull_odds_api():
    key = get_odds_key()
    url = ("https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds/"
           f"?apiKey={key}&regions=us&markets=h2h&oddsFormat=american")
    return http_get_json(url, timeout=30)

def classify_game_from_odds(game):
    """Return (classification_4class, fav_name, und_name, fav_american, und_american, fav_decimal, is_mismatch, draw_american)."""
    outcomes = {}
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] != "h2h":
                continue
            for o in mkt["outcomes"]:
                outcomes.setdefault(o["name"], []).append(o["price"])
    medians = {name: sorted(ps)[len(ps)//2] for name, ps in outcomes.items() if name != "Draw"}
    if len(medians) < 2:
        return 'close', None, None, None, None, None, None, None
    teams = sorted(medians.items(), key=lambda x: x[1])  # most negative first = favorite
    fav_name, fav_american = teams[0]
    und_name, und_american = teams[1]
    fav_decimal = american_to_decimal(fav_american)
    odds_class = classify_odds_class(fav_decimal)
    fav_name = normalize_team_name(fav_name)
    und_name = normalize_team_name(und_name)
    # 'mismatch' gate: heavy favorite (american <= -400, decimal < 1.25) → DO NOT TRADE TIE
    is_mismatch = fav_american is not None and fav_american <= -400
    # Capture draw odds for 3-way no-vig calibration
    draw_prices = outcomes.get("Draw", [])
    draw_american = sorted(draw_prices)[len(draw_prices)//2] if draw_prices else None
    return (odds_class, fav_name, und_name, fav_american, und_american, fav_decimal, is_mismatch, draw_american)

# ---------------------------------------------------------------------------
# Kalshi — all 3 legs
# ---------------------------------------------------------------------------
def get_kalshi_client():
    sys.path.insert(0, SCRIPT_DIR)
    from kalshi_auth import KalshiClient
    return KalshiClient()

def get_kalshi_markets(kc, event_ticker):
    """Return dict: {'HOME_CODE': {...}, 'TIE': {...}, 'AWAY_CODE': {...}}."""
    r = kc.get('/markets', params={'event_ticker': event_ticker, 'limit': 20})
    out = {}
    for m in r.get('markets', []):
        # ticker like KXWCGAME-26JUN20NEDSWE-NED / -TIE / -SWE
        suffix = m['ticker'].rsplit('-', 1)[-1]
        out[suffix] = {
            'ticker': m['ticker'],
            'last': float(m.get('last_price_dollars') or 0),
            'bid': float(m.get('yes_bid_dollars') or 0),
            'ask': float(m.get('yes_ask_dollars') or 0),
            'vol24h': float(m.get('volume_24h_fp') or 0),
        }
    return out

def get_kalshi_positions(kc):
    """Return dict: market_ticker -> {shares, cost, fees}."""
    try:
        pos = kc.get('/portfolio/positions')
    except Exception:
        return {}
    out = {}
    for mp in pos.get('market_positions', []):
        out[mp.get('ticker')] = {
            'shares': float(mp.get('position_fp') or 0),
            'cost': float(mp.get('total_traded_dollars') or 0),
            'fees': float(mp.get('fees_paid_dollars') or 0),
        }
    return out

def load_trade_log_positions():
    """Aggregate TIE fills from trade_log.csv by event_ticker."""
    trade_log = os.path.join(PROJECT_DIR, 'data', 'trade_log.csv')
    if not os.path.exists(trade_log):
        return {}
    positions = {}
    for row in csv.DictReader(open(trade_log)):
        if row.get('side_code') != 'TIE' or not row.get('event_ticker'):
            continue
        ticker = row['event_ticker']
        shares = float(row.get('shares', 0) or 0)
        price = float(row.get('fill_price', 0) or 0)
        p = positions.setdefault(ticker, {'shares': 0.0, 'avg_price': 0.0, 'game': row.get('game', '')})
        total = p['shares'] + shares
        if total:
            p['avg_price'] = (p['shares'] * p['avg_price'] + shares * price) / total
        p['shares'] = total
    return positions

# ---------------------------------------------------------------------------
# Combined transition matrix
# ---------------------------------------------------------------------------
def load_matrix():
    return json.load(open(MATRIX_FILE))

def bucket_minute(minute):
    """Snap to nearest 5-min interval [0, 90]."""
    b = (int(minute) // 5) * 5
    return min(max(b, 0), 90)

def find_nearest_state(states, fav_score, und_score):
    """Find the exact score state in the matrix, or the nearest one if missing."""
    target = f"{fav_score}-{und_score}"
    if target in states:
        return target
    # Fall back: try states with the same goal difference, then closest total goals
    target_gd = fav_score - und_score
    target_total = fav_score + und_score
    candidates = []
    for s in states:
        try:
            f, u = s.split('-')
            gd = int(f) - int(u)
            total = int(f) + int(u)
        except (ValueError, IndexError):
            continue
        # Prefer same goal difference, then closest total goals
        score = (abs(gd - target_gd) * 100) + abs(total - target_total)
        candidates.append((score, s))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]

def lookup_matrix(matrix, odds_class, minute, fav_score, und_score):
    """Return {fav_win, tie, und_win, sample} or None."""
    cls = matrix.get(odds_class) or matrix.get('close')
    if not cls:
        return None
    mb = bucket_minute(minute)
    cell = cls.get(str(mb))
    if not cell:
        # try nearest minute bucket
        for delta in (5, -5, 10, -10):
            alt = str(min(max(mb + delta, 0), 90))
            cell = cls.get(alt)
            if cell:
                break
    if not cell:
        return None
    state = find_nearest_state(cell, fav_score, und_score)
    if not state:
        return None
    entry = cell[state]
    return {
        'fav_win': entry.get('fav_win', 0),
        'tie': entry.get('tie', 0),
        'und_win': entry.get('und_win', 0),
        'sample': entry.get('sample', 0),
        'state_used': state,
        'minute_bucket': mb,
    }

# ---------------------------------------------------------------------------
# Kalshi event ticker derivation
# ---------------------------------------------------------------------------
def derive_event_ticker(gs, fav_name, und_name):
    """Derive KXWCGAME-26{MON}{DD}{HOMECODE}{AWAYCODE} from ESPN game state."""
    sys.path.insert(0, SCRIPT_DIR)
    from team_codes import NAME_TO_CODE
    home_norm = normalize_team_name(gs['home_team'])
    away_norm = normalize_team_name(gs['away_team'])
    hcode = NAME_TO_CODE.get(home_norm)
    acode = NAME_TO_CODE.get(away_norm)
    if not hcode or not acode:
        return None, None
    try:
        ct = datetime.fromisoformat(gs['start_time'].replace('Z', '+00:00')).astimezone(ET)
    except Exception:
        return None, None
    kalshi_date = f"26{ct.strftime('%b').upper()}{ct.strftime('%d').upper()}"
    return f"KXWCGAME-{kalshi_date}{hcode}{acode}", {'home': hcode, 'away': acode}

# ---------------------------------------------------------------------------
# Edge computation
# ---------------------------------------------------------------------------
def compute_edges(matrix_probs, kalshi_markets, home_is_fav):
    """For each leg (fav/und/tie), compute edge = kalshi_price - matrix_prob.

    Kalshi markets are keyed by team code suffix (home code, 'TIE', away code).
    We map fav/und to home/away based on home_is_fav.
    Returns dict with per-leg price, prob, edge_pp, and 'sample'.
    """
    fav_code_key = 'home' if home_is_fav else 'away'
    und_code_key = 'away' if home_is_fav else 'home'
    # The market suffix is the team code; we need to resolve it from codes
    # But we only have the markets dict keyed by suffix. We'll resolve outside.
    # Here we assume kalshi_markets already has 'fav', 'und', 'tie' keys resolved.
    result = {}
    p_fav = matrix_probs['fav_win']
    p_tie = matrix_probs['tie']
    p_und = matrix_probs['und_win']
    fav_m = kalshi_markets.get('fav', {})
    und_m = kalshi_markets.get('und', {})
    tie_m = kalshi_markets.get('tie', {})
    fav_price = fav_m.get('last') or fav_m.get('mid') or 0
    und_price = und_m.get('last') or und_m.get('mid') or 0
    tie_price = tie_m.get('last') or tie_m.get('mid') or 0
    result['fav'] = {'price': fav_price, 'prob': p_fav, 'edge_pp': (fav_price - p_fav) * 100,
                     'bid': fav_m.get('bid'), 'ask': fav_m.get('ask'), 'ticker': fav_m.get('ticker')}
    result['tie'] = {'price': tie_price, 'prob': p_tie, 'edge_pp': (tie_price - p_tie) * 100,
                     'bid': tie_m.get('bid'), 'ask': tie_m.get('ask'), 'ticker': tie_m.get('ticker')}
    result['und'] = {'price': und_price, 'prob': p_und, 'edge_pp': (und_price - p_und) * 100,
                     'bid': und_m.get('bid'), 'ask': und_m.get('ask'), 'ticker': und_m.get('ticker')}
    return result

def resolve_kalshi_legs(kalshi_markets, home_code, away_code, home_is_fav):
    """Reshape markets dict (keyed by suffix) into fav/und/tie keys."""
    fav_suffix = home_code if home_is_fav else away_code
    und_suffix = away_code if home_is_fav else home_code
    return {
        'fav': kalshi_markets.get(fav_suffix, {}),
        'und': kalshi_markets.get(und_suffix, {}),
        'tie': kalshi_markets.get('TIE', {}),
    }

# ---------------------------------------------------------------------------
# Three-exit recommendation
# ---------------------------------------------------------------------------
def recommend_exit(edges, position, minute, score_state, odds_class):
    """Return (exit_label, reason) based on the three-exit strategy."""
    if not position or position.get('shares', 0) <= 0:
        return ('NO_POSITION', 'No TIE position held.')
    tie = edges['tie']
    shares = position['shares']
    entry = position.get('avg_price', 0)
    tie_bid = tie.get('bid') or 0
    tie_prob = tie['prob']
    # Exit 2 (erosion): market overprices TIE relative to true prob → sell
    if tie_bid > tie_prob + 0.03:
        sell_proceeds = tie_bid * shares
        cost_basis = entry * shares
        return ('EXIT_2_EROSION_SELL',
                f"Kalshi TIE bid {tie_bid*100:.0f}% > matrix P(tie) {tie_prob*100:.0f}% + 3pp. "
                f"Sell {shares:.0f} shares at ${tie_bid:.2f} → ${sell_proceeds:.2f} "
                f"(cost ${cost_basis:.2f}, P&L ${sell_proceeds-cost_basis:+.2f}).")
    # Exit 1 (equalizer spike): if score is 1-1 and tie bid is rich
    if score_state in ('1-1', '2-2', '3-3'):
        if tie_bid > tie_prob + 0.03:
            return ('EXIT_1_EQUALIZER_SELL',
                    f"Equalizer state {score_state}. TIE bid {tie_bid*100:.0f}% > "
                    f"P(tie holds) {tie_prob*100:.0f}% + 3pp. Sell into the spike.")
        else:
            return ('HOLD_THROUGH_EQUALIZER',
                    f"Equalizer at {score_state}, but TIE bid {tie_bid*100:.0f}% ≤ "
                    f"P(tie holds) {tie_prob*100:.0f}% + 3pp. Hold for settlement.")
    # Exit 3 (hold to settlement): tie prob still above market
    if tie_prob > tie_bid:
        return ('EXIT_3_HOLD',
                f"Matrix P(tie) {tie_prob*100:.0f}% > Kalshi TIE bid {tie_bid*100:.0f}%. "
                f"Hold to settlement (tie pays $1, currently valued at ${tie_bid:.2f}).")
    return ('MONITOR', f"TIE bid {tie_bid*100:.0f}% vs P(tie) {tie_prob*100:.0f}%. Within margin.")

# ---------------------------------------------------------------------------
# Gemma4 (vLLM) commentary
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a Kalshi World Cup trading commentator providing live, minute-by-minute "
    "analysis. You receive a structured game state every 60 seconds with matrix "
    "probabilities, Kalshi market prices, edges, the trader's position, and the "
    "previous tick's probabilities for trend comparison. Generate 2-4 sentences of "
    "direct pricing dialogue. Every tick matters: comment on time decay, price "
    "movements, how close the position is to exit thresholds, and whether the edge "
    "is widening or narrowing. State the action and the reasoning. No filler, no "
    "preamble, no disclaimers. Be concrete: name the leg, the edge in percentage "
    "points, and whether to buy, sell, or stand aside."
)

def build_gemma_prompt(obs):
    """Build the user-message prompt from an observation dict."""
    g = obs['game']
    pos = obs.get('position')
    pos_str = "0 shares (no position)" if not pos or pos.get('shares', 0) <= 0 else \
        f"{pos['shares']:.0f} shares TIE @ avg ${pos.get('avg_price', 0):.3f}"
    edges = obs['edges']
    mp = obs['matrix_probs']
    recent = "; ".join(
        f"{goal['minute']} {goal['team']} ({'home' if goal.get('is_home') else 'away'})"
        for goal in g['goals'][-3:]
    ) or "No goals yet."

    # Previous tick trends (if available)
    prev = obs.get('prev_tick', {})
    trend_lines = []
    if prev:
        prev_mp = prev.get('matrix_probs', {})
        prev_tie = prev_mp.get('tie', 0)
        cur_tie = mp['tie']
        tie_delta = (cur_tie - prev_tie) * 100
        if abs(tie_delta) > 0.1:
            trend_lines.append(f"P(tie) moved {tie_delta:+.1f}pp since last tick ({prev_tie*100:.1f}% → {cur_tie*100:.1f}%)")
        prev_fav = prev_mp.get('fav_win', 0)
        fav_delta = (mp['fav_win'] - prev_fav) * 100
        if abs(fav_delta) > 0.1:
            trend_lines.append(f"P(fav) moved {fav_delta:+.1f}pp ({prev_fav*100:.1f}% → {mp['fav_win']*100:.1f}%)")
        prev_minute = prev.get('minute', 0)
        if prev_minute and prev_minute != obs['minute']:
            trend_lines.append(f"Time elapsed: {obs['minute'] - prev_minute} min since last analysis")
        # Price movement
        prev_edges = prev.get('edges', {})
        prev_tie_price = prev_edges.get('tie', {}).get('price', 0) if prev_edges else 0
        cur_tie_price = edges['tie']['price']
        if prev_tie_price and cur_tie_price:
            price_delta = (cur_tie_price - prev_tie_price) * 100
            if abs(price_delta) > 0.5:
                trend_lines.append(f"Kalshi TIE price moved {price_delta:+.1f}pp (${prev_tie_price:.2f} → ${cur_tie_price:.2f})")
    trend_str = "\n".join(trend_lines) if trend_lines else "No significant change since last tick."

    # Exit proximity analysis
    exit_proximity = []
    tie_bid = edges['tie'].get('bid') or 0
    tie_prob = mp['tie']
    erosion_gap = (tie_bid - tie_prob - 0.03) * 100  # gap to Exit 2 trigger
    if pos and pos.get('shares', 0) > 0:
        exit_proximity.append(f"Exit 2 (erosion) gap: {erosion_gap:+.1f}pp (triggers when Kalshi TIE bid > P(tie) + 3pp = {(tie_prob+0.03)*100:.0f}%)")
        if erosion_gap > -3:
            exit_proximity.append("⚠ APPROACHING EROSION EXIT — within 3pp of sell trigger")
    exit_str = "\n".join(exit_proximity) if exit_proximity else "No position — no exit thresholds to monitor."

    # Minutes remaining
    mins_left = max(0, 90 - obs['minute'])
    if mins_left <= 15 and pos and pos.get('shares', 0) > 0:
        exit_str += f"\n⏱ {mins_left} min remaining — settlement approaching"

    lines = [
        f"Game: {g['home_team']} vs {g['away_team']}",
        f"Minute: {obs['minute']} ({mins_left} min remaining)  Score: {g['home_score']}-{g['away_score']} ({obs['score_state']} from favorite's perspective)",
        f"Odds class: {obs['odds_class']} (favorite {obs['fav_name']} {obs.get('fav_decimal','?')} decimal / {obs.get('fav_american','?')} American, underdog {obs['und_name']} {obs.get('und_american','?')} American)",
    ]

    # Calibration / mismatch context
    if mp.get('calibrated'):
        vegas_fav = mp.get('vegas_fav', 0)
        vegas_tie = mp.get('vegas_tie', 0)
        vegas_und = mp.get('vegas_und', 0)
        lines.append(f"Vegas calibration: P(fav) {vegas_fav*100:.1f}%, P(tie) {vegas_tie*100:.1f}%, P(und) {vegas_und*100:.1f}% (matrix baseline adjusted by log-ratio to match Vegas no-vig)")
    if obs.get('is_mismatch'):
        lines.append(f"⚠ EXTREME FAVORITE ({obs.get('fav_american','?')}): Probabilities are Vegas-calibrated. Small sample size in this odds range — exercise caution on ALL legs, not just TIE.")

    fav_label = obs['fav_name'] or 'FAV'
    und_label = obs['und_name'] or 'UND'
    tie_label = 'TIE (draw)'

    lines.extend([
        f"Matrix @ minute {mp['minute_bucket']}, state {mp['state_used']}:",
        f"  {fav_label} (favorite) win: {mp['fav_win']*100:.1f}%  (sample n={mp['sample']:,})",
        f"  {tie_label}: {mp['tie']*100:.1f}%",
        f"  {und_label} (underdog) win: {mp['und_win']*100:.1f}%",
        f"Kalshi market prices:",
        f"  {fav_label}: ${edges['fav']['price']*100:.0f} (bid {edges['fav']['bid']*100:.0f}/ask {edges['fav']['ask']*100:.0f})",
        f"  {tie_label}: ${edges['tie']['price']*100:.0f} (bid {edges['tie']['bid']*100:.0f}/ask {edges['tie']['ask']*100:.0f})",
        f"  {und_label}: ${edges['und']['price']*100:.0f} (bid {edges['und']['bid']*100:.0f}/ask {edges['und']['ask']*100:.0f})",
        f"Edge = Kalshi price MINUS matrix probability (positive = Kalshi OVERPRICING this leg = sell signal; "
        f"negative = Kalshi UNDERPRICING = buy signal):",
        f"  {fav_label}: Kalshi {edges['fav']['price']*100:.0f}% vs matrix {mp['fav_win']*100:.1f}% → edge {edges['fav']['edge_pp']:+.1f}pp → "
        f"{'OVERPRICED (sell ' + fav_label + ')' if edges['fav']['edge_pp'] > 6 else 'UNDERPRICED (buy ' + fav_label + ')' if edges['fav']['edge_pp'] < -6 else 'fair value'}",
        f"  {tie_label}: Kalshi {edges['tie']['price']*100:.0f}% vs matrix {mp['tie']*100:.1f}% → edge {edges['tie']['edge_pp']:+.1f}pp → "
        f"{'OVERPRICED (sell TIE)' if edges['tie']['edge_pp'] > 6 else 'UNDERPRICED (buy TIE)' if edges['tie']['edge_pp'] < -6 else 'fair value'}"
        + (' ⚠ small sample — caution' if obs.get('is_mismatch') else ''),
        f"  {und_label}: Kalshi {edges['und']['price']*100:.0f}% vs matrix {mp['und_win']*100:.1f}% → edge {edges['und']['edge_pp']:+.1f}pp → "
        f"{'OVERPRICED (sell ' + und_label + ')' if edges['und']['edge_pp'] > 6 else 'UNDERPRICED (buy ' + und_label + ')' if edges['und']['edge_pp'] < -6 else 'fair value'}",
        f"Position: {pos_str}",
        f"Three-exit recommendation: {obs['exit_label']} — {obs['exit_reason']}",
        f"Exit proximity: {exit_str}",
        f"Trends since last tick: {trend_str}",
        f"Recent events: {recent}",
        "",
        f"Generate 2-4 sentences of live pricing commentary. Use team names ({fav_label}, {und_label}), not abstract labels. "
        "Comment on time decay, price movements, edge trends, and exit proximity. State the action and the reasoning. "
        "IMPORTANT: A positive edge means Kalshi is OVERPRICING that leg (sell signal). "
        "A negative edge means Kalshi is UNDERPRICING that leg (buy signal). Do not confuse the direction. "
        "Cite the specific matrix probability and Kalshi price for each leg you discuss. "
        "When all edges are within ±6pp (fair value), briefly state all three matrix probabilities so the reader can verify.",
    ])
    return "\n".join(lines)

def call_gemma4(prompt, dry_run=False):
    if dry_run:
        return f"[DRY-RUN] Would send to Gemma4:\n{prompt}"
    payload = {
        "model": VLLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 200,
    }
    req = urllib.request.Request(
        VLLM_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=VLLM_TIMEOUT) as r:
        data = json.load(r)
    return data['choices'][0]['message']['content'].strip()

# ---------------------------------------------------------------------------
# State + change detection
# ---------------------------------------------------------------------------
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}

def save_state(state):
    json.dump(state, open(STATE_FILE, 'w'))

def append_commentary(line):
    os.makedirs(os.path.dirname(COMMENTARY_LOG), exist_ok=True)
    with open(COMMENTARY_LOG, 'a') as f:
        f.write(line + "\n")

def detect_state_change(prev, gs):
    """Return a short change label, or None if no meaningful change."""
    if not prev:
        return 'KICKOFF' if gs['state'] == 'in' else None
    prev_score = prev.get('score', '0-0')
    prev_state = prev.get('state', '?')
    cur_score = f"{gs['home_score']}-{gs['away_score']}"
    cur_state = gs['state']
    if prev_state != 'in' and cur_state == 'in':
        return 'KICKOFF'
    if prev_state == 'in' and cur_state == 'post':
        return 'FULL_TIME'
    if cur_score != prev_score:
        prev_h, prev_a = map(int, prev_score.split('-'))
        cur_h, cur_a = map(int, cur_score.split('-'))
        if prev_h == prev_a and cur_h != cur_a:
            return 'GOAL_BREAKS_TIE'
        if prev_h != prev_a and cur_h == cur_a:
            return 'EQUALIZER'
        if max(cur_h, cur_a) >= 2 and max(prev_h, prev_a) < 2:
            return 'SECOND_GOAL'
        return 'GOAL'
    return None

# ---------------------------------------------------------------------------
# Per-game processing
# ---------------------------------------------------------------------------
def process_game(gs, cls_data, kc, matrix, trade_positions, kalshi_positions, dry_run, verbose):
    """Process one live game. Returns (commentary_text or None, alert_type or None)."""
    odds_class = cls_data.get('odds_class', 'close')
    fav_name = cls_data.get('favorite')
    und_name = cls_data.get('underdog')
    fav_decimal = cls_data.get('fav_decimal')
    is_mismatch = cls_data.get('is_mismatch', False)
    fav_american = cls_data.get('fav_american')
    und_american = cls_data.get('und_american')
    draw_american = cls_data.get('draw_american')

    event_ticker, codes = derive_event_ticker(gs, fav_name, und_name)
    if not event_ticker:
        return None, None

    # Determine home vs favorite
    home_norm = normalize_team_name(gs['home_team'])
    away_norm = normalize_team_name(gs['away_team'])
    if fav_name and home_norm == fav_name:
        home_is_fav = True
    elif fav_name and away_norm == fav_name:
        home_is_fav = False
    else:
        # Can't determine favorite — default home as favorite (best guess)
        home_is_fav = True

    # Score from favorite's perspective
    fav_score = gs['home_score'] if home_is_fav else gs['away_score']
    und_score = gs['away_score'] if home_is_fav else gs['home_score']
    score_state = f"{fav_score}-{und_score}"

    # Matrix lookup
    mp = lookup_matrix(matrix, odds_class, gs['minute'], fav_score, und_score)
    if not mp:
        if verbose:
            print(f"  [{event_ticker}] no matrix cell for {odds_class}/{gs['minute']}/{score_state}")
        return None, None

    # Vegas-anchored calibration — corrects matrix probs to match Vegas no-vig
    mp = vegas_calibrate(mp, matrix, odds_class, fav_american, und_american, draw_american)

    # Kalshi prices (all 3 legs)
    try:
        kalshi_markets = get_kalshi_markets(kc, event_ticker)
    except Exception as e:
        if verbose:
            print(f"  [{event_ticker}] Kalshi error: {e}")
        return None, None
    if not kalshi_markets:
        return None, None

    legs = resolve_kalshi_legs(kalshi_markets, codes['home'], codes['away'], home_is_fav)
    # Add mid price
    for leg in legs.values():
        leg['mid'] = (leg.get('bid', 0) + leg.get('ask', 0)) / 2 if leg.get('bid') and leg.get('ask') else leg.get('last', 0)

    edges = compute_edges(mp, legs, home_is_fav)

    # Position (TIE leg)
    event_pos = trade_positions.get(event_ticker, {})
    tie_ticker = legs['tie'].get('ticker')
    if tie_ticker and tie_ticker in kalshi_positions:
        live_pos = kalshi_positions[tie_ticker]
        if live_pos['shares'] > 0:
            event_pos = {'shares': live_pos['shares'], 'avg_price': event_pos.get('avg_price', 0)}

    exit_label, exit_reason = recommend_exit(edges, event_pos, gs['minute'], score_state, odds_class)

    # Determine if we should alert
    max_edge = max(abs(edges['fav']['edge_pp']), abs(edges['tie']['edge_pp']), abs(edges['und']['edge_pp']))
    should_alert = max_edge >= EDGE_THRESHOLD_PP

    obs = {
        'game': gs,
        'event_ticker': event_ticker,
        'odds_class': odds_class,
        'fav_name': fav_name,
        'und_name': und_name,
        'fav_decimal': fav_decimal,
        'fav_american': fav_american,
        'und_american': und_american,
        'is_mismatch': is_mismatch,
        'minute': gs['minute'],
        'score_state': score_state,
        'home_is_fav': home_is_fav,
        'matrix_probs': mp,
        'edges': edges,
        'position': event_pos if event_pos.get('shares', 0) > 0 else None,
        'exit_label': exit_label,
        'exit_reason': exit_reason,
        'max_edge_pp': max_edge,
    }

    return obs, ('EDGE' if should_alert else None)

# ---------------------------------------------------------------------------
# Classification cache
# ---------------------------------------------------------------------------
def refresh_classifications(state, today_str, verbose=False):
    """Pull Odds API and cache pre-game classifications. Only store pre-game games."""
    try:
        odds_games = pull_odds_api()
    except Exception as e:
        if verbose:
            print(f"Odds API error: {e}")
        return state.get('classifications', {})
    classifications = {}
    now = datetime.now(timezone.utc)
    for g in odds_games:
        try:
            ct = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
        except Exception:
            continue
        is_pre = ct > now
        result = classify_game_from_odds(g)
        odds_class, fav, und, fav_am, und_am, fav_dec, is_mismatch, draw_am = result
        key = f"{normalize_team_name(g.get('home_team',''))} vs {normalize_team_name(g.get('away_team',''))}"
        key_rev = f"{normalize_team_name(g.get('away_team',''))} vs {normalize_team_name(g.get('home_team',''))}"
        entry = {
            'odds_class': odds_class,
            'favorite': fav,
            'underdog': und,
            'fav_american': fav_am,
            'und_american': und_am,
            'fav_decimal': fav_dec,
            'is_mismatch': is_mismatch,
            'draw_american': draw_am,
        }
        # Only cache if pre-game OR we don't already have it
        existing = state.get('classifications', {}).get(key) or state.get('classifications', {}).get(key_rev)
        if is_pre or not existing:
            classifications[key] = entry
            classifications[key_rev] = entry  # store both orientations for easy lookup
        else:
            classifications[key] = existing
            classifications[key_rev] = existing
    state['classifications'] = classifications
    state['odds_date'] = today_str
    return classifications

def match_classification(state, gs):
    key = f"{normalize_team_name(gs['home_team'])} vs {normalize_team_name(gs['away_team'])}"
    key_rev = f"{normalize_team_name(gs['away_team'])} vs {normalize_team_name(gs['home_team'])}"
    return state.get('classifications', {}).get(key) or state.get('classifications', {}).get(key_rev)

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def emit(commentary, verbose=False):
    """Print to stdout and append to the commentary log."""
    ts = datetime.now(ET).strftime('%H:%M:%S ET')
    line = f"[{ts}] {commentary}"
    print(line, flush=True)
    append_commentary(line)

def run_tick(kc, matrix, state, dry_run, verbose, simulate_game=None):
    """Run one polling tick. Returns number of commentaries emitted."""
    emitted = 0

    if simulate_game:
        live_events = [(simulate_game, 'in')]
    else:
        try:
            scoreboard = get_espn_scoreboard()
        except Exception as e:
            if verbose:
                print(f"ESPN scoreboard error: {e}")
            return 0
        live_events = [(e, e.get('status', {}).get('type', {}).get('state', '?'))
                       for e in scoreboard.get('events', [])]

    # Filter to in-progress games (or pre if we want early classification)
    in_events = [e for e in live_events if e[1] == 'in']

    # Refresh classifications once per day — do this BEFORE the live-game check
    # so pre-game odds are cached before kickoff (critical: live odds contaminate).
    today = datetime.now(ET).strftime('%Y-%m-%d')
    if state.get('odds_date') != today:
        refresh_classifications(state, today, verbose=verbose)
        save_state(state)

    if not in_events:
        if verbose:
            print(f"No live games ({len(live_events)} total events).")
        return 0

    # Load positions once per tick
    trade_positions = load_trade_log_positions()
    kalshi_positions = get_kalshi_positions(kc) if kc else {}

    for event, _ in in_events:
        try:
            if simulate_game:
                gs = simulate_game
            else:
                summary = get_espn_summary(event['id'])
                gs = parse_game_state(summary)
        except Exception as e:
            if verbose:
                print(f"ESPN summary error: {e}")
            continue

        cls_data = match_classification(state, gs) or {}
        if not cls_data:
            if verbose:
                print(f"  No classification for {gs['home_team']} vs {gs['away_team']} — skipping.")
            continue

        obs, alert_type = process_game(gs, cls_data, kc, matrix, trade_positions, kalshi_positions, dry_run, verbose)
        if not obs:
            continue

        # State change detection
        state_key = f"game_{gs['event_id']}"
        prev = state.get(state_key, {})
        change = detect_state_change(prev, gs)

        # Load previous tick data for trend analysis
        prev_tick = prev.get('last_obs')
        if prev_tick:
            obs['prev_tick'] = prev_tick

        # Call Gemma4 EVERY tick during live games — continuous analysis
        # State changes (goals, etc.) get special headers but don't skip regular ticks
        last_call_ts = prev.get('last_gemma_call_ts', 0)
        time_since_last = time.time() - last_call_ts
        should_call = time_since_last >= COOLDOWN_SECONDS

        if should_call:
            obs['change'] = change
            prompt = build_gemma_prompt(obs)
            try:
                commentary = call_gemma4(prompt, dry_run=dry_run)
            except Exception as e:
                commentary = f"[Gemma4 error: {e}]\nPrompt was:\n{prompt}"
            edge_flag = " ⚡" if obs['max_edge_pp'] >= EDGE_THRESHOLD_PP else ""
            header = f"{'['+change+'] ' if change else ''}{gs['home_team']} vs {gs['away_team']} | min {gs['minute']} | {gs['home_score']}-{gs['away_score']} | max edge {obs['max_edge_pp']:+.1f}pp{edge_flag} | exit: {obs['exit_label']}"
            emit(header, verbose=verbose)
            emit(commentary, verbose=verbose)
            emit("", verbose=verbose)  # blank separator
            emitted += 1
            state.setdefault(state_key, {})['last_gemma_call_ts'] = time.time()
        elif verbose:
            print(f"  {gs['home_team']} vs {gs['away_team']}: min {gs['minute']} {gs['home_score']}-{gs['away_score']} "
                  f"| edges F{obs['edges']['fav']['edge_pp']:+.1f} T{obs['edges']['tie']['edge_pp']:+.1f} "
                  f"U{obs['edges']['und']['edge_pp']:+.1f} | cooldown ({COOLDOWN_SECONDS - time_since_last:.0f}s remaining)")

        # Update state — store last observation for trend analysis on next tick
        # Strip non-serializable items and keep what we need for trends
        last_obs_snapshot = {
            'minute': obs['minute'],
            'score_state': obs['score_state'],
            'matrix_probs': obs['matrix_probs'],
            'edges': {
                leg: {'price': obs['edges'][leg]['price'], 'edge_pp': obs['edges'][leg]['edge_pp']}
                for leg in ('fav', 'tie', 'und')
            },
            'exit_label': obs['exit_label'],
        }
        state[state_key] = {
            'score': f"{gs['home_score']}-{gs['away_score']}",
            'state': gs['state'],
            'minute': gs['minute'],
            'last_checked': datetime.now(timezone.utc).isoformat(),
            'last_gemma_call_ts': state.get(state_key, {}).get('last_gemma_call_ts', 0),
            'last_obs': last_obs_snapshot,
        }

    save_state(state)
    return emitted

def make_simulate_game():
    """Construct a fake live game state for testing (Turkey vs Paraguay-like scenario)."""
    return {
        'event_id': 'SIMULATED',
        'home_team': 'Netherlands',
        'away_team': 'Sweden',
        'home_abbr': 'NED',
        'away_abbr': 'SWE',
        'home_score': 0,
        'away_score': 1,  # underdog (Sweden) leads 1-0
        'clock': "63'",
        'minute': 63,
        'status': 'In Progress',
        'state': 'in',
        'start_time': '2026-06-20T17:00:00Z',
        'goals': [{
            'minute': "2'",
            'minute_reg': 2,
            'team': 'Sweden',
            'team_abbr': 'SWE',
            'is_home': False,
            'scorer': 'Simulated Scorer',
        }],
    }

def main():
    parser = argparse.ArgumentParser(description='WC live edge detector with Gemma4 commentary')
    parser.add_argument('--dry-run', action='store_true', help='Show Gemma4 prompts without calling the API')
    parser.add_argument('--once', action='store_true', help='Run a single tick and exit')
    parser.add_argument('--simulate', action='store_true', help='Inject a simulated live game for testing')
    parser.add_argument('--verbose', action='store_true', help='Print state every tick')
    args = parser.parse_args()

    matrix = load_matrix()
    # Always create the Kalshi client — we need real market data even in dry-run.
    # dry_run only gates the Gemma4 LLM call, not data collection.
    kc = get_kalshi_client()
    state = load_state()
    simulate_game = make_simulate_game() if args.simulate else None

    if args.once:
        run_tick(kc, matrix, state, args.dry_run, args.verbose, simulate_game)
        return

    # Continuous loop
    print(f"WC LIVE EDGE DETECTOR — started {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}"
          f"{' [DRY-RUN]' if args.dry_run else ''}{' [SIMULATE]' if args.simulate else ''}", flush=True)
    print(f"Polling ESPN every {POLL_INTERVAL_LIVE}s (live) / {POLL_INTERVAL_IDLE}s (idle). "
          f"Edge threshold {EDGE_THRESHOLD_PP}pp. Gemma4 model {VLLM_MODEL}.", flush=True)
    print(f"Commentary log: {COMMENTARY_LOG}", flush=True)

    while True:
        try:
            n = run_tick(kc, matrix, state, args.dry_run, args.verbose, simulate_game)
            # Decide sleep interval: if we simulated or emitted, use live interval; else check if any games live
            if simulate_game:
                interval = POLL_INTERVAL_LIVE
            else:
                try:
                    sb = get_espn_scoreboard()
                    any_live = any(e.get('status', {}).get('type', {}).get('state') == 'in'
                                   for e in sb.get('events', []))
                    interval = POLL_INTERVAL_LIVE if any_live else POLL_INTERVAL_IDLE
                    if not any_live and args.verbose:
                        print(f"No live games — sleeping {interval}s", flush=True)
                except Exception:
                    interval = POLL_INTERVAL_IDLE
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)
            break
        except Exception as e:
            print(f"[tick error: {e}]", flush=True)
            interval = POLL_INTERVAL_LIVE
        time.sleep(interval)

if __name__ == "__main__":
    main()
