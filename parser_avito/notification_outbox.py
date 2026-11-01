"""Durable Telegram notification outbox.

Scanning enqueues events; this module atomically claims jobs and reserves the
sliding-hour budgets before any Telegram network call. Telegram delivery is
at-least-once: a lost response can still cause a duplicate after claim recovery.
"""
import json
import os
import time
from datetime import date, datetime
from decimal import Decimal

import psycopg2
from psycopg2.extras import Json

from tg_alerts import deliver_lot, lots_muted

NEW_PER_HOUR = int(os.environ.get("NEW_LOTS_PER_HOUR", "20"))
MAX_LOT_MSGS_PER_HOUR = int(os.environ.get("ALERT_LOT_MSG_LIMIT", "10"))
SEND_BATCH = int(os.environ.get("OUTBOX_SEND_BATCH", "5"))
CLAIM_TIMEOUT = int(os.environ.get("OUTBOX_CLAIM_TIMEOUT", "300"))
RETRY_BASE = int(os.environ.get("OUTBOX_RETRY_BASE", "30"))
RETRY_MAX = int(os.environ.get("OUTBOX_RETRY_MAX", "1800"))

# Serializes the short claim/budget transaction across all possible senders.
_BUDGET_LOCK_KEY = 721984633


def _connect(db: dict):
    return psycopg2.connect(**db)


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _payload(value: dict):
    return Json(value, dumps=lambda v: json.dumps(
        v, ensure_ascii=False, default=_json_default,
    ))


def reason_key(item: dict, kind: str) -> str | None:
    if kind == "new":
        return "first_seen"
    price = Decimal(str(item["price"])).quantize(Decimal("0.01"))
    if not item.get("alerted_at"):
        return "initial"
    previous = item.get("alerted_price")
    # Повторный good допустим только при реальном снижении цены минимум на 3%.
    if previous is None or price > Decimal(str(previous)) * Decimal("0.97"):
        return None
    return f"price:{format(price.normalize(), 'f')}"


def enqueue_lots(db: dict, items: list[dict], kind: str) -> int:
    """Insert new events and refresh payloads of jobs still waiting to send."""
    if not items:
        return 0
    changed = 0
    with _connect(db) as conn, conn.cursor() as cur:
        for item in items:
            event_key = reason_key(item, kind)
            if event_key is None:
                continue
            cur.execute(
                """
                INSERT INTO avito_original.notification_outbox
                    (avito_id, kind, reason_key, payload, status, next_retry_at)
                VALUES (%s, %s, %s, %s, 'pending', now())
                ON CONFLICT (avito_id, kind, reason_key) DO UPDATE
                SET payload = EXCLUDED.payload
                WHERE notification_outbox.status = 'pending'
                """,
                (item["avito_id"], kind, event_key, _payload(item)),
            )
            changed += max(cur.rowcount, 0)
    return changed


def enqueue_lot(db: dict, item: dict, kind: str) -> int:
    return enqueue_lots(db, [item], kind)


def _recover_stale(cur) -> int:
    cur.execute(
        """
        UPDATE avito_original.notification_outbox
        SET status = 'failed',
            next_retry_at = now() + make_interval(secs => %s),
            last_error = %s
        WHERE status = 'sending'
          AND claimed_at < now() - make_interval(secs => %s)
        """,
        (RETRY_BASE, "claim timeout; Telegram result may be unknown", CLAIM_TIMEOUT),
    )
    return cur.rowcount


def recover_stale(db: dict) -> int:
    with _connect(db) as conn, conn.cursor() as cur:
        return _recover_stale(cur)


def claim_next(db: dict) -> dict | None:
    """Claim one ready job and atomically reserve both sliding-hour budgets."""
    with _connect(db) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_BUDGET_LOCK_KEY,))
        _recover_stale(cur)
        cur.execute(
            """
            SELECT count(*) FROM avito_original.notification_outbox
            WHERE status IN ('sending', 'sent')
              AND coalesce(sent_at, claimed_at) > now() - interval '1 hour'
            """
        )
        if cur.fetchone()[0] >= MAX_LOT_MSGS_PER_HOUR:
            return None
        cur.execute(
            """
            SELECT count(*) FROM avito_original.notification_outbox
            WHERE kind = 'new' AND status IN ('sending', 'sent')
              AND coalesce(sent_at, claimed_at) > now() - interval '1 hour'
            """
        )
        new_used = cur.fetchone()[0]
        cur.execute(
            """
            SELECT id, avito_id, kind, reason_key, payload
            FROM avito_original.notification_outbox
            WHERE status IN ('pending', 'failed')
              AND next_retry_at <= now()
              AND (%s < %s OR kind <> 'new')
            ORDER BY created_at, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (new_used, NEW_PER_HOUR),
        )
        row = cur.fetchone()
        if not row:
            return None
        cur.execute(
            """
            UPDATE avito_original.notification_outbox
            SET status = 'sending', claimed_at = now(),
                attempt_count = attempt_count + 1, last_error = NULL
            WHERE id = %s
            RETURNING attempt_count
            """,
            (row[0],),
        )
        attempt_count = cur.fetchone()[0]
        return {
            "id": row[0], "avito_id": row[1], "kind": row[2],
            "reason_key": row[3], "payload": row[4],
            "attempt_count": attempt_count,
        }


def finish_success(db: dict, job: dict, message_id: int | None) -> None:
    """Persist Telegram and legacy detail.ads marks in one transaction."""
    with _connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE avito_original.notification_outbox
            SET status = 'sent', sent_at = now(), telegram_message_id = %s,
                last_error = NULL, next_retry_at = NULL
            WHERE id = %s AND status = 'sending'
            """,
            (message_id, job["id"]),
        )
        if job["kind"] == "new":
            cur.execute(
                "UPDATE detail.ads SET new_notified_at = now() WHERE avito_id = %s",
                (job["avito_id"],),
            )
        else:
            cur.execute(
                """
                UPDATE detail.ads
                SET alerted_at = now(), alerted_price = %s
                WHERE avito_id = %s
                """,
                (job["payload"].get("price"), job["avito_id"]),
            )


def finish_failure(db: dict, job: dict, result: dict) -> None:
    backoff = min(RETRY_MAX, RETRY_BASE * (2 ** max(0, job["attempt_count"] - 1)))
    retry_after = result.get("retry_after")
    delay = max(backoff, int(retry_after or 0))
    error = str(result.get("error") or "Telegram delivery failed")[:2000]
    with _connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE avito_original.notification_outbox
            SET status = 'failed', last_error = %s,
                next_retry_at = now() + make_interval(secs => %s)
            WHERE id = %s AND status = 'sending'
            """,
            (error, delay, job["id"]),
        )
    print(
        f"outbox: job {job['id']} retry in {delay}s: {error}", flush=True,
    )


def send_pending(db: dict, limit: int | None = None) -> int:
    """Deliver a small batch; errors stay in outbox and never stop the parser."""
    if lots_muted():
        return 0
    sent = 0
    for _ in range(SEND_BATCH if limit is None else limit):
        job = claim_next(db)
        if not job:
            break
        try:
            result = deliver_lot(job["payload"], job["kind"])
        except Exception as exc:  # defensive: delivery must not break enricher
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if result.get("ok"):
            finish_success(db, job, result.get("message_id"))
            sent += 1
            print(
                f"outbox: sent job {job['id']} {job['kind']}:{job['avito_id']}",
                flush=True,
            )
            time.sleep(0.7)
        else:
            finish_failure(db, job, result)
    return sent
