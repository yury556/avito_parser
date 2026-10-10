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
import random
import time

import psycopg2
from loguru import logger
from playwright.sync_api import sync_playwright

DB = dict(
    host=os.environ.get("POSTGRES_HOST", "external-postgres"),
    port=os.environ.get("POSTGRES_PORT", "5432"),
    dbname=os.environ.get("POSTGRES_DB", "avito_dwh"),
    user=os.environ.get("POSTGRES_USER", "avito"),
    password=os.environ.get("POSTGRES_PASSWORD", "avito123"),
)
INTERVAL = int(os.environ.get("ENRICH_INTERVAL", "60"))
STATUS_INTERVAL = int(os.environ.get("STATUS_INTERVAL", "600"))  # проверка статуса раз в 10 мин
HEADLESS = os.environ.get("AVITO_HEADLESS", "1").lower() in ("1", "true", "yes")
WARM_URL = os.environ.get(
    "AVITO_WARMUP_URL",
    "https://www.avito.ru/sankt-peterburg/tovary_dlya_kompyutera",
)
PROXY = os.environ.get("AVITO_PROXY_URL", "").strip() or None

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


def pick_status_candidate(skip: set[int]) -> tuple[int, str] | None:
    """Приоритет: активные лоты НИЖЕ медианы своей категории (по свежей цене)."""
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH med AS (
                SELECT category, percentile_cont(0.5) WITHIN GROUP (ORDER BY price_rub) AS med
                FROM detail.ads WHERE state = 'активно' AND price_rub > 0
                GROUP BY category
            )
            SELECT d.avito_id, m.url
            FROM detail.ads d
            JOIN midraw.avito_ads m ON m.avito_id = d.avito_id
            JOIN med ON med.category = d.category
            WHERE d.state = 'активно' AND d.price_rub > 0
              AND d.price_rub < med.med
              AND d.avito_id <> ALL(%(skip)s)
              AND (d.last_checked_at IS NULL OR d.last_checked_at < now() - interval '6 hours')
            ORDER BY d.price_rub ASC
            LIMIT 10
            """,
            {"skip": list(skip) or [0]},
        )
        rows = cur.fetchall()
    if not rows:
        return None
    return random.choice(rows)


def mark_checked(avito_id: int, state: str) -> None:
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE detail.ads SET state=%s, last_checked_at=now() WHERE avito_id=%s",
            (state, avito_id),
        )


def read_price(page) -> float | None:
    """Цена прямо со страницы лота."""
    el = page.query_selector('[data-marker="item-price"]') or \
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

        pause = float(INTERVAL)
        last_status_check = 0.0
        while True:
            # --- приоритет: проверка статуса (раз в STATUS_INTERVAL секунд) ---
            if time.time() - last_status_check >= STATUS_INTERVAL:
                last_status_check = time.time()
                cand = pick_status_candidate({k for k, v in FAILED.items() if v > time.time()})
                if cand is None:
                    logger.info("Проверка статуса: нет кандидатов ниже медианы")
                else:
                    avito_id, url = cand
                    try:
                        ok = goto(url, wait_selector=DESC_SELECTOR)
                        title_txt = (page.title() or "").lower()
                        if not ok or "доступ ограничен" in title_txt or "проблема с ip" in title_txt:
                            logger.info("Проверка {}: лот закрыт/недоступен — помечаю", avito_id)
                            mark_checked(avito_id, "закрыто")
                        else:
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
                    except Exception as e:
                        logger.warning("Ошибка проверки {}: {}", avito_id, e)
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
                warmup()
                ok = goto(url, wait_selector=DESC_SELECTOR)
                if ok:
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
