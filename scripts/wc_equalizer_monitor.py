#!/usr/bin/env python3
"""
wc_equalizer_monitor.py — Detects equalizer goals and recommends sell/hold.

Runs as a cron job every 1 minute during live WC games.
When the score goes from 1-0 (underdog leading) to 1-1 (equalized):
  - Pulls current Kalshi TIE bid/ask
  - Computes P(tie holds | 1-1 at minute X) from historical data
  - Recommends SELL if Kalshi TIE > P(tie holds) + cost margin
  - Recommends HOLD if Kalshi TIE < P(tie holds)
  - Alerts with the recommendation

Also monitors for the second goal (2-0 or 0-2) which kills the tie bet.

Silent when nothing changes (empty stdout = no alert in no_agent cron).

Usage:
    python3 wc_equalizer_monitor.py --cron   # cron mode
    python3 wc_equalizer_monitor.py          # verbose
"""
import os, sys, json, csv, subprocess, urllib.request, time
from datetime import datetime, timezone, timedelta
from decimal import Decimal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = '/tmp/wc_equalizer_state.json'
ALERTS_CSV = os.path.join(SCRIPT_DIR, '..', 'data', 'equalizer_alerts.csv')
ET = timezone(timedelta(hours=-4))

# P(tie holds | 1-1 at minute X) — from World Cup group stage 1930-2022
TIE_HOLDS_RATES = {
    30: 0.306, 45: 0.322, 60: 0.421, 70: 0.509,
    75: 0.545, 80: 0.663, 85: 0.742, 90: 0.988,
}

# Cost margin for sell decision (covers fees + spread)
SELL_COST_MARGIN = 0.03  # 3pp — maker sell fee is ~1%, spread ~1%, buffer 1%

# Active positions to monitor (from trade_log.csv)
def load_active_positions():
    """Load TIE positions from trade_log.csv, aggregated by event_ticker.
    Multiple trades on the same ticker are combined into one position."""
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
            # Aggregate: weighted average price
            p = positions[ticker]
            total_shares = p['shares'] + shares
            avg_price = (p['shares'] * p['avg_price'] + shares * price) / total_shares
            p['shares'] = total_shares
            p['avg_price'] = avg_price
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

    result = {
        'home_team': home[0]['team']['displayName'] if home else '?',
        'away_team': away[0]['team']['displayName'] if away else '?',
        'home_score': int(home[0]['score']) if home else 0,
        'away_score': int(away[0]['score']) if away else 0,
        'clock': c.get('status', {}).get('displayClock', '?'),
        'status': c.get('status', {}).get('type', {}).get('description', '?'),
        'state': c.get('status', {}).get('type', {}).get('state', '?'),
        'goals': [],
    }

    for g in c.get('details', []):
        if g.get('scoringPlay'):
            minute_val = g.get('clock', {}).get('value', 0)
            result['goals'].append({
                'minute': g.get('clock', {}).get('displayValue', '?'),
                'minute_regulation': int(minute_val) // 60 if minute_val else 0,
                'team': g.get('team', {}).get('displayName', '?'),
                'team_abbr': g.get('team', {}).get('abbreviation', '?'),
                'is_home': g.get('home_team') == '1',
                'scorer': g.get('participants', [{}])[0].get('athlete', {}).get('displayName', '?'),
            })

    return result


def get_kalshi_tie_market(event_ticker):
    """Get Kalshi TIE market with bid/ask for sell decision."""
    sys.path.insert(0, SCRIPT_DIR)
    from kalshi_auth import KalshiClient
    kc = KalshiClient()
    r = kc.get('/markets', params={'event_ticker': event_ticker, 'limit': 20})
    for m in r.get('markets', []):
        if m['ticker'].endswith('-TIE'):
            return {
                'ticker': m['ticker'],
                'last': float(m.get('last_price_dollars', 0) or 0),
                'bid': float(m.get('yes_bid_dollars', 0) or 0),
                'ask': float(m.get('yes_ask_dollars', 0) or 0),
                'vol24h': float(m.get('volume_24h_fp', 0) or 0),
            }
    return None


def get_kalshi_position(ticker):
    """Get current position in a market."""
    sys.path.insert(0, SCRIPT_DIR)
    from kalshi_auth import KalshiClient
    kc = KalshiClient()
    pos = kc.get('/portfolio/positions')
    for mp in pos.get('market_positions', []):
        if mp.get('ticker') == ticker:
            return {
                'shares': float(mp.get('position_fp', 0)),
                'cost': float(mp.get('total_traded_dollars', 0)),
                'fees': float(mp.get('fees_paid_dollars', 0)),
            }
    return None


def find_closest_minute(minute_regulation):
    thresholds = sorted(TIE_HOLDS_RATES.keys())
    return min(thresholds, key=lambda t: abs(t - minute_regulation))


def compute_sell_decision(tie_bid, minute, entry_price, shares):
    """Decide whether to sell or hold on equalizer.

    Returns (action, hold_prob, sell_price, sell_profit, hold_ev, reason).
    """
    closest = find_closest_minute(minute)
    hold_prob = TIE_HOLDS_RATES.get(closest, 0.9)  # default high if late

    # We sell at the bid (maker) or ask (taker).
    # For a sell, we hit the bid. Use bid as the sell price.
    sell_price = tie_bid

    # Sell if: sell_price > hold_prob + cost_margin
    # i.e., the guaranteed sell price exceeds the expected hold value after costs
    sell_threshold = hold_prob + SELL_COST_MARGIN

    if sell_price > sell_threshold:
        sell_profit = shares * (sell_price - entry_price)
        hold_ev = shares * (hold_prob * 1.0 - entry_price)
        return ('SELL', hold_prob, sell_price, sell_profit, hold_ev,
                f'Kalshi TIE bid ${sell_price:.2f} > P(tie holds) {hold_prob*100:.0f}% + {SELL_COST_MARGIN*100:.0f}pp margin')
    else:
        hold_ev = shares * (hold_prob * 1.0 - entry_price)
        sell_profit = shares * (sell_price - entry_price)
        return ('HOLD', hold_prob, sell_price, sell_profit, hold_ev,
                f'Kalshi TIE bid ${sell_price:.2f} < P(tie holds) {hold_prob*100:.0f}% + {SELL_COST_MARGIN*100:.0f}pp margin')


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except:
        return {}


def save_state(state):
    json.dump(state, open(STATE_FILE, 'w'))


def log_alert(row):
    os.makedirs(os.path.dirname(ALERTS_CSV), exist_ok=True)
    new = not os.path.exists(ALERTS_CSV)
    with open(ALERTS_CSV, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=[
            'alert_ts', 'game', 'event_ticker', 'trigger', 'minute',
            'score', 'kalshi_tie_bid', 'kalshi_tie_ask', 'hold_prob',
            'sell_threshold', 'action', 'shares', 'entry_price',
            'sell_profit', 'hold_ev', 'reason',
        ])
        if new:
            w.writeheader()
        w.writerow(row)


def normalize_team_name(name):
    name = name.strip()
    replacements = {
        'Türkiye': 'Turkey', 'United States': 'USA',
        'Republic of Korea': 'South Korea', 'Korea Republic': 'South Korea',
        'IR Iran': 'Iran', 'Czechia': 'Czech Republic',
    }
    return replacements.get(name, name)


def main():
    cron_mode = '--cron' in sys.argv

    # 1. Get active positions
    positions = load_active_positions()

    if not positions:
        if not cron_mode:
            print("No active TIE positions.")
        return

    # 2. Get ESPN scoreboard
    try:
        scoreboard = get_espn_scoreboard()
    except Exception as e:
        if not cron_mode:
            print(f"ESPN error: {e}")
        return

    live_events = [e for e in scoreboard.get('events', [])
                   if e.get('status', {}).get('type', {}).get('state') in ('in', 'pre')]

    state = load_state()
    alerts = []

    for event in live_events:
        event_id = event['id']

        try:
            summary = get_espn_summary(event_id)
            gs = parse_game_state(summary)
        except Exception as e:
            continue

        # Match to our positions via team codes
        sys.path.insert(0, SCRIPT_DIR)
        from team_codes import NAME_TO_CODE
        home_norm = normalize_team_name(gs['home_team'])
        away_norm = normalize_team_name(gs['away_team'])
        hcode = NAME_TO_CODE.get(home_norm)
        acode = NAME_TO_CODE.get(away_norm)

        if not hcode or not acode:
            continue

        # Derive event ticker
        ct = datetime.fromisoformat(
            summary['header']['competitions'][0]['date'].replace('Z', '+00:00')
        ).astimezone(ET)
        kalshi_date = f"26{ct.strftime('%b').upper()}{ct.strftime('%d').upper()}"
        event_ticker = f"KXWCGAME-{kalshi_date}{hcode}{acode}"

        if event_ticker not in positions:
            continue

        position = positions[event_ticker]
        shares = position['shares']
        entry_price = position['avg_price']

        # Track score state
        state_key = f"eq_{event_id}"
        prev = state.get(state_key, {})
        prev_score = prev.get('score', '0-0')
        prev_goals = prev.get('goals_count', 0)
        prev_alerted = prev.get('alerted', False)
        prev_was_1_0 = prev.get('was_1_0', False)

        current_score = f"{gs['home_score']}-{gs['away_score']}"
        current_goals = len(gs['goals'])
        score_changed = current_score != prev_score

        # Determine current game state
        is_1_0 = ((gs['home_score'] == 1 and gs['away_score'] == 0) or
                  (gs['home_score'] == 0 and gs['away_score'] == 1))
        is_1_1 = (gs['home_score'] == 1 and gs['away_score'] == 1)
        is_2_plus = (gs['home_score'] >= 2 or gs['away_score'] >= 2)
        is_tie = gs['home_score'] == gs['away_score']

        # Parse clock to minute
        clock_str = gs['clock'].replace("'", "").replace("+", "").strip()
        try:
            minute_reg = int(clock_str)
        except:
            minute_reg = 0

        new_alert = None

        # TRIGGER 1: Equalizer (1-0 → 1-1)
        if score_changed and is_1_1 and prev_was_1_0 and not prev_alerted:
            # Equalizer! Get Kalshi TIE price
            kalshi = get_kalshi_tie_market(event_ticker)
            if kalshi:
                tie_bid = kalshi['bid']
                action, hold_prob, sell_price, sell_profit, hold_ev, reason = \
                    compute_sell_decision(tie_bid, minute_reg, entry_price, shares)

                new_alert = (
                    f"EQUALIZER ALERT: {gs['home_team']} vs {gs['away_team']}\n"
                    f"  Score: 1-1 at minute {gs['clock']}\n"
                    f"  Position: {shares} shares at ${entry_price:.3f}\n"
                    f"  Kalshi TIE: bid ${kalshi['bid']:.2f}, ask ${kalshi['ask']:.2f}\n"
                    f"  P(tie holds | 1-1 at min {minute_reg}): {hold_prob*100:.0f}%\n"
                    f"  >>> RECOMMENDATION: {action}\n"
                    f"      {reason}\n"
                )

                if action == 'SELL':
                    new_alert += (
                        f"      Sell {shares} shares at ${sell_price:.2f} = ${sell_price*shares:.2f} received\n"
                        f"      Profit: ${sell_profit:.2f} (risk-free, {(sell_profit/(entry_price*shares))*100:.0f}% return)\n"
                        f"      vs Hold EV: ${hold_ev:.2f} (with {(1-hold_prob)*100:.0f}% loss risk)\n"
                    )
                else:
                    new_alert += (
                        f"      Hold for settlement — {hold_prob*100:.0f}% chance of ${shares:.2f} payout\n"
                        f"      Sell would only get ${sell_price*shares:.2f} (below hold EV ${hold_ev:.2f})\n"
                    )

                log_alert({
                    'alert_ts': datetime.now(timezone.utc).isoformat(),
                    'game': f"{gs['home_team']} vs {gs['away_team']}",
                    'event_ticker': event_ticker,
                    'trigger': 'equalizer',
                    'minute': minute_reg,
                    'score': current_score,
                    'kalshi_tie_bid': kalshi['bid'],
                    'kalshi_tie_ask': kalshi['ask'],
                    'hold_prob': hold_prob,
                    'sell_threshold': hold_prob + SELL_COST_MARGIN,
                    'action': action,
                    'shares': shares,
                    'entry_price': entry_price,
                    'sell_profit': sell_profit,
                    'hold_ev': hold_ev,
                    'reason': reason,
                })

            state[state_key] = {
                'score': current_score, 'goals_count': current_goals,
                'alerted': True, 'was_1_0': False, 'was_1_1': True,
                'last_checked': datetime.now(timezone.utc).isoformat(),
            }

        # TRIGGER 2: Second goal kills the tie (1-0 → 2-0 or 0-2)
        elif score_changed and is_2_plus and not prev_alerted and prev_was_1_0:
            new_alert = (
                f"SECOND GOAL ALERT: {gs['home_team']} vs {gs['away_team']}\n"
                f"  Score: {current_score} at minute {gs['clock']}\n"
                f"  Position: {shares} shares at ${entry_price:.3f}\n"
                f"  >>> RECOMMENDATION: CUT LOSS\n"
                f"      Second goal scored — tie probability near zero\n"
                f"      Sell at market (hit bid) to recover residual value\n"
            )

            kalshi = get_kalshi_tie_market(event_ticker)
            if kalshi:
                new_alert += f"  Kalshi TIE bid: ${kalshi['bid']:.2f} (recover ${kalshi['bid']*shares:.2f})\n"
                log_alert({
                    'alert_ts': datetime.now(timezone.utc).isoformat(),
                    'game': f"{gs['home_team']} vs {gs['away_team']}",
                    'event_ticker': event_ticker,
                    'trigger': 'second_goal',
                    'minute': minute_reg, 'score': current_score,
                    'kalshi_tie_bid': kalshi['bid'], 'kalshi_tie_ask': kalshi['ask'],
                    'hold_prob': 0, 'action': 'CUT_LOSS',
                    'shares': shares, 'entry_price': entry_price,
                    'sell_profit': kalshi['bid'] * shares - entry_price * shares,
                    'hold_ev': 0, 'reason': 'second_goal_kills_tie',
                    'sell_threshold': 0,
                })

            state[state_key] = {
                'score': current_score, 'goals_count': current_goals,
                'alerted': True, 'was_1_0': False, 'was_1_1': False,
            }

        # TRIGGER 3: Game ends (settlement)
        elif gs['state'] == 'post' and prev.get('state', '') != 'post' and not prev_alerted:
            won = is_tie and gs['home_score'] == gs['away_score']
            payout = shares * 1.0 if won else 0
            profit = payout - entry_price * shares
            new_alert = (
                f"SETTLEMENT ALERT: {gs['home_team']} vs {gs['away_team']}\n"
                f"  Final: {current_score}\n"
                f"  Result: {'TIE — WON' if won else 'NO TIE — LOST'}\n"
                f"  Payout: ${payout:.2f}\n"
                f"  Profit: ${profit:+.2f}\n"
            )

            state[state_key] = {
                'score': current_score, 'goals_count': current_goals,
                'alerted': True, 'settled': True,
            }

        # Update state
        state[state_key] = state.get(state_key, {})
        state[state_key].update({
            'score': current_score,
            'goals_count': current_goals,
            'was_1_0': is_1_0,
            'was_1_1': is_1_1,
            'state': gs['state'],
            'last_checked': datetime.now(timezone.utc).isoformat(),
        })

        if not cron_mode:
            print(f"\n{gs['home_team']} vs {gs['away_team']}: {current_score} (min {gs['clock']}, {gs['status']})")
            print(f"  Position: {shares} shares at ${entry_price:.3f}")
            print(f"  State: 1-0={is_1_0}, 1-1={is_1_1}, 2+={is_2_plus}, tie={is_tie}")

        if new_alert:
            alerts.append(new_alert)

    save_state(state)

    if cron_mode:
        if alerts:
            print(f"WC EQUALIZER MONITOR — {datetime.now(ET).strftime('%H:%M ET')}")
            for a in alerts:
                print(a)
    else:
        if alerts:
            print("\n" + "=" * 80)
            print("ALERTS")
            print("=" * 80)
            for a in alerts:
                print(a)
        else:
            print("\nNo alerts.")


if __name__ == "__main__":
    main()
