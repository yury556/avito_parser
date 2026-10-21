"""
Фоновый enricher: полные описания лотов СПб через живой браузер (Playwright).

Страницы лотов Авито защищены JS-челленджем, который HTTP-клиент (curl_cffi)
не исполняет — потому они отдают 439. В живом браузере челлендж выполняется
сам, как у настоящего пользователя: открываем лот, ждём отрисовку,
вытаскиваем полное описание, обновляем avito_original.ads.

Цикл: 1 лот за итерацию, адаптивная пауза (успех -> быстрее, неудача -> тише),
skip-лист на 30 минут, каталог-прогрев раз в 10 минут.
"""
import os
import json
import random
import time
import urllib.request

import psycopg2
from loguru import logger
from playwright.sync_api import sync_playwright

from tg_alerts import send_message  # noqa: F401 (единый модуль алертов)
import alerts_worker

PARSER_URL = os.environ.get("AVITO_PARSER_URL", "http://avito-parser-service:8000").rstrip("/")
HTTP_DESC_LIMIT = int(os.environ.get("HTTP_DESC_LIMIT", "60"))  # лотов/час через парсер

DB = dict(
    host=os.environ.get("POSTGRES_HOST", "external-postgres"),
    port=os.environ.get("POSTGRES_PORT", "5432"),
    dbname=os.environ.get("POSTGRES_DB", "avito_dwh"),
    user=os.environ.get("POSTGRES_USER", "avito"),
    password=os.environ.get("POSTGRES_PASSWORD", ""),
)
INTERVAL = int(os.environ.get("ENRICH_INTERVAL", "60"))
STATUS_INTERVAL = int(os.environ.get("STATUS_INTERVAL", "180"))  # проверка статуса раз в 3 мин
HEADLESS = os.environ.get("AVITO_HEADLESS", "1").lower() in ("1", "true", "yes")
WARM_URL = os.environ.get(
    "AVITO_WARMUP_URL",
    "https://www.avito.ru/sankt-peterburg/tovary_dlya_kompyutera",
)
PROXY = os.environ.get("AVITO_PROXY_URL", "").strip() or None

BLOCK_MARKERS = ("доступ ограничен", "проблема с ip")  # блокировка IP/челлендж — временно, НЕ закрытие лота
LOT_TITLE_SELECTOR = '[data-marker="item-view/item-title"], h1'


def page_is_blocked(page) -> bool:
    """Признаки блокировки (IP-бан / челлендж), а не закрытия лота."""
    title = (page.title() or "").lower()
    return any(marker in title for marker in BLOCK_MARKERS)


def lot_is_open(page) -> bool:
    """Открытый лот: на странице есть заголовок товара, цена или описание.
    Если нет ничего из этого — лот реально закрыт/удалён."""
    return bool(
        page.query_selector(LOT_TITLE_SELECTOR)
        or read_price(page)
        or extract_description(page)
    )


FAILED: dict[int, float] = {}  # avito_id -> retry_after_ts

DESC_SELECTOR = '[data-marker="item-view/item-description"]'
CSV_HEADER = "avito_id,title,price_rub,url,location,seller,description,parsed_at"


def csv_cell(value) -> str:
    """Контракт raw: запятые в текстах запрещены."""
    return ("" if value is None else str(value)).replace(",", " ").strip()


def insert_raw_snapshot(avito_id: int, title: str, price: float, url: str,
                        description: str, when: str) -> None:
    """Снапшот проверки в raw (контракт csv) — цена доедет через dbt в midraw/витрины."""
    row = ",".join([
        csv_cell(avito_id), csv_cell(title), csv_cell(price), csv_cell(url),
        "", "", csv_cell(description), csv_cell(when),
    ])
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw.avito_ads_csv (run_id, row_number, source_url, csv_header, csv_row, loaded_at) "
            "VALUES (%s, 1, %s, %s, %s, now())",
            (f"browser_check_{time.strftime('%Y%m%d')}", "browser_check", CSV_HEADER, row),
        )


HTTP_DESC_TIMES: list[float] = []  # timestamps использования быстрого пути


def fetch_description_http(avito_id: int) -> str | None | bool:
    """Описание через парсер (/fetch_lot, прогретая сессия + solver).

    Возвращает: str — получено; None — страница получена, описания нет;
    False — путь недоступен/лимит/блокировка → fallback Playwright.
    """
    if len([t for t in HTTP_DESC_TIMES if time.time() - t < 3600]) >= HTTP_DESC_LIMIT:
        return False
    try:
        url = f"{PARSER_URL}/fetch_lot?avito_id={avito_id}"
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.loads(r.read())
    except Exception as exc:
        logger.warning("/fetch_lot недоступен ({}), fallback Playwright", exc)
        return False
    if data.get("description"):
        HTTP_DESC_TIMES.append(time.time())
        return data["description"]
    if data.get("reason") and "не прошёл" in data["reason"]:
        return False  # блокировка — есть смысл попробовать браузер
    return None  # страница получена, описания нет


PROBE = {"fails": 0, "paused_until": 0.0, "alerted": False}  # режим экономии проверщика


def _touch_heartbeat() -> None:
    """Heartbeat перед каждой потенциально долгой операцией (goto/fetch).
    Иначе долгий проход пачки выглядел для watchdog'а как смерть."""
    try:
        with open(os.environ.get("HEARTBEAT_FILE", "/shared/heartbeat_enricher"), "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def mark_probe(db: dict, ok: bool) -> None:
    """Учёт попыток проверщика: 5 провалов подряд -> пауза 30 мин + алерт."""
    if ok:
        if PROBE["alerted"]:
            send_recovery("Проверщик возобновил работу — канал ожил")
            PROBE["alerted"] = False
        PROBE["fails"] = 0
        return
    PROBE["fails"] += 1
    if PROBE["fails"] >= 5:
        PROBE["paused_until"] = time.time() + 1800
        if not PROBE["alerted"]:
            if not alerts_worker._mute_all_on(db):
                send_failure(
                    "Проверщик приостановлен на 30 мин:\n"
                    "5 проверок подряд не прошли (канал в бан-фазе).\n"
                    "Очередь проверок сохранена, возобновим автоматически."
                )
            PROBE["alerted"] = True


def _http_lot(avito_id: int):
    """Быстрый чек лота через /fetch_lot: (title, price, desc) | None | False.

    False — недоступен/лимит/блокировка → fallback Playwright.
    None — страница получена, но пуста (нет ни цены, ни заголовка) → закрыт.
    dict — живой лот.
    """
    _touch_heartbeat()
    if len([t for t in HTTP_DESC_TIMES if time.time() - t < 3600]) >= HTTP_DESC_LIMIT:
        return False
    try:
        url = f"{PARSER_URL}/fetch_lot?avito_id={avito_id}"
        with urllib.request.urlopen(url, timeout=90) as r:
            data = json.loads(r.read())
    except Exception as exc:
        logger.warning("/fetch_lot недоступен ({}), fallback Playwright", exc)
        return False
    if data.get("ok") is False:
        return False  # блокировка — есть смысл попробовать браузер
    title, price, desc = data.get("title"), data.get("price"), data.get("description")
    if not title and not price and not desc:
        return None
    return {"title": title, "price": price, "desc": desc if (desc and len(desc) > 260) else None}


def pick_status_candidates(skip: set[int]) -> list[tuple[int, str]]:
    """Пачка кандидатов: приоритет 1) ниже медианы 2) устаревшая цена
    (чем дольше цена не обновлялась, тем раньше) 3) СПб. Ротация 12 ч."""
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH med AS (
                SELECT category, percentile_cont(0.5) WITHIN GROUP (ORDER BY price_rub) AS med
                FROM detail.ads WHERE state = 'активно' AND price_rub > 0
                GROUP BY category
            ),
            last AS (
                SELECT DISTINCT ON (avito_id) avito_id, price_rub, parsed_at
                FROM midraw.avito_ads WHERE price_rub > 0
                ORDER BY avito_id, parsed_at DESC
            )
            SELECT d.avito_id, m.url
            FROM detail.ads d
            JOIN midraw.avito_ads m ON m.avito_id = d.avito_id
            JOIN last lp ON lp.avito_id = d.avito_id
            JOIN med ON med.category = d.category
            WHERE d.state = 'активно' AND d.price_rub > 0
              AND d.avito_id <> ALL(%(skip)s)
              AND (d.last_checked_at IS NULL OR d.last_checked_at < now() - interval '12 hours')
            ORDER BY
                CASE WHEN d.price_rub < med.med THEN 0 ELSE 1 END,
                lp.parsed_at ASC,
                CASE WHEN strpos(lower(m.url), 'sankt-peterburg') > 0 THEN 0 ELSE 1 END,
                d.price_rub ASC,
                d.last_checked_at ASC NULLS FIRST
            LIMIT 10
            """,
            {"skip": list(skip) or [0]},
        )
        return cur.fetchall()


def mark_checked(avito_id: int, state: str) -> None:
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE detail.ads SET state=%s, last_checked_at=now() WHERE avito_id=%s",
            (state, avito_id),
        )


def read_price(page) -> float | None:
    """Цена прямо со страницы лота."""
    el = page.query_selector('[data-marker="item-view/item-price"]') or \
         page.query_selector('[data-marker="item-price"]') or \
         page.query_selector('[itemprop="price"]')
    if el:
        digits = "".join(ch for ch in el.inner_text() if ch.isdigit())
        if digits:
            return float(digits)
    return None


def pick_ad(skip: set[int]) -> tuple[int, str] | None:
    """Один лот СПб без полного описания, из свежих, вне skip-листа."""
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT avito_id, url FROM avito_original.ads
            WHERE location ILIKE '%%Санкт-Петербург%%'
              AND url IS NOT NULL
              AND (description IS NULL OR length(description) <= 260)
              AND avito_id <> ALL(%(skip)s)
            ORDER BY parsed_at DESC
            LIMIT 10
            """,
            {"skip": list(skip) or [0]},
        )
        rows = cur.fetchall()
    if not rows:
        return None
    return random.choice(rows)


def save_description(avito_id: int, description: str) -> None:
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE avito_original.ads SET description=%s WHERE avito_id=%s",
            (description, avito_id),
        )


def human_pause(page, lo: float = 0.8, hi: float = 2.0) -> None:
    """Пауза-как-человек."""
    page.wait_for_timeout(int(random.uniform(lo, hi) * 1000))


def human_scroll(page) -> None:
    """Пара неровных скроллов + движение мыши — поведенческие сигналы."""
    page.mouse.move(random.uniform(100, 900), random.uniform(100, 500))
    human_pause(page, 0.3, 0.9)
    page.evaluate("window.scrollBy(0, window.innerHeight * 0.4)")
    human_pause(page, 0.4, 1.1)
    page.evaluate("window.scrollBy(0, window.innerHeight * 0.25)")


def extract_description(page) -> str | None:
    el = page.query_selector(DESC_SELECTOR)
    if el:
        text = el.inner_text().strip()
        if text:
            return text
    return None


def main() -> None:
    logger.info(
        "enricher(playwright) запущен: интервал {}с, headless={}", INTERVAL, HEADLESS
    )
    with sync_playwright() as p:
        launch_kwargs = dict(headless=HEADLESS)
        if PROXY:
            launch_kwargs["proxy"] = {"server": PROXY}
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1440, "height": 860},
        )
        page = context.new_page()
        # Текст нам нужен только текст: режем картинки/шрифты/медиа —
        # страница грузится в разы быстрее через медленный прокси.
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "font", "media")
            else route.continue_(),
        )

        def goto(url: str, wait_selector: str | None = None) -> bool:
            """Навигация как человек: goto + пауза + опционально ждём селектор."""
            _touch_heartbeat()
            try:
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                human_pause(page)
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=15000)
                    except Exception:
                        pass
                return True
            except Exception as e:
                logger.warning("goto {} упал: {}", url[:90], e)
                return False

        def warmup(force: bool = False) -> bool:
            """Каталог-прогрев: куки валидны, без него лот отдаёт челлендж."""
            if not force and time.time() - warmup.last < 600:
                return True
            logger.info("Прогрев каталога...")
            ok = goto(WARM_URL)
            if ok:
                human_scroll(page)
                warmup.last = time.time()
                logger.info("Прогрев OK")
            return ok

        warmup.last = 0.0
        warmup(force=True)

        # Алерты: ensured таблицы + тик в каждом проходе цикла (сам решает, пора ли)
        with psycopg2.connect(**DB) as _conn, _conn.cursor() as _cur:
            _cur.execute(
                "ALTER TABLE detail.ads ADD COLUMN IF NOT EXISTS alerted_at timestamptz"
            )
            _cur.execute(
                "CREATE TABLE IF NOT EXISTS avito_original.bot_settings ("
                "key text PRIMARY KEY, value text)"
            )
        _conn_ensured = True

        pause = float(INTERVAL)
        last_status_check = 0.0
        while True:
            # Чистка просроченных откладываний — FAILED не растёт бесконечно.
            now_ts = time.time()
            for expired_id in [k for k, v in FAILED.items() if v <= now_ts]:
                del FAILED[expired_id]
            # Режим экономии: после 5 провалов подряд — пауза 30 мин
            if time.time() < PROBE["paused_until"]:
                time.sleep(60)
                continue
            # Алерты (батч лотов / молчание парсера) — сам решает, пора ли.
            try:
                alerts_worker.alert_tick(DB)
            except Exception as _alert_err:
                logger.warning("alert tick упал (не критично): {}", _alert_err)
            # --- приоритет: проверка статуса (раз в STATUS_INTERVAL секунд) ---
            if time.time() - last_status_check >= STATUS_INTERVAL:
                last_status_check = time.time()
                cands = pick_status_candidates({k for k, v in FAILED.items() if v > time.time()})
                if not cands:
                    logger.info("Проверка статуса: нет кандидатов ниже медианы")
                else:
                    logger.info("Пачка проверок: {} кандидатов", len(cands))
                    for avito_id, url in cands:
                        if time.time() < PROBE["paused_until"]:
                            break
                        try:
                            # --- быстрый чек через парсер (лимит общий с описаниями) ---
                            lot = _http_lot(avito_id)
                            if isinstance(lot, dict):
                                if lot["desc"]:
                                    save_description(avito_id, lot["desc"])
                                mark_checked(avito_id, "активно")
                                if lot["price"]:
                                    insert_raw_snapshot(
                                        avito_id, lot["title"] or "", lot["price"], url,
                                        lot["desc"] or "", time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                                    )
                                logger.info("Проверка {}: жив (HTTP), цена {} ₽", avito_id, lot["price"])
                                mark_probe(DB, True)
                                time.sleep(random.uniform(1, 3))
                                continue
                            if lot is None:
                                # страница получена чисто, но нет ни цены, ни заголовка — закрыт
                                logger.info("Проверка {}: лот закрыт (HTTP: пустая страница) — помечаю", avito_id)
                                mark_probe(DB, True)
                                mark_checked(avito_id, "закрыто")
                                time.sleep(random.uniform(1, 3))
                                continue
                            # lot is False → лимит/блокировка → браузерный путь ниже
                            ok = goto(url, wait_selector=DESC_SELECTOR)
                            if not ok:
                                # Страница не открылась (таймаут/прокси) — временное
                                # состояние, помечать «закрыто» нельзя. Откладываем.
                                logger.info("Проверка {}: страница не открылась — НЕ помечаю, откладываю на 30 мин", avito_id)
                                mark_probe(DB, False)
                                FAILED[avito_id] = time.time() + 1800
                            elif page_is_blocked(page):
                                # «Доступ ограничен» / «проблема с IP» — блокировка,
                                # а не закрытие лота. Раньше здесь было ложное «закрыто».
                                logger.warning("Проверка {}: блокировка IP/челлендж — НЕ помечаю закрытым, откладываю на 30 мин", avito_id)
                                mark_probe(DB, False)
                                FAILED[avito_id] = time.time() + 1800
                            elif lot_is_open(page):
                                price = read_price(page)
                                desc = extract_description(page)
                                if desc and len(desc) > 260:
                                    save_description(avito_id, desc)
                                mark_checked(avito_id, "активно")
                                if price:
                                    insert_raw_snapshot(
                                        avito_id, page.title(), price, url,
                                        desc or "", time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                                    )
                                logger.info("Проверка {}: жив, цена {} ₽", avito_id, price)
                            else:
                                # Страница открылась, блокировки нет, но нет ни заголовка,
                                # ни цены, ни описания — лот реально закрыт/удалён.
                                logger.info("Проверка {}: лот закрыт (нет заголовка/цены) — помечаю", avito_id)
                                mark_probe(DB, True)
                                mark_checked(avito_id, "закрыто")
                        except Exception as e:
                            logger.warning("Ошибка проверки {}: {}", avito_id, e)
                        time.sleep(random.uniform(2, 5))
                time.sleep(10)
                continue

            # --- описание: обычная очередь ---
            ad = pick_ad({k for k, v in FAILED.items() if v > time.time()})
            if ad is None:
                logger.info("Нет лотов для обогащения — сплю")
                time.sleep(INTERVAL * 5)
                continue
            avito_id, url = ad
            try:
                # --- быстрый путь: через HTTP-клиент парсера (прогретая сессия) ---
                desc_http = fetch_description_http(avito_id)
                if isinstance(desc_http, str) and desc_http and len(desc_http) > 260:
                    save_description(avito_id, desc_http)
                    logger.info("OK {} (HTTP): {} симв.", avito_id, len(desc_http))
                    pause = max(15.0, pause / 2)
                    time.sleep(pause + random.uniform(0, 10))
                    continue
                if desc_http is None:
                    # страница получена, описания нет — Playwright не поможет быстрее
                    logger.info("SKIP {}: описание не извлеклось (HTTP)", avito_id)
                    FAILED[avito_id] = time.time() + 1800
                    pause = min(300.0, pause * 2 + 30)
                    time.sleep(pause + random.uniform(0, 10))
                    continue
                # desc_http is False -> fallback Playwright
                warmup()
                ok = goto(url, wait_selector=DESC_SELECTOR)
                if ok:
                    mark_probe(DB, True)
                    human_scroll(page)
                    desc = extract_description(page)
                    if desc and len(desc) > 260:
                        save_description(avito_id, desc)
                        logger.info("OK {}: {} симв.", avito_id, len(desc))
                        pause = max(15.0, pause / 2)  # снимаем урожай, пока дают
                    else:
                        logger.info("SKIP {}: описание не извлеклось", avito_id)
                        FAILED[avito_id] = time.time() + 1800
                        pause = min(300.0, pause * 2 + 30)
                else:
                    mark_probe(DB, False)
                    FAILED[avito_id] = time.time() + 1800
                    pause = min(300.0, pause * 2 + 30)
                    logger.info(
                        "Лот {} не открылся — отложен на 30 мин, пауза {:.0f}с (failed: {})",
                        avito_id, pause, len(FAILED),
                    )
            except Exception as e:
                logger.warning("Ошибка лота {}: {}", avito_id, e)
                pause = min(300.0, pause * 2 + 30)
            time.sleep(pause + random.uniform(0, 10))


if __name__ == "__main__":
    main()
