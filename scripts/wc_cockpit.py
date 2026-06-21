#!/usr/bin/env python3
"""
wc_cockpit.py — WC Trading Cockpit web app.

A browser-based interface that combines:
  1. Chat with Gemma4 (vLLM, localhost:8765) with streaming responses
  2. Live WC games panel (ESPN scoreboard, auto-refreshing)
  3. Kalshi prices for all 3 legs (fav/und/tie)
  4. Edge detector commentary feed (tails data/live_edge_commentary.log)
  5. Account balance + position status

Run:
  python3 scripts/wc_cockpit.py
  # opens on http://localhost:8877

Access from other devices via Tailscale:
  http://your-tailnet-hostname:8877  (or http://100.x.x.x:8877)
"""
import os, sys, json, time, asyncio, urllib.request, subprocess, csv, math
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import uvicorn
import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.join(SCRIPT_DIR, '..')
COMMENTARY_LOG = os.path.join(PROJECT_DIR, 'data', 'live_edge_commentary.log')
RUN_LOG = os.path.join(PROJECT_DIR, 'data', 'live_edge_run.log')
TRADE_LOG = os.path.join(PROJECT_DIR, 'data', 'trade_log.csv')

VLLM_URL = "http://localhost:8765/v1/chat/completions"
VLLM_MODELS_URL = "http://localhost:8765/v1/models"
VLLM_MODEL = "gemma4-31b"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
ET = timezone(timedelta(hours=-4))

def http_get_json(url, timeout=15):
    """Simple HTTP GET that returns parsed JSON."""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)

DEFAULT_SYSTEM_PROMPT = (
    "You are a Kalshi World Cup trading analyst. You have access to real-time game state, "
    "Kalshi market prices, and a transition matrix model built from 94,792 club football matches. "
    "Answer questions about trading strategy, edge calculations, and game state. Be direct and concise. "
    "When discussing probabilities, cite the matrix numbers. When recommending actions, state the edge "
    "in percentage points and the reasoning. "
    "CRITICAL: Use ONLY the live game data provided in the system prompt. Do not make up prices, "
    "probabilities, edges, or statistics. If a data point is not provided, say 'data not available' "
    "rather than guessing. Every number you cite must come from the injected live game state."
)

app = FastAPI(title="WC Trading Cockpit")

# Cache expensive resources instead of recreating per request
_MATRIX = None
_KALSHI_CLIENT = None

def get_matrix():
    global _MATRIX
    if _MATRIX is None:
        matrix_path = os.path.join(PROJECT_DIR, 'data', 'combined_transition_matrix.json')
        try:
            _MATRIX = json.load(open(matrix_path))
        except Exception:
            _MATRIX = {}
    return _MATRIX

def get_kalshi():
    global _KALSHI_CLIENT
    if _KALSHI_CLIENT is None:
        try:
            sys.path.insert(0, SCRIPT_DIR)
            from kalshi_auth import KalshiClient
            _KALSHI_CLIENT = KalshiClient()
        except Exception:
            return None
    return _KALSHI_CLIENT

def read_cached_classifications():
    """Read pre-game classifications cached by the edge detector daemon."""
    try:
        with open('/tmp/wc_live_edge_state.json') as f:
            state = json.load(f)
        return state.get('classifications', {})
    except Exception:
        return {}

def normalize_team_name(name):
    name = (name or '').strip()
    repl = {
        'Türkiye': 'Turkey', 'Türki̇ye': 'Turkey',
        'Republic of Korea': 'South Korea', 'Korea Republic': 'South Korea',
        'IR Iran': 'Iran', 'Czechia': 'Czech Republic',
        "Côte d'Ivoire": 'Ivory Coast', 'United States': 'USA',
    }
    return repl.get(name, name)

def parse_clock_minute(clock_str):
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

def bucket_minute(minute):
    b = (int(minute) // 5) * 5
    return min(max(b, 0), 90)

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
    return 0.5

def vegas_calibrate(matrix_probs, matrix, odds_class, fav_american, und_american, draw_american=None):
    """Vegas-anchored calibration of matrix probabilities.

    Shifts the matrix probability surface so the pre-game point matches Vegas
    no-vig probabilities, while preserving the matrix's transition shape.

    Uses log-ratio calibration: for each outcome i,
        adjusted_p_i = matrix_p_i(m,s) * exp(delta_i)
    where delta_i = ln(p_vegas_i / p_matrix_i(0,0))
    Then renormalizes to sum to 1.
    """
    if not fav_american or not und_american:
        return matrix_probs

    cls = matrix.get(odds_class) or matrix.get('close')
    if not cls or '0' not in cls or '0-0' not in cls['0']:
        return matrix_probs
    baseline = cls['0']['0-0']
    p_fav_m0 = baseline.get('fav_win', 0)
    p_tie_m0 = baseline.get('tie', 0)
    p_und_m0 = baseline.get('und_win', 0)
    if p_fav_m0 <= 0 or p_tie_m0 <= 0 or p_und_m0 <= 0:
        return matrix_probs

    p_fav_v = american_to_implied(fav_american)
    p_und_v = american_to_implied(und_american)
    if not p_fav_v or not p_und_v:
        return matrix_probs

    if draw_american is not None:
        p_tie_v = american_to_implied(draw_american)
        if p_tie_v:
            total = p_fav_v + p_tie_v + p_und_v
            p_fav_vegas = p_fav_v / total
            p_tie_vegas = p_tie_v / total
            p_und_vegas = p_und_v / total
        else:
            draw_american = None

    if draw_american is None:
        p_fav_vegas = p_fav_v
        remainder = 1.0 - p_fav_vegas
        tie_und_ratio = p_tie_m0 / (p_tie_m0 + p_und_m0)
        p_tie_vegas = remainder * tie_und_ratio
        p_und_vegas = remainder * (1.0 - tie_und_ratio)

    delta_fav = math.log(p_fav_vegas / p_fav_m0)
    delta_tie = math.log(p_tie_vegas / p_tie_m0)
    delta_und = math.log(p_und_vegas / p_und_m0)

    raw_fav = matrix_probs['fav_win'] * math.exp(delta_fav)
    raw_tie = matrix_probs['tie'] * math.exp(delta_tie)
    raw_und = matrix_probs['und_win'] * math.exp(delta_und)
    total = raw_fav + raw_tie + raw_und
    if total <= 0:
        return matrix_probs

    calibrated = dict(matrix_probs)
    calibrated['fav_win'] = raw_fav / total
    calibrated['tie'] = raw_tie / total
    calibrated['und_win'] = raw_und / total
    calibrated['calibrated'] = True
    calibrated['vegas_fav'] = p_fav_vegas
    calibrated['vegas_tie'] = p_tie_vegas
    calibrated['vegas_und'] = p_und_vegas
    return calibrated

def derive_event_ticker(home_name, away_name, start_date_str):
    """Derive KXWCGAME-26{MON}{DD}{HOMECODE}{AWAYCODE} from team names + date."""
    sys.path.insert(0, SCRIPT_DIR)
    try:
        from team_codes import NAME_TO_CODE
    except Exception:
        return None
    home_norm = normalize_team_name(home_name)
    away_norm = normalize_team_name(away_name)
    hcode = NAME_TO_CODE.get(home_norm)
    acode = NAME_TO_CODE.get(away_norm)
    if not hcode or not acode:
        return None
    try:
        ct = datetime.fromisoformat(start_date_str.replace('Z', '+00:00')).astimezone(ET)
    except Exception:
        return None
    kalshi_date = f"26{ct.strftime('%b').upper()}{ct.strftime('%d').upper()}"
    return f"KXWCGAME-{kalshi_date}{hcode}{acode}"

def lookup_matrix(matrix, odds_class, minute, fav_score, und_score):
    """Return {fav_win, tie, und_win, sample, state_used, minute_bucket} or None."""
    cls = matrix.get(odds_class) or matrix.get('close')
    if not cls:
        return None
    mb = bucket_minute(minute)
    cell = cls.get(str(mb))
    if not cell:
        for delta in (5, -5, 10, -10):
            alt = str(min(max(mb + delta, 0), 90))
            cell = cls.get(alt)
            if cell:
                break
    if not cell:
        return None
    target = f"{fav_score}-{und_score}"
    state = target if target in cell else None
    if not state:
        target_gd = fav_score - und_score
        target_total = fav_score + und_score
        candidates = []
        for s in cell:
            try:
                f, u = s.split('-')
                gd = int(f) - int(u)
                total = int(f) + int(u)
            except (ValueError, IndexError):
                continue
            score = (abs(gd - target_gd) * 100) + abs(total - target_total)
            candidates.append((score, s))
        if not candidates:
            return None
        candidates.sort()
        state = candidates[0][1]
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
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    """Check if vLLM is reachable and return model info."""
    try:
        resp = requests.get(VLLM_MODELS_URL, timeout=5)
        data = resp.json()
        models = [m["id"] for m in data.get("data", [])]
        return {"vllm": "online", "models": models, "default_model": VLLM_MODEL}
    except Exception as e:
        return {"vllm": "offline", "error": str(e)}

@app.get("/api/games")
async def get_games():
    """Pull ESPN scoreboard + Kalshi prices + matrix probs + edges for all WC games."""
    try:
        with urllib.request.urlopen(ESPN_SCOREBOARD, timeout=15) as r:
            sb = json.load(r)
    except Exception as e:
        return {"error": f"ESPN: {e}"}

    matrix = get_matrix()
    classifications = read_cached_classifications()
    kc = get_kalshi()

    games = []
    for ev in sb.get("events", []):
        state = ev.get("status", {}).get("type", {}).get("state", "?")
        desc = ev.get("status", {}).get("type", {}).get("description", "?")
        competitions = ev.get("competitions", [])
        if not competitions:
            continue
        c = competitions[0]
        comps = c.get("competitors", [])
        home = [t for t in comps if t.get("homeAway") == "home"]
        away = [t for t in comps if t.get("homeAway") == "away"]

        home_team = home[0]["team"]["displayName"] if home else "?"
        away_team = away[0]["team"]["displayName"] if away else "?"
        home_abbr = home[0]["team"].get("abbreviation", "?") if home else "?"
        away_abbr = away[0]["team"].get("abbreviation", "?") if away else "?"
        home_score = int(home[0]["score"]) if home else 0
        away_score = int(away[0]["score"]) if away else 0
        clock = c.get("status", {}).get("displayClock", "?")
        minute = parse_clock_minute(clock) if state == "in" else 0
        start_date = c.get("date", ev.get("date", ""))

        # Derive Kalshi event ticker
        event_ticker = derive_event_ticker(home_team, away_team, start_date)

        # Match cached classification
        home_norm = normalize_team_name(home_team)
        away_norm = normalize_team_name(away_team)
        cls_key = f"{home_norm} vs {away_norm}"
        cls_key_rev = f"{away_norm} vs {home_norm}"
        cls = classifications.get(cls_key) or classifications.get(cls_key_rev, {})

        odds_class = cls.get('odds_class', 'close')
        fav_name = cls.get('favorite')
        und_name = cls.get('underdog')
        fav_decimal = cls.get('fav_decimal')
        is_mismatch = cls.get('is_mismatch', False)

        # Determine home vs favorite
        if fav_name and home_norm == fav_name:
            home_is_fav = True
        elif fav_name and away_norm == fav_name:
            home_is_fav = False
        else:
            home_is_fav = True  # default

        # Pull Kalshi markets for all 3 legs
        kalshi_legs = {}
        if kc and event_ticker:
            try:
                r = kc.get('/markets', params={'event_ticker': event_ticker, 'limit': 20})
                for m in r.get('markets', []):
                    suffix = m['ticker'].rsplit('-', 1)[-1]
                    kalshi_legs[suffix] = {
                        'ticker': m['ticker'],
                        'last': float(m.get('last_price_dollars') or 0),
                        'bid': float(m.get('yes_bid_dollars') or 0),
                        'ask': float(m.get('yes_ask_dollars') or 0),
                        'vol24h': float(m.get('volume_24h_fp') or 0),
                    }
            except Exception:
                pass

        # Map to home/away/tie using Kalshi team codes (not ESPN abbreviations)
        # ESPN and Kalshi sometimes differ (e.g., Iran: ESPN=IRN, Kalshi=IRI)
        sys.path.insert(0, SCRIPT_DIR)
        try:
            from team_codes import NAME_TO_CODE
            home_key = NAME_TO_CODE.get(normalize_team_name(home_team), home_abbr)
            away_key = NAME_TO_CODE.get(normalize_team_name(away_team), away_abbr)
        except Exception:
            home_key = home_abbr
            away_key = away_abbr
        home_mkt = kalshi_legs.get(home_key, {})
        away_mkt = kalshi_legs.get(away_key, {})
        tie_mkt = kalshi_legs.get('TIE', {})

        # Matrix lookup (from favorite's perspective)
        fav_score = home_score if home_is_fav else away_score
        und_score = away_score if home_is_fav else home_score
        mp = None
        if state == 'in':
            mp = lookup_matrix(matrix, odds_class, minute, fav_score, und_score)
        elif state == 'pre':
            mp = lookup_matrix(matrix, odds_class, 0, 0, 0)

        # Vegas-anchored calibration
        if mp:
            mp = vegas_calibrate(mp, matrix, odds_class,
                                 cls.get('fav_american'), cls.get('und_american'),
                                 cls.get('draw_american'))

        # Compute edges (Kalshi last - matrix prob) per leg
        edges = {}
        if mp:
            if home_is_fav:
                edges['home'] = (home_mkt.get('last', 0) - mp['fav_win']) * 100
                edges['away'] = (away_mkt.get('last', 0) - mp['und_win']) * 100
            else:
                edges['home'] = (home_mkt.get('last', 0) - mp['und_win']) * 100
                edges['away'] = (away_mkt.get('last', 0) - mp['fav_win']) * 100
            edges['tie'] = (tie_mkt.get('last', 0) - mp['tie']) * 100

        games.append({
            "id": ev.get("id", ""),
            "name": ev.get("name", ""),
            "date": ev.get("date", ""),
            "state": state,
            "status": desc,
            "clock": clock,
            "minute": minute,
            "home_team": home_team,
            "home_abbr": home_abbr,
            "home_score": home_score,
            "home_color": home[0]["team"].get("color", "666666") if home else "666666",
            "away_team": away_team,
            "away_abbr": away_abbr,
            "away_score": away_score,
            "away_color": away[0]["team"].get("color", "666666") if away else "666666",
            "event_ticker": event_ticker,
            "classification": {
                "odds_class": odds_class,
                "favorite": fav_name,
                "underdog": und_name,
                "fav_decimal": fav_decimal,
                "is_mismatch": is_mismatch,
                "home_is_fav": home_is_fav,
            },
            "kalshi": {
                "home": home_mkt,
                "away": away_mkt,
                "tie": tie_mkt,
            },
            "matrix": mp,
            "edges": edges,
        })

    # Sort: live first, then pre (upcoming), then post (final) last
    state_order = {"in": 0, "pre": 1, "post": 2}
    games.sort(key=lambda g: state_order.get(g["state"], 9))

    return {"games": games, "updated": datetime.now(ET).strftime("%H:%M:%S ET")}

@app.get("/api/commentary")
async def get_commentary():
    """Return recent commentary lines from the edge detector log."""
    try:
        with open(COMMENTARY_LOG, "r") as f:
            lines = f.readlines()
        # Return last 50 lines
        recent = [l.rstrip() for l in lines[-50:] if l.strip()]
        return {"lines": recent, "total_lines": len(lines)}
    except FileNotFoundError:
        return {"lines": [], "total_lines": 0, "note": "No commentary yet — edge detector not active or no edges detected."}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/balance")
async def get_balance():
    """Get Kalshi account balance and positions."""
    try:
        sys.path.insert(0, SCRIPT_DIR)
        from kalshi_auth import KalshiClient
        kc = KalshiClient()
        bal = kc.get("/portfolio/balance")
        positions = kc.get("/portfolio/positions")
        pos_list = []
        for mp in positions.get("market_positions", []):
            pos_list.append({
                "ticker": mp.get("ticker"),
                "shares": float(mp.get("position_fp") or 0),
                "cost": float(mp.get("total_traded_dollars") or 0),
            })
        return {
            "balance": float(bal.get("balance") or 0) / 100,
            "balance_str": bal.get("balance_dollars", "?"),
            "positions": pos_list,
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/daemon")
async def get_daemon_status():
    """Check if the edge detector daemon is running."""
    try:
        result = subprocess.run(["pgrep", "-f", "wc_live_edge.py"], capture_output=True, text=True)
        pids = [p for p in result.stdout.strip().split("\n") if p]
        running = len(pids) > 0
        # Get last few lines of run log
        try:
            with open(RUN_LOG, "r") as f:
                log_lines = f.readlines()[-5:]
        except:
            log_lines = []
        return {
            "running": running,
            "pids": pids,
            "last_log_lines": [l.rstrip() for l in log_lines if l.strip()],
        }
    except Exception as e:
        return {"error": str(e)}

def build_live_context():
    """Build a live game state context string for injection into chat system prompt.
    Pulls ESPN + Kalshi + matrix data so Gemma4 can answer questions with real numbers
    instead of hallucinating."""
    try:
        sb = http_get_json(ESPN_SCOREBOARD, timeout=10)
    except Exception as e:
        return f"[Live data unavailable: {e}]"

    matrix = get_matrix()
    classifications = read_cached_classifications()
    kc = get_kalshi()

    games = sb.get("events", [])
    if not games:
        return "No WC games scheduled right now."

    lines = ["=== LIVE GAME STATE (injected at %s ET) ===" % datetime.now(ET).strftime("%H:%M:%S")]

    for ev in games[:6]:  # cap at 6 games to keep prompt manageable
        state = ev.get("status", {}).get("type", {}).get("state", "?")
        competitions = ev.get("competitions", [])
        if not competitions:
            continue
        c = competitions[0]
        comps = c.get("competitors", [])
        home = [t for t in comps if t.get("homeAway") == "home"]
        away = [t for t in comps if t.get("homeAway") == "away"]
        if not home or not away:
            continue

        home_team = home[0]["team"]["displayName"]
        away_team = away[0]["team"]["displayName"]
        home_abbr = home[0]["team"].get("abbreviation", "?")
        away_abbr = away[0]["team"].get("abbreviation", "?")
        home_score = int(home[0].get("score", 0))
        away_score = int(away[0].get("score", 0))
        clock = c.get("status", {}).get("displayClock", "?")
        status_desc = c.get("status", {}).get("type", {}).get("description", "?")
        start_date = c.get("date", ev.get("date", ""))

        # Classification
        home_norm = normalize_team_name(home_team)
        away_norm = normalize_team_name(away_team)
        cls = classifications.get(f"{home_norm} vs {away_norm}") or \
              classifications.get(f"{away_norm} vs {home_norm}", {})
        odds_class = cls.get('odds_class', 'unknown')
        fav_name = cls.get('favorite', 'unknown')
        und_name = cls.get('underdog', 'unknown')
        fav_decimal = cls.get('fav_decimal', '?')
        is_mismatch = cls.get('is_mismatch', False)
        home_is_fav = (home_norm == fav_name) if fav_name != 'unknown' else True

        # Kalshi markets
        event_ticker = derive_event_ticker(home_team, away_team, start_date)
        kalshi_str = "N/A"
        legs = {}
        if kc and event_ticker:
            try:
                r = kc.get('/markets', params={'event_ticker': event_ticker, 'limit': 20})
                for m in r.get('markets', []):
                    suffix = m['ticker'].rsplit('-', 1)[-1]
                    legs[suffix] = {
                        'last': float(m.get('last_price_dollars') or 0),
                        'bid': float(m.get('yes_bid_dollars') or 0),
                        'ask': float(m.get('yes_ask_dollars') or 0),
                        'vol24h': float(m.get('volume_24h_fp') or 0),
                    }
                # Use Kalshi team codes (not ESPN abbreviations) — they can differ (e.g., Iran: ESPN=IRN, Kalshi=IRI)
                sys.path.insert(0, SCRIPT_DIR)
                try:
                    from team_codes import NAME_TO_CODE
                    home_kalshi_code = NAME_TO_CODE.get(home_norm, home_abbr)
                    away_kalshi_code = NAME_TO_CODE.get(away_norm, away_abbr)
                except Exception:
                    home_kalshi_code = home_abbr
                    away_kalshi_code = away_abbr
                home_mkt = legs.get(home_kalshi_code, {})
                away_mkt = legs.get(away_kalshi_code, {})
                tie_mkt = legs.get('TIE', {})
                kalshi_str = (
                    f"{home_abbr} ${home_mkt.get('last','?')} (bid {home_mkt.get('bid','?')}/ask {home_mkt.get('ask','?')}, vol {home_mkt.get('vol24h',0):,.0f}), "
                    f"{away_abbr} ${away_mkt.get('last','?')} (bid {away_mkt.get('bid','?')}/ask {away_mkt.get('ask','?')}, vol {away_mkt.get('vol24h',0):,.0f}), "
                    f"TIE ${tie_mkt.get('last','?')} (bid {tie_mkt.get('bid','?')}/ask {tie_mkt.get('ask','?')}, vol {tie_mkt.get('vol24h',0):,.0f})"
                )
            except Exception:
                pass

        # Matrix lookup
        minute = parse_clock_minute(clock) if state == "in" else 0
        fav_score = home_score if home_is_fav else away_score
        und_score = away_score if home_is_fav else home_score
        mp = None
        if odds_class != 'unknown':
            if state == "in":
                mp = lookup_matrix(matrix, odds_class, minute, fav_score, und_score)
            elif state == "pre":
                mp = lookup_matrix(matrix, odds_class, 0, 0, 0)
            # Vegas-anchored calibration
            if mp:
                mp = vegas_calibrate(mp, matrix, odds_class,
                                     cls.get('fav_american'), cls.get('und_american'),
                                     cls.get('draw_american'))

        matrix_str = "N/A"
        edges_str = ""
        if mp:
            fav_price_key = home_kalshi_code if home_is_fav else away_kalshi_code
            und_price_key = away_kalshi_code if home_is_fav else home_kalshi_code
            fav_leg = legs.get(fav_price_key, {})
            und_leg = legs.get(und_price_key, {})
            tie_leg = legs.get('TIE', {})
            fav_price = fav_leg.get('last', 0)
            und_price = und_leg.get('last', 0)
            tie_price = tie_leg.get('last', 0)
            cal_note = " [Vegas-calibrated]" if mp.get('calibrated') else ""
            matrix_str = f"P(fav win)={mp['fav_win']*100:.1f}%, P(tie)={mp['tie']*100:.1f}%, P(und win)={mp['und_win']*100:.1f}%{cal_note} (n={mp['sample']:,}, state={mp['state_used']} @ min {mp['minute_bucket']})"
            fav_edge = (fav_price - mp['fav_win']) * 100
            tie_edge = (tie_price - mp['tie']) * 100
            und_edge = (und_price - mp['und_win']) * 100
            def edge_label(e):
                if e > 6: return f"OVERPRICED by {e:+.1f}pp (sell)"
                if e < -6: return f"UNDERPRICED by {e:+.1f}pp (buy)"
                return f"{e:+.1f}pp (fair value)"
            edges_str = (f"Edges (positive=Kalshi OVERPRICING=sell, negative=UNDERPRICING=buy): "
                         f"FAV {edge_label(fav_edge)}, TIE {edge_label(tie_edge)}, UND {edge_label(und_edge)}")

        # Trade history for this game
        trade_str = "No trades"
        try:
            trade_log = os.path.join(PROJECT_DIR, 'data', 'trade_log.csv')
            if os.path.exists(trade_log) and event_ticker:
                for row in csv.DictReader(open(trade_log)):
                    if row.get('event_ticker') == event_ticker:
                        shares = float(row.get('shares', 0))
                        if shares > 0:
                            trade_str = f"Position: {shares:.0f} shares TIE @ ${float(row.get('fill_price',0)):.3f}"
                        elif shares < 0:
                            trade_str = f"Closed: {shares:.0f} shares TIE @ ${float(row.get('fill_price',0)):.3f}"
        except Exception:
            pass

        mismatch_warn = " ⚠ EXTREME FAV (caution on all legs, Vegas-calibrated)" if is_mismatch else ""
        lines.append(
            f"\nGame: {home_team} ({home_abbr}) vs {away_team} ({away_abbr})\n"
            f"  Status: {status_desc} | Score: {home_score}-{away_score} | Clock: {clock}\n"
            f"  Ticker: {event_ticker}\n"
            f"  Classification: {odds_class} | Favorite: {fav_name} ({fav_decimal} decimal) | Underdog: {und_name}{mismatch_warn}\n"
            f"  Kalshi prices: {kalshi_str}\n"
            f"  Matrix: {matrix_str}\n"
            f"  {edges_str}\n"
            f"  Trade: {trade_str}"
        )

    lines.append("\n=== END LIVE GAME STATE ===")
    lines.append(
        "When answering questions, use ONLY these numbers. Do not make up prices, "
        "probabilities, or edges. If data is N/A, say so. Cite the matrix probability "
        "and Kalshi price when discussing edges."
    )
    return "\n".join(lines)

@app.post("/api/chat")
async def chat(request: Request):
    """Proxy chat request to vLLM with streaming. Returns SSE stream."""
    body = await request.json()
    messages = body.get("messages", [])
    system_prompt = body.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
    temperature = body.get("temperature", 0.3)
    max_tokens = body.get("max_tokens", 1000)

    # Inject live game state into the system prompt so Gemma4 has real data
    live_context = build_live_context()
    full_system = system_prompt + "\n\n" + live_context

    # Prepend system prompt if not already present
    full_messages = []
    if not any(m.get("role") == "system" for m in messages):
        full_messages.append({"role": "system", "content": full_system})
    else:
        # Replace existing system prompt with enriched one
        for m in messages:
            if m.get("role") == "system":
                full_messages.append({"role": "system", "content": full_system})
            else:
                full_messages.append(m)
    full_messages.extend([m for m in messages if m.get("role") != "system"])

    payload = {
        "model": VLLM_MODEL,
        "messages": full_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }

    def stream_generator():
        try:
            resp = requests.post(VLLM_URL, json=payload, stream=True, timeout=120)
            for line in resp.iter_lines():
                if line:
                    line_str = line.decode("utf-8")
                    if line_str.startswith("data: "):
                        data_str = line_str[6:]
                        if data_str.strip() == "[DONE]":
                            yield "data: [DONE]\n\n"
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield f"data: {json.dumps({'content': content})}\n\n"
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(stream_generator(), media_type="text/event-stream")

# ---------------------------------------------------------------------------
# Frontend (single HTML page, dark theme, vanilla JS)
# ---------------------------------------------------------------------------

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WC Trading Cockpit</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #c9d1d9; height: 100vh; overflow: hidden; }
  .header { background: #161b22; padding: 10px 20px; border-bottom: 1px solid #30363d; display: flex; justify-content: space-between; align-items: center; }
  .header h1 { font-size: 18px; color: #58a6ff; }
  .header .status { font-size: 12px; color: #8b949e; }
  .header .status .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
  .dot.green { background: #3fb950; } .dot.red { background: #f85149; } .dot.yellow { background: #d29922; }
  .container { display: flex; height: calc(100vh - 50px); }
  .chat-panel { width: 45%; display: flex; flex-direction: column; border-right: 1px solid #30363d; }
  .monitor-panel { width: 55%; display: flex; flex-direction: column; overflow: hidden; }
  .chat-messages { flex: 1; overflow-y: auto; padding: 15px; }
  .msg { margin-bottom: 12px; max-width: 95%; }
  .msg.user { margin-left: auto; }
  .msg-bubble { padding: 10px 14px; border-radius: 12px; font-size: 14px; line-height: 1.5; white-space: pre-wrap; }
  .msg.user .msg-bubble { background: #1f6feb; color: white; }
  .msg.assistant .msg-bubble { background: #21262d; border: 1px solid #30363d; }
  .msg.assistant.streaming .msg-bubble { border-color: #58a6ff; }
  .chat-input-area { padding: 10px 15px; border-top: 1px solid #30363d; background: #161b22; }
  .chat-input-row { display: flex; gap: 8px; }
  #chat-input { flex: 1; background: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 10px 14px; color: #c9d1d9; font-size: 14px; resize: none; height: 44px; max-height: 120px; }
  #chat-input:focus { outline: none; border-color: #58a6ff; }
  #send-btn { background: #238636; color: white; border: none; border-radius: 8px; padding: 0 20px; font-size: 14px; cursor: pointer; white-space: nowrap; }
  #send-btn:hover { background: #2ea043; }
  #send-btn:disabled { background: #21262d; color: #484f58; cursor: not-allowed; }
  #clear-btn { background: #21262d; color: #8b949e; border: 1px solid #30363d; border-radius: 8px; padding: 0 12px; font-size: 13px; cursor: pointer; }
  #clear-btn:hover { background: #30363d; }
  .preset-row { display: flex; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; }
  .preset-btn { background: #21262d; border: 1px solid #30363d; color: #8b949e; border-radius: 6px; padding: 4px 10px; font-size: 12px; cursor: pointer; }
  .preset-btn:hover { background: #30363d; color: #c9d1d9; }
  .monitor-section { padding: 12px 15px; border-bottom: 1px solid #21262d; }
  .monitor-section h3 { font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; display: flex; justify-content: space-between; }
  .monitor-section h3 .refresh-time { font-size: 11px; color: #484f58; }
  .games-container { max-height: 45vh; overflow-y: auto; }
  .game-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 10px 12px; margin-bottom: 6px; font-size: 13px; }
  .game-card.live { border-color: #f85149; }
  .game-card.pre { border-color: #58a6ff44; opacity: 0.92; }
  .game-card.post { opacity: 0.5; }
  .game-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
  .game-name { font-weight: 600; }
  .game-state-badge { font-size: 10px; padding: 2px 6px; border-radius: 4px; text-transform: uppercase; }
  .badge-live { background: #f8514933; color: #f85149; }
  .badge-pre { background: #30363d; color: #8b949e; }
  .badge-post { background: #21262d; color: #484f58; }
  .game-score { display: flex; justify-content: space-between; align-items: center; }
  .team-row { display: flex; align-items: center; gap: 8px; }
  .team-color { width: 4px; height: 20px; border-radius: 2px; }
  .team-name { flex: 1; }
  .team-score { font-weight: 700; font-size: 16px; }
  .game-clock { font-size: 12px; color: #8b949e; }
  .odds-table { width: 100%; margin-top: 8px; border-collapse: collapse; font-size: 12px; }
  .odds-table th { text-align: left; color: #8b949e; font-weight: 500; padding: 3px 6px; border-bottom: 1px solid #30363d; font-size: 11px; text-transform: uppercase; }
  .odds-table td { padding: 4px 6px; border-bottom: 1px solid #21262d; }
  .odds-table tr:last-child td { border-bottom: none; }
  .odds-table .leg-name { font-weight: 600; }
  .odds-table .leg-name.fav::after { content: " ★"; color: #d29922; font-size: 10px; }
  .odds-table .price { color: #58a6ff; font-weight: 600; }
  .odds-table .bid-ask { color: #8b949e; font-family: 'SF Mono', monospace; font-size: 11px; }
  .odds-table .matrix-prob { color: #8b949e; font-family: 'SF Mono', monospace; }
  .odds-table .edge { font-weight: 700; font-family: 'SF Mono', monospace; }
  .edge.positive { color: #3fb950; } .edge.negative { color: #f85149; } .edge.neutral { color: #484f58; }
  .game-meta { font-size: 11px; color: #484f58; margin-top: 6px; display: flex; gap: 12px; flex-wrap: wrap; }
  .game-meta .mismatch-warn { color: #f85149; font-weight: 600; }
  .vol { color: #8b949e; font-size: 11px; }
  .commentary-container { flex: 1; overflow-y: auto; padding: 0 15px 15px; }
  .commentary-line { font-size: 12px; line-height: 1.6; padding: 4px 0; border-bottom: 1px solid #21262d33; font-family: 'SF Mono', 'Consolas', monospace; }
  .commentary-line.header { color: #58a6ff; font-weight: 600; }
  .commentary-line.empty { color: #484f58; font-style: italic; }
  .balance-row { display: flex; gap: 20px; font-size: 13px; }
  .balance-item { display: flex; flex-direction: column; }
  .balance-label { font-size: 11px; color: #8b949e; }
  .balance-value { font-size: 16px; font-weight: 600; color: #3fb950; }
  .daemon-status { font-size: 12px; padding: 6px 10px; border-radius: 6px; margin-top: 6px; }
  .daemon-status.running { background: #3fb95022; color: #3fb950; }
  .daemon-status.stopped { background: #f8514922; color: #f85149; }
  .scrollbar::-webkit-scrollbar { width: 6px; }
  .scrollbar::-webkit-scrollbar-track { background: #0d1117; }
  .scrollbar::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
  .scrollbar::-webkit-scrollbar-thumb:hover { background: #484f58; }
</style>
</head>
<body>

<div class="header">
  <h1>⚽ WC Trading Cockpit</h1>
  <div class="status">
    <span id="vllm-status"><span class="dot yellow"></span>Checking vLLM...</span>
    &nbsp;|&nbsp;
    <span id="daemon-status-header"><span class="dot yellow"></span>Checking daemon...</span>
    &nbsp;|&nbsp;
    <span id="balance-status">Balance: ...</span>
  </div>
</div>

<div class="container">
  <!-- LEFT: Chat Panel -->
  <div class="chat-panel">
    <div class="chat-messages scrollbar" id="chat-messages">
      <div class="msg assistant">
        <div class="msg-bubble">Welcome to the WC Trading Cockpit. I'm Gemma4, running on your local eGPU. Ask me about the current games, edge calculations, or trading strategy.</div>
      </div>
    </div>
    <div class="chat-input-area">
      <div class="preset-row">
        <button class="preset-btn" onclick="sendPreset('What are the current live games and their Kalshi prices?')">Live games + prices</button>
        <button class="preset-btn" onclick="sendPreset('What is the edge on the TURPAR TIE trade? Explain what went wrong.')">TURPAR analysis</button>
        <button class="preset-btn" onclick="sendPreset('Explain the three-exit strategy and when each exit triggers.')">Three-exit strategy</button>
        <button class="preset-btn" onclick="sendPreset('What odds class produces the best TIE edge? Show the matrix numbers.')">Best edge class</button>
        <button class="preset-btn" onclick="sendPreset('If the underdog scores first in a close game at minute 15, what is P(tie)? What price should I buy TIE at?')">Underdog-first scenario</button>
      </div>
      <div class="chat-input-row">
        <textarea id="chat-input" placeholder="Ask Gemma4 about WC trading..." rows="1"></textarea>
        <button id="clear-btn" onclick="clearChat()" title="Clear chat">Clear</button>
        <button id="send-btn" onclick="sendMessage()">Send</button>
      </div>
    </div>
  </div>

  <!-- RIGHT: Monitor Panel -->
  <div class="monitor-panel scrollbar">
    <!-- Games Section -->
    <div class="monitor-section">
      <h3>Live Games <span class="refresh-time" id="games-refresh"></span></h3>
      <div class="games-container scrollbar" id="games-container">
        <div class="commentary-line empty">Loading ESPN scoreboard...</div>
      </div>
    </div>

    <!-- Account Section -->
    <div class="monitor-section">
      <h3>Account</h3>
      <div class="balance-row" id="balance-row">
        <div class="balance-item"><span class="balance-label">Balance</span><span class="balance-value" id="balance-value">...</span></div>
        <div class="balance-item"><span class="balance-label">Positions</span><span class="balance-value" id="positions-value">0</span></div>
      </div>
      <div class="daemon-status" id="daemon-status-div">Checking edge detector...</div>
    </div>

    <!-- Commentary Feed -->
    <div class="monitor-section" style="flex:1; display:flex; flex-direction:column; overflow:hidden; border-bottom:none;">
      <h3>Edge Detector Commentary <span class="refresh-time" id="commentary-refresh"></span></h3>
      <div class="commentary-container scrollbar" id="commentary-container">
        <div class="commentary-line empty">No commentary yet.</div>
      </div>
    </div>
  </div>
</div>

<script>
// --- Chat ---
let chatHistory = [];
let isStreaming = false;

function addMessage(role, content) {
  const container = document.getElementById('chat-messages');
  const msgDiv = document.createElement('div');
  msgDiv.className = 'msg ' + role;
  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  bubble.textContent = content;
  msgDiv.appendChild(bubble);
  container.appendChild(msgDiv);
  container.scrollTop = container.scrollHeight;
  return bubble;
}

async function sendMessage() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text || isStreaming) return;

  input.value = '';
  input.style.height = '44px';
  addMessage('user', text);
  chatHistory.push({ role: 'user', content: text });

  isStreaming = true;
  document.getElementById('send-btn').disabled = true;
  document.getElementById('send-btn').textContent = '...';

  const assistantDiv = document.createElement('div');
  assistantDiv.className = 'msg assistant streaming';
  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  bubble.textContent = '';
  assistantDiv.appendChild(bubble);
  document.getElementById('chat-messages').appendChild(assistantDiv);

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: chatHistory, temperature: 0.3, max_tokens: 1000 }),
    });

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let fullContent = '';
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const dataStr = line.slice(6);
          if (dataStr === '[DONE]') continue;
          try {
            const data = JSON.parse(dataStr);
            if (data.error) { fullContent += '\\n[Error: ' + data.error + ']'; }
            if (data.content) { fullContent += data.content; }
            bubble.textContent = fullContent;
            document.getElementById('chat-messages').scrollTop = 999999;
          } catch(e) {}
        }
      }
    }
    chatHistory.push({ role: 'assistant', content: fullContent });
    assistantDiv.classList.remove('streaming');
  } catch(e) {
    bubble.textContent = '[Error: ' + e.message + ']';
  }

  isStreaming = false;
  document.getElementById('send-btn').disabled = false;
  document.getElementById('send-btn').textContent = 'Send';
}

function sendPreset(text) {
  document.getElementById('chat-input').value = text;
  sendMessage();
}

function clearChat() {
  chatHistory = [];
  document.getElementById('chat-messages').innerHTML = '<div class="msg assistant"><div class="msg-bubble">Chat cleared. Ask me anything about WC trading.</div></div>';
}

// Enter to send, Shift+Enter for newline
document.getElementById('chat-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
document.getElementById('chat-input').addEventListener('input', function() {
  this.style.height = '44px';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

// --- Monitor: Games ---
async function refreshGames() {
  try {
    const resp = await fetch('/api/games');
    const data = await resp.json();
    const container = document.getElementById('games-container');
    document.getElementById('games-refresh').textContent = data.updated || '';

    if (data.error) { container.innerHTML = '<div class="commentary-line empty">' + data.error + '</div>'; return; }
    if (!data.games || data.games.length === 0) { container.innerHTML = '<div class="commentary-line empty">No WC games scheduled.</div>'; return; }

    container.innerHTML = data.games.map(g => {
      const stateClass = g.state === 'in' ? 'live' : g.state === 'pre' ? 'pre' : 'post';
      const badgeClass = g.state === 'in' ? 'badge-live' : g.state === 'pre' ? 'badge-pre' : 'badge-post';
      const badgeText = g.state === 'in' ? 'LIVE ' + (g.clock || '') : g.state === 'pre' ? 'UPCOMING' : 'FINAL';
      const cls = g.classification || {};
      const k = g.kalshi || {};
      const hm = k.home || {}, am = k.away || {}, tm = k.tie || {};
      const mp = g.matrix, edges = g.edges || {};
      const homeIsFav = cls.home_is_fav !== false;
      const fmtPct = v => v ? (v * 100).toFixed(0) + '%' : '–';
      const fmtPrice = v => v ? v.toFixed(2) : '–';
      const fmtEdge = v => {
        if (v === undefined || v === null) return '<span class="edge neutral">–</span>';
        const cls = v > 6 ? 'positive' : v < -6 ? 'negative' : 'neutral';
        const sign = v > 0 ? '+' : '';
        return '<span class="edge ' + cls + '">' + sign + v.toFixed(1) + '</span>';
      };
      const fmtVol = v => {
        if (!v) return '';
        if (v > 1e6) return (v/1e6).toFixed(1) + 'M';
        if (v > 1e3) return (v/1e3).toFixed(0) + 'K';
        return v.toFixed(0);
      };

      let oddsTable = '';
      if (hm.last || am.last || tm.last) {
        oddsTable = '<table class="odds-table">' +
          '<tr><th>Leg</th><th>Price</th><th>Bid/Ask</th><th>Model</th><th>Edge</th></tr>' +
          '<tr><td class="leg-name' + (homeIsFav ? ' fav' : '') + '">' + g.home_abbr + '</td>' +
            '<td class="price">' + fmtPrice(hm.last) + '</td>' +
            '<td class="bid-ask">' + fmtPrice(hm.bid) + '/' + fmtPrice(hm.ask) + '</td>' +
            '<td class="matrix-prob">' + (mp ? (homeIsFav ? fmtPct(mp.fav_win) : fmtPct(mp.und_win)) : '–') + '</td>' +
            '<td>' + fmtEdge(edges.home) + '</td></tr>' +
          '<tr><td class="leg-name' + (!homeIsFav ? ' fav' : '') + '">' + g.away_abbr + '</td>' +
            '<td class="price">' + fmtPrice(am.last) + '</td>' +
            '<td class="bid-ask">' + fmtPrice(am.bid) + '/' + fmtPrice(am.ask) + '</td>' +
            '<td class="matrix-prob">' + (mp ? (!homeIsFav ? fmtPct(mp.fav_win) : fmtPct(mp.und_win)) : '–') + '</td>' +
            '<td>' + fmtEdge(edges.away) + '</td></tr>' +
          '<tr><td class="leg-name">TIE</td>' +
            '<td class="price">' + fmtPrice(tm.last) + '</td>' +
            '<td class="bid-ask">' + fmtPrice(tm.bid) + '/' + fmtPrice(tm.ask) + '</td>' +
            '<td class="matrix-prob">' + (mp ? fmtPct(mp.tie) : '–') + '</td>' +
            '<td>' + fmtEdge(edges.tie) + '</td></tr>' +
        '</table>';
      }

      let meta = '<div class="game-meta">';
      if (cls.odds_class) meta += '<span>Class: ' + cls.odds_class + '</span>';
      if (cls.fav_decimal) meta += '<span>Fav: ' + cls.fav_decimal + '</span>';
      if (cls.is_mismatch) meta += '<span class="mismatch-warn">⚠ EXTREME FAV — VEGAS CALIBRATED</span>';
      if (mp && mp.sample) meta += '<span>n=' + mp.sample.toLocaleString() + '</span>';
      if (mp && mp.calibrated) meta += '<span style="color:#58a6ff">📊 Vegas-calibrated</span>';
      if (mp && mp.state_used && g.state === 'in') meta += '<span>State: ' + mp.state_used + ' @ ' + mp.minute_bucket + "'</span>";
      meta += '</div>';

      let volInfo = '';
      if (hm.vol24h || am.vol24h || tm.vol24h) {
        volInfo = '<div class="vol">Vol: ' + g.home_abbr + ' ' + fmtVol(hm.vol24h) + ' | ' + g.away_abbr + ' ' + fmtVol(am.vol24h) + ' | TIE ' + fmtVol(tm.vol24h) + '</div>';
      }

      return `<div class="game-card ${stateClass}">
        <div class="game-header">
          <span class="game-name">${g.home_abbr} vs ${g.away_abbr}</span>
          <span class="game-state-badge ${badgeClass}">${badgeText}</span>
        </div>
        <div class="game-score">
          <div class="team-row">
            <div class="team-color" style="background:#${g.home_color}"></div>
            <span class="team-name">${g.home_team}</span>
            <span class="team-score">${g.home_score}</span>
          </div>
        </div>
        <div class="game-score">
          <div class="team-row">
            <div class="team-color" style="background:#${g.away_color}"></div>
            <span class="team-name">${g.away_team}</span>
            <span class="team-score">${g.away_score}</span>
          </div>
        </div>
        ${oddsTable}
        ${volInfo}
        ${meta}
      </div>`;
    }).join('');
  } catch(e) { console.error('Games refresh error:', e); }
}

// --- Monitor: Commentary ---
async function refreshCommentary() {
  try {
    const resp = await fetch('/api/commentary');
    const data = await resp.json();
    const container = document.getElementById('commentary-container');
    document.getElementById('commentary-refresh').textContent = new Date().toLocaleTimeString();

    if (data.error) { container.innerHTML = '<div class="commentary-line empty">' + data.error + '</div>'; return; }
    if (!data.lines || data.lines.length === 0) {
      container.innerHTML = '<div class="commentary-line empty">' + (data.note || 'No commentary yet.') + '</div>';
      return;
    }

    container.innerHTML = data.lines.map(l => {
      if (l.match(/^\\[\\d{2}:\\d{2}:\\d{2} ET\\] \\[/)) return '<div class="commentary-line header">' + escapeHtml(l) + '</div>';
      if (l.match(/^\\[\\d{2}:\\d{2}:\\d{2} ET\\]/)) return '<div class="commentary-line">' + escapeHtml(l) + '</div>';
      return '<div class="commentary-line" style="padding-left:20px;color:#8b949e;">' + escapeHtml(l) + '</div>';
    }).join('');
    container.scrollTop = container.scrollHeight;
  } catch(e) { console.error('Commentary refresh error:', e); }
}

function escapeHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// --- Monitor: Balance + Daemon ---
async function refreshBalance() {
  try {
    const resp = await fetch('/api/balance');
    const data = await resp.json();
    if (data.error) { document.getElementById('balance-value').textContent = 'Error'; return; }
    document.getElementById('balance-value').textContent = '$' + data.balance.toFixed(2);
    document.getElementById('positions-value').textContent = data.positions.length;
  } catch(e) {}
}

async function refreshDaemon() {
  try {
    const resp = await fetch('/api/daemon');
    const data = await resp.json();
    const div = document.getElementById('daemon-status-div');
    const header = document.getElementById('daemon-status-header');
    if (data.running) {
      div.className = 'daemon-status running';
      div.textContent = 'Edge detector: RUNNING (PID ' + data.pids.join(',') + ')';
      header.innerHTML = '<span class="dot green"></span>Daemon: Running';
    } else {
      div.className = 'daemon-status stopped';
      div.textContent = 'Edge detector: STOPPED — start with: python3 -u scripts/wc_live_edge.py --verbose >> data/live_edge_run.log 2>&1 &';
      header.innerHTML = '<span class="dot red"></span>Daemon: Stopped';
    }
  } catch(e) {}
}

async function checkVLLM() {
  try {
    const resp = await fetch('/api/health');
    const data = await resp.json();
    const el = document.getElementById('vllm-status');
    if (data.vllm === 'online') {
      el.innerHTML = '<span class="dot green"></span>Gemma4: Online';
    } else {
      el.innerHTML = '<span class="dot red"></span>Gemma4: Offline';
    }
  } catch(e) {}
}

// --- Auto-refresh ---
checkVLLM();
refreshGames();
refreshCommentary();
refreshBalance();
refreshDaemon();
setInterval(refreshGames, 30000);
setInterval(refreshCommentary, 10000);
setInterval(refreshBalance, 60000);
setInterval(refreshDaemon, 30000);
setInterval(checkVLLM, 60000);
</script>

</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE

if __name__ == "__main__":
    print("WC Trading Cockpit — http://localhost:8877")
    print("Access via Tailscale: http://your-tailnet-hostname:8877")
    uvicorn.run(app, host="0.0.0.0", port=8877, log_level="info")
