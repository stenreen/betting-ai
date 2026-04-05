from fastapi import FastAPI, HTTPException
import pandas as pd
import sqlite3
import requests
import os
from datetime import date, datetime, timezone, timedelta

app = FastAPI(title="Betting AI V2 PRO")

conn = sqlite3.connect("data.db", check_same_thread=False)
conn.row_factory = sqlite3.Row

# -----------------------------
# DB SETUP
# -----------------------------
conn.execute("""
CREATE TABLE IF NOT EXISTS picks (
    event_id TEXT,
    match TEXT,
    league TEXT,
    selection TEXT,
    odds REAL,
    edge REAL,
    score REAL
)
""")

conn.execute("""
CREATE TABLE IF NOT EXISTS history (
    event_id TEXT,
    match TEXT,
    league TEXT,
    selection TEXT,
    odds REAL,
    edge REAL,
    score REAL,
    decision TEXT,
    created_at TEXT
)
""")

conn.execute("""
CREATE TABLE IF NOT EXISTS results (
    event_id TEXT PRIMARY KEY,
    home_score REAL,
    away_score REAL,
    winner TEXT,
    status TEXT,
    settled_at TEXT
)
""")

conn.execute("""
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
)
""")

conn.commit()

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")

# -----------------------------
# META FIX (NY)
# -----------------------------
def was_updated_today(key):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return bool(row and row["value"] == today)

def mark_updated_today(key):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, today))
    conn.commit()

# -----------------------------
# CONFIG
# -----------------------------
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
]

# -----------------------------
# HELPERS
# -----------------------------
def now():
    return datetime.now(timezone.utc).isoformat()

def score(odds, league):
    implied = 1 / odds
    bonus = 0.02
    edge = bonus
    score = edge * 100
    return round(edge, 4), round(score, 2)

def decision(score, edge):
    if score >= 2.5 and edge >= 0.025:
        return "🔥 SPELA"
    elif score >= 1.5 and edge >= 0.015:
        return "⚠️ BEVAKA"
    return "❌ PASS"

def norm(s):
    return str(s).lower().replace("if","").replace("fc","").replace(".","").strip()

# -----------------------------
# ROOT
# -----------------------------
@app.get("/")
def root():
    return {"status":"running"}

@app.get("/health")
def health():
    return {
        "picks": conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0],
        "history": conn.execute("SELECT COUNT(*) FROM history").fetchone()[0],
        "results": conn.execute("SELECT COUNT(*) FROM results").fetchone()[0],
    }

# -----------------------------
# UPDATE ODDS
# -----------------------------
@app.get("/update")
def update():

    if not ODDS_API_KEY:
        raise HTTPException(500,"Missing ODDS_API_KEY")

    if was_updated_today("odds"):
        return {"status":"skipped"}

    conn.execute("DELETE FROM picks")

    inserted = 0

    for sport, league in LEAGUES:

        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h"

        try:
            data = requests.get(url).json()
        except:
            continue

        for e in data:

            match = f"{e['home_team']} vs {e['away_team']}"
            event_id = e["id"]

            try:
                outcomes = e["bookmakers"][0]["markets"][0]["outcomes"]
            except:
                continue

            for o in outcomes:

                odds = o["price"]
                sel = o["name"]

                edge, score_val = score(odds, league)
                dec = decision(score_val, edge)

                conn.execute("INSERT INTO picks VALUES (?,?,?,?,?,?,?)",
                    (event_id, match, league, sel, odds, edge, score_val))

                # dedupe history (ny)
                exists = conn.execute("""
                    SELECT 1 FROM history
                    WHERE event_id=? AND selection=?
                    AND DATE(created_at)=DATE('now')
                """,(event_id, sel)).fetchone()

                if not exists:
                    conn.execute("""
                        INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?)
                    """,(event_id, match, league, sel, odds, edge, score_val, dec, now()))

                inserted += 1

    conn.commit()
    mark_updated_today("odds")

    return {"status":"updated","rows":inserted}

# -----------------------------
# UPDATE RESULTS
# -----------------------------
@app.get("/update-results")
def update_results(force: bool=False):

    if not API_FOOTBALL_KEY:
        raise HTTPException(500,"Missing API key")

    if not force and was_updated_today("results"):
        return {"status":"skipped"}

    headers = {"x-apisports-key": API_FOOTBALL_KEY}

    today = date.today()
    days = [(today - timedelta(days=i)).isoformat() for i in range(7)]

    inserted = 0

    for cfg in RESULT_LEAGUES:
        for d in days:

            url = f"https://v3.football.api-sports.io/fixtures?league={cfg['league_id']}&season=2024&date={d}"

            try:
                data = requests.get(url,headers=headers).json()
            except:
                continue

            for m in data.get("response",[]):

                home = norm(m["teams"]["home"]["name"])
                away = norm(m["teams"]["away"]["name"])

                hist = conn.execute("""
                    SELECT event_id, match FROM history
                    WHERE league=?
                """,(cfg["league"],)).fetchall()

                for h in hist:
                    txt = norm(h["match"])

                    if home in txt and away in txt:

                        hs = m["goals"]["home"]
                        aw = m["goals"]["away"]

                        winner = "DRAW"
                        if hs > aw: winner="HOME"
                        if aw > hs: winner="AWAY"

                        conn.execute("""
                            INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?)
                        """,(h["event_id"],hs,aw,winner,"FT",now()))

                        inserted += 1

    conn.commit()

    if inserted > 0:
        mark_updated_today("results")

    return {"rows":inserted}

# -----------------------------
# PICKS
# -----------------------------
@app.get("/picks")
def picks():
    df = pd.read_sql("SELECT * FROM picks ORDER BY score DESC LIMIT 20",conn)
    if df.empty: return []
    df["decision"]=df["score"].apply(lambda x:"🔥 SPELA" if x>=2 else "⚠️")
    return df.to_dict("records")

# -----------------------------
# STATS
# -----------------------------
@app.get("/stats")
def stats():

    df = pd.read_sql("""
        SELECT h.*, r.winner
        FROM history h
        LEFT JOIN results r ON h.event_id=r.event_id
    """,conn)

    if df.empty: return {"plays":0}

    def res(row):
        if pd.isna(row["winner"]): return None
        home,away=row["match"].split(" vs ")
        if row["selection"]==home and row["winner"]=="HOME": return 1
        if row["selection"]==away and row["winner"]=="AWAY": return 1
        return 0

    df["win"]=df.apply(res,axis=1)
    df=df[df["win"].notna()]

    if df.empty: return {"plays":0}

    df["profit"]=df.apply(lambda r:(r["odds"]-1) if r["win"]==1 else -1,axis=1)

    return {
        "plays":len(df),
        "profit":round(df["profit"].sum(),2),
        "roi":round(df["profit"].sum()/len(df)*100,2)
    }
