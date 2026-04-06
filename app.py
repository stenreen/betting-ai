from fastapi import FastAPI, HTTPException, Body
import requests
import os
import pandas as pd
import unicodedata
from datetime import date, datetime, timezone, timedelta
from supabase import create_client

app = FastAPI(title="Betting AI Supabase")

# =============================
# 🔑 SUPABASE CONNECTION
# =============================
supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")

# =============================
# ⚙️ CONFIG
# =============================
LEAGUES = [
    ("soccer_sweden_allsvenskan", "Allsvenskan"),
    ("soccer_sweden_superettan", "Superettan"),
    ("soccer_denmark_superliga", "Superliga"),
    ("soccer_norway_eliteserien", "Eliteserien"),
    ("soccer_usa_mls", "MLS"),

    ("soccer_spain_la_liga", "La Liga"),
    ("soccer_italy_serie_a", "Serie A"),
    ("soccer_efl_champ", "Championship"),
]

RESULT_LEAGUES = [
    {"league_id": 113, "league": "Allsvenskan"},
    {"league_id": 114, "league": "Superettan"},
    {"league_id": 119, "league": "Superliga"},
    {"league_id": 103, "league": "Eliteserien"},
    {"league_id": 253, "league": "MLS"},

    {"league_id": 140, "league": "La Liga"},
    {"league_id": 135, "league": "Serie A"},
    {"league_id": 40, "league": "Championship"},
]
FINAL_STATUSES = {"FT", "AET", "PEN"}

# =============================
# 🧠 HELPERS
# =============================
def now():
    return datetime.now(timezone.utc).isoformat()

def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return s.replace(" if","").replace(" fc","").replace("-", " ").strip()

def score_pick(odds, league):
    implied = 1 / odds
    bonus = 0.02 if league in ["Allsvenskan","MLS","Eliteserien"] else 0.01
    model = min(max(implied + bonus, 0.01), 0.99)
    edge = model - implied
    return round(edge, 4), round(edge * 100, 2)

def decision(score):
    if score >= 2.5: return "🔥 SPELA"
    if score >= 1.5: return "⚠️ BEVAKA"
    return "❌ PASS"

# =============================
# 🟢 FETCH ODDS (1x per dag)
# =============================
@app.get("/fetch-odds")
def fetch_odds():
    if not ODDS_API_KEY:
        raise HTTPException(500, "Missing ODDS_API_KEY")

    inserted = 0

    for sport, league in LEAGUES:
        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h"

        try:
            data = requests.get(url, timeout=30).json()
        except:
            continue

        for e in data:
            match = f"{e.get('home_team')} vs {e.get('away_team')}"
            event_id = e.get("id")

            try:
                outcomes = e["bookmakers"][0]["markets"][0]["outcomes"]
            except:
                continue

            for o in outcomes:
                if not o.get("price"):
                    continue

                supabase.table("odds_snapshot").insert({
                    "event_id": event_id,
                    "match": match,
                    "league": league,
                    "selection": o["name"],
                    "odds": o["price"],
                    "pulled_at": now()
                }).execute()

                inserted += 1

    return {"status": "odds fetched", "rows": inserted}

# =============================
# 🔵 GENERATE PICKS
# =============================
@app.get("/generate-picks")
def generate():
    data = supabase.table("odds_snapshot").select("*").execute().data

    if not data:
        return {"status": "no odds"}

    supabase.table("picks").delete().neq("event_id", "").execute()

    inserted = 0

    for row in data:
        edge, score = score_pick(row["odds"], row["league"])
        dec = decision(score)

        if dec == "❌ PASS":
            continue

        supabase.table("picks").insert({
            "event_id": row["event_id"],
            "match": row["match"],
            "league": row["league"],
            "selection": row["selection"],
            "odds": row["odds"],
            "edge": edge,
            "score": score,
            "decision": dec,
            "generated_at": now()
        }).execute()

        supabase.table("history").insert({
            **row,
            "edge": edge,
            "score": score,
            "decision": dec,
            "generated_at": now()
        }).execute()

        inserted += 1

    return {"status": "picks generated", "rows": inserted}

# =============================
# 🟠 FETCH RESULTS
# =============================
@app.get("/fetch-results")
def fetch_results():
    if not API_FOOTBALL_KEY:
        raise HTTPException(500, "Missing API_FOOTBALL_KEY")

    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    season = date.today().year

    history = supabase.table("history").select("*").execute().data

    inserted = 0

    for cfg in RESULT_LEAGUES:
        for d in [(date.today() - timedelta(days=i)).isoformat() for i in range(3)]:
            url = f"https://v3.football.api-sports.io/fixtures?league={cfg['league_id']}&season={season}&date={d}"

            try:
                data = requests.get(url, headers=headers).json()
            except:
                continue

            for m in data.get("response", []):
                if m["fixture"]["status"]["short"] not in FINAL_STATUSES:
                    continue

                home = norm(m["teams"]["home"]["name"])
                away = norm(m["teams"]["away"]["name"])

                for h in history:
                    txt = norm(h["match"])
                    if home in txt and away in txt:
                        hs = m["goals"]["home"]
                        aw = m["goals"]["away"]

                        winner = "DRAW"
                        if hs > aw: winner = "HOME"
                        elif aw > hs: winner = "AWAY"

                        supabase.table("results").upsert({
                            "event_id": h["event_id"],
                            "home_score": hs,
                            "away_score": aw,
                            "winner": winner,
                            "status": "FT",
                            "settled_at": now()
                        }).execute()

                        inserted += 1

    return {"status": "results updated", "rows": inserted}

# =============================
# 🧾 PICKS / HISTORY / STATS
# =============================
@app.get("/picks")
def picks():
    return supabase.table("picks").select("*").limit(20).execute().data

@app.get("/history")
def history():
    return supabase.table("history").select("*").limit(200).execute().data

@app.get("/stats")
def stats():
    data = supabase.table("history").select("*").execute().data
    results = supabase.table("results").select("*").execute().data

    if not data:
        return {"plays":0,"wins":0,"losses":0,"roi":0}

    wins = 0
    plays = 0

    for h in data:
        for r in results:
            if h["event_id"] == r["event_id"]:
                plays += 1

                home, away = h["match"].split(" vs ")

                if h["selection"] == home and r["winner"] == "HOME":
                    wins += 1
                elif h["selection"] == away and r["winner"] == "AWAY":
                    wins += 1

    roi = round((wins / plays) * 100, 2) if plays else 0

    return {
        "plays": plays,
        "wins": wins,
        "losses": plays - wins,
        "roi": roi
    }

# =============================
# 💰 BET TRACKING
# =============================
@app.post("/place-bet")
def place_bet(data: dict = Body(...)):
    supabase.table("bets").insert({
        "event_id": data["event_id"],
        "match": data["match"],
        "selection": data["selection"],
        "odds": data["odds"],
        "stake": data["stake"],
        "created_at": now()
    }).execute()

    return {"status": "bet saved"}

@app.get("/my-bets")
def my_bets():
    return supabase.table("bets").select("*").execute().data
