#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import sqlite3
import threading
import signal
import os
from pathlib import Path
import lgpio
import requests

# =========================
# GPIO設定
# GPIO_PIN ── スイッチ ── GND
# =========================
GPIO_PIN = 17

POLL_INTERVAL_S = 0.02    # 20ms ポーリング間隔（CPU負荷軽減）
MIN_INTERVAL_MS = 200     # SPM180(サイクル333ms)対応
MIN_HIGH_MS = 50          # HIGH最低持続時間（バウンス除去）
CONFIRM_MS = 20           # LOW維持確認（ノイズ除去）
STARTUP_IGNORE_SEC = 5

DB_FLUSH_INTERVAL = 3
API_SEND_INTERVAL = 20
API_BATCH_SIZE = 100
SYS_MONITOR_INTERVAL = 600  # 10分ごと
DB_CLEANUP_INTERVAL = 3600  # 1時間ごと

API_URL = "http://192.168.50.63:8000/api/iot/events"

DB_PATH = Path(__file__).with_name("press_events.db")
CONFIG_PATH = Path(__file__).with_name("config.txt")
CLEAN_SHUTDOWN_FLAG = Path(__file__).with_name(".clean_shutdown")

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

stroke_count = 0  # init_db()後にload_stroke_count()で上書き
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

def load_stroke_count():
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM events")
    return cur.fetchone()[0]

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
    backoff = 1

    while not stop_event.is_set():
        time.sleep(backoff)
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
                backoff = 1

                print(f"[API] {batch_count}  ({sent_total}/{stroke_count})", flush=True)

        except Exception:
            api_err_count += 1
            backoff = min(backoff * 2, 60)  # 1s → 2s → 4s → ... → 60s

# =========================
# システム監視（10分ごと）
# =========================
def sys_monitor_loop():
    while not stop_event.is_set():
        time.sleep(SYS_MONITOR_INTERVAL)
        try:
            # CPU温度
            temp = "?"
            try:
                with open("/sys/class/thermal/thermal_zone0/temp") as f:
                    temp = f"{int(f.read().strip()) / 1000:.1f}"
            except Exception:
                pass

            # メモリ
            mem_info = "?"
            try:
                with open("/proc/meminfo") as f:
                    lines = f.readlines()
                total = int(lines[0].split()[1]) // 1024
                avail = int(lines[2].split()[1]) // 1024
                mem_info = f"{total - avail}/{total}MB"
            except Exception:
                pass

            # DBサイズ
            db_size = "?"
            try:
                size = os.path.getsize(DB_PATH)
                db_size = f"{size / 1024 / 1024:.1f}MB"
            except Exception:
                pass

            print(f"[SYS] temp={temp}°C  mem={mem_info}  db={db_size}  count={stroke_count}", flush=True)
        except Exception:
            pass

# =========================
# DB掃除（1時間ごと）
# =========================
def db_cleanup_loop():
    while not stop_event.is_set():
        time.sleep(DB_CLEANUP_INTERVAL)
        try:
            with db_lock:
                cur = db_conn.cursor()
                cur.execute("DELETE FROM events WHERE sent=1")
                deleted = cur.rowcount
                if deleted > 0:
                    db_conn.commit()
                    print(f"[DB] cleaned {deleted} sent records", flush=True)
        except Exception:
            pass

# =========================
# Main
# =========================
def main():
    global chip, stroke_count

    if CLEAN_SHUTDOWN_FLAG.exists():
        stroke_count = 0
        CLEAN_SHUTDOWN_FLAG.unlink()
        print("[INIT] clean restart -> count=0", flush=True)
    else:
        stroke_count = load_stroke_count()
        print(f"[INIT] power recovery -> count={stroke_count}", flush=True)

    chip = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_input(chip, GPIO_PIN, lgpio.SET_PULL_UP)

    threading.Thread(target=gpio_poll_loop, daemon=True).start()
    threading.Thread(target=db_flush_loop, daemon=True).start()
    threading.Thread(target=api_sender_loop, daemon=True).start()
    threading.Thread(target=sys_monitor_loop, daemon=True).start()
    threading.Thread(target=db_cleanup_loop, daemon=True).start()

    print(f"Press Counter Started [{RASPI_NO}] (polling {POLL_INTERVAL_S*1000:.0f}ms) count={stroke_count}", flush=True)

    def shutdown(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        stop_event.set()
        CLEAN_SHUTDOWN_FLAG.write_text("1")
        lgpio.gpiochip_close(chip)
        print("[SHUTDOWN] clean shutdown flag written", flush=True)

if __name__ == "__main__":
    main()
