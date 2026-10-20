"""Алерты в enricher-цикле: выгодные лоты (батч) + молчание парсера.

Вызывается из enricher.py: alert_tick(db, is_time) каждый проход цикла.
"""
import os
import time

import psycopg2

from tg_alerts import send_lots_batch, send_failure, send_recovery

SILENCE_THRESHOLD_MIN = int(os.environ.get("SILENCE_THRESHOLD_MIN", "90"))
ALERT_SCAN_INTERVAL = int(os.environ.get("ALERT_SCAN_INTERVAL", "300"))  # 5 мин
DISCOUNT_PCT = int(os.environ.get("ALERT_DISCOUNT_PCT", "20"))

# состояние между тиками (в процессе enricher)
_state = {
    "last_scan": 0.0,
    "last_silence_check": 0.0,
    "silence_alerted": False,   # алерт «парсер молчит» уже отправлен
    "last_parsed_at": None,     # для детекции восстановления
}


def _db_conn(db: dict):
    return psycopg2.connect(**db)


def _scan_good_lots(db: dict) -> list[dict] | None:
    """Активные лоты дешевле медианы модели на DISCOUNT_PCT%+, не алерченные."""
    try:
        with _db_conn(db) as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH med AS (
                    SELECT d2.category, percentile_cont(0.5)
                           WITHIN GROUP (ORDER BY d2.price_rub) AS med
                    FROM detail.ads d2
                    WHERE d2.state = 'активно' AND d2.price_rub > 0
                    GROUP BY d2.category
                ),
                seen AS (
                    SELECT avito_id, max(parsed_at) AS last_seen
                    FROM midraw.avito_ads GROUP BY avito_id
                )
                SELECT d.avito_id, d.model, d.price_rub, m.url,
                       m.location, med.med
                FROM detail.ads d
                JOIN midraw.avito_ads m ON m.avito_id = d.avito_id
                JOIN med ON med.category = d.category
                JOIN seen ON seen.avito_id = d.avito_id
                WHERE d.state = 'активно' AND d.price_rub > 0
                  AND d.alerted_at IS NULL
                  AND seen.last_seen > now() - interval '48 hours'
                  AND d.price_rub < med.med * %(k)s
                ORDER BY (d.price_rub / med.med) ASC
                LIMIT 5
                """,
                {"k": 1 - DISCOUNT_PCT / 100.0},
            )
            rows = cur.fetchall()
    except Exception as exc:
        print(f"alerts: scan failed: {exc}", flush=True)
        return None
    return [
        {
            "avito_id": r[0],
            "model": (r[1] or "?")[:40],
            "price": float(r[2]),
            "url": r[3],
            "location": r[4],
            "median": float(r[5]),
            "benefit_pct": int(round((float(r[2]) / float(r[5]) - 1) * 100)),
        }
        for r in rows
    ]


def _mark_alerted(db: dict, ids: list[int]) -> None:
    with _db_conn(db) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE detail.ads SET alerted_at = now() WHERE avito_id = ANY(%s)",
            (ids,),
        )
        conn.commit()


def _parser_silence_check(db: dict) -> None:
    """Парсер молчит > порога → один алерт; восстановление → отдельный алерт."""
    global _state
    with _db_conn(db) as conn, conn.cursor() as cur:
        cur.execute("SELECT max(parsed_at) FROM avito_original.ads")
        row = cur.fetchone()
    last = row[0] if row else None
    if last is None:
        return
    silence_min = (time.time() - last.timestamp()) / 60.0
    if silence_min > SILENCE_THRESHOLD_MIN and not _state["silence_alerted"]:
        send_failure(
            f"Парсер молчит {int(silence_min)} минут\n"
            f"последняя запись: {last:%d.%m %H:%M}.\n"
            f"Смотри: docker logs airflow-avito-project-avito-parser-service-1"
        )
        _state["silence_alerted"] = True
    elif silence_min <= SILENCE_THRESHOLD_MIN and _state["silence_alerted"]:
        send_recovery(f"Парсер снова пишет (последняя запись {last:%d.%m %H:%M})")
        _state["silence_alerted"] = False


def _mute_all_on(db: dict) -> bool:
    """Глобальное отключение ВСЕХ уведомлений (bot_settings.mute_all)."""
    try:
        with _db_conn(db) as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM avito_original.bot_settings WHERE key = 'mute_all'")
            row = cur.fetchone()
            return bool(row and row[0] == "1")
    except Exception:
        return False


def _collect_drop_check(db: dict) -> None:
    """Сбор упал на 80% против предыдущих суток → алерт раз в сутки."""
    try:
        with _db_conn(db) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                  (SELECT count(*) FROM avito_original.ads
                    WHERE parsed_at > now() - interval '24 hours'),
                  (SELECT count(*) FROM avito_original.ads
                    WHERE parsed_at > now() - interval '48 hours'
                      AND parsed_at <= now() - interval '24 hours')
            """)
            cur24, prev24 = cur.fetchone()
    except Exception as exc:
        print(f"alerts: drop check failed: {exc}", flush=True)
        return
    if prev24 >= 100 and cur24 < prev24 * 0.2:
        if not _state.get("drop_alerted"):
            send_failure(
                f"Аномалия сбора: за сутки {cur24} лотов против {prev24} предыдущих\n"
                f"(падение на {100 - int(cur24 / max(prev24,1) * 100)}%).\n"
                "Проверь: канал, структуру выдачи, статус парсера."
            )
            _state["drop_alerted"] = True
    elif cur24 >= prev24 * 0.5:
        _state["drop_alerted"] = False


def alert_tick(db: dict) -> None:
    """Вызывать каждый проход цикла enricher; сам решает, пора ли."""
    global _state
    now = time.time()

    # heartbeat для внешнего watchdog
    try:
        with open(os.environ.get("HEARTBEAT_FILE", "/shared/heartbeat_enricher"), "w") as f:
            f.write(str(now))
    except Exception:
        pass

    if _mute_all_on(db):
        return

    if now - _state["last_silence_check"] >= 900:  # раз в 15 мин
        _state["last_silence_check"] = now
        try:
            _parser_silence_check(db)
            _collect_drop_check(db)
        except Exception as exc:
            print(f"alerts: silence check failed: {exc}", flush=True)

    if now - _state["last_scan"] >= ALERT_SCAN_INTERVAL:
        _state["last_scan"] = now
        items = _scan_good_lots(db)
        if items:
            if send_lots_batch(items):
                _mark_alerted(db, [it["avito_id"] for it in items[:5]])
                print(f"alerts: отправлено {len(items[:5])} выгодных лотов", flush=True)
            else:
                print("alerts: лот-алерт не отправлен (мьют/бюджет/ошибка)", flush=True)