from fastapi import FastAPI, HTTPException, Body
import requests
import os
import unicodedata
from datetime import date, datetime, timezone, timedelta
from supabase import create_client

app = FastAPI(title="Betting AI Supabase")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")

if not SUPABASE_URL:
    raise RuntimeError("Missing SUPABASE_URL environment variable")

if not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_KEY environment variable")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

LEAGUES = [
    ("soccer_sweden_allsvenskan", "Allsvenskan"),
    ("soccer_sweden_superettan", "Superettan"),
    ("soccer_denmark_superliga", "Superliga"),
    ("soccer_norway_eliteserien", "Eliteserien"),
    ("soccer_finland_veikkausliiga", "Veikkausliiga"),
    ("soccer_efl_champ", "Championship"),
    ("soccer_spain_la_liga", "La Liga"),
    ("soccer_italy_serie_a", "Serie A"),
    ("soccer_france_ligue_one", "Ligue 1"),
    ("soccer_usa_mls", "MLS"),
]

RESULT_LEAGUES = [
    {"league_id": 113, "league": "Allsvenskan"},
    {"league_id": 114, "league": "Superettan"},
    {"league_id": 119, "league": "Superliga"},
    {"league_id": 103, "league": "Eliteserien"},
    {"league_id": 244, "league": "Veikkausliiga"},
    {"league_id": 40, "league": "Championship"},
    {"league_id": 140, "league": "La Liga"},
    {"league_id": 135, "league": "Serie A"},
    {"league_id": 61, "league": "Ligue 1"},
    {"league_id": 253, "league": "MLS"},
]

FINAL_STATUSES = {"FT", "AET", "PEN"}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return (
        s.replace(" if", "")
         .replace(" fc", "")
         .replace(" bk", "")
         .replace(" ff", "")
         .replace("-", " ")
         .replace(".", "")
         .strip()
    )

def score_pick(odds: float, league: str):
    implied = 1.0 / odds
    league_bonus = {
        "Allsvenskan": 0.020,
        "Superettan": 0.015,
        "Superliga": 0.015,
        "Eliteserien": 0.020,
        "Veikkausliiga": 0.015,
        "Championship": 0.015,
        "La Liga": 0.005,
        "Serie A": 0.005,
        "Ligue 1": 0.005,
        "MLS": 0.020,
    }.get(league, 0.0)

    odds_bonus = 0.0
    if 1.70 <= odds <= 2.50:
        odds_bonus = 0.010
    elif 2.50 < odds <= 3.50:
        odds_bonus = 0.005

    model_prob = min(max(implied + league_bonus + odds_bonus, 0.01), 0.99)
    edge = model_prob - implied
    score = edge * 100
    return round(edge, 4), round(score, 2)

def decision_from_score(score: float) -> str:
    if score >= 2.5:
        return "🔥 SPELA"
    if score >= 1.5:
        return "⚠️ BEVAKA"
    return "❌ PASS"

@app.get("/")
def root():
    return {"status": "running"}

@app.get("/health")
def health():
    odds_rows = len(supabase.table("odds_snapshot").select("event_id").execute().data or [])
    pick_rows = len(supabase.table("picks").select("event_id").execute().data or [])
    history_rows = len(supabase.table("history").select("event_id").execute().data or [])
    result_rows = len(supabase.table("results").select("event_id").execute().data or [])
    bet_rows = len(supabase.table("bets").select("id").execute().data or [])

    return {
        "status": "ok",
        "odds_rows": odds_rows,
        "pick_rows": pick_rows,
        "history_rows": history_rows,
        "result_rows": result_rows,
        "bet_rows": bet_rows,
    }

@app.get("/fetch-odds")
def fetch_odds():
    if not ODDS_API_KEY:
        raise HTTPException(status_code=500, detail="Missing ODDS_API_KEY")

    inserted = 0

    for sport, league in LEAGUES:
        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h&oddsFormat=decimal"

        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code != 200:
                continue
            data = resp.json()
        except Exception:
            continue

        for e in data:
            match = f"{e.get('home_team', '')} vs {e.get('away_team', '')}"
            event_id = e.get("id")

            try:
                outcomes = e["bookmakers"][0]["markets"][0]["outcomes"]
            except Exception:
                continue

            for o in outcomes:
                odds = o.get("price")
                if odds is None:
                    continue

                supabase.table("odds_snapshot").insert({
                    "event_id": event_id,
                    "match": match,
                    "league": league,
                    "selection": o.get("name", ""),
                    "odds": float(odds),
                    "pulled_at": now_iso()
                }).execute()

                inserted += 1

    return {"status": "odds fetched", "rows": inserted}

@app.get("/generate-picks")
def generate_picks():
    data = supabase.table("odds_snapshot").select("*").execute().data

    if not data:
        return {"status": "no odds"}

    supabase.table("picks").delete().neq("event_id", "").execute()

    inserted = 0

    for row in data:
        edge, score = score_pick(float(row["odds"]), row["league"])
        dec = decision_from_score(score)

        if dec == "❌ PASS":
            continue

        pick_payload = {
            "event_id": row["event_id"],
            "match": row["match"],
            "league": row["league"],
            "selection": row["selection"],
            "odds": row["odds"],
            "edge": edge,
            "score": score,
            "decision": dec,
            "generated_at": now_iso()
        }

        supabase.table("picks").insert(pick_payload).execute()
        supabase.table("history").insert(pick_payload).execute()
        inserted += 1

    return {"status": "picks generated", "rows": inserted}

@app.get("/fetch-results")
def fetch_results():
    if not API_FOOTBALL_KEY:
        raise HTTPException(status_code=500, detail="Missing API_FOOTBALL_KEY")

    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    season = date.today().year
    history = supabase.table("history").select("*").execute().data or []

    inserted = 0

    for cfg in RESULT_LEAGUES:
        for d in [(date.today() - timedelta(days=i)).isoformat() for i in range(3)]:
            url = f"https://v3.football.api-sports.io/fixtures?league={cfg['league_id']}&season={season}&date={d}"

            try:
                resp = requests.get(url, headers=headers, timeout=30)
                if resp.status_code != 200:
                    continue
                data = resp.json()
            except Exception:
                continue

            for m in data.get("response", []):
                status = m.get("fixture", {}).get("status", {}).get("short", "")
                if status not in FINAL_STATUSES:
                    continue

                home = norm(m["teams"]["home"]["name"])
                away = norm(m["teams"]["away"]["name"])

                for h in history:
                    txt = norm(h["match"])
                    if home in txt and away in txt:
                        hs = m["goals"]["home"]
                        aw = m["goals"]["away"]

                        winner = "DRAW"
                        if hs > aw:
                            winner = "HOME"
                        elif aw > hs:
                            winner = "AWAY"

                        supabase.table("results").upsert({
                            "event_id": h["event_id"],
                            "home_score": hs,
                            "away_score": aw,
                            "winner": winner,
                            "status": status,
                            "settled_at": now_iso()
                        }).execute()

                        inserted += 1

    return {"status": "results updated", "rows": inserted}

@app.get("/picks")
def picks():
    return supabase.table("picks").select("*").limit(20).execute().data

@app.get("/history")
def history():
    return supabase.table("history").select("*").limit(200).execute().data

@app.get("/stats")
def stats():
    history = supabase.table("history").select("*").execute().data or []
    results = supabase.table("results").select("*").execute().data or []

    if not history:
        return {"plays": 0, "wins": 0, "losses": 0, "roi_percent": 0.0}

    wins = 0
    plays = 0

    results_by_event = {r["event_id"]: r for r in results}

    for h in history:
        r = results_by_event.get(h["event_id"])
        if not r:
            continue

        plays += 1

        try:
            home, away = h["match"].split(" vs ")
        except ValueError:
            continue

        if h["selection"] == home and r["winner"] == "HOME":
            wins += 1
        elif h["selection"] == away and r["winner"] == "AWAY":
            wins += 1
        elif str(h["selection"]).upper() == "DRAW" and r["winner"] == "DRAW":
            wins += 1

    losses = plays - wins
    roi_percent = round((wins / plays) * 100, 2) if plays else 0.0

    return {
        "plays": plays,
        "wins": wins,
        "losses": losses,
        "roi_percent": roi_percent
    }

@app.post("/place-bet")
def place_bet(data: dict = Body(...)):
    required = ["event_id", "match", "selection", "odds", "stake"]
    for field in required:
        if field not in data:
            raise HTTPException(status_code=400, detail=f"Missing {field}")

    supabase.table("bets").insert({
        "event_id": data["event_id"],
        "match": data["match"],
        "selection": data["selection"],
        "odds": data["odds"],
        "stake": data["stake"],
        "created_at": now_iso()
    }).execute()

    return {"status": "bet saved"}

@app.get("/my-bets")
def my_bets():
    return supabase.table("bets").select("*").execute().data

@app.get("/my-stats")
def my_stats():
    bets = supabase.table("bets").select("*").execute().data or []
    results = supabase.table("results").select("*").execute().data or []

    if not bets:
        return {
            "plays": 0,
            "wins": 0,
            "losses": 0,
            "profit_units": 0.0,
            "roi_percent": 0.0
        }

    results_by_event = {r["event_id"]: r for r in results}

    settled = []
    for b in bets:
        r = results_by_event.get(b["event_id"])
        if not r:
            continue

        try:
            home, away = b["match"].split(" vs ")
        except ValueError:
            continue

        stake = float(b["stake"])
        odds = float(b["odds"])

        profit = -stake
        if b["selection"] == home and r["winner"] == "HOME":
            profit = stake * (odds - 1.0)
        elif b["selection"] == away and r["winner"] == "AWAY":
            profit = stake * (odds - 1.0)
        elif str(b["selection"]).upper() == "DRAW" and r["winner"] == "DRAW":
            profit = stake * (odds - 1.0)

        settled.append(profit)

    if not settled:
        return {
            "plays": 0,
            "wins": 0,
            "losses": 0,
            "profit_units": 0.0,
            "roi_percent": 0.0
        }

    plays = len(settled)
    wins = len([p for p in settled if p > 0])
    losses = len([p for p in settled if p < 0])
    profit_units = round(sum(settled), 2)
    total_stake = sum(float(b["stake"]) for b in bets[:plays]) if plays else 0
    roi_percent = round((profit_units / total_stake) * 100, 2) if total_stake else 0.0

    return {
        "plays": plays,
        "wins": wins,
        "losses": losses,
        "profit_units": profit_units,
        "roi_percent": roi_percent
    }
