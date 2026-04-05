from fastapi import FastAPI
import pandas as pd
import sqlite3

app = FastAPI()

conn = sqlite3.connect("data.db", check_same_thread=False)

# skapa enkel tabell om den inte finns
conn.execute("""
CREATE TABLE IF NOT EXISTS picks (
    event_id TEXT,
    selection TEXT,
    odds REAL,
    edge REAL,
    score REAL
)
""")

@app.get("/")
def root():
    return {"status": "running"}

@app.get("/picks")
def picks():
    df = pd.read_sql("SELECT * FROM picks ORDER BY score DESC LIMIT 5", conn)
    return df.to_dict(orient="records")

@app.get("/update")
def update():
    # fake data första gången så du ser att det funkar
    conn.execute("DELETE FROM picks")

    conn.execute("INSERT INTO picks VALUES ('1','Malmö',1.9,0.03,3.2)")
    conn.execute("INSERT INTO picks VALUES ('2','AIK',2.1,0.04,3.5)")

    conn.commit()

    return {"status": "updated"}
