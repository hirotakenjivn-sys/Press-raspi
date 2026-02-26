"""
========================================
IoT Batch Receiver (Production Ready)
----------------------------------------
✔ raspi_no 任意
✔ 余分フィールド許可
✔ 型エラー防止
✔ ts_ms 異常値防止
✔ バッチ上限制限
✔ UTC固定
✔ logging対応
========================================
"""

from fastapi import FastAPI
from pydantic import BaseModel, field_validator
from typing import List, Optional
from datetime import datetime, timezone
import logging
import time

# -----------------------------------
# Logging 設定
# -----------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("iot")

app = FastAPI()


# -----------------------------------
# 1件イベント定義
# -----------------------------------
class EventItem(BaseModel):
    ts_ms: int

    @field_validator("ts_ms")
    @classmethod
    def validate_timestamp(cls, v):
        now_ms = int(time.time() * 1000)

        if v <= 0:
            raise ValueError("ts_ms must be positive")

        # 未来5分以上は禁止
        if v > now_ms + 5 * 60 * 1000:
            raise ValueError("ts_ms too far in future")

        return v

    class Config:
        extra = "ignore"


# -----------------------------------
# バッチ定義
# -----------------------------------
class BatchRequest(BaseModel):
    raspi_no: Optional[str] = "unknown"
    events: List[EventItem]

    @field_validator("events")
    @classmethod
    def validate_batch_size(cls, v):
        if len(v) == 0:
            raise ValueError("events cannot be empty")

        if len(v) > 1000:
            raise ValueError("Too many events in one batch (max 1000)")

        return v

    class Config:
        extra = "ignore"


# -----------------------------------
# Root確認用
# -----------------------------------
@app.get("/")
async def root():
    return {"status": "IoT API running"}


# -----------------------------------
# バッチ受信エンドポイント
# -----------------------------------
@app.post("/api/iot/events")
async def receive_events(batch: BatchRequest):

    count = len(batch.events)

    logger.info("===================================")
    logger.info(f"BATCH RECEIVED")
    logger.info(f"raspi_no: {batch.raspi_no}")
    logger.info(f"count: {count}")

    # 最初の3件だけログ表示（UTC）
    for e in batch.events[:3]:
        dt = datetime.fromtimestamp(e.ts_ms / 1000, tz=timezone.utc)
        logger.info(f"  event_time_utc: {dt}")

    return {
        "status": "ok",
        "received": count
    }