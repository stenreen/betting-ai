from fastapi import FastAPI, HTTPException
import pandas as pd
import sqlite3
import requests
import os
from datetime import date, datetime, timezone

app = FastAPI(title="Betting AI with Result History")

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
    market TEXT,
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
    market TEXT,
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
# CONFIG
# -----------------------------
# 15-liga safe setup mot Odds API free tier
ODDS_LEAGUES = [
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
    ("icehockey_sweden_shl", "SHL"),
    ("icehockey_sweden_allsvenskan", "HockeyAllsvenskan"),
    ("icehockey_nhl", "NHL"),
    ("icehockey_finland_liiga", "Liiga"),
    ("icehockey_switzerland_nl", "National League"),
]

# Fotbollsresultat via API-Football
# Verifiera gärna dessa league IDs i ditt eget API-Sports-konto.
FOOTBALL_RESULT_LEAGUES = [
    {"league_id": 113, "league": "Allsvenskan"},
    {"league_id": 114, "league": "Superettan"},
    {"league_id": 119, "league": "Superliga"},
    {"league_id": 103, "league": "Eliteserien"},
    {"league_id": 244, "league": "Veikkausliiga"},
    {"league_id": 40,  "league": "Championship"},
    {"league_id": 140, "league": "La Liga"},
    {"league_id": 135, "league": "Serie A"},
    {"league_id": 61,  "league": "Ligue 1"},
    {"league_id": 253, "league": "MLS"},
]

# -----------------------------
# HELPERS
# -----------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def current_football_season() -> int:
    today = date.today()
    return today.year if today.month >= 7 else today.year - 1

def score_pick(odds: float, league: str) -> tuple[float, float]:
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
        "SHL": 0.010,
        "HockeyAllsvenskan": 0.015,
        "NHL": 0.000,
        "Liiga": 0.010,
        "National League": 0.010,
    }.get(league, 0.0)

    odds_bonus = 0.0
    if 1.70 <= odds <= 2.50:
        odds_bonus = 0.010
    elif 2.50 < odds <= 3.50:
        odds_bonus = 0.005

    model_prob = min(max(implied + league_bonus + odds_bonus, 0.01), 0.99)
    edge = model_prob - implied
    score = round(edge * 100, 4)

    return round(edge, 4), score

def decision_from_score(score: float) -> str:
    return "🔥 SPELA" if score >= 2.0 else "⚠️ BEVAKA"

def already_updated_today(key_name: str) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key_name,)).fetchone()

    if row and row["value"] == today:
        return True

    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (key_name, today)
    )
    conn.commit()
    return False

# -----------------------------
# ROOT / HEALTH
# -----------------------------
@app.get("/")
def root():
    return {"status": "running"}

@app.get("/health")
def health():
    picks_count = conn.execute("SELECT COUNT(*) AS c FROM picks").fetchone()["c"]
    history_count = conn.execute("SELECT COUNT(*) AS c FROM history").fetchone()["c"]
    results_count = conn.execute("SELECT COUNT(*) AS c FROM results").fetchone()["c"]

    return {
        "status": "ok",
        "pick_rows": picks_count,
        "history_rows": history_count,
        "result_rows": results_count
    }

# -----------------------------
# UPDATE ODDS / PICKS
# -----------------------------
@app.get("/update")
def update():
    if not ODDS_API_KEY:
        raise HTTPException(status_code=500, detail="Missing ODDS_API_KEY")

    # skydd mot att bränna credits i onödan
    if already_updated_today("last_odds_update"):
        return {"status": "skipped", "reason": "already updated today"}

    conn.execute("DELETE FROM picks")

    inserted = 0
    now = utc_now_iso()

    for sport_key, league_name in ODDS_LEAGUES:
        url = (
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
            f"?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h&oddsFormat=decimal"
        )

        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code != 200:
                continue
            data = resp.json()
        except Exception:
            continue

        for event in data:
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            match = f"{home} vs {away}"
            event_id = event.get("id", "")

            bookmakers = event.get("bookmakers", [])
            if not bookmakers:
                continue

            markets = bookmakers[0].get("markets", [])
            if not markets:
                continue

            outcomes = markets[0].get("outcomes", [])
            for outcome in outcomes:
                selection = outcome.get("name", "")
                odds = outcome.get("price")

                if odds is None:
                    continue

                edge, score = score_pick(float(odds), league_name)
                decision = decision_from_score(score)

                conn.execute("""
                    INSERT INTO picks
                    (event_id, match, league, market, selection, odds, edge, score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event_id,
                    match,
                    league_name,
                    "h2h",
                    selection,
                    float(odds),
                    float(edge),
                    float(score)
                ))

                conn.execute("""
                    INSERT INTO history
                    (event_id, match, league, market, selection, odds, edge, score, decision, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event_id,
                    match,
                    league_name,
                    "h2h",
                    selection,
                    float(odds),
                    float(edge),
                    float(score),
                    decision,
                    now
                ))

                inserted += 1

    conn.commit()

    return {
        "status": "updated",
        "rows_inserted": inserted,
        "leagues_used": len(ODDS_LEAGUES)
    }

# -----------------------------
# UPDATE RESULTS (FOOTBALL)
# -----------------------------
@app.get("/update-results")
def update_results():
    if not API_FOOTBALL_KEY:
        raise HTTPException(status_code=500, detail="Missing API_FOOTBALL_KEY")

    if already_updated_today("last_results_update"):
        return {"status": "skipped", "reason": "results already updated today"}

    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    season = current_football_season()
    today = date.today().isoformat()

    inserted = 0

    for cfg in FOOTBALL_RESULT_LEAGUES:
        url = (
            "https://v3.football.api-sports.io/fixtures"
            f"?league={cfg['league_id']}&season={season}&date={today}"
        )

        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                continue
            data = resp.json()
        except Exception:
            continue

        for item in data.get("response", []):
            fixture = item.get("fixture", {})
            teams = item.get("teams", {})
            goals = item.get("goals", {})
            status = fixture.get("status", {})

            home = teams.get("home", {}).get("name", "")
            away = teams.get("away", {}).get("name", "")
            match = f"{home} vs {away}"

            event = conn.execute("""
                SELECT event_id
                FROM history
                WHERE league = ? AND match = ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (cfg["league"], match)).fetchone()

            if not event:
                continue

            hs = goals.get("home")
            aw = goals.get("away")

            winner = ""
            if hs is not None and aw is not None:
                if hs > aw:
                    winner = "HOME"
                elif aw > hs:
                    winner = "AWAY"
                else:
                    winner = "DRAW"

            conn.execute("""
                INSERT OR REPLACE INTO results
                (event_id, home_score, away_score, winner, status, settled_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                event["event_id"],
                hs,
                aw,
                winner,
                status.get("short", ""),
                utc_now_iso()
            ))
            inserted += 1

    conn.commit()

    return {
        "status": "updated-results",
        "rows_inserted": inserted,
        "leagues_checked": len(FOOTBALL_RESULT_LEAGUES)
    }

# -----------------------------
# PICKS
# -----------------------------
@app.get("/picks")
def picks():
    df = pd.read_sql("""
        SELECT
            event_id,
            match,
            league,
            market,
            selection,
            odds,
            edge,
            score
        FROM picks
        ORDER BY score DESC, edge DESC
        LIMIT 25
    """, conn)

    if df.empty:
        return []

    df["decision"] = df["score"].apply(decision_from_score)
    return df.to_dict(orient="records")

# -----------------------------
# HISTORY
# -----------------------------
@app.get("/history")
def history():
    df = pd.read_sql("""
        SELECT
            h.event_id,
            h.match,
            h.league,
            h.market,
            h.selection,
            h.odds,
            h.edge,
            h.score,
            h.decision,
            h.created_at,
            r.home_score,
            r.away_score,
            r.winner,
            r.status
        FROM history h
        LEFT JOIN results r
            ON h.event_id = r.event_id
        ORDER BY h.created_at DESC
        LIMIT 300
    """, conn)

    return df.to_dict(orient="records")

# -----------------------------
# STATS
# -----------------------------
@app.get("/stats")
def stats():
    df = pd.read_sql("""
        SELECT
            h.event_id,
            h.match,
            h.league,
            h.market,
            h.selection,
            h.odds,
            h.edge,
            h.score,
            h.decision,
            r.home_score,
            r.away_score,
            r.winner
        FROM history h
        LEFT JOIN results r
            ON h.event_id = r.event_id
    """, conn)

    if df.empty:
        return {
            "plays": 0,
            "wins": 0,
            "losses": 0,
            "profit_units": 0.0,
            "roi_percent": 0.0
        }

    def calc_win(row):
        if pd.isna(row["winner"]):
            return None

        if row["market"] != "h2h":
            return None

        try:
            home, away = row["match"].split(" vs ")
        except ValueError:
            return None

        if row["selection"] == home and row["winner"] == "HOME":
            return 1
        if row["selection"] == away and row["winner"] == "AWAY":
            return 1
        if str(row["selection"]).upper() == "DRAW" and row["winner"] == "DRAW":
            return 1

        return 0

    df["won"] = df.apply(calc_win, axis=1)
    settled = df[df["won"].notna()].copy()

    if settled.empty:
        return {
            "plays": 0,
            "wins": 0,
            "losses": 0,
            "profit_units": 0.0,
            "roi_percent": 0.0
        }

    settled["profit"] = settled.apply(
        lambda r: (float(r["odds"]) - 1.0) if r["won"] == 1 else -1.0,
        axis=1
    )

    plays = int(len(settled))
    wins = int((settled["won"] == 1).sum())
    losses = int((settled["won"] == 0).sum())
    profit_units = round(float(settled["profit"].sum()), 2)
    roi_percent = round((profit_units / plays) * 100, 2) if plays else 0.0

    return {
        "plays": plays,
        "wins": wins,
        "losses": losses,
        "profit_units": profit_units,
        "roi_percent": roi_percent
    }
