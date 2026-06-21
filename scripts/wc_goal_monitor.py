#!/usr/bin/env python3
"""
wc_goal_monitor.py — Automated World Cup goal detection + TIE edge alerting.

Runs as a cron job every 1 minute during WC game windows.
Self-gates: only makes API calls when ESPN shows a game is in progress.
Silent when nothing happens (empty stdout = no alert in no_agent cron).

What it does:
1. Pre-game: classifies each game as close/mismatch using The Odds API
2. Live: polls ESPN scoreboard for score changes
3. On first goal in a close game:
   - Pulls Kalshi TIE price
   - Computes edge against historical tie rates (underdog-first vs favorite-first)
   - Alerts if edge exceeds threshold (6pp)
   - Logs to data/goal_alerts.csv
4. Tracks score state in /tmp/wc_monitor_state.json

Usage:
    python3 wc_goal_monitor.py --cron     # cron mode (silent unless alert)
    python3 wc_goal_monitor.py            # verbose (interactive)
"""
import os, sys, json, csv, subprocess, urllib.request, time
from datetime import datetime, timezone, timedelta
from decimal import Decimal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = '/tmp/wc_monitor_state.json'
ALERTS_CSV = os.path.join(SCRIPT_DIR, '..', 'data', 'goal_alerts.csv')
ET = timezone(timedelta(hours=-4))

# Historical tie rates from EDGE.md
# Close games only (final margin ≤ 1), split by who scored first
TIE_RATES = {
    'underdog': {15: 0.466, 30: 0.384, 45: 0.337, 60: 0.310, 75: 0.171},
    'favorite': {15: 0.364, 30: 0.364, 45: 0.268, 60: 0.177, 75: 0.165},
}

ALERT_THRESHOLD_PP = 6.0  # minimum edge to trigger alert
COST_MARGIN_PP = 6.0      # safety margin for fees + spread


def get_odds_key():
    return subprocess.check_output(["pass", "show", "oddsdotcom"]).decode().strip()


def pull_odds_api():
    """Pull WC moneylines from The Odds API. Returns list of games."""
    key = get_odds_key()
    url = (f"https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds/"
           f"?apiKey={key}&regions=us&markets=h2h&oddsFormat=american")
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def normalize_team_name(name):
    """Normalize team names between ESPN and Odds API."""
    name = name.strip()
    replacements = {
        'Türkiye': 'Turkey',
        'Türki̇ye': 'Turkey',
        'Republic of Korea': 'South Korea',
        'Korea Republic': 'South Korea',
        'IR Iran': 'Iran',
        'Czechia': 'Czech Republic',
        'Côte d\'Ivoire': 'Ivory Coast',
        'United States': 'USA',
    }
    return replacements.get(name, name)


def classify_game(game):
    """Classify a game as 'close' or 'mismatch' based on Vegas moneyline.
    Close = favorite moneyline ≤ -200 (i.e., -133, -163 qualify; -809 does not).
    Returns (classification, favorite_team, underdog_team, fav_odds, und_odds).
    Team names are normalized for ESPN matching."""
    outcomes = {}
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] != "h2h":
                continue
            for o in mkt["outcomes"]:
                outcomes.setdefault(o["name"], []).append(o["price"])

    medians = {}
    for name, prices in outcomes.items():
        ps = sorted(prices)
        medians[name] = ps[len(ps) // 2]

    # Find favorite (most negative odds) and underdog
    teams = [(name, odds) for name, odds in medians.items() if name != "Draw"]
    if len(teams) < 2:
        return 'unknown', None, None, None, None

    teams.sort(key=lambda x: x[1])  # most negative (favorite) first
    fav_name, fav_odds = teams[0]
    und_name, und_odds = teams[1]

    # Normalize names for ESPN matching
    fav_name = normalize_team_name(fav_name)
    und_name = normalize_team_name(und_name)

    # Close game: favorite odds > -200 (less favored than -200)
    classification = 'close' if fav_odds > -200 else 'mismatch'

    return classification, fav_name, und_name, fav_odds, und_odds


def get_espn_scoreboard():
    """Pull ESPN WC scoreboard. Returns list of events."""
    url = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def get_espn_summary(event_id):
    """Pull ESPN game summary with goal details."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event={event_id}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def parse_game_state(summary):
    """Extract score, clock, status, goals from ESPN summary."""
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
            result['goals'].append({
                'minute': g.get('clock', {}).get('displayValue', '?'),
                'minute_regulation': int(g.get('clock', {}).get('value', 0)) // 60 if g.get('clock', {}).get('value') else 0,
                'team': g.get('team', {}).get('displayName', '?'),
                'team_abbr': g.get('team', {}).get('abbreviation', '?'),
                'is_home': g.get('home_team') == '1',
                'scorer': g.get('participants', [{}])[0].get('athlete', {}).get('displayName', '?'),
            })

    return result


def get_kalshi_tie_price(event_ticker):
    """Pull Kalshi TIE market for a given event."""
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


def find_closest_minute(minute_regulation):
    """Map actual minute to nearest threshold in TIE_RATES."""
    thresholds = [15, 30, 45, 60, 75]
    return min(thresholds, key=lambda t: abs(t - minute_regulation))


def compute_edge(tie_price, minute, scorer_type):
    """Compute edge = historical_tie_rate - kalshi_tie_price."""
    closest = find_closest_minute(minute)
    hist_rate = TIE_RATES[scorer_type].get(closest, 0)
    edge_pp = (hist_rate - tie_price) * 100
    buy_threshold = hist_rate - (COST_MARGIN_PP / 100)
    return hist_rate, edge_pp, buy_threshold, closest


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
            'alert_ts', 'game', 'event_ticker', 'scorer_type', 'goal_minute',
            'goal_team', 'goal_scorer', 'score', 'kalshi_tie_last', 'kalshi_tie_bid',
            'kalshi_tie_ask', 'historical_tie_rate', 'edge_pp', 'buy_threshold',
            'kalshi_tie_vol24h', 'action',
        ])
        if new:
            w.writeheader()
        w.writerow(row)


def main():
    cron_mode = '--cron' in sys.argv

    # 1. Pull ESPN scoreboard — get all live games
    try:
        scoreboard = get_espn_scoreboard()
    except Exception as e:
        if not cron_mode:
            print(f"ESPN scoreboard error: {e}")
        return

    live_events = [e for e in scoreboard.get('events', [])
                   if e.get('status', {}).get('type', {}).get('state') in ('in', 'pre')]

    if not live_events:
        if not cron_mode:
            print("No live games.")
        return

    # 2. Pull Odds API for game classification
    # CRITICAL: only pull and cache classifications when games are PRE-game.
    # Once a game is live, the Odds API returns live odds (which change with score).
    # We need the PRE-GAME odds for classification.
    state = load_state()
    today = datetime.now(ET).strftime('%Y-%m-%d')

    # Check if we need to refresh classifications (once per day, or if we haven't yet)
    need_refresh = state.get('odds_date') != today
    if need_refresh:
        try:
            odds_games = pull_odds_api()
            new_classifications = {}
            for g in odds_games:
                ct = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
                game_state = 'pre' if ct > datetime.now(timezone.utc) else 'live'
                
                cls, fav, und, fav_odds, und_odds = classify_game(g)
                key = f"{g['home_team']} vs {g['away_team']}"
                
                # Only store new classification if game hasn't started yet
                # OR if we don't already have one for this game
                existing = state.get('classifications', {}).get(key)
                if game_state == 'pre' or not existing:
                    new_classifications[key] = {
                        'classification': cls,
                        'favorite': fav,
                        'underdog': und,
                        'fav_odds': fav_odds,
                        'und_odds': und_odds,
                    }
                else:
                    # Keep existing (pre-game) classification
                    new_classifications[key] = existing

            state['classifications'] = new_classifications
            state['odds_date'] = today
            if not cron_mode:
                print(f"Classified {len(new_classifications)} games from Odds API")
        except Exception as e:
            if not cron_mode:
                print(f"Odds API error: {e}")

    # 3. Match ESPN events to Odds API classifications
    alerts = []
    for event in live_events:
        event_id = event['id']
        event_name = event['name']  # e.g., "Paraguay at Türkiye"

        # Pull ESPN summary for live games
        try:
            summary = get_espn_summary(event_id)
            gs = parse_game_state(summary)
        except Exception as e:
            if not cron_mode:
                print(f"ESPN summary error for {event_id}: {e}")
            continue

        game_key = f"{normalize_team_name(gs['home_team'])} vs {normalize_team_name(gs['away_team'])}"
        # Try reverse key too
        game_key_rev = f"{normalize_team_name(gs['away_team'])} vs {normalize_team_name(gs['home_team'])}"

        cls_data = state.get('classifications', {}).get(game_key) or \
                   state.get('classifications', {}).get(game_key_rev, {})

        classification = cls_data.get('classification', 'unknown')
        favorite = cls_data.get('favorite')
        underdog = cls_data.get('underdog')

        # Track score state per event
        state_key = f"event_{event_id}"
        prev_state = state.get(state_key, {})
        prev_score = prev_state.get('score', '0-0')
        prev_goals = prev_state.get('goals_count', 0)
        alerted = prev_state.get('alerted', False)

        current_score = f"{gs['home_score']}-{gs['away_score']}"
        current_goals = len(gs['goals'])

        score_changed = current_score != prev_score
        new_goal = current_goals > prev_goals

        # Update state
        state[state_key] = {
            'score': current_score,
            'goals_count': current_goals,
            'alerted': alerted,
            'last_checked': datetime.now(timezone.utc).isoformat(),
        }

        if not cron_mode:
            print(f"\n{gs['home_team']} vs {gs['away_team']}: {current_score} "
                  f"(min {gs['clock']}, {gs['status']})")
            print(f"  Classification: {classification}")
            if favorite:
                print(f"  Favorite: {favorite}, Underdog: {underdog}")
            print(f"  Goals: {len(gs['goals'])}, Score changed: {score_changed}, New goal: {new_goal}")

        # 4. On first goal in a close game → alert
        if new_goal and not alerted and classification == 'close' and len(gs['goals']) == 1:
            goal = gs['goals'][0]
            goal_team = goal['team']
            minute_reg = goal.get('minute_regulation', 0)

            # Determine if favorite or underdog scored first
            scorer_type = 'unknown'
            if favorite and underdog:
                if goal_team == favorite:
                    scorer_type = 'favorite'
                elif goal_team == underdog:
                    scorer_type = 'underdog'
                else:
                    # Try fuzzy match
                    if favorite.lower() in goal_team.lower() or goal_team.lower() in favorite.lower():
                        scorer_type = 'favorite'
                    elif underdog.lower() in goal_team.lower() or goal_team.lower() in underdog.lower():
                        scorer_type = 'underdog'

            if scorer_type == 'unknown':
                # Can't classify, skip
                if not cron_mode:
                    print(f"  Can't classify scorer: {goal_team} (fav={favorite}, und={underdog})")
                continue

            # Derive Kalshi event ticker
            # We need the team codes — try to find from the event ticker pattern
            # For now, skip ticker derivation and just alert
            alert_msg = (
                f"GOAL ALERT: {gs['home_team']} vs {gs['away_team']} "
                f"| {goal_team} ({scorer_type}) scored at min {goal['minute']} "
                f"| Score: {current_score} "
                f"| Classification: {classification} "
                f"| Scorer: {goal['scorer']}"
            )

            # Try to get Kalshi TIE price
            kalshi_data = None
            try:
                # Derive event ticker from team codes
                sys.path.insert(0, SCRIPT_DIR)
                from team_codes import NAME_TO_CODE
                # Normalize ESPN team names to match Odds API names in NAME_TO_CODE
                home_norm = normalize_team_name(gs['home_team'])
                away_norm = normalize_team_name(gs['away_team'])
                hcode = NAME_TO_CODE.get(home_norm)
                acode = NAME_TO_CODE.get(away_norm)
                if hcode and acode:
                    ct = datetime.fromisoformat(
                        summary['header']['competitions'][0]['date'].replace('Z', '+00:00')
                    ).astimezone(ET)
                    kalshi_date = f"26{ct.strftime('%b').upper()}{ct.strftime('%d').upper()}"
                    event_ticker = f"KXWCGAME-{kalshi_date}{hcode}{acode}"
                    kalshi_data = get_kalshi_tie_price(event_ticker)
                    if kalshi_data:
                        tie_price = kalshi_data['last'] or kalshi_data['ask']
                        hist_rate, edge_pp, buy_threshold, closest_min = compute_edge(
                            tie_price, minute_reg, scorer_type
                        )

                        alert_msg += (
                            f"\n  Kalshi TIE: ${tie_price:.2f} (bid {kalshi_data['bid']:.2f}, ask {kalshi_data['ask']:.2f})"
                            f"\n  Historical tie rate ({scorer_type} first, min {closest_min}): {hist_rate*100:.1f}%"
                            f"\n  Edge: {edge_pp:+.1f}pp"
                            f"\n  Buy threshold: ${buy_threshold:.2f}"
                            f"\n  Vol 24h: {kalshi_data['vol24h']:,.0f}"
                        )

                        if edge_pp >= ALERT_THRESHOLD_PP:
                            alert_msg += f"\n  >>> ACTION: BUY TIE (edge {edge_pp:+.1f}pp exceeds {ALERT_THRESHOLD_PP}pp threshold)"
                            action = 'BUY'
                        else:
                            alert_msg += f"\n  >>> Edge below {ALERT_THRESHOLD_PP}pp threshold, no action"
                            action = 'WATCH'

                        # Log to CSV
                        log_alert({
                            'alert_ts': datetime.now(timezone.utc).isoformat(),
                            'game': f"{gs['home_team']} vs {gs['away_team']}",
                            'event_ticker': event_ticker,
                            'scorer_type': scorer_type,
                            'goal_minute': goal['minute'],
                            'goal_team': goal_team,
                            'goal_scorer': goal['scorer'],
                            'score': current_score,
                            'kalshi_tie_last': tie_price,
                            'kalshi_tie_bid': kalshi_data['bid'],
                            'kalshi_tie_ask': kalshi_data['ask'],
                            'historical_tie_rate': hist_rate,
                            'edge_pp': edge_pp,
                            'buy_threshold': buy_threshold,
                            'kalshi_tie_vol24h': kalshi_data['vol24h'],
                            'action': action,
                        })
            except Exception as e:
                alert_msg += f"\n  Kalshi lookup failed: {e}"

            alerts.append(alert_msg)
            state[state_key]['alerted'] = True

        elif new_goal and alerted and len(gs['goals']) > 1:
            goal = gs['goals'][-1]
            alert_msg = (
                f"GOAL UPDATE: {gs['home_team']} vs {gs['away_team']} "
                f"| {goal['team']} scored at min {goal['minute']} "
                f"| Score: {current_score} "
                f"| Previous alert already sent"
            )
            alerts.append(alert_msg)

    save_state(state)

    # 5. Output
    if cron_mode:
        # Only print if there are alerts
        if alerts:
            print(f"WC GOAL MONITOR — {datetime.now(ET).strftime('%H:%M ET')}")
            for a in alerts:
                print(a)
            print()
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
