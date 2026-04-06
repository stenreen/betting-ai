from fastapi import FastAPI, HTTPException
import pandas as pd
import sqlite3
import requests
import os
from datetime import date, datetime, timezone, timedelta

app = FastAPI(title="Betting AI Fetch/Generate Model")

conn = sqlite3.connect("data.db", check_same_thread=False)
conn.row_factory = sqlite3.Row

# -----------------------------
# DB SETUP
# -----------------------------
conn.execute("""
CREATE TABLE IF NOT EXISTS odds_snapshot (
    event_id TEXT,
    match TEXT,
    league TEXT,
    selection TEXT,
    odds REAL,
    pulled_at TEXT
)
""")

conn.execute("""
CREATE TABLE IF NOT EXISTS picks (
    event_id TEXT,
    match TEXT,
    league TEXT,
    selection TEXT,
    odds REAL,
    edge REAL,
    score REAL,
    decision TEXT,
    generated_at TEXT
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
    generated_at TEXT
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

# -----------------------------
# HELPERS
# -----------------------------
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def was_updated_today(key: str) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return bool(row and row["value"] == today_utc())

def mark_updated_today(key: str):
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (key, today_utc())
    )
    conn.commit()

def norm(s: str):
    return (
        str(s or "")
        .lower()
        .replace(" if", "")
        .replace(" fc", "")
        .replace(" bk", "")
        .replace(".", "")
        .replace("-", " ")
        .replace("  ", " ")
        .strip()
    )

def score_pick(odds: float, league: str):
    implied = 1 / odds

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

def decision_from_score(score: float, edge: float):
    if score >= 2.5 and edge >= 0.025:
        return "🔥 SPELA"
    elif score >= 1.5 and edge >= 0.015:
        return "⚠️ BEVAKA"
    return "❌ PASS"

# -----------------------------
# ROOT
# -----------------------------
@app.get("/")
def root():
    return {"status": "running"}

@app.get("/health")
def health():
    return {
        "status": "ok",
        "odds_rows": conn.execute("SELECT COUNT(*) FROM odds_snapshot").fetchone()[0],
        "pick_rows": conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0],
        "history_rows": conn.execute("SELECT COUNT(*) FROM history").fetchone()[0],
        "result_rows": conn.execute("SELECT COUNT(*) FROM results").fetchone()[0],
    }

# -----------------------------
# 1) FETCH ODDS
# -----------------------------
@app.get("/fetch-odds")
def fetch_odds(force: bool = False):
    if not ODDS_API_KEY:
        raise HTTPException(status_code=500, detail="Missing ODDS_API_KEY")

    if not force and was_updated_today("odds_fetch"):
        return {"status": "skipped", "reason": "odds already fetched today"}

    inserted = 0
    pulled_at = now_iso()

    # Vi sparar snapshots, men tar bort dagens snapshot först så inte samma dag dubblas vid force
    conn.execute("DELETE FROM odds_snapshot WHERE substr(pulled_at,1,10) = ?", (today_utc(),))

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
            home = e.get("home_team", "")
            away = e.get("away_team", "")
            match = f"{home} vs {away}"
            event_id = e.get("id", "")

            try:
                outcomes = e["bookmakers"][0]["markets"][0]["outcomes"]
            except Exception:
                continue

            for o in outcomes:
                selection = o.get("name", "")
                odds = o.get("price")
                if odds is None:
                    continue

                conn.execute("""
                    INSERT INTO odds_snapshot
                    (event_id, match, league, selection, odds, pulled_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (event_id, match, league, selection, float(odds), pulled_at))

                inserted += 1

    conn.commit()

    if inserted > 0:
        mark_updated_today("odds_fetch")

    return {"status": "fetched", "rows_inserted": inserted, "force": force}

# -----------------------------
# 2) GENERATE PICKS
# -----------------------------
@app.get("/generate-picks")
def generate_picks():
    conn.execute("DELETE FROM picks")

    # Senaste snapshot per event_id + selection
    df = pd.read_sql("""
        SELECT s1.event_id, s1.match, s1.league, s1.selection, s1.odds, s1.pulled_at
        FROM odds_snapshot s1
        JOIN (
            SELECT event_id, selection, MAX(pulled_at) AS max_pulled_at
            FROM odds_snapshot
            GROUP BY event_id, selection
        ) s2
        ON s1.event_id = s2.event_id
        AND s1.selection = s2.selection
        AND s1.pulled_at = s2.max_pulled_at
    """, conn)

    if df.empty:
        return {"status": "no_data", "reason": "No odds snapshots found. Run /fetch-odds first."}

    inserted = 0
    generated_at = now_iso()
    today_stamp = today_utc()

    for _, row in df.iterrows():
        edge, score = score_pick(float(row["odds"]), str(row["league"]))
        decision = decision_from_score(score, edge)

        if decision == "❌ PASS":
            continue

        conn.execute("""
            INSERT INTO picks
            (event_id, match, league, selection, odds, edge, score, decision, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["event_id"],
            row["match"],
            row["league"],
            row["selection"],
            float(row["odds"]),
            edge,
            score,
            decision,
            generated_at
        ))

        exists = conn.execute("""
            SELECT 1
            FROM history
            WHERE event_id = ?
              AND selection = ?
              AND substr(generated_at,1,10) = ?
        """, (row["event_id"], row["selection"], today_stamp)).fetchone()

        if not exists:
            conn.execute("""
                INSERT INTO history
                (event_id, match, league, selection, odds, edge, score, decision, generated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["event_id"],
                row["match"],
                row["league"],
                row["selection"],
                float(row["odds"]),
                edge,
                score,
                decision,
                generated_at
            ))

        inserted += 1

    conn.commit()

    return {"status": "generated", "rows_inserted": inserted}

# -----------------------------
# 3) FETCH RESULTS
# -----------------------------
@app.get("/fetch-results")
def fetch_results(force: bool = False):
    if not API_FOOTBALL_KEY:
        raise HTTPException(status_code=500, detail="Missing API_FOOTBALL_KEY")

    if not force and was_updated_today("results_fetch"):
        return {"status": "skipped", "reason": "results already fetched today"}

    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    today = date.today()
    season = today.year if today.month >= 7 else today.year - 1
    days = [(today - timedelta(days=i)).isoformat() for i in range(7)]

    inserted = 0
    debug_rows = []

    for cfg in RESULT_LEAGUES:
        for d in days:
            url = f"https://v3.football.api-sports.io/fixtures?league={cfg['league_id']}&season={season}&date={d}"

            try:
                resp = requests.get(url, headers=headers, timeout=30)
                if resp.status_code != 200:
                    continue
                data = resp.json()
            except Exception:
                continue

            for m in data.get("response", []):
                home_raw = m.get("teams", {}).get("home", {}).get("name", "")
                away_raw = m.get("teams", {}).get("away", {}).get("name", "")
                home = norm(home_raw)
                away = norm(away_raw)

                hist = conn.execute("""
                    SELECT event_id, match
                    FROM history
                    WHERE league = ?
                    ORDER BY generated_at DESC
                """, (cfg["league"],)).fetchall()

                matched_event_id = None

                for h in hist:
                    txt = norm(h["match"])
                    if home in txt and away in txt:
                        matched_event_id = h["event_id"]
                        break

                if not matched_event_id:
                    debug_rows.append({
                        "league": cfg["league"],
                        "home": home_raw,
                        "away": away_raw,
                        "reason": "no history match"
                    })
                    continue

                hs = m.get("goals", {}).get("home")
                aw = m.get("goals", {}).get("away")
                status = m.get("fixture", {}).get("status", {}).get("short", "")

                winner = "DRAW"
                if hs is not None and aw is not None:
                    if hs > aw:
                        winner = "HOME"
                    elif aw > hs:
                        winner = "AWAY"

                conn.execute("""
                    INSERT OR REPLACE INTO results
                    (event_id, home_score, away_score, winner, status, settled_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (matched_event_id, hs, aw, winner, status, now_iso()))

                inserted += 1

    conn.commit()

    if inserted > 0:
        mark_updated_today("results_fetch")

    return {
        "status": "fetched-results",
        "rows_inserted": inserted,
        "forced": force,
        "debug_sample": debug_rows[:10]
    }

# -----------------------------
# OUTPUTS
# -----------------------------
@app.get("/picks")
def picks():
    df = pd.read_sql("""
        SELECT event_id, match, league, selection, odds, edge, score, decision, generated_at
        FROM picks
        ORDER BY score DESC, edge DESC
        LIMIT 20
    """, conn)

    if df.empty:
        return []

    return df.to_dict(orient="records")

@app.get("/history")
def history():
    df = pd.read_sql("""
        SELECT
            h.event_id,
            h.match,
            h.league,
            h.selection,
            h.odds,
            h.edge,
            h.score,
            h.decision,
            h.generated_at,
            r.home_score,
            r.away_score,
            r.winner,
            r.status
        FROM history h
        LEFT JOIN results r ON h.event_id = r.event_id
        ORDER BY h.generated_at DESC
        LIMIT 300
    """, conn)
    return df.to_dict(orient="records")

@app.get("/stats")
def stats():
    df = pd.read_sql("""
        SELECT
            h.event_id,
            h.match,
            h.selection,
            h.odds,
            r.winner
        FROM history h
        LEFT JOIN results r ON h.event_id = r.event_id
    """, conn)

    if df.empty:
        return {"plays": 0, "wins": 0, "losses": 0, "profit_units": 0.0, "roi_percent": 0.0}

    def calc_win(row):
        if pd.isna(row["winner"]):
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

    df["win"] = df.apply(calc_win, axis=1)
    settled = df[df["win"].notna()].copy()

    if settled.empty:
        return {"plays": 0, "wins": 0, "losses": 0, "profit_units": 0.0, "roi_percent": 0.0}

    settled["profit"] = settled.apply(
        lambda r: (float(r["odds"]) - 1.0) if r["win"] == 1 else -1.0,
        axis=1
    )

    plays = int(len(settled))
    wins = int((settled["win"] == 1).sum())
    losses = int((settled["win"] == 0).sum())
    profit_units = round(float(settled["profit"].sum()), 2)
    roi_percent = round((profit_units / plays) * 100, 2) if plays else 0.0

    return {
        "plays": plays,
        "wins": wins,
        "losses": losses,
        "profit_units": profit_units,
        "roi_percent": roi_percent
    }
