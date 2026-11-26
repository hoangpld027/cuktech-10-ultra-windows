"""Local config and SQLite session/telemetry storage for Cuktech Monitor."""
import csv
import io
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "history.db"

DEFAULT_CONFIG: Dict[str, Any] = {
    "mac": "",
    "token": "",
    "ble_key": "",
    "device_name": "CUKTECH 10 Ultra",
    "device_model": "njcuk.fitting.ad1204",
    "server": "cn",
    "auto_connect": True,
    "language": "vi",
}


def load_config() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        return merged
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    CONFIG_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    return merged


class HistoryStore:
    """Thread-safe SQLite store for charging sessions and power samples."""

    def __init__(self, db_path: Path = DB_PATH):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        port_id INTEGER NOT NULL,
                        port_name TEXT NOT NULL,
                        start_time REAL NOT NULL,
                        end_time REAL,
                        energy_wh REAL DEFAULT 0.0,
                        peak_power REAL DEFAULT 0.0,
                        protocol TEXT DEFAULT ''
                    );
                    CREATE TABLE IF NOT EXISTS samples (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL NOT NULL,
                        total_w REAL DEFAULT 0.0,
                        c1_v REAL DEFAULT 0.0, c1_a REAL DEFAULT 0.0, c1_w REAL DEFAULT 0.0,
                        c2_v REAL DEFAULT 0.0, c2_a REAL DEFAULT 0.0, c2_w REAL DEFAULT 0.0,
                        c3_v REAL DEFAULT 0.0, c3_a REAL DEFAULT 0.0, c3_w REAL DEFAULT 0.0,
                        a_v REAL DEFAULT 0.0,  a_a REAL DEFAULT 0.0,  a_w REAL DEFAULT 0.0
                    );
                    CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
                    CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_time);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def start_session(self, port_id: int, port_name: str, protocol: str = "") -> int:
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "INSERT INTO sessions (port_id, port_name, start_time, protocol) VALUES (?, ?, ?, ?)",
                    (port_id, port_name, now, protocol),
                )
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def update_session(self, session_id: int, energy_wh: float, peak_power: float, protocol: str = "") -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE sessions SET energy_wh = ?, peak_power = ?, protocol = COALESCE(NULLIF(?, ''), protocol) WHERE id = ?",
                    (round(energy_wh, 4), round(peak_power, 2), protocol, session_id),
                )
                conn.commit()
            finally:
                conn.close()

    def end_session(self, session_id: int, energy_wh: float, peak_power: float) -> None:
        now = time.time()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE sessions SET end_time = ?, energy_wh = ?, peak_power = ? WHERE id = ?",
                    (now, round(energy_wh, 4), round(peak_power, 2), session_id),
                )
                conn.commit()
            finally:
                conn.close()

    def record_sample(self, ports: Dict[int, Dict[str, Any]]) -> None:
        now = time.time()
        total_w = sum(float(ports.get(i, {}).get("power", 0.0)) for i in (1, 2, 3, 4))
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO samples (
                        ts, total_w,
                        c1_v, c1_a, c1_w,
                        c2_v, c2_a, c2_w,
                        c3_v, c3_a, c3_w,
                        a_v,  a_a,  a_w
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        round(total_w, 2),
                        ports.get(1, {}).get("voltage", 0.0),
                        ports.get(1, {}).get("current", 0.0),
                        ports.get(1, {}).get("power", 0.0),
                        ports.get(2, {}).get("voltage", 0.0),
                        ports.get(2, {}).get("current", 0.0),
                        ports.get(2, {}).get("power", 0.0),
                        ports.get(3, {}).get("voltage", 0.0),
                        ports.get(3, {}).get("current", 0.0),
                        ports.get(3, {}).get("power", 0.0),
                        ports.get(4, {}).get("voltage", 0.0),
                        ports.get(4, {}).get("current", 0.0),
                        ports.get(4, {}).get("power", 0.0),
                    ),
                )
                # Keep last 7 days of samples to keep DB compact
                cutoff = now - 7 * 86400
                conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
                conn.commit()
            finally:
                conn.close()

    def get_recent_sessions(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM sessions ORDER BY start_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_today_energy_wh(self) -> float:
        import datetime

        today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(energy_wh), 0.0) AS total FROM sessions WHERE start_time >= ?",
                    (today_start,),
                ).fetchone()
                return round(float(row["total"] or 0.0), 2)
            finally:
                conn.close()

    def export_csv(self) -> str:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT ts, total_w, c1_v, c1_a, c1_w, c2_v, c2_a, c2_w, c3_v, c3_a, c3_w, a_v, a_a, a_w FROM samples ORDER BY ts DESC LIMIT 5000"
                ).fetchall()
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(["timestamp", "total_w", "c1_v", "c1_a", "c1_w", "c2_v", "c2_a", "c2_w", "c3_v", "c3_a", "c3_w", "a_v", "a_a", "a_w"])
                for r in rows:
                    writer.writerow(list(r))
                return buf.getvalue()
            finally:
                conn.close()
