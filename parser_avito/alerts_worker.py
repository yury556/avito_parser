"""Алерты в enricher-цикле: выгодные лоты (батч) + молчание парсера.

Вызывается из enricher.py: alert_tick(db, is_time) каждый проход цикла.
"""
import os
import time

import psycopg2

from tg_alerts import send_lots_batch, send_failure, send_recovery, send_system

# Троттлинг алерта «скан выгодных лотов упал»: не чаще раза в час
_SCAN_FAIL_ALERTED_AT = 0.0

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
    """Активные лоты дешевле медианы на DISCOUNT_PCT%+.

    Медиана считается ПО КОНКРЕТНОЙ МОДЕЛИ (если ≥3 активных лотов модели),
    иначе — по категории (fallback). Фильтры: непустая модель, чёрный список
    аксессуаров в заголовке, только СПб/Лен область, цена не ниже 30% медианы
    (отсекает заглушки/обман за 350 ₽).
    """
    try:
        with _db_conn(db) as conn, conn.cursor() as cur:
            cur.execute(
                r"""
                WITH base AS (
                    SELECT d.avito_id, d.price_rub, d.state, d.category, d.model,
                           d.alerted_at, coalesce(d.chip, '') AS b_chip, d.tags,
                           lower(d.model) AS lmodel,
                           coalesce(
                               nullif(coalesce(d.chip, ''), ''),
                               substring(lower(d.model) from '((rtx|gtx)\s?[0-9]{4}(\s?(ti|super))*)'),
                               substring(lower(d.model) from '(rx\s?[0-9]{4}(\s?xt)*)'),
                               substring(lower(d.model) from '(i[3579]-[0-9]{4,5}[fk]?)'),
                               substring(lower(d.model) from '(ryzen\s?[0-9]\s?[0-9]{4}[x3df]*)'),
                               substring(lower(d.model) from '(ddr[345]\s?[0-9]{1,2}\s?gb)'),
                               CASE WHEN lower(d.model) ~ '(прицеп|титан|оптима|optima|бортов|аккума|мзса|кузов|тралер|trailer)'
                                         OR lower(d.category) LIKE '%%прицеп%%'
                                    THEN 'прицеп' END,
                               d.category
                           ) AS family
                    FROM detail.ads d
                ),
                med_fam AS (
                    SELECT family, percentile_cont(0.5)
                           WITHIN GROUP (ORDER BY price_rub) AS med
                    FROM base
                    WHERE state = 'активно' AND price_rub > 0
                      AND model IS NOT NULL AND model != ''
                    GROUP BY family
                    HAVING count(*) >= 3
                ),
                med_cat AS (
                    SELECT category, percentile_cont(0.5)
                           WITHIN GROUP (ORDER BY price_rub) AS med
                    FROM base
                    WHERE state = 'активно' AND price_rub > 0
                      AND model IS NOT NULL AND model != ''
                    GROUP BY category
                ),
                seen AS (
                    -- в midraw несколько строк на лот (история цен) — берём свежую,
                    -- но с непустыми location/title (в свежих снимках они бывают пусты)
                    SELECT DISTINCT ON (avito_id)
                           avito_id, url, location, title,
                           max(parsed_at) OVER (PARTITION BY avito_id) AS last_seen
                    FROM midraw.avito_ads
                    ORDER BY avito_id,
                             (coalesce(location, '') = '') ASC,
                             (coalesce(title, '') = '') ASC,
                             parsed_at DESC
                ),
                lot_meta AS (
                    -- дата публикации, просмотры и описание из avito_original.ads
                    SELECT avito_id, published_at, views_total, views_today, description
                    FROM avito_original.ads
                )
                SELECT b.avito_id, b.model, b.price_rub, m.url,
                       m.location,
                       COALESCE(med_fam.med, med_cat.med) AS ref_med,
                       b.tags, lm.published_at, lm.views_total, lm.views_today,
                       lm.description
                FROM base b
                JOIN med_cat ON med_cat.category = b.category
                LEFT JOIN med_fam ON med_fam.family = b.family
                JOIN seen m ON m.avito_id = b.avito_id
                LEFT JOIN lot_meta lm ON lm.avito_id = b.avito_id
                WHERE b.state = 'активно' AND b.price_rub > 0
                  AND b.alerted_at IS NULL
                  AND m.last_seen > now() - interval '48 hours'
                  AND b.model IS NOT NULL AND b.model != ''
                  AND (m.title IS NULL OR m.title !~*
                       '(переходник|кабел|кронштейн|подставк|заглушк|адаптер|шлейф|креплени|держател)')
                  AND (position('петербург' in lower(coalesce(m.location,''))) > 0
                       OR position('ленинград' in lower(coalesce(m.location,''))) > 0
                       OR position('sankt-peterburg' in lower(coalesce(m.url,''))) > 0
                       OR position('leningradskaya' in lower(coalesce(m.url,''))) > 0)
                  AND b.price_rub >= COALESCE(med_fam.med, med_cat.med) * 0.30
                  AND b.price_rub < COALESCE(med_fam.med, med_cat.med) * %(k)s
                ORDER BY (b.price_rub / COALESCE(med_fam.med, med_cat.med)) ASC
                LIMIT 10
                """,
                {"k": 1 - DISCOUNT_PCT / 100.0},
            )
            rows = cur.fetchall()
    except Exception as exc:
        print(f"alerts: scan failed: {exc}", flush=True)
        # Молчащий отказ скана хуже падения: система перестаёт присылать лоты.
        # Сообщаем в Telegram (не чаще раза в час).
        global _SCAN_FAIL_ALERTED_AT
        if time.time() - _SCAN_FAIL_ALERTED_AT > 3600:
            _SCAN_FAIL_ALERTED_AT = time.time()
            try:
                send_system(f"⚠️ Скан выгодных лотов упал: {exc}\n"
                            f"Лоты не присылаются, пока не починим.")
            except Exception:
                pass
        return None
    return [
        {
            "avito_id": r[0],
            "model": (r[1] or "?")[:60],
            "price": float(r[2]),
            "url": r[3],
            "location": r[4],
            "median": float(r[5]),
            "benefit_pct": int(round((float(r[2]) / float(r[5]) - 1) * 100)),
            "tags": r[6] or [],
            "published_at": r[7],
            "views_total": r[8],
            "views_today": r[9],
            "description": r[10] or "",
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
            sent = send_lots_batch(items)
            if sent:
                _mark_alerted(db, sent)
                print(f"alerts: отправлено {len(sent)} выгодных лотов "
                      f"(отдельными сообщениями)", flush=True)
            else:
                print("alerts: лот-алерт не отправлен (мьют/бюджет/ошибка)", flush=True)