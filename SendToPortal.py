#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import sqlite3
import threading
from pathlib import Path
import pigpio
import requests

# =========================
# GPIO設定（現在の配線用）
# GPIO27 ── スイッチ ── GND
# =========================
GPIO_PIN = 27

ACTIVE_LEVEL = 0      # 押したとき LOW
INACTIVE_LEVEL = 1    # 通常 HIGH

GLITCH_FILTER_US = 1000
MIN_INTERVAL_MS = 120
MIN_PULSE_MS = 10
STARTUP_IGNORE_SEC = 5

DB_FLUSH_INTERVAL = 3
API_SEND_INTERVAL = 20
API_BATCH_SIZE = 100

API_URL = "http://192.168.50.63:8000/api/iot/events"
DB_PATH = Path(__file__).with_name("press_events.db")

pi = None
press_tick = None
last_valid_tick = None

stroke_count = 0
sent_total = 0

ram_buffer = []
buffer_lock = threading.Lock()

db_lock = threading.Lock()
stop_event = threading.Event()
last_api_send = 0

start_time = time.time()

# =========================
# SQLite
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
def gpio_callback(gpio, level, tick):
    global press_tick, last_valid_tick, stroke_count

    # 起動直後無視
    if time.time() - start_time < STARTUP_IGNORE_SEC:
        return

    # 押した瞬間（LOW）
    if level == ACTIVE_LEVEL:
        press_tick = tick
        return

    # 離した瞬間（HIGH）
    if level == INACTIVE_LEVEL and press_tick is not None:

        pulse_us = pigpio.tickDiff(press_tick, tick)
        pulse_ms = pulse_us / 1000.0

        if pulse_ms < MIN_PULSE_MS:
            press_tick = None
            return

        if last_valid_tick is not None:
            interval = pigpio.tickDiff(last_valid_tick, tick) / 1000.0
            if interval < MIN_INTERVAL_MS:
                press_tick = None
                return

        last_valid_tick = tick
        stroke_count += 1
        press_tick = None

        ts = int(time.time() * 1000)

        with buffer_lock:
            ram_buffer.append(ts)

        print(f"[COUNT] {stroke_count}   pulse={pulse_ms:.2f}ms")

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
            "raspi_no": "raspi_01",
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

                print(f"[API] {batch_count}   ({sent_total} / {stroke_count})")

            else:
                print("[API ERROR]", r.status_code, r.text)

        except Exception as e:
            print("[API ERROR]", e)

# =========================
# Main
# =========================
def main():
    global pi

    pi = pigpio.pi()
    if not pi.connected:
        raise RuntimeError("pigpiod not running")

    pi.set_mode(GPIO_PIN, pigpio.INPUT)

    # ★ ここが重要 ★
    pi.set_pull_up_down(GPIO_PIN, pigpio.PUD_UP)

    pi.set_glitch_filter(GPIO_PIN, GLITCH_FILTER_US)

    # コールバック参照保持
    cb = pi.callback(GPIO_PIN, pigpio.EITHER_EDGE, gpio_callback)

    threading.Thread(target=db_flush_loop, daemon=True).start()
    threading.Thread(target=api_sender_loop, daemon=True).start()

    print("Press Counter Started (PUD_UP mode)")

    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()