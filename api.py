from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional
import sqlite3
from pathlib import Path

app = FastAPI()

# =========================
# SQLite 永続化
# =========================
DB_PATH = Path(__file__).with_name("events.db")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL,
            raspi_no TEXT DEFAULT 'unknown'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts_ms ON events(ts_ms)")
    conn.commit()
    return conn


# 起動時にテーブル作成
get_db().close()


# =========================
# Pydantic Models
# =========================
class EventItem(BaseModel):
    ts_ms: int

    class Config:
        extra = "ignore"


class BatchRequest(BaseModel):
    raspi_no: Optional[str] = "unknown"
    events: List[EventItem]

    class Config:
        extra = "ignore"


# =========================
# 受信API
# =========================
@app.post("/api/iot/events")
async def receive_events(batch: BatchRequest):
    conn = get_db()
    try:
        conn.executemany(
            "INSERT INTO events(ts_ms, raspi_no) VALUES(?, ?)",
            [(e.ts_ms, batch.raspi_no) for e in batch.events],
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {"status": "ok", "total": count}
    finally:
        conn.close()


# =========================
# イベント取得API
# =========================
@app.get("/api/iot/events")
async def query_events(start_ms: int, end_ms: int):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT ts_ms FROM events WHERE ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms",
            (start_ms, end_ms),
        ).fetchall()
        return {"events": [r[0] for r in rows]}
    finally:
        conn.close()


# =========================
# ダッシュボード
# =========================
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>IoT Monitor</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: Arial, sans-serif; margin: 30px; background: #f5f5f5; color: #333; }
  h1 { margin-bottom: 10px; }
  .stats { margin: 10px 0 20px; font-size: 14px; color: #666; }
  .legend {
    display: flex; gap: 18px; margin-bottom: 24px; font-size: 13px;
  }
  .legend-item { display: flex; align-items: center; gap: 5px; }
  .legend-color {
    width: 16px; height: 16px; border-radius: 3px; flex-shrink: 0;
  }

  .bar-section { margin-bottom: 28px; }
  .bar-label { font-weight: bold; margin-bottom: 4px; font-size: 14px; }
  .bar-wrap {
    position: relative;
    border: 1px solid #ccc; border-radius: 4px;
    overflow: hidden; background: #eee;
  }
  .bar {
    display: flex; width: 100%; height: 38px;
  }
  .bar-seg { height: 100%; min-width: 0; }
  .bar-seg:hover { opacity: 0.8; }

  .axis {
    position: relative; width: 100%; height: 22px;
    font-size: 11px; color: #888; margin-top: 2px;
  }
  .tick {
    position: absolute; transform: translateX(-50%);
    white-space: nowrap;
  }
  .tick:first-child { transform: translateX(0); }
  .tick:last-child { transform: translateX(-100%); }

  .summary-row {
    display: flex; gap: 20px; margin-bottom: 24px; flex-wrap: wrap;
  }
  .summary-card {
    background: #fff; border-radius: 6px; padding: 14px 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1); min-width: 140px;
  }
  .summary-card .num { font-size: 28px; font-weight: bold; }
  .summary-card .label { font-size: 12px; color: #888; }

  h2 { margin: 20px 0 8px; font-size: 16px; }
  table { border-collapse: collapse; font-size: 13px; }
  table th, table td {
    border: 1px solid #ddd; padding: 4px 10px; text-align: left;
  }
  table th { background: #f0f0f0; }
</style>
</head>
<body>

<h1>IoT Monitor - Press Machine</h1>

<div class="legend">
  <div class="legend-item">
    <div class="legend-color" style="background:#4CAF50"></div><span>稼働中</span>
  </div>
  <div class="legend-item">
    <div class="legend-color" style="background:#FFC107"></div><span>チョコ停 (&ge;30s)</span>
  </div>
  <div class="legend-item">
    <div class="legend-color" style="background:#F44336"></div><span>ドカ停 (&ge;5min)</span>
  </div>
  <div class="legend-item">
    <div class="legend-color" style="background:#BDBDBD"></div><span>データなし</span>
  </div>
</div>

<div class="summary-row">
  <div class="summary-card"><div class="num" id="cnt-total">-</div><div class="label">Today Events</div></div>
  <div class="summary-card"><div class="num" id="cnt-uptime">-</div><div class="label">稼働率 (Today)</div></div>
  <div class="summary-card"><div class="num" id="cnt-status">-</div><div class="label">現在の状態</div></div>
</div>

<div class="bar-section">
  <div class="bar-label">1 Day View (Today)</div>
  <div class="bar-wrap"><div class="bar" id="bar-day"></div></div>
  <div class="axis" id="axis-day"></div>
</div>

<div class="bar-section">
  <div class="bar-label">1 Hour View (Recent 60min)</div>
  <div class="bar-wrap"><div class="bar" id="bar-hour"></div></div>
  <div class="axis" id="axis-hour"></div>
</div>

<h2>Recent Events (Last 20)</h2>
<table id="tbl">
  <tr><th>#</th><th>ts_ms</th><th>Time</th></tr>
</table>

<script>
const CHOKO = 30000;
const DOKA  = 300000;
const C = {green:'#4CAF50', yellow:'#FFC107', red:'#F44336', gray:'#BDBDBD'};
const L = {green:'稼働中', yellow:'チョコ停', red:'ドカ停', gray:'データなし'};

function classify(ms) {
  if (ms < CHOKO) return 'green';
  if (ms < DOKA) return 'yellow';
  return 'red';
}

function buildSegments(ev, ws, we) {
  if (!ev.length) return [{s:ws, e:we, c:'gray'}];
  const segs = [];
  let cs = ws, cc = classify(ev[0] - ws);

  for (let i = 0; i < ev.length - 1; i++) {
    const nc = classify(ev[i+1] - ev[i]);
    if (nc !== cc) {
      segs.push({s:cs, e:ev[i], c:cc});
      cs = ev[i]; cc = nc;
    }
  }
  const last = ev[ev.length-1];
  const tc = classify(we - last);
  if (tc === cc) {
    segs.push({s:cs, e:we, c:cc});
  } else {
    segs.push({s:cs, e:last, c:cc});
    segs.push({s:last, e:we, c:tc});
  }
  return segs;
}

function renderBar(id, segs, ws, we) {
  const bar = document.getElementById(id);
  bar.innerHTML = '';
  const total = we - ws;
  if (total <= 0) return;
  segs.forEach(seg => {
    const pct = (seg.e - seg.s) / total * 100;
    if (pct <= 0) return;
    const d = document.createElement('div');
    d.className = 'bar-seg';
    d.style.flexBasis = pct + '%';
    d.style.background = C[seg.c];
    const t0 = new Date(seg.s).toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    const t1 = new Date(seg.e).toLocaleTimeString('ja-JP',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    d.title = t0 + ' - ' + t1 + '  ' + L[seg.c];
    bar.appendChild(d);
  });
}

function renderAxis(id, ws, we, count) {
  const ax = document.getElementById(id);
  ax.innerHTML = '';
  for (let i = 0; i <= count; i++) {
    const t = ws + (we - ws) * i / count;
    const d = new Date(t);
    const sp = document.createElement('span');
    sp.className = 'tick';
    sp.style.left = (i / count * 100) + '%';
    sp.textContent = d.getHours().toString().padStart(2,'0')
                   + ':' + d.getMinutes().toString().padStart(2,'0');
    ax.appendChild(sp);
  }
}

function calcUptime(segs, ws, we) {
  const total = we - ws;
  if (total <= 0) return 0;
  let green = 0;
  segs.forEach(s => { if (s.c === 'green') green += (s.e - s.s); });
  return Math.round(green / total * 100);
}

async function refresh() {
  const now = Date.now();

  const today = new Date();
  today.setHours(0,0,0,0);
  const dayStart = today.getTime();
  const dayEnd = dayStart + 86400000;

  const hourStart = now - 3600000;
  const hourEnd = now;

  try {
    const [rDay, rHour] = await Promise.all([
      fetch('/api/iot/events?start_ms=' + dayStart + '&end_ms=' + dayEnd),
      fetch('/api/iot/events?start_ms=' + hourStart + '&end_ms=' + hourEnd)
    ]);
    const dDay = await rDay.json();
    const dHour = await rHour.json();

    const effEnd = Math.min(dayEnd, now);
    const segsDay = buildSegments(dDay.events, dayStart, effEnd);
    renderBar('bar-day', segsDay, dayStart, dayEnd);
    renderAxis('axis-day', dayStart, dayEnd, 24);

    const segsHour = buildSegments(dHour.events, hourStart, hourEnd);
    renderBar('bar-hour', segsHour, hourStart, hourEnd);
    renderAxis('axis-hour', hourStart, hourEnd, 12);

    // summary
    document.getElementById('cnt-total').textContent = dDay.events.length;
    document.getElementById('cnt-uptime').textContent = calcUptime(segsDay, dayStart, effEnd) + '%';

    const lastSeg = segsDay[segsDay.length - 1];
    const statusEl = document.getElementById('cnt-status');
    statusEl.textContent = L[lastSeg.c];
    statusEl.style.color = C[lastSeg.c];

    // recent events table
    const tbl = document.getElementById('tbl');
    while (tbl.rows.length > 1) tbl.deleteRow(1);
    const recent = dDay.events.slice(-20).reverse();
    recent.forEach((ts, i) => {
      const row = tbl.insertRow();
      row.insertCell().textContent = i + 1;
      row.insertCell().textContent = ts;
      row.insertCell().textContent = new Date(ts).toLocaleString('ja-JP');
    });
  } catch(err) {
    console.error('refresh error:', err);
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
