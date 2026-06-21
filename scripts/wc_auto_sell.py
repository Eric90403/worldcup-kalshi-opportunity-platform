#!/usr/bin/env python3
"""
wc_auto_sell.py — Automatically manages TIE sell limit orders based on game clock.

For each active TIE position:
1. Monitors ESPN game state (score, clock)
2. If score is still 1-0 (waiting for equalizer):
   - Computes optimal sell limit based on P(tie holds | 1-1 at current minute)
   - If the resting sell order price differs from optimal by >$0.02, cancels and replaces
   - Sell limit = P(tie holds at current minute) + small premium, capped at $0.65
3. If score becomes 1-1 (equalizer happened):
   - Checks if resting sell filled; if not, replaces at market bid (taker) to lock in
4. If score becomes 2-0/0-2 (tie killed):
   - Cancels sell, places market sell to cut loss
5. If game ends:
   - Reports settlement result

Runs every 1 minute via cron. Silent unless it takes an action.

Usage:
    python3 wc_auto_sell.py --cron     # cron mode (silent unless action taken)
    python3 wc_auto_sell.py            # verbose (shows state every run)
    python3 wc_auto_sell.py --dry-run  # shows what it WOULD do, doesn't place orders
"""
import os, sys, json, csv, subprocess, urllib.request, time, uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = '/tmp/wc_auto_sell_state.json'
ET = timezone(timedelta(hours=-4))

# P(tie holds | 1-1 at minute X) from EDGE.md
TIE_HOLDS_RATES = {
    30: 0.306, 45: 0.322, 60: 0.421, 70: 0.509,
    75: 0.545, 80: 0.663, 85: 0.742, 90: 0.988,
}

# P(tie | 1-0 underdog first, at minute X) from EDGE.md — for buy decisions
TIE_BUY_RATES_UNDERDOG = {
    15: 0.466, 30: 0.384, 45: 0.337, 60: 0.310, 75: 0.171,
}

# Parameters
SELL_PREMIUM = 0.02      # sell at P(tie holds) + 2pp (slightly above true value, captures overshoot)
MIN_SELL_PRICE = 0.35    # don't sell below this (not worth it vs holding)
MAX_SELL_PRICE = 0.65    # cap — above this, holding is better
REPLACE_THRESHOLD = 0.02 # only replace order if price differs by >2pp
EQUALIZER_SELL_DROP = 0.03  # on equalizer, drop limit 3pp to ensure fill
STOP_LOSS_DROP = 0.05    # on 2nd goal, drop to market minus 5pp to cut loss fast


def load_active_positions():
    """Load TIE positions from trade_log.csv, aggregated by event_ticker."""
    trade_log = os.path.join(SCRIPT_DIR, '..', 'data', 'trade_log.csv')
    if not os.path.exists(trade_log):
        return {}
    positions = {}
    for row in csv.DictReader(open(trade_log)):
        if row.get('side_code') != 'TIE' or not row.get('event_ticker'):
            continue
        ticker = row['event_ticker']
        shares = float(row.get('shares', 0))
        price = float(row.get('fill_price', 0))
        if ticker in positions:
            p = positions[ticker]
            total = p['shares'] + shares
            p['shares'] = total
            p['avg_price'] = (p['shares'] * p['avg_price'] + shares * price) / total if total else 0
        else:
            positions[ticker] = {
                'shares': shares,
                'avg_price': price,
                'game': row.get('game', ''),
            }
    return positions


def get_espn_scoreboard():
    url = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def get_espn_summary(event_id):
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event={event_id}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def parse_game_state(summary):
    c = summary['header']['competitions'][0]
    comps = c.get('competitors', [])
    home = [t for t in comps if t.get('homeAway') == 'home']
    away = [t for t in comps if t.get('homeAway') == 'away']
    
    clock_str = c.get('status', {}).get('displayClock', '0')
    # Parse "66'" or "45'+3'" → 66 or 48
    minute_reg = 0
    try:
        clean = clock_str.replace("'", "").replace("+", " ").strip()
        parts = clean.split()
        minute_reg = int(parts[0])
        if len(parts) > 1:
            minute_reg += int(parts[1])  # add stoppage
    except:
        pass

    return {
        'home_team': home[0]['team']['displayName'] if home else '?',
        'away_team': away[0]['team']['displayName'] if away else '?',
        'home_score': int(home[0]['score']) if home else 0,
        'away_score': int(away[0]['score']) if away else 0,
        'clock': clock_str,
        'minute_reg': minute_reg,
        'status': c.get('status', {}).get('type', {}).get('description', '?'),
        'state': c.get('status', {}).get('type', {}).get('state', '?'),
        'goals': [{
            'minute': g.get('clock', {}).get('displayValue', '?'),
            'team': g.get('team', {}).get('displayName', '?'),
            'scorer': g.get('participants', [{}])[0].get('athlete', {}).get('displayName', '?'),
        } for g in c.get('details', []) if g.get('scoringPlay')],
    }


def normalize_team_name(name):
    name = name.strip()
    replacements = {
        'Türkiye': 'Turkey', 'United States': 'USA',
        'Republic of Korea': 'South Korea', 'Korea Republic': 'South Korea',
        'IR Iran': 'Iran', 'Czechia': 'Czech Republic',
    }
    return replacements.get(name, name)


def get_kalshi_client():
    sys.path.insert(0, SCRIPT_DIR)
    from kalshi_auth import KalshiClient
    return KalshiClient()


def get_kalshi_tie_market(kc, event_ticker):
    r = kc.get('/markets', params={'event_ticker': event_ticker, 'limit': 20})
    for m in r.get('markets', []):
        if m['ticker'].endswith('-TIE'):
            return {
                'ticker': m['ticker'],
                'last': float(m.get('last_price_dollars', 0) or 0),
                'bid': float(m.get('yes_bid_dollars', 0) or 0),
                'ask': float(m.get('yes_ask_dollars', 0) or 0),
            }
    return None


def get_active_sell_order(kc, tie_ticker):
    """Find the active resting sell order for this ticker."""
    r = kc.get('/portfolio/orders')
    for o in r.get('orders', []):
        if (o.get('ticker') == tie_ticker and
            o.get('action') == 'sell' and
            o.get('status') == 'resting'):
            return o
    return None


def cancel_order(kc, order_id):
    """Cancel an order."""
    kc.delete(f'/portfolio/orders/{order_id}')


def place_sell_order(kc, tie_ticker, shares, price, dry_run=False):
    """Place a sell (ask) order — maker limit."""
    order = {
        'ticker': tie_ticker,
        'client_order_id': str(uuid.uuid4()),
        'side': 'ask',           # ask = sell YES
        'count': str(int(shares)),
        'price': f'{price:.4f}',
        'time_in_force': 'good_till_canceled',
        'self_trade_prevention_type': 'taker_at_cross',
        'post_only': True,       # maker order
        'cancel_order_on_pause': False,
        'reduce_only': False,
        'subaccount': 0,
        'exchange_index': 0,
    }
    if dry_run:
        return {'dry_run': True, 'would_place': order}
    return kc.post('/portfolio/events/orders', body=order)


def place_market_sell(kc, tie_ticker, shares, bid_price, dry_run=False):
    """Place a taker sell at the bid to fill immediately."""
    order = {
        'ticker': tie_ticker,
        'client_order_id': str(uuid.uuid4()),
        'side': 'ask',
        'count': str(int(shares)),
        'price': f'{bid_price:.4f}',
        'time_in_force': 'immediate_or_cancel',
        'self_trade_prevention_type': 'taker_at_cross',
        'post_only': False,      # taker
        'cancel_order_on_pause': False,
        'reduce_only': False,
        'subaccount': 0,
        'exchange_index': 0,
    }
    if dry_run:
        return {'dry_run': True, 'would_place': order}
    return kc.post('/portfolio/events/orders', body=order)


def find_closest_minute(minute, rates_dict):
    thresholds = sorted(rates_dict.keys())
    return min(thresholds, key=lambda t: abs(t - minute))


def compute_optimal_sell_price(minute):
    """Compute the optimal sell limit price based on game minute.
    
    If equalizer happens NOW, P(tie holds) = TIE_HOLDS_RATES[minute].
    We set sell limit slightly above that to capture Kalshi's overshoot.
    """
    closest = find_closest_minute(minute, TIE_HOLDS_RATES)
    p_holds = TIE_HOLDS_RATES[closest]
    
    # Sell at P(tie holds) + premium, but within bounds
    sell_price = p_holds + SELL_PREMIUM
    sell_price = max(MIN_SELL_PRICE, min(MAX_SELL_PRICE, sell_price))
    
    return sell_price, p_holds, closest


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except:
        return {}


def save_state(state):
    json.dump(state, open(STATE_FILE, 'w'))


def main():
    cron_mode = '--cron' in sys.argv
    dry_run = '--dry-run' in sys.argv
    
    positions = load_active_positions()
    if not positions:
        if not cron_mode:
            print("No active TIE positions.")
        return

    try:
        scoreboard = get_espn_scoreboard()
    except Exception as e:
        if not cron_mode:
            print(f"ESPN error: {e}")
        return

    live_events = [e for e in scoreboard.get('events', [])
                   if e.get('status', {}).get('type', {}).get('state') in ('in', 'post')]

    state = load_state()
    actions = []
    kc = None if dry_run else get_kalshi_client()

    for event in live_events:
        event_id = event['id']
        try:
            summary = get_espn_summary(event_id)
            gs = parse_game_state(summary)
        except:
            continue

        # Match to position
        sys.path.insert(0, SCRIPT_DIR)
        from team_codes import NAME_TO_CODE
        home_norm = normalize_team_name(gs['home_team'])
        away_norm = normalize_team_name(gs['away_team'])
        hcode = NAME_TO_CODE.get(home_norm)
        acode = NAME_TO_CODE.get(away_norm)
        if not hcode or not acode:
            continue

        ct = datetime.fromisoformat(
            summary['header']['competitions'][0]['date'].replace('Z', '+00:00')
        ).astimezone(ET)
        kalshi_date = f"26{ct.strftime('%b').upper()}{ct.strftime('%d').upper()}"
        event_ticker = f"KXWCGAME-{kalshi_date}{hcode}{acode}"
        tie_ticker = f"{event_ticker}-TIE"

        if event_ticker not in positions:
            continue

        pos = positions[event_ticker]
        shares = pos['shares']
        entry_price = pos['avg_price']
        minute = gs['minute_reg']
        score = f"{gs['home_score']}-{gs['away_score']}"
        
        is_1_0 = ((gs['home_score'] == 1 and gs['away_score'] == 0) or
                  (gs['home_score'] == 0 and gs['away_score'] == 1))
        is_1_1 = gs['home_score'] == 1 and gs['away_score'] == 1
        is_2_plus = gs['home_score'] >= 2 or gs['away_score'] >= 2
        is_tie = gs['home_score'] == gs['away_score']
        is_post = gs['state'] == 'post'

        state_key = f"sell_{event_id}"
        prev = state.get(state_key, {})
        prev_score = prev.get('score', '')
        prev_action = prev.get('last_action', 'none')

        action_taken = None
        action_msg = None

        # === GAME OVER ===
        if is_post:
            won = is_tie and gs['home_score'] == gs['away_score'] and gs['home_score'] > 0
            payout = shares * 1.0 if won else 0
            profit = payout - entry_price * shares
            if prev_action != 'settled':
                action_taken = 'settled'
                action_msg = (f"SETTLED: {gs['home_team']} vs {gs['away_team']} "
                             f"final {score} | {'TIE — WON' if won else 'NO TIE — LOST'} | "
                             f"payout ${payout:.2f} | profit ${profit:+.2f}")
                state[state_key] = {'score': score, 'last_action': 'settled'}

        # === EQUALIZER (1-0 → 1-1) ===
        elif is_1_1 and prev_score and prev_score != score and '1-1' not in prev_score:
            if prev_action != 'equalizer_handled':
                action_taken = 'equalizer'
                # Check if sell order already filled (position reduced)
                if not dry_run:
                    kalshi = get_kalshi_tie_market(kc, event_ticker)
                    existing = get_active_sell_order(kc, tie_ticker)
                    if existing:
                        # Resting sell still there — drop price to ensure fill
                        new_price = max(kalshi['bid'] - EQUALIZER_SELL_DROP, 0.01)
                        cancel_order(kc, existing['order_id'])
                        place_sell_order(kc, tie_ticker, shares, new_price)
                        action_msg = (f"EQUALIZER: {score} at min {gs['clock']} | "
                                     f"Replaced sell at ${new_price:.2f} (dropped from ${float(existing.get('yes_price_dollars',0)):.2f}) | "
                                     f"Locking in ~${(new_price - entry_price) * shares:.2f} profit")
                    else:
                        action_msg = (f"EQUALIZER: {score} at min {gs['clock']} | "
                                     f"Sell order may have filled — check position")
                else:
                    action_msg = f"EQUALIZER: {score} at min {gs['clock']} | [DRY RUN] would drop sell price to fill"
                
                state[state_key] = {'score': score, 'last_action': 'equalizer_handled'}

        # === SECOND GOAL (tie killed) ===
        elif is_2_plus and not is_tie and prev_score and '1-' in prev_score:
            if prev_action != 'cut_loss':
                action_taken = 'cut_loss'
                if not dry_run:
                    kalshi = get_kalshi_tie_market(kc, event_ticker)
                    existing = get_active_sell_order(kc, tie_ticker)
                    if existing:
                        cancel_order(kc, existing['order_id'])
                    if kalshi and kalshi['bid'] > 0.01:
                        place_market_sell(kc, tie_ticker, shares, kalshi['bid'] - STOP_LOSS_DROP)
                        action_msg = (f"SECOND GOAL: {score} at min {gs['clock']} | "
                                     f"Cut loss — market sell at ${kalshi['bid'] - STOP_LOSS_DROP:.2f} | "
                                     f"Recover ~${(kalshi['bid'] - STOP_LOSS_DROP) * shares:.2f}")
                    else:
                        action_msg = f"SECOND GOAL: {score} | TIE near zero, can't sell"
                else:
                    action_msg = f"SECOND GOAL: {score} | [DRY RUN] would cut loss"
                
                state[state_key] = {'score': score, 'last_action': 'cut_loss'}

        # === STILL 1-0 — ADJUST SELL LIMIT BASED ON CLOCK ===
        elif is_1_0 and gs['state'] == 'in':
            optimal_sell, p_holds, closest_min = compute_optimal_sell_price(minute)
            
            if not dry_run:
                existing = get_active_sell_order(kc, tie_ticker)
                current_sell = float(existing.get('yes_price_dollars', 0)) if existing else 0
                needs_update = abs(current_sell - optimal_sell) > REPLACE_THRESHOLD
                
                if needs_update or not existing:
                    if existing:
                        cancel_order(kc, existing['order_id'])
                    place_sell_order(kc, tie_ticker, shares, optimal_sell)
                    
                    action_taken = 'adjust_sell'
                    action_msg = (f"ADJUST SELL: {gs['home_team']} vs {gs['away_team']} "
                                 f"{score} min {gs['clock']} | "
                                 f"P(tie holds at 1-1 min {closest_min})={p_holds*100:.0f}% | "
                                 f"sell limit ${current_sell:.2f}→${optimal_sell:.2f} | "
                                 f"profit if fills: ${(optimal_sell - entry_price) * shares:.2f}")
                else:
                    if not cron_mode:
                        print(f"  {gs['home_team']} vs {gs['away_team']} {score} min {gs['clock']} | "
                              f"sell ${current_sell:.2f} OK (optimal ${optimal_sell:.2f}, "
                              f"P(holds)={p_holds*100:.0f}%)")
            else:
                if not cron_mode:
                    print(f"  [DRY RUN] {gs['home_team']} vs {gs['away_team']} {score} min {gs['clock']} | "
                          f"optimal sell ${optimal_sell:.2f} (P(holds)={p_holds*100:.0f}%)")
            
            state[state_key] = {'score': score, 'last_action': 'monitoring',
                               'current_sell': optimal_sell, 'p_holds': p_holds}

        # === UPDATE STATE ===
        state[state_key] = state.get(state_key, {})
        state[state_key].update({
            'score': score,
            'minute': minute,
            'last_checked': datetime.now(timezone.utc).isoformat(),
        })

        if action_msg:
            actions.append(action_msg)

    save_state(state)

    # Output
    if cron_mode:
        if actions:
            print(f"WC AUTO-SELL — {datetime.now(ET).strftime('%H:%M ET')}")
            for a in actions:
                print(a)
    else:
        if actions:
            print("\n" + "=" * 80)
            print("ACTIONS TAKEN")
            print("=" * 80)
            for a in actions:
                print(a)
        else:
            print("\nNo actions needed.")


if __name__ == "__main__":
    main()
