from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone

app = FastAPI()

all_events = []


class EventItem(BaseModel):
    ts_ms: int

    class Config:
        extra = "ignore"


class BatchRequest(BaseModel):
    raspi_no: Optional[str] = "unknown"
    events: List[EventItem]

    class Config:
        extra = "ignore"


# ---------------------------
# ダッシュボード
# ---------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard():

    rows = ""
    for e in reversed(all_events):
        dt = datetime.fromtimestamp(e / 1000, tz=timezone.utc)
        rows += f"<tr><td>{e}</td><td>{dt}</td></tr>"

    return f"""
    <html>
    <head>
        <meta http-equiv="refresh" content="2">
        <title>IoT Monitor</title>
    </head>
    <body style="font-family: Arial; margin:40px;">
        <h1>📡 IoT Monitor</h1>
        <h2>累計受信数: {len(all_events)}</h2>
        <table border="1" cellpadding="5">
            <tr>
                <th>ts_ms</th>
                <th>UTC Time</th>
            </tr>
            {rows}
        </table>
    </body>
    </html>
    """


# ---------------------------
# 受信API
# ---------------------------
@app.post("/api/iot/events")
async def receive_events(batch: BatchRequest):

    for e in batch.events:
        all_events.append(e.ts_ms)

    return {"status": "ok", "total": len(all_events)}ç