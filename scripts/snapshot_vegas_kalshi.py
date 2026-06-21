#!/usr/bin/env python3
"""
snapshot_vegas_kalshi.py — compare The Odds API (Vegas 3-way) vs Kalshi live
API (3-way game-winner) for World Cup games.

Modes:
  python3 snapshot_vegas_kalshi.py                # today, verbose (interactive)
  python3 snapshot_vegas_kalshi.py 2026-06-19     # explicit date, verbose
  python3 snapshot_vegas_kalshi.py --cron         # cron mode (see below)

Cron mode (--cron):
  - Checks if any WC game is currently live (commence_time < now < commence + 105m).
  - If a game is live: always snapshot (15-min cadence).
  - If no game is live: snapshot only on :00 and :30 (30-min cadence).
  - Silent on normal runs (empty stdout = no alert in no_agent cron).
  - Prints to stdout ONLY if any |edge| >= ALERT_THRESHOLD pp (5pp default),
    so significant divergences get delivered as alerts.

CSV: data/vegas_kalshi_baseline.csv (appended).
"""
import os, sys, json, csv, subprocess, urllib.request
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_CSV = os.path.join(SCRIPT_DIR, "..", "data", "vegas_kalshi_baseline.csv")
ALERT_THRESHOLD = 5.0  # pp — only print stdout if any |edge| >= this
GAME_DURATION_MIN = 105  # soccer: 90 + 15 halftime + stoppage buffer

ET = timezone(timedelta(hours=-4))


def get_odds_key():
    return subprocess.check_output(["pass", "show", "oddsdotcom"]).decode().strip()


def american_to_prob(p):
    if p > 0:
        return Decimal(100) / Decimal(p + 100)
    else:
        return Decimal(-p) / Decimal(-p + 100)


def novig_3way(odds_a, odds_b, odds_c):
    pa, pb, pc = american_to_prob(odds_a), american_to_prob(odds_b), american_to_prob(odds_c)
    s = pa + pb + pc
    return pa / s, pb / s, pc / s


def median_book_odds(game):
    outcomes = {}
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] != "h2h":
                continue
            for o in mkt["outcomes"]:
                outcomes.setdefault(o["name"], []).append(o["price"])
    return {name: sorted(ps)[len(ps) // 2] for name, ps in outcomes.items()}


def pull_odds_api():
    key = get_odds_key()
    url = ("https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds/"
           f"?apiKey={key}&regions=us&markets=h2h&oddsFormat=american")
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def any_game_live(games, now=None):
    """True if any game is currently in-play (commence_time <= now <= commence + 105m)."""
    if now is None:
        now = datetime.now(timezone.utc)
    for g in games:
        ct = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
        end = ct + timedelta(minutes=GAME_DURATION_MIN)
        if ct <= now <= end:
            return True, g
    return False, None


def snapshot(games, verbose=False):
    """Run the comparison for a list of Odds API games. Returns list of row dicts."""
    sys.path.insert(0, SCRIPT_DIR)
    from kalshi_auth import KalshiClient
    from team_codes import NAME_TO_CODE

    kc = KalshiClient()
    rows = []

    for g in games:
        home, away = g["home_team"], g["away_team"]
        hcode = NAME_TO_CODE.get(home)
        acode = NAME_TO_CODE.get(away)
        if hcode is None or acode is None:
            if verbose:
                print(f"  WARN: no code for {home} or {away}, skipping")
            continue

        ct_et = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00")).astimezone(ET)
        kalshi_date = f"26{ct_et.strftime('%b').upper()}{ct_et.strftime('%d').upper()}"
        event_ticker = f"KXWCGAME-{kalshi_date}{hcode}{acode}"

        r = kc.get("/markets", params={"event_ticker": event_ticker, "limit": 20})
        kalshi_markets = {}
        if "markets" in r:
            for m in r["markets"]:
                leg = m["ticker"].rsplit("-", 1)[-1]
                kalshi_markets[leg] = m

        vegas_med = median_book_odds(g)
        h_odds = vegas_med.get(home)
        a_odds = vegas_med.get(away)
        d_odds = vegas_med.get("Draw")
        if h_odds is None or a_odds is None or d_odds is None:
            if verbose:
                print(f"  WARN {event_ticker}: missing Vegas outcome. have {list(vegas_med.keys())}")
            continue

        h_novig, a_novig, d_novig = novig_3way(h_odds, a_odds, d_odds)
        snap_ts = datetime.now(timezone.utc).isoformat()

        for side, vegas_prob, kalshi_leg, code, ml in [
            (home, h_novig, kalshi_markets.get(hcode), hcode, h_odds),
            (away, a_novig, kalshi_markets.get(acode), acode, a_odds),
            ("TIE", d_novig, kalshi_markets.get("TIE"), "TIE", d_odds),
        ]:
            if kalshi_leg is None:
                if verbose:
                    print(f"  WARN {event_ticker}: no Kalshi leg for {code}")
                continue
            kp = Decimal(kalshi_leg.get("last_price_dollars", "0") or "0")
            kb = Decimal(kalshi_leg.get("yes_bid_dollars", "0") or "0")
            ka = Decimal(kalshi_leg.get("yes_ask_dollars", "0") or "0")
            vol24 = kalshi_leg.get("volume_24h_fp", "0")
            edge = (kp - vegas_prob) * Decimal(100)
            rows.append({
                "snapshot_ts": snap_ts,
                "event_ticker": event_ticker,
                "game": f"{home} vs {away}",
                "side": side,
                "side_code": code,
                "vegas_ml": ml,
                "vegas_novig_3way": float(round(vegas_prob, 4)),
                "kalshi_last": float(round(kp, 4)),
                "kalshi_bid": float(round(kb, 4)),
                "kalshi_ask": float(round(ka, 4)),
                "kalshi_vol_24h": vol24,
                "edge_pp": float(round(edge, 1)),
                "vegas_event_id": g["id"],
            })
    return rows


def write_csv(rows):
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    new = not os.path.exists(OUT_CSV)
    with open(OUT_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "snapshot_ts", "event_ticker", "game", "side", "side_code",
            "vegas_ml", "vegas_novig_3way", "kalshi_last", "kalshi_bid",
            "kalshi_ask", "kalshi_vol_24h", "edge_pp", "vegas_event_id",
        ])
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    cron_mode = "--cron" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--cron"]

    # 1. Pull all upcoming WC games from Odds API
    games = pull_odds_api()

    now = datetime.now(timezone.utc)

    if cron_mode:
        # Decide whether to snapshot this tick
        live, live_game = any_game_live(games, now)
        minute = now.minute
        if not live and minute not in (0, 30):
            # 30-min cadence, not a :00/:30 slot, no game live → skip silently
            return
        # In cron mode, snapshot all games for today (ET) + tomorrow (ET) to catch
        # late games. Filter to games within +-1 day of now.
        target_dates = {now.astimezone(ET).date(), (now + timedelta(days=1)).astimezone(ET).date()}
        today_games = [g for g in games
                       if datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00")).astimezone(ET).date() in target_dates]
    else:
        target = args[0] if args else now.astimezone(ET).date().isoformat()
        target_dt = datetime.fromisoformat(target + "T00:00:00+00:00")
        today_games = [g for g in games
                       if datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00")).astimezone(ET).date() == target_dt.date()]
        print(f"Vegas games on {target}: {len(today_games)}")
        for g in today_games:
            print(f"  {g['home_team']} vs {g['away_team']} @ {g['commence_time']}")

    # 2. Snapshot
    rows = snapshot(today_games, verbose=not cron_mode)
    write_csv(rows)

    if cron_mode:
        # Only produce stdout (→ alert) if any edge exceeds threshold
        big = [r for r in rows if abs(r["edge_pp"]) >= ALERT_THRESHOLD]
        if big:
            print(f"KALSHI-VEGAS EDGE ALERT ({len(big)} legs >= {ALERT_THRESHOLD}pp) — {datetime.now(ET).strftime('%H:%M ET')}")
            for r in big:
                print(f"  {r['game']:30s} {r['side_code']:4s}  Vegas {r['vegas_novig_3way']:.1%}  Kalshi {r['kalshi_last']:.1%}  edge {r['edge_pp']:+.1f}pp  vol24h={r['kalshi_vol_24h']}")
    else:
        print(f"\nWrote {len(rows)} rows to {OUT_CSV}")
        print("\n=== EDGE SUMMARY (Kalshi last − Vegas no-vig, in pp) ===")
        for r in rows:
            print(f"  {r['game']:30s} {r['side_code']:4s}  Vegas {r['vegas_novig_3way']:.1%}  Kalshi {r['kalshi_last']:.1%}  edge {r['edge_pp']:+.1f}pp")


if __name__ == "__main__":
    main()
