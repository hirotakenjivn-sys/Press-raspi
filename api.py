from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone

app = FastAPI()

# メモリ保存用（超シンプル）
last_received = 0
last_time = None


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
# 見える化ページ
# ---------------------------
@app.get("/")
async def dashboard():

    return f"""
    <html>
        <head>
            <meta http-equiv="refresh" content="2">
            <title>IoT Monitor</title>
        </head>
        <body style="font-family: Arial; text-align:center; margin-top:100px;">
            <h1>📡 IoT Monitor</h1>
            <h2>受信回数: {last_received}</h2>
            <h3>最終受信時刻: {last_time}</h3>
        </body>
    </html>
    """


# ---------------------------
# データ受信
# ---------------------------
@app.post("/api/iot/events")
async def receive_events(batch: BatchRequest):

    global last_received, last_time

    last_received = len(batch.events)
    last_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return {"status": "ok"}