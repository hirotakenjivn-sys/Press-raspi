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

ACTIVE_LEVEL = 0      # 押したとき LOW
INACTIVE_LEVEL = 1    # 通常 HIGH

GLITCH_FILTER_US = 1000
MIN_INTERVAL_MS = 120
MIN_PULSE_MS = 10
MAX_PULSE_MS = 2000
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
press_ts = None
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
# GPIO Callback
# =========================
def gpio_callback(chip_handle, gpio, level, timestamp_ns):
    global press_ts, last_valid_ts, stroke_count

    if time.time() - start_time < STARTUP_IGNORE_SEC:
        return

    # 押した瞬間
    if level == ACTIVE_LEVEL:
        press_ts = timestamp_ns
        return

    # 離した瞬間
    if level == INACTIVE_LEVEL and press_ts is not None:

        pulse_ns = timestamp_ns - press_ts
        pulse_ms = pulse_ns / 1_000_000.0

        if pulse_ms < MIN_PULSE_MS or pulse_ms > MAX_PULSE_MS:
            press_ts = None
            return

        if last_valid_ts is not None:
            interval_ms = (timestamp_ns - last_valid_ts) / 1_000_000.0
            if interval_ms < MIN_INTERVAL_MS:
                press_ts = None
                return

        last_valid_ts = timestamp_ns
        stroke_count += 1
        press_ts = None

        ts = int(time.time() * 1000)

        with buffer_lock:
            ram_buffer.append(ts)

        print(f"[COUNT] {stroke_count}  pulse={pulse_ms:.2f}ms", flush=True)

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

            else:
                api_err_count += 1
                if now - last_err_log >= 60:
                    print(f"[API ERROR] {r.status_code} (x{api_err_count})", flush=True)
                    last_err_log = now

        except Exception as e:
            api_err_count += 1
            if now - last_err_log >= 60:
                print(f"[API ERROR] {e} (x{api_err_count})", flush=True)
                last_err_log = now

# =========================
# Main
# =========================
def main():
    global chip

    chip = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_input(chip, GPIO_PIN, lgpio.SET_PULL_UP)
    lgpio.gpio_set_debounce_micros(chip, GPIO_PIN, GLITCH_FILTER_US)
    lgpio.gpio_claim_alert(chip, GPIO_PIN, lgpio.BOTH_EDGES)
    cb = lgpio.callback(chip, GPIO_PIN, lgpio.BOTH_EDGES, gpio_callback)

    threading.Thread(target=db_flush_loop, daemon=True).start()
    threading.Thread(target=api_sender_loop, daemon=True).start()

    print(f"Press Counter Started [{RASPI_NO}]", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        cb.cancel()
        lgpio.gpiochip_close(chip)

if __name__ == "__main__":
    main()
