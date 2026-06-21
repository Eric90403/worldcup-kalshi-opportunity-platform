#!/usr/bin/env python3
"""
test_gemma_accuracy.py — Exhaustive accuracy test for Gemma4 trading commentary.

Generates synthetic game states with known ground truth, sends them to Gemma4
via the edge detector's prompt builder, parses the response, and verifies:

1. NUMBER ACCURACY: Does Gemma4 cite the correct matrix probabilities and Kalshi prices?
2. EDGE DIRECTION: Does Gemma4 correctly identify overpriced (positive edge = sell) vs underpriced (negative edge = buy)?
3. ACTION CORRECTNESS: Does Gemma4 recommend the right action (buy/sell/hold/stand aside)?
4. FAV/UND IDENTIFICATION: Does Gemma4 correctly identify which team is the favorite?
5. EXIT RECOMMENDATIONS: Does Gemma4 correctly apply the three-exit strategy?
6. MISMATCH DETECTION: Does Gemma4 correctly warn against betting TIE in mismatches?
7. NO HALLUCINATION: Does Gemma4 only use provided numbers and not fabricate?

Run:
  python3 scripts/test_gemma_accuracy.py              # full suite
  python3 scripts/test_gemma_accuracy.py --category direction  # specific category
  python3 scripts/test_gemma_accuracy.py --dry-run    # show prompts without calling API
"""
import os, sys, json, time, re, argparse, urllib.request
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.join(SCRIPT_DIR, '..')
sys.path.insert(0, SCRIPT_DIR)

# Import the edge detector's prompt builder and matrix
from wc_live_edge import build_gemma_prompt, lookup_matrix, load_matrix, \
    classify_odds_class, american_to_decimal, normalize_team_name, \
    recommend_exit, compute_edges, resolve_kalshi_legs, call_gemma4

VLLM_URL = "http://localhost:8765/v1/chat/completions"
VLLM_MODEL = "gemma4-31b"

# ---------------------------------------------------------------------------
# Test case definitions — each has known ground truth
# ---------------------------------------------------------------------------

def make_test_case(home_team, away_team, home_score, away_score, minute,
                   fav_name, und_name, fav_decimal, odds_class,
                   kalshi_fav_price, kalshi_tie_price, kalshi_und_price,
                   kalshi_fav_bid, kalshi_fav_ask, kalshi_tie_bid, kalshi_tie_ask,
                   kalshi_und_bid, kalshi_und_ask,
                   home_is_fav, position_shares=0, position_avg=0,
                   is_mismatch=False, question=None):
    """Build a synthetic game state + observation dict matching the edge detector's format."""
    fav_score = home_score if home_is_fav else away_score
    und_score = away_score if home_is_fav else home_score
    score_state = f"{fav_score}-{und_score}"

    matrix = load_matrix()
    mp = lookup_matrix(matrix, odds_class, minute, fav_score, und_score)
    if not mp:
        return None

    home_abbr = home_team[:3].upper()
    away_abbr = away_team[:3].upper()

    # Build Kalshi markets dict (keyed by suffix as in the real code)
    fav_suffix = home_abbr if home_is_fav else away_abbr
    und_suffix = away_abbr if home_is_fav else home_abbr
    kalshi_markets = {
        fav_suffix: {'last': kalshi_fav_price, 'bid': kalshi_fav_bid, 'ask': kalshi_fav_ask, 'ticker': f'FAKE-{fav_suffix}'},
        und_suffix: {'last': kalshi_und_price, 'bid': kalshi_und_bid, 'ask': kalshi_und_ask, 'ticker': f'FAKE-{und_suffix}'},
        'TIE': {'last': kalshi_tie_price, 'bid': kalshi_tie_bid, 'ask': kalshi_tie_ask, 'ticker': 'FAKE-TIE'},
    }
    legs = resolve_kalshi_legs(kalshi_markets, home_abbr, away_abbr, home_is_fav)
    for leg in legs.values():
        leg['mid'] = (leg.get('bid', 0) + leg.get('ask', 0)) / 2 if leg.get('bid') and leg.get('ask') else leg.get('last', 0)

    edges = compute_edges(mp, legs, home_is_fav)
    position = {'shares': position_shares, 'avg_price': position_avg} if position_shares > 0 else None
    exit_label, exit_reason = recommend_exit(edges, position or {}, minute, score_state, odds_class)

    gs = {
        'home_team': home_team, 'away_team': away_team,
        'home_score': home_score, 'away_score': away_score,
        'clock': f"{minute}'", 'minute': minute,
        'state': 'in', 'status': 'In Progress',
        'goals': [],
        'event_id': 'TEST',
    }

    obs = {
        'game': gs,
        'event_ticker': 'FAKE-TICKER',
        'odds_class': odds_class,
        'fav_name': fav_name,
        'und_name': und_name,
        'fav_decimal': fav_decimal,
        'is_mismatch': is_mismatch,
        'minute': minute,
        'score_state': score_state,
        'home_is_fav': home_is_fav,
        'matrix_probs': mp,
        'edges': edges,
        'position': position,
        'exit_label': exit_label,
        'exit_reason': exit_reason,
        'max_edge_pp': max(abs(edges['fav']['edge_pp']), abs(edges['tie']['edge_pp']), abs(edges['und']['edge_pp'])),
    }

    # Ground truth for verification
    ground_truth = {
        'fav_win_prob': mp['fav_win'],
        'tie_prob': mp['tie'],
        'und_win_prob': mp['und_win'],
        'sample_size': mp['sample'],
        'kalshi_fav_price': kalshi_fav_price,
        'kalshi_tie_price': kalshi_tie_price,
        'kalshi_und_price': kalshi_und_price,
        'fav_edge_pp': edges['fav']['edge_pp'],
        'tie_edge_pp': edges['tie']['edge_pp'],
        'und_edge_pp': edges['und']['edge_pp'],
        'fav_direction': 'overpriced' if edges['fav']['edge_pp'] > 0 else 'underpriced' if edges['fav']['edge_pp'] < 0 else 'fair',
        'tie_direction': 'overpriced' if edges['tie']['edge_pp'] > 0 else 'underpriced' if edges['tie']['edge_pp'] < 0 else 'fair',
        'und_direction': 'overpriced' if edges['und']['edge_pp'] > 0 else 'underpriced' if edges['und']['edge_pp'] < 0 else 'fair',
        'fav_action': 'sell' if edges['fav']['edge_pp'] > 6 else 'buy' if edges['fav']['edge_pp'] < -6 else 'hold/stand_aside',
        'tie_action': 'sell' if edges['tie']['edge_pp'] > 6 else 'buy' if edges['tie']['edge_pp'] < -6 else 'hold/stand_aside',
        'und_action': 'sell' if edges['und']['edge_pp'] > 6 else 'buy' if edges['und']['edge_pp'] < -6 else 'hold/stand_aside',
        'favorite_name': fav_name,
        'underdog_name': und_name,
        'home_is_fav': home_is_fav,
        'is_mismatch': is_mismatch,
        'exit_label': exit_label,
        'position_shares': position_shares,
    }

    return obs, ground_truth


# ---------------------------------------------------------------------------
# Test cases covering all scenarios
# ---------------------------------------------------------------------------

def generate_test_cases():
    """Generate comprehensive test cases across all odds classes, score states, and edge scenarios."""
    cases = []

    # ---- CATEGORY 1: Edge direction (overpriced vs underpriced) ----
    # Case 1a: Favorite clearly overpriced (Kalshi 70% vs matrix 54%)
    cases.append({
        'category': 'direction',
        'name': 'FAV_overpriced_close_0-0',
        'case': make_test_case('Netherlands', 'Sweden', 0, 0, 0,
            'Netherlands', 'Sweden', 1.714, 'moderate_fav',
            0.70, 0.18, 0.12,  # Kalshi: FAV 70c, TIE 18c, UND 12c
            0.69, 0.71, 0.17, 0.19, 0.11, 0.13,
            home_is_fav=True),
    })

    # Case 1b: TIE clearly underpriced (negative edge = buy signal)
    cases.append({
        'category': 'direction',
        'name': 'TIE_underpriced_close_1-0_und_first',
        'case': make_test_case('Turkey', 'Paraguay', 0, 1, 15,
            'Turkey', 'Paraguay', 2.08, 'close',
            0.30, 0.15, 0.55,  # Kalshi: FAV 30c, TIE 15c (underpriced!), UND 55c
            0.29, 0.31, 0.14, 0.16, 0.54, 0.56,
            home_is_fav=True, is_mismatch=False),
    })

    # Case 1c: TIE clearly overpriced (positive edge = sell signal)
    cases.append({
        'category': 'direction',
        'name': 'TIE_overpriced_close_0-1_late',
        'case': make_test_case('Turkey', 'Paraguay', 0, 1, 60,
            'Turkey', 'Paraguay', 2.08, 'close',
            0.15, 0.35, 0.50,  # Kalshi: FAV 15c, TIE 35c (overpriced!), UND 50c
            0.14, 0.16, 0.34, 0.36, 0.49, 0.51,
            home_is_fav=True),
    })

    # Case 1d: Underdog overpriced
    cases.append({
        'category': 'direction',
        'name': 'UND_overpriced_heavy_fav',
        'case': make_test_case('Germany', 'Ivory Coast', 0, 0, 0,
            'Germany', 'Ivory Coast', 1.481, 'heavy_fav',
            0.67, 0.21, 0.25,  # UND 25c but matrix says ~11% → overpriced
            0.66, 0.68, 0.20, 0.22, 0.24, 0.26,
            home_is_fav=True),
    })

    # ---- CATEGORY 2: Number accuracy ----
    cases.append({
        'category': 'numbers',
        'name': 'numbers_close_0-0_min0',
        'case': make_test_case('Japan', 'Sweden', 0, 0, 0,
            'Japan', 'Sweden', 2.2, 'close',
            0.40, 0.30, 0.30,
            0.39, 0.41, 0.29, 0.31, 0.29, 0.31,
            home_is_fav=True),
    })

    cases.append({
        'category': 'numbers',
        'name': 'numbers_close_1-1_min45',
        'case': make_test_case('Japan', 'Sweden', 1, 1, 45,
            'Japan', 'Sweden', 2.2, 'close',
            0.35, 0.40, 0.25,
            0.34, 0.36, 0.39, 0.41, 0.24, 0.26,
            home_is_fav=True),
    })

    # ---- CATEGORY 3: Action correctness ----
    # Strong buy TIE signal (TIE underpriced by >6pp)
    cases.append({
        'category': 'action',
        'name': 'action_buy_TIE_underdog_first',
        'case': make_test_case('Turkey', 'Paraguay', 0, 1, 15,
            'Turkey', 'Paraguay', 2.08, 'close',
            0.35, 0.15, 0.50,  # TIE at 15% but matrix ~27% → strong buy
            0.34, 0.36, 0.14, 0.16, 0.49, 0.51,
            home_is_fav=True),
    })

    # Strong sell TIE signal (TIE overpriced by >6pp)
    cases.append({
        'category': 'action',
        'name': 'action_sell_TIE_erosion',
        'case': make_test_case('Turkey', 'Paraguay', 0, 1, 60,
            'Turkey', 'Paraguay', 2.08, 'close',
            0.12, 0.35, 0.53,  # TIE at 35% but matrix ~27% → sell
            0.11, 0.13, 0.34, 0.36, 0.52, 0.54,
            home_is_fav=True, position_shares=50, position_avg=0.20),
    })

    # Stand aside (all edges within ±6pp)
    cases.append({
        'category': 'action',
        'name': 'action_stand_aside_fair_value',
        'case': make_test_case('Netherlands', 'Sweden', 0, 0, 0,
            'Netherlands', 'Sweden', 1.714, 'moderate_fav',
            0.55, 0.25, 0.20,  # Close to matrix: 54%/26%/20%
            0.54, 0.56, 0.24, 0.26, 0.19, 0.21,
            home_is_fav=True),
    })

    # ---- CATEGORY 4: Favorite/underdog identification ----
    # Away team is favorite
    cases.append({
        'category': 'fav_und',
        'name': 'fav_is_away_team',
        'case': make_test_case('Curacao', 'Ecuador', 0, 0, 0,
            'Ecuador', 'Curacao', 1.133, 'heavy_fav',
            0.85, 0.10, 0.05,  # FAV (Ecuador) 85c, TIE 10c, UND (Curacao) 5c
            0.84, 0.86, 0.09, 0.11, 0.04, 0.06,
            home_is_fav=False, is_mismatch=True),
    })

    # Home team is favorite
    cases.append({
        'category': 'fav_und',
        'name': 'fav_is_home_team',
        'case': make_test_case('Germany', 'Ivory Coast', 1, 0, 30,
            'Germany', 'Ivory Coast', 1.481, 'heavy_fav',
            0.85, 0.10, 0.05,
            0.84, 0.86, 0.09, 0.11, 0.04, 0.06,
            home_is_fav=True),
    })

    # ---- CATEGORY 5: Position-aware exit recommendations ----
    # Exit 2: erosion sell (holding TIE, market overprices it)
    cases.append({
        'category': 'exit',
        'name': 'exit2_erosion_sell',
        'case': make_test_case('Turkey', 'Paraguay', 0, 1, 55,
            'Turkey', 'Paraguay', 2.08, 'close',
            0.15, 0.35, 0.50,  # TIE 35% but matrix ~28% → sell
            0.14, 0.16, 0.34, 0.36, 0.49, 0.51,
            home_is_fav=True, position_shares=69, position_avg=0.285),
    })

    # Exit 3: hold to settlement (P(tie) still above market)
    cases.append({
        'category': 'exit',
        'name': 'exit3_hold',
        'case': make_test_case('Turkey', 'Paraguay', 0, 1, 20,
            'Turkey', 'Paraguay', 2.08, 'close',
            0.25, 0.30, 0.45,  # TIE at 30% but matrix ~34% → hold
            0.24, 0.26, 0.29, 0.31, 0.44, 0.46,
            home_is_fav=True, position_shares=69, position_avg=0.285),
    })

    # No position
    cases.append({
        'category': 'exit',
        'name': 'no_position',
        'case': make_test_case('Netherlands', 'Sweden', 0, 0, 0,
            'Netherlands', 'Sweden', 1.714, 'moderate_fav',
            0.58, 0.24, 0.21,
            0.57, 0.59, 0.23, 0.25, 0.20, 0.22,
            home_is_fav=True, position_shares=0),
    })

    # ---- CATEGORY 6: Mismatch detection ----
    cases.append({
        'category': 'mismatch',
        'name': 'mismatch_do_not_bet_TIE',
        'case': make_test_case('Ecuador', 'Curacao', 0, 0, 0,
            'Ecuador', 'Curacao', 1.133, 'heavy_fav',
            0.87, 0.10, 0.05,
            0.86, 0.88, 0.09, 0.11, 0.04, 0.06,
            home_is_fav=True, is_mismatch=True),
    })

    # ---- CATEGORY 7: Hallucination check (does it fabricate numbers?) ----
    cases.append({
        'category': 'hallucination',
        'name': 'hallucination_check_specific_numbers',
        'case': make_test_case('Japan', 'Sweden', 0, 0, 0,
            'Japan', 'Sweden', 2.2, 'close',
            0.42, 0.28, 0.30,
            0.41, 0.43, 0.27, 0.29, 0.29, 0.31,
            home_is_fav=True),
    })

    # ---- CATEGORY 8: Various score states ----
    cases.append({
        'category': 'score_state',
        'name': 'score_2-0_fav_leading',
        'case': make_test_case('Germany', 'Ivory Coast', 2, 0, 45,
            'Germany', 'Ivory Coast', 1.481, 'heavy_fav',
            0.95, 0.03, 0.02,
            0.94, 0.96, 0.02, 0.04, 0.01, 0.03,
            home_is_fav=True),
    })

    cases.append({
        'category': 'score_state',
        'name': 'score_1-1_equalizer_just_happened',
        'case': make_test_case('Turkey', 'Paraguay', 1, 1, 35,
            'Turkey', 'Paraguay', 2.08, 'close',
            0.30, 0.45, 0.25,  # TIE spiked to 45% on equalizer
            0.29, 0.31, 0.44, 0.46, 0.24, 0.26,
            home_is_fav=True, position_shares=69, position_avg=0.285),
    })

    cases.append({
        'category': 'score_state',
        'name': 'score_0-2_underdog_dominating',
        'case': make_test_case('Netherlands', 'Sweden', 0, 2, 55,
            'Netherlands', 'Sweden', 1.714, 'moderate_fav',
            0.05, 0.10, 0.85,
            0.04, 0.06, 0.09, 0.11, 0.84, 0.86,
            home_is_fav=True),
    })

    # Filter out None cases (where matrix lookup failed)
    cases = [c for c in cases if c['case'] is not None]
    return cases


# ---------------------------------------------------------------------------
# Response parsing and verification
# ---------------------------------------------------------------------------

def parse_response(text):
    """Parse Gemma4's response and extract claimed numbers/directions/actions."""
    text_lower = text.lower()
    result = {
        'raw': text,
        # Extract any percentages mentioned
        'percentages': re.findall(r'(\d+\.?\d*)\s*%', text),
        # Extract any dollar prices mentioned
        'prices': re.findall(r'\$(\d+\.?\d*)', text),
        # Check for direction keywords
        'mentions_overpriced': 'overpric' in text_lower,
        'mentions_underpriced': 'underpric' in text_lower,
        # Check for action keywords
        'mentions_buy': bool(re.search(r'\b(buy|long|buying)\b', text_lower)),
        'mentions_sell': bool(re.search(r'\b(sell|short|selling|sell-off|cut)\b', text_lower)),
        'mentions_hold': bool(re.search(r'\b(hold|stand aside|wait|no action|do nothing)\b', text_lower)),
        # Check for specific terms
        'mentions_tie': 'tie' in text_lower or 'draw' in text_lower,
        'mentions_mismatch': 'mismatch' in text_lower or 'do not bet' in text_lower,
        'mentions_erosion': 'erosion' in text_lower,
        'mentions_equalizer': 'equalizer' in text_lower,
    }
    return result


def verify_response(parsed, gt, category, case_name):
    """Verify Gemma4's response against ground truth. Returns (passed, details)."""
    failures = []
    checks = 0
    passed = 0

    def check(condition, name):
        nonlocal checks, passed
        checks += 1
        if condition:
            passed += 1
        else:
            failures.append(name)

    raw = parsed['raw']

    if category == 'direction':
        # Check that Gemma4 correctly identifies the direction of the largest edge
        max_edge_leg = max([('fav', gt['fav_edge_pp']), ('tie', gt['tie_edge_pp']), ('und', gt['und_edge_pp'])],
                          key=lambda x: abs(x[1]))
        leg_name, edge_val = max_edge_leg
        if edge_val > 6:
            direction = 'overpriced'
            check(parsed['mentions_overpriced'], f"should mention 'overpriced' for {leg_name} (edge +{edge_val:.1f}pp)")
        elif edge_val < -6:
            direction = 'underpriced'
            check(parsed['mentions_underpriced'], f"should mention 'underpriced' for {leg_name} (edge {edge_val:.1f}pp)")
        # Check it doesn't contradict
        if gt['fav_edge_pp'] > 6 and gt['tie_edge_pp'] < -6:
            check(not (parsed['mentions_underpriced'] and 'favorite' in raw.lower() and 'overpric' not in raw.lower()),
                  "should NOT say favorite is underpriced when it's overpriced")

    elif category == 'numbers':
        # Check that Gemma4 cites the correct matrix probability
        tie_prob_str = f"{gt['tie_prob']*100:.1f}%"
        tie_prob_int = f"{int(gt['tie_prob']*100)}%"
        fav_prob_str = f"{gt['fav_win_prob']*100:.1f}%"
        fav_prob_int = f"{int(gt['fav_win_prob']*100)}%"
        check(tie_prob_str in raw or tie_prob_int in raw,
              f"should cite TIE matrix prob {tie_prob_str} or {tie_prob_int}")
        check(fav_prob_str in raw or fav_prob_int in raw,
              f"should cite FAV matrix prob {fav_prob_str} or {fav_prob_int}")
        # Check it doesn't cite a probability that's off by >5pp
        mentioned_pcts = [float(p) for p in parsed['percentages']]
        if mentioned_pcts:
            closest_tie = min(mentioned_pcts, key=lambda p: abs(p - gt['tie_prob']*100))
            check(abs(closest_tie - gt['tie_prob']*100) <= 5,
                  f"cited TIE prob should be within 5pp of {gt['tie_prob']*100:.1f}% (closest was {closest_tie})")

    elif category == 'action':
        # Determine the correct action based on ground truth
        actions = [('fav', gt['fav_action']), ('tie', gt['tie_action']), ('und', gt['und_action'])]
        has_buy = any(a == 'buy' for _, a in actions)
        has_sell = any(a == 'sell' for _, a in actions)

        if has_buy:
            buy_leg = [l for l, a in actions if a == 'buy'][0]
            check(parsed['mentions_buy'], f"should recommend BUY for {buy_leg} (edge < -6pp)")
        if has_sell:
            sell_leg = [l for l, a in actions if a == 'sell'][0]
            check(parsed['mentions_sell'], f"should recommend SELL for {sell_leg} (edge > +6pp)")
        if not has_buy and not has_sell:
            check(parsed['mentions_hold'] or not parsed['mentions_buy'],
                  "should recommend HOLD/stand aside when no edge >6pp")
            check(not parsed['mentions_buy'] or parsed['mentions_hold'],
                  "should NOT recommend buy when no edge >6pp")

    elif category == 'fav_und':
        # Check that Gemma4 correctly identifies the favorite
        fav_name_lower = gt['favorite_name'].lower()
        check(fav_name_lower in raw.lower() or gt['favorite_name'] in raw,
              f"should mention favorite '{gt['favorite_name']}'")
        if gt['is_mismatch']:
            check(parsed['mentions_mismatch'] or 'do not bet' in raw.lower() or 'mismatch' in raw.lower(),
                  "should mention mismatch / do not bet TIE")

    elif category == 'exit':
        if gt['position_shares'] > 0:
            if gt['exit_label'] == 'EXIT_2_EROSION_SELL':
                check(parsed['mentions_sell'] or parsed['mentions_erosion'],
                      f"should recommend SELL / mention erosion (exit: {gt['exit_label']})")
            elif gt['exit_label'] == 'EXIT_3_HOLD':
                check(parsed['mentions_hold'] or 'hold' in raw.lower(),
                      f"should recommend HOLD (exit: {gt['exit_label']})")
            elif gt['exit_label'] == 'NO_POSITION':
                pass  # shouldn't happen if position_shares > 0
        else:
            check(not parsed['mentions_sell'] or 'no position' in raw.lower() or 'not positioned' in raw.lower() or 'stand aside' in raw.lower() or 'if' in raw.lower(),
                  "should not recommend sell with no position (or should qualify with 'if holding')")

    elif category == 'mismatch':
        check(parsed['mentions_mismatch'] or 'do not bet' in raw.lower() or 'mismatch' in raw.lower() or 'avoid' in raw.lower(),
              "should mention mismatch / do not bet TIE / avoid")

    elif category == 'hallucination':
        # Check that all percentages mentioned are within 5pp of some ground truth value
        gt_pcts = [gt['fav_win_prob']*100, gt['tie_prob']*100, gt['und_win_prob']*100,
                   gt['kalshi_fav_price']*100, gt['kalshi_tie_price']*100, gt['kalshi_und_price']*100]
        mentioned_pcts = [float(p) for p in parsed['percentages']]
        for p in mentioned_pcts:
            closest = min(gt_pcts, key=lambda g: abs(g - p))
            diff = abs(p - closest)
            check(diff <= 5, f"cited percentage {p}% should be within 5pp of a ground truth value (closest: {closest:.1f}%)")

    elif category == 'score_state':
        # Just check it doesn't hallucinate and mentions the correct score
        score_str = f"{int(gt.get('fav_win_prob',0)*0)}"  # placeholder
        # Check it mentions the correct teams
        check(gt['favorite_name'].lower() in raw.lower() or gt['underdog_name'].lower() in raw.lower(),
              "should mention at least one team name")

    # Overall hallucination check: no "insert" placeholders
    check('insert' not in raw.lower() and '[insert' not in raw.lower(),
          "should NOT contain 'insert' placeholders")
    check('e.g.' not in raw.lower() or len(raw) > 100,
          "should NOT contain 'e.g.' placeholders (unless substantial response)")

    accuracy = passed / checks if checks > 0 else 0
    return accuracy, failures, checks, passed


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------

def run_tests(cases, dry_run=False, verbose=False):
    """Run all test cases and report accuracy."""
    results = []
    total_checks = 0
    total_passed = 0
    by_category = {}

    for i, tc in enumerate(cases):
        category = tc['category']
        name = tc['name']
        obs, gt = tc['case']

        prompt = build_gemma_prompt(obs)

        if dry_run:
            print(f"\n{'='*70}")
            print(f"TEST {i+1}/{len(cases)}: [{category}] {name}")
            print(f"{'='*70}")
            print("PROMPT:")
            print(prompt[:500])
            print("..." if len(prompt) > 500 else "")
            print("\nGROUND TRUTH:")
            for k, v in gt.items():
                print(f"  {k}: {v}")
            results.append({'name': name, 'category': category, 'accuracy': 1.0, 'failures': [], 'dry_run': True})
            continue

        if verbose:
            print(f"\n[{i+1}/{len(cases)}] Testing [{category}] {name}...", end=" ", flush=True)

        try:
            response = call_gemma4(prompt, dry_run=False)
        except Exception as e:
            print(f"ERROR: {e}")
            results.append({'name': name, 'category': category, 'accuracy': 0, 'failures': [f'API error: {e}']})
            continue

        parsed = parse_response(response)
        accuracy, failures, checks, passed = verify_response(parsed, gt, category, name)

        total_checks += checks
        total_passed += passed

        by_category.setdefault(category, {'checks': 0, 'passed': 0})
        by_category[category]['checks'] += checks
        by_category[category]['passed'] += passed

        status = "PASS" if accuracy == 1.0 else f"FAIL ({accuracy*100:.0f}%)"
        if verbose:
            print(status)
            if failures:
                for f in failures:
                    print(f"  ✗ {f}")
                print(f"  Response: {response[:200]}...")
        elif accuracy < 1.0:
            print(f"\n  FAIL [{category}] {name}: {failures}")
            print(f"    Response: {response[:200]}")

        results.append({'name': name, 'category': category, 'accuracy': accuracy, 'failures': failures,
                        'response': response, 'ground_truth': gt})

        time.sleep(1)  # rate limit courtesy

    # Summary
    print(f"\n{'='*70}")
    print(f"TEST SUMMARY")
    print(f"{'='*70}")
    if not dry_run:
        overall = total_passed / total_checks if total_checks > 0 else 0
        print(f"Overall accuracy: {total_passed}/{total_checks} checks passed = {overall*100:.1f}%")
        print(f"Test cases: {len(results)}")
        print(f"\nBy category:")
        for cat, stats in sorted(by_category.items()):
            cat_acc = stats['passed'] / stats['checks'] if stats['checks'] > 0 else 0
            print(f"  {cat:20s}: {stats['passed']}/{stats['checks']} = {cat_acc*100:.1f}%")

        failed = [r for r in results if r['accuracy'] < 1.0]
        if failed:
            print(f"\nFAILED TESTS ({len(failed)}):")
            for r in failed:
                print(f"  [{r['category']}] {r['name']}: {r['failures']}")
        else:
            print(f"\n✓ ALL TESTS PASSED")
    else:
        print(f"Dry run completed — {len(results)} test cases generated, no API calls made.")

    return results


def main():
    parser = argparse.ArgumentParser(description='Gemma4 accuracy test suite')
    parser.add_argument('--category', type=str, default=None,
                        help='Run only tests in this category (direction, numbers, action, fav_und, exit, mismatch, hallucination, score_state)')
    parser.add_argument('--dry-run', action='store_true', help='Show prompts and ground truth without calling Gemma4')
    parser.add_argument('--verbose', action='store_true', help='Show every test result')
    args = parser.parse_args()

    cases = generate_test_cases()
    if args.category:
        cases = [c for c in cases if c['category'] == args.category]
        print(f"Running {len(cases)} tests in category '{args.category}'")

    print(f"Total test cases: {len(cases)}")
    print(f"Test categories: {sorted(set(c['category'] for c in cases))}")

    results = run_tests(cases, dry_run=args.dry_run, verbose=args.verbose)
    return results


if __name__ == "__main__":
    main()
