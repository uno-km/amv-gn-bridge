# Copyright (c) 2026 Eunho Kim / AMEVA Foundation. All rights reserved.
# SPDX-License-Identifier: Apache-2.0 OR Commercial-License
"""
Embedded SQLite Telemetry Database for amv-gn-bridge.
Identical schema and API contract to amv-cp-bridge's TelemetryDB, plus per-account breakdown.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List

DB_DIR = Path.home() / ".amv_gn_bridge"
DB_PATH = DB_DIR / "telemetry.db"


class TelemetryDB:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        try:
            self._init_db()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                f"사용 기록 DB를 열 수 없습니다: {self.db_path} ({exc}). "
                "프로그램을 종료하고 이 파일을 다른 곳으로 옮긴 뒤 다시 실행하십시오. 새 DB가 만들어집니다."
            ) from exc

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    _NEW_COLS = {
        "source":            "TEXT DEFAULT 'api'",
        "task_id":           "TEXT",
        "client_app":        "TEXT",
        "client_version":    "TEXT",
        "queue_wait_ms":     "REAL",
        "prompt_tokens_est": "INTEGER",
        "ip_addr":           "TEXT",
        "full_prompt":       "TEXT",
        "full_response":     "TEXT",
    }

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS usage_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    prompt_preview TEXT,
                    duration_ms REAL,
                    tokens_est INTEGER,
                    bytes_len INTEGER,
                    worker_port INTEGER,
                    account_label TEXT,
                    model TEXT,
                    source TEXT DEFAULT 'api',
                    success INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON usage_records(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON usage_records(source)")
            existing = {row[1] for row in conn.execute("PRAGMA table_info(usage_records)").fetchall()}
            for col, typedef in self._NEW_COLS.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE usage_records ADD COLUMN {col} {typedef}")
            conn.commit()

    def record_query(
        self,
        prompt: str,
        duration_ms: float,
        bytes_len: int,
        worker_port: int,
        account_label: str,
        model: str = "gemini-web",
        source: str = "api",
        success: bool = True,
        task_id: str = "",
        client_app: str = "",
        client_version: str = "",
        queue_wait_ms: float = 0.0,
        prompt_bytes: int = 0,
        ip_addr: str = "",
        full_response: str = "",
    ) -> int:
        tokens_est = max(1, int(bytes_len / 4.2)) if bytes_len > 0 else 0
        prompt_tokens = max(1, int(prompt_bytes / 4.2)) if prompt_bytes > 0 else 0
        prompt_preview = (prompt[:300] + "...") if len(prompt) > 300 else prompt

        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO usage_records (
                    prompt_preview, duration_ms, tokens_est, bytes_len,
                    worker_port, account_label, model, source, success,
                    task_id, client_app, client_version,
                    queue_wait_ms, prompt_tokens_est, ip_addr,
                    full_prompt, full_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                prompt_preview, round(duration_ms, 2), tokens_est, bytes_len,
                worker_port, account_label, model, source, 1 if success else 0,
                task_id or "", client_app or "", client_version or "",
                round(queue_wait_ms, 2), prompt_tokens, ip_addr or "",
                prompt or "", full_response or "",
            ))
            conn.commit()
            return int(cursor.lastrowid or 0)

    def get_summary(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            total_row = conn.execute("""
                SELECT
                    COUNT(*) as total_queries,
                    COALESCE(SUM(tokens_est), 0) as total_tokens,
                    COALESCE(SUM(bytes_len), 0) as total_bytes,
                    COALESCE(AVG(duration_ms), 0) as avg_duration_ms,
                    COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) as success_queries
                FROM usage_records
            """).fetchone()

            today_row = conn.execute("""
                SELECT
                    COUNT(*) as queries,
                    COALESCE(SUM(tokens_est), 0) as tokens,
                    COALESCE(AVG(duration_ms), 0) as avg_duration_ms
                FROM usage_records
                WHERE DATE(timestamp, 'localtime') = DATE('now', 'localtime')
            """).fetchone()

            week_row = conn.execute("""
                SELECT COUNT(*) as queries, COALESCE(SUM(tokens_est), 0) as tokens
                FROM usage_records
                WHERE strftime('%Y-%W', timestamp, 'localtime') = strftime('%Y-%W', 'now', 'localtime')
            """).fetchone()

            month_row = conn.execute("""
                SELECT COUNT(*) as queries, COALESCE(SUM(tokens_est), 0) as tokens
                FROM usage_records
                WHERE strftime('%Y-%m', timestamp, 'localtime') = strftime('%Y-%m', 'now', 'localtime')
            """).fetchone()

            year_row = conn.execute("""
                SELECT COUNT(*) as queries, COALESCE(SUM(tokens_est), 0) as tokens
                FROM usage_records
                WHERE strftime('%Y', timestamp, 'localtime') = strftime('%Y', 'now', 'localtime')
            """).fetchone()

            recent_rows = conn.execute("""
                SELECT
                    id,
                    datetime(timestamp, 'localtime') as time_str,
                    prompt_preview,
                    duration_ms,
                    tokens_est,
                    bytes_len,
                    worker_port,
                    account_label,
                    model,
                    COALESCE(source, 'api') as source,
                    COALESCE(task_id, '') as task_id,
                    COALESCE(client_app, '') as client_app,
                    COALESCE(client_version, '') as client_version,
                    COALESCE(queue_wait_ms, 0) as queue_wait_ms,
                    COALESCE(prompt_tokens_est, 0) as prompt_tokens_est,
                    COALESCE(ip_addr, '') as ip_addr,
                    success
                FROM usage_records
                ORDER BY id DESC
                LIMIT 20
            """).fetchall()

            source_rows = conn.execute("""
                SELECT
                    COALESCE(source, 'api') as source,
                    COUNT(*) as queries,
                    COALESCE(SUM(tokens_est), 0) as tokens
                FROM usage_records
                GROUP BY COALESCE(source, 'api')
            """).fetchall()

            account_rows = conn.execute("""
                SELECT
                    worker_port,
                    COALESCE(account_label, '') as account_label,
                    COUNT(*) as queries,
                    COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) as success_queries,
                    COALESCE(SUM(CASE WHEN DATE(timestamp, 'localtime') = DATE('now', 'localtime') THEN 1 ELSE 0 END), 0) as today_queries
                FROM usage_records
                GROUP BY worker_port
            """).fetchall()

            total_q = total_row["total_queries"]
            succ_q = total_row["success_queries"]
            success_rate = round((succ_q / total_q * 100), 1) if total_q > 0 else 100.0

            by_source = {row["source"]: {"queries": row["queries"], "tokens": row["tokens"]} for row in source_rows}
            by_account = {
                str(row["worker_port"]): {
                    "label": row["account_label"],
                    "queries": row["queries"],
                    "success_queries": row["success_queries"],
                    "today_queries": row["today_queries"],
                }
                for row in account_rows
            }

            return {
                "total": {
                    "queries": total_q,
                    "tokens": total_row["total_tokens"],
                    "bytes": total_row["total_bytes"],
                    "avg_duration_ms": round(total_row["avg_duration_ms"], 1),
                    "success_rate": success_rate,
                },
                "by_source": by_source,
                "by_account": by_account,
                "today": {
                    "queries": today_row["queries"],
                    "tokens": today_row["tokens"],
                    "avg_duration_ms": round(today_row["avg_duration_ms"], 1),
                },
                "week": {"queries": week_row["queries"], "tokens": week_row["tokens"]},
                "month": {"queries": month_row["queries"], "tokens": month_row["tokens"]},
                "year": {"queries": year_row["queries"], "tokens": year_row["tokens"]},
                "recent": [dict(r) for r in recent_rows],
            }

    def get_record_full(self, record_id: int) -> Dict[str, Any]:
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT id, datetime(timestamp, 'localtime') as time_str,
                       full_prompt, full_response,
                       source, client_app, client_version,
                       duration_ms, tokens_est, queue_wait_ms,
                       worker_port, account_label, ip_addr, success
                FROM usage_records WHERE id = ?
            """, (record_id,)).fetchone()
            return dict(row) if row else {}

    def get_daily_chart(self, days: int = 14) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(f"""
                SELECT
                    DATE(timestamp, 'localtime') as day,
                    COUNT(*) as queries,
                    COALESCE(SUM(tokens_est), 0) as tokens
                FROM usage_records
                WHERE timestamp >= datetime('now', '-{int(days)} days')
                GROUP BY DATE(timestamp, 'localtime')
                ORDER BY day ASC
            """).fetchall()
            return [dict(r) for r in rows]

    def reset_all(self) -> bool:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM usage_records")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='usage_records'")
            conn.commit()
            conn.execute("VACUUM")
        return True


telemetry_db = TelemetryDB()
