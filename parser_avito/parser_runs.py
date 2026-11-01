"""Журнал циклов парсера и безопасная запись ошибок без секретов."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import psycopg2


MAX_ERROR_LENGTH = 1000
_RUN_STATUSES = {"success", "partial", "failed"}


def new_run_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}"


def sanitize_error(error: BaseException | str | None, limit: int = MAX_ERROR_LENGTH) -> str | None:
    if error is None:
        return None
    text = str(error)
    text = re.sub(
        r"postgres(?:ql)?://[^:\s/@]+:[^@\s]+@",
        "postgresql://***:***@",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "***", text)
    text = re.sub(
        r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret|authorization)"
        r"(\s*[=:]\s*)([^\s,;]+)",
        lambda match: f"{match.group(1)}{match.group(2)}***",
        text,
    )
    return text[:limit]


def start_parser_run(
    pg_config: dict,
    run_id: str,
    urls_count: int,
    parser_version: str,
) -> None:
    conn = psycopg2.connect(**pg_config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO avito_original.parser_runs
                    (run_id, started_at, status, urls_count, found_count,
                     written_count, parser_version)
                VALUES (%s, now(), 'running', %s, 0, 0, %s)
                """,
                (run_id, urls_count, parser_version),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_parser_run(
    pg_config: dict,
    run_id: str,
    status: str,
    found_count: int,
    written_count: int,
    error: BaseException | str | None = None,
) -> None:
    if status not in _RUN_STATUSES:
        raise ValueError(f"unsupported parser run status: {status}")
    conn = psycopg2.connect(**pg_config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE avito_original.parser_runs
                SET finished_at = now(), status = %s, found_count = %s,
                    written_count = %s, error = %s
                WHERE run_id = %s AND status = 'running'
                """,
                (status, found_count, written_count, sanitize_error(error), run_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"parser run {run_id} is not running")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
