#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import sqlite3
import threading
from pathlib import Path
import lgpio
import requests

# =========================
# GPIO設定
# GPIO_PIN ── スイッチ ── GND
# =========================
GPIO_PIN = 17

POLL_INTERVAL_S = 0.005   # 5ms ポーリング間隔
MIN_INTERVAL_MS = 200     # SPM180(サイクル333ms)対応
MIN_HIGH_MS = 50          # HIGH最低持続時間（バウンス除去）
CONFIRM_MS = 20           # LOW維持確認（ノイズ除去）
STARTUP_IGNORE_SEC = 5

DB_FLUSH_INTERVAL = 3
API_SEND_INTERVAL = 20
API_BATCH_SIZE = 100

API_URL = "http://192.168.50.63:8000/api/iot/events"

DB_PATH = Path(__file__).with_name("press_events.db")
CONFIG_PATH = Path(__file__).with_name("config.txt")

# =========================
# raspi_no をconfig.txtから読み込み
# =========================
def load_raspi_no():
    try:
        return CONFIG_PATH.read_text().strip()
    except FileNotFoundError:
        return "unknown"

RASPI_NO = load_raspi_no()

# =========================
# グローバル変数
# =========================
chip = None
last_valid_ts = None

stroke_count = 0
sent_total = 0

ram_buffer = []
buffer_lock = threading.Lock()

db_lock = threading.Lock()
stop_event = threading.Event()
last_api_send = 0

start_time = time.time()

# =========================
# SQLite初期化
# =========================
def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL,
            sent INTEGER DEFAULT 0
        );
    """)

    conn.commit()
    return conn

db_conn = init_db()

# =========================
# GPIO ポーリング
# =========================
def gpio_poll_loop():
    global last_valid_ts, stroke_count
    prev_state = lgpio.gpio_read(chip, GPIO_PIN)
    high_start = time.time() if prev_state == 1 else None

    while not stop_event.is_set():
        state = lgpio.gpio_read(chip, GPIO_PIN)

        if state != prev_state:
            if state == 1:
                # LOW→HIGH: HIGH開始時刻を記録
                high_start = time.time()
            elif state == 0 and prev_state == 1:
                # HIGH→LOW遷移
                # フィルタ1: 直前HIGH持続チェック（バウンス除去）
                high_ms = (time.time() - high_start) * 1000 if high_start else 0
                if high_ms >= MIN_HIGH_MS:
                    # フィルタ2: LOW維持確認（ノイズ除去）
                    confirm_start = time.time()
                    confirmed = True
                    while (time.time() - confirm_start) * 1000 < CONFIRM_MS:
                        if lgpio.gpio_read(chip, GPIO_PIN) != 0:
                            confirmed = False
                            break
                        time.sleep(POLL_INTERVAL_S)

                    if confirmed:
                        now_ms = time.time() * 1000
                        if time.time() - start_time >= STARTUP_IGNORE_SEC:
                            # フィルタ3: 最小間隔チェック
                            if last_valid_ts is None or (now_ms - last_valid_ts) >= MIN_INTERVAL_MS:
                                last_valid_ts = now_ms
                                stroke_count += 1
                                ts = int(now_ms)
                                with buffer_lock:
                                    ram_buffer.append(ts)
                                print(f"[COUNT] {stroke_count}", flush=True)

            prev_state = state
        time.sleep(POLL_INTERVAL_S)

# =========================
# DB Flush
# =========================
def db_flush_loop():
    while not stop_event.is_set():
        time.sleep(DB_FLUSH_INTERVAL)

        with buffer_lock:
            if not ram_buffer:
                continue
            data = ram_buffer.copy()
            ram_buffer.clear()

        with db_lock:
            cur = db_conn.cursor()
            cur.executemany(
                "INSERT INTO events(ts_ms,sent) VALUES(?,0)",
                [(ts,) for ts in data]
            )
            db_conn.commit()

# =========================
# API Sender
# =========================
def api_sender_loop():
    global last_api_send, sent_total
    http = requests.Session()
    api_err_count = 0
    last_err_log = 0

    while not stop_event.is_set():
        time.sleep(1)
        now = time.time()

        with db_lock:
            cur = db_conn.cursor()
            cur.execute(
                "SELECT id, ts_ms FROM events WHERE sent=0 ORDER BY id LIMIT ?",
                (API_BATCH_SIZE,)
            )
            rows = cur.fetchall()

        if not rows:
            continue

        if len(rows) < API_BATCH_SIZE and (now - last_api_send) < API_SEND_INTERVAL:
            continue

        payload = {
            "raspi_no": RASPI_NO,
            "events": [{"ts_ms": r[1]} for r in rows]
        }

        ids = [r[0] for r in rows]
        batch_count = len(ids)

        try:
            r = http.post(API_URL, json=payload, timeout=5)
            if r.status_code == 200:

                with db_lock:
                    cur = db_conn.cursor()
                    cur.executemany(
                        "UPDATE events SET sent=1 WHERE id=?",
                        [(i,) for i in ids]
                    )
                    db_conn.commit()

                sent_total += batch_count
                last_api_send = now
                if api_err_count > 0:
                    print(f"[API] recovered after {api_err_count} errors", flush=True)
                api_err_count = 0

                print(f"[API] {batch_count}  ({sent_total}/{stroke_count})", flush=True)

            # else:
            #     api_err_count += 1
            #     if now - last_err_log >= 60:
            #         print(f"[API ERROR] {r.status_code} (x{api_err_count})", flush=True)
            #         last_err_log = now

        except Exception:
            pass
            # api_err_count += 1
            # if now - last_err_log >= 60:
            #     print(f"[API ERROR] {e} (x{api_err_count})", flush=True)
            #     last_err_log = now

# =========================
# Main
# =========================
def main():
    global chip

    chip = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_input(chip, GPIO_PIN, lgpio.SET_PULL_UP)

    threading.Thread(target=gpio_poll_loop, daemon=True).start()
    threading.Thread(target=db_flush_loop, daemon=True).start()
    threading.Thread(target=api_sender_loop, daemon=True).start()

    print(f"Press Counter Started [{RASPI_NO}] (polling {POLL_INTERVAL_S*1000:.0f}ms)", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        lgpio.gpiochip_close(chip)

if __name__ == "__main__":
    main()
