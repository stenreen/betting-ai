from fastapi import FastAPI, HTTPException
from supabase import create_client
import requests
import os
from datetime import datetime, timezone

app = FastAPI(title="Betfair V1")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BETFAIR_APP_KEY = os.getenv("BETFAIR_APP_KEY", "")
BETFAIR_SESSION_TOKEN = os.getenv("BETFAIR_SESSION_TOKEN", "")

if not SUPABASE_URL:
    raise RuntimeError("Missing SUPABASE_URL")
if not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def safe_data(resp):
    return resp.data or []

def make_event_key(event_name: str, league: str) -> str:
    return f"{league.strip().lower()}|{event_name.strip().lower()}"

def score_edge(edge: float) -> float:
    return round(edge * 100, 2)

@app.get("/health")
def health():
    return {
        "status": "ok",
        "betfair_rows": len(safe_data(supabase.table("betfair_market").select("id").execute())),
        "bookmaker_rows": len(safe_data(supabase.table("bookmaker_market").select("id").execute())),
        "value_rows": len(safe_data(supabase.table("value_candidates").select("id").execute())),
    }

@app.get("/fetch-betfair")
def fetch_betfair():
    if not BETFAIR_APP_KEY:
        raise HTTPException(status_code=500, detail="Missing BETFAIR_APP_KEY")
    if not BETFAIR_SESSION_TOKEN:
        raise HTTPException(status_code=500, detail="Missing BETFAIR_SESSION_TOKEN")

    headers = {
        "X-Application": BETFAIR_APP_KEY,
        "X-Authentication": BETFAIR_SESSION_TOKEN,
        "Content-Type": "application/json"
    }

    # 1) hämta marknader (fotboll, MATCH_ODDS)
    catalogue_payload = {
        "filter": {
            "eventTypeIds": ["1"],   # Soccer
            "marketTypeCodes": ["MATCH_ODDS"]
        },
        "maxResults": "100",
        "marketProjection": ["EVENT", "COMPETITION", "RUNNER_DESCRIPTION"]
    }

    cat_resp = requests.post(
        "https://api.betfair.com/exchange/betting/json-rpc/v1",
        headers=headers,
        json=[{
            "jsonrpc": "2.0",
            "method": "SportsAPING/v1.0/listMarketCatalogue",
            "params": catalogue_payload,
            "id": 1
        }],
        timeout=30
    )

    if cat_resp.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Betfair catalogue failed: {cat_resp.text}")

    cat_json = cat_resp.json()
    markets = cat_json[0].get("result", [])

    if not markets:
        return {"status": "no markets", "rows": 0}

    market_ids = [m["marketId"] for m in markets[:50]]

    # 2) hämta priser
    book_payload = {
        "marketIds": market_ids,
        "priceProjection": {
            "priceData": ["EX_BEST_OFFERS"]
        }
    }

    book_resp = requests.post(
        "https://api.betfair.com/exchange/betting/json-rpc/v1",
        headers=headers,
        json=[{
            "jsonrpc": "2.0",
            "method": "SportsAPING/v1.0/listMarketBook",
            "params": book_payload,
            "id": 1
        }],
        timeout=30
    )

    if book_resp.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Betfair book failed: {book_resp.text}")

    book_json = book_resp.json()
    books = book_json[0].get("result", [])

    market_lookup = {m["marketId"]: m for m in markets}
    rows = []

    for book in books:
        market_id = book["marketId"]
        meta = market_lookup.get(market_id)
        if not meta:
            continue

        event_name = meta.get("event", {}).get("name", "")
        league = meta.get("competition", {}).get("name", "Unknown")

        runner_names = {}
        for r in meta.get("runners", []):
            runner_names[r["selectionId"]] = r["runnerName"]

        for runner in book.get("runners", []):
            ex = runner.get("ex", {})
            offers = ex.get("availableToBack", [])
            if not offers:
                continue

            back_price = float(offers[0]["price"])
            implied_prob = round(1.0 / back_price, 4)

            rows.append({
                "market_id": market_id,
                "event_name": event_name,
                "league": league,
                "selection": runner_names.get(runner["selectionId"], str(runner["selectionId"])),
                "back_price": back_price,
                "implied_prob": implied_prob,
                "fetched_at": now_iso()
            })

    if rows:
        supabase.table("betfair_market").insert(rows).execute()

    return {"status": "betfair fetched", "rows": len(rows)}

@app.post("/ingest-bookmaker-odds")
def ingest_bookmaker_odds(data: list[dict] = Body(...)):
    rows = []

    for row in data:
        required = ["event_name", "league", "selection", "bookmaker", "odds"]
        for field in required:
            if field not in row:
                raise HTTPException(status_code=400, detail=f"Missing {field}")

        event_key = row.get("event_key") or make_event_key(row["event_name"], row["league"])

        rows.append({
            "event_key": event_key,
            "event_name": row["event_name"],
            "league": row["league"],
            "selection": row["selection"],
            "bookmaker": row["bookmaker"],
            "odds": float(row["odds"]),
            "fetched_at": now_iso()
        })

    if rows:
        supabase.table("bookmaker_market").insert(rows).execute()

    return {"status": "bookmaker odds ingested", "rows": len(rows)}

@app.get("/build-value-candidates")
def build_value_candidates():
    betfair_rows = safe_data(supabase.table("betfair_market").select("*").execute())
    bookmaker_rows = safe_data(supabase.table("bookmaker_market").select("*").execute())

    if not betfair_rows:
        return {"status": "no betfair data", "rows": 0}
    if not bookmaker_rows:
        return {"status": "no bookmaker data", "rows": 0}

    # index Betfair per event+selection
    bf_map = {}
    for row in betfair_rows:
        key = (make_event_key(row["event_name"], row["league"]), row["selection"].strip().lower())
        # behåll bästa (lägsta implied / högsta price om flera)
        if key not in bf_map or float(row["back_price"]) > float(bf_map[key]["back_price"]):
            bf_map[key] = row

    candidates = []

    for bm in bookmaker_rows:
        key = (bm["event_key"], bm["selection"].strip().lower())
        bf = bf_map.get(key)
        if not bf:
            continue

        bookmaker_odds = float(bm["odds"])
        betfair_odds = float(bf["back_price"])

        implied_betfair = 1.0 / betfair_odds
        implied_bookmaker = 1.0 / bookmaker_odds
        edge = implied_betfair - implied_bookmaker

        if edge <= 0:
            continue

        candidates.append({
            "event_key": bm["event_key"],
            "event_name": bm["event_name"],
            "league": bm["league"],
            "selection": bm["selection"],
            "bookmaker": bm["bookmaker"],
            "bookmaker_odds": bookmaker_odds,
            "betfair_odds": betfair_odds,
            "edge": round(edge, 4),
            "score": score_edge(edge),
            "created_at": now_iso()
        })

    supabase.table("value_candidates").delete().neq("event_key", "").execute()

    if candidates:
        candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)
        supabase.table("value_candidates").insert(candidates).execute()

    return {"status": "built", "rows": len(candidates)}

@app.get("/value-picks")
def value_picks():
    return safe_data(
        supabase.table("value_candidates")
        .select("*")
        .order("score", desc=True)
        .limit(20)
        .execute()
    )
    def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def make_event_key(event_name: str, league: str) -> str:
    return f"{league.strip().lower()}|{event_name.strip().lower()}"
