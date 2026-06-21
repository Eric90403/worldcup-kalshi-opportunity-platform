#!/usr/bin/env python3
"""monitor_turpar.py — watch Kalshi order fills + ESPN live score.
Silent when nothing changes. Prints alerts on: goals, score changes,
game status changes, order fills. Polls every 30 seconds.
"""
import json, time, subprocess, sys, os
from datetime import datetime, timezone

sys.path.insert(0, './scripts')
from kalshi_auth import KalshiClient

ORDER_ID = "4b89c1b9-65c7-4dfc-83af-6ae85767a83a"
ESPN_EVENT_ID = 760443
STATE_FILE = '/tmp/turpar_monitor_state.json'
kc = KalshiClient()

import urllib.request

def get_espn():
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event={ESPN_EVENT_ID}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)

def get_order():
    r = kc.get('/portfolio/orders')
    for o in r.get('orders', []):
        if o.get('order_id') == ORDER_ID:
            return o
    return None

def load_state():
    try:
        return json.load(open(STATE_FILE))
    except:
        return {"last_score": None, "last_status": None, "last_fill": "0.00", "last_goals": []}

def save_state(s):
    json.dump(s, open(STATE_FILE, 'w'))

state = load_state()
print(f"Monitoring TURPAR — order {ORDER_ID[:8]}... + ESPN event {ESPN_EVENT_ID}")
print(f"Game: Turkey vs Paraguay | Polling every 30s | {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
print("---")

while True:
    try:
        # 1. ESPN
        espn = get_espn()
        c = espn['header']['competitions'][0]
        comps = c.get('competitors', [])
        home = [t for t in comps if t.get('homeAway') == 'home']
        away = [t for t in comps if t.get('homeAway') == 'away']
        hs = home[0]['score'] if home else '0'
        as_ = away[0]['score'] if away else '0'
        score = f"{hs}-{as_}"
        clock = c.get('status', {}).get('displayClock', '?')
        status = c.get('status', {}).get('type', {}).get('description', '?')
        goals = []
        for g in c.get('details', []):
            if g.get('scoringPlay'):
                m = g.get('clock', {}).get('displayValue', '?')
                t = g.get('team', {}).get('abbreviation', '?')
                p = g.get('participants', [{}])[0].get('athlete', {}).get('displayName', '?')
                goals.append(f"{m} {t} ({p})")

        alerts = []
        if score != state.get('last_score'):
            alerts.append(f"SCORE: {state.get('last_score','?')} -> {score} (min {clock})")
        if status != state.get('last_status'):
            alerts.append(f"STATUS: {state.get('last_status','?')} -> {status}")
        new_goals = [g for g in goals if g not in state.get('last_goals', [])]
        for g in new_goals:
            alerts.append(f"GOAL: {g}")
        state['last_score'] = score
        state['last_status'] = status
        state['last_goals'] = goals

        # 2. Kalshi order
        order = get_order()
        if order:
            fill = order.get('fill_count_fp', '0.00')
            status_o = order.get('status', '?')
            if fill != state.get('last_fill', '0.00'):
                alerts.append(f"ORDER: fill {state.get('last_fill','0')} -> {fill}, status={status_o}")
                state['last_fill'] = fill
                # Also print position if filled
                if float(fill) > 0:
                    pos = kc.get('/portfolio/positions')
                    for mp in pos.get('market_positions', []):
                        if 'TURPAR' in mp.get('ticker', ''):
                            alerts.append(f"POSITION: {mp['position_fp']} shares, cost ${mp['total_traded_dollars']}, fees ${mp['fees_paid_dollars']}")

        if alerts:
            ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
            for a in alerts:
                print(f"[{ts} UTC] {a}")

        save_state(state)
    except Exception as e:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] ERROR: {e}")

    time.sleep(30)
