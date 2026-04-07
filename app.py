from fastapi import FastAPI, HTTPException, Body
from supabase import create_client
import os
import unicodedata
from datetime import datetime, timezone

app = FastAPI(title="Betting AI Aggregator V1")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL:
    raise RuntimeError("Missing SUPABASE_URL")
if not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def safe_data(resp):
    return resp.data or []

def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = (
        s.replace(" if", "")
         .replace(" fc", "")
         .replace(" bk", "")
         .replace(" ff", "")
         .replace(".", "")
         .replace("-", " ")
         .replace("  ", " ")
         .strip()
    )
    return s

def make_event_key(league: str, match: str) -> str:
    league_n = normalize_text(league)
    match_n = normalize_text(match)
    return f"{league_n}|{match_n}"

def score_pick(odds: float, league: str):
    implied = 1.0 / odds

    league_bonus = {
        "Allsvenskan": 0.020,
        "Superettan": 0.015,
        "Superliga": 0.015,
        "Eliteserien": 0.020,
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
    score = edge * 100.0

    return round(edge, 4), round(score, 2)

def decision_from_score(score: float) -> str:
    if score >= 2.5:
        return "🔥 SPELA"
    if score >= 1.5:
        return "⚠️ BEVAKA"
    return "❌ PASS"

@app.get("/")
def root():
    return {"status": "running", "version": "aggregator-v1"}

@app.get("/health")
def health():
    try:
        result = {"status": "ok"}

        try:
            result["odds_market_rows"] = len(safe_data(supabase.table("odds_market").select("id").execute()))
        except Exception as e:
            result["odds_market_rows"] = f"error: {str(e)}"

        try:
            result["best_odds_rows"] = len(safe_data(supabase.table("best_odds").select("id").execute()))
        except Exception as e:
            result["best_odds_rows"] = f"error: {str(e)}"

        try:
            result["pick_rows"] = len(safe_data(supabase.table("picks").select("id").execute()))
        except Exception as e:
            result["pick_rows"] = f"error: {str(e)}"

        return result

    except Exception as e:
        return {"status": "error", "detail": str(e)}

@app.post("/ingest-odds")
def ingest_odds(data: list[dict] = Body(...)):
    rows = []

    for row in data:
        required = ["match", "league", "selection", "odds", "bookmaker"]
        for field in required:
            if field not in row:
                raise HTTPException(status_code=400, detail=f"Missing {field}")

        event_key = row.get("event_key") or make_event_key(row["league"], row["match"])

        rows.append({
            "event_key": event_key,
            "event_id": row.get("event_id", ""),
            "match": row["match"],
            "league": row["league"],
            "selection": row["selection"],
            "odds": float(row["odds"]),
            "bookmaker": row["bookmaker"],
            "source_url": row.get("source_url", ""),
            "scraped_at": now_iso()
        })

    if rows:
        supabase.table("odds_market").insert(rows).execute()

    return {"status": "ingested", "rows": len(rows)}

@app.get("/build-best-odds")
def build_best_odds():
    market_rows = safe_data(supabase.table("odds_market").select("*").execute())

    if not market_rows:
        return {"status": "no market data", "rows": 0}

    grouped = {}

    for row in market_rows:
        key = (row["event_key"], row["selection"])
        if key not in grouped:
            grouped[key] = row
        else:
            if float(row["odds"]) > float(grouped[key]["odds"]):
                grouped[key] = row

    best_rows = []
    for (_, _), row in grouped.items():
        best_rows.append({
            "event_key": row["event_key"],
            "match": row["match"],
            "league": row["league"],
            "selection": row["selection"],
            "best_odds": float(row["odds"]),
            "best_bookmaker": row["bookmaker"],
            "updated_at": now_iso()
        })

    supabase.table("best_odds").delete().neq("event_key", "").execute()

    if best_rows:
        supabase.table("best_odds").insert(best_rows).execute()

    return {"status": "built", "rows": len(best_rows)}

@app.get("/generate-picks")
def generate_picks():
    rows = safe_data(supabase.table("best_odds").select("*").execute())

    if not rows:
        return {"status": "no best odds", "rows": 0}

    picks = []
    for row in rows:
        edge, score = score_pick(float(row["best_odds"]), row["league"])
        decision = decision_from_score(score)

        if decision == "❌ PASS":
            continue

        picks.append({
            "event_key": row["event_key"],
            "match": row["match"],
            "league": row["league"],
            "selection": row["selection"],
            "odds": float(row["best_odds"]),
            "bookmaker": row["best_bookmaker"],
            "edge": edge,
            "score": score,
            "decision": decision,
            "created_at": now_iso()
        })

    picks = sorted(picks, key=lambda x: x["score"], reverse=True)[:20]

    supabase.table("picks").delete().neq("event_key", "").execute()

    if picks:
        supabase.table("picks").insert(picks).execute()

    return {"status": "generated", "rows": len(picks)}

@app.get("/picks")
def picks():
    return safe_data(
        supabase.table("picks")
        .select("*")
        .order("score", desc=True)
        .limit(20)
        .execute()
    )

@app.get("/best-odds")
def best_odds():
    return safe_data(
        supabase.table("best_odds")
        .select("*")
        .order("updated_at", desc=True)
        .limit(200)
        .execute()
    )
