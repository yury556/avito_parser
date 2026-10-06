"""
Фоновый enricher: полное описание для лотов СПб.

Медленный фоновый процесс (1 лот в минуту): берёт из avito_original.ads лот
СПб с коротким описанием (<=260 символов), ходит на его страницу, вытаскивает
полное описание и обновляет запись. Основной цикл парсера не затрагивает.

Не гарантирует прохождение защиты на страницах лотов (439-ветка может не
поддаться) — при неудаче лот остаётся с коротким описанием и будет повторён
в следующих прогонах.
"""
import os
import random
import re
import time

import psycopg2
from bs4 import BeautifulSoup
from curl_cffi import requests
from loguru import logger

DB = dict(
    host=os.environ.get("POSTGRES_HOST", "external-postgres"),
    port=os.environ.get("POSTGRES_PORT", "5432"),
    dbname=os.environ.get("POSTGRES_DB", "avito_dwh"),
    user=os.environ.get("POSTGRES_USER", "avito"),
    password=os.environ.get("POSTGRES_PASSWORD", "avito123"),
)
INTERVAL = int(os.environ.get("ENRICH_INTERVAL", "60"))
SPB = "санкт-петербург"


def get_http_client() -> requests.Session:
    session = requests.Session(impersonate="firefox147")
    return session


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


FAILED: dict[int, float] = {}  # avito_id -> retry_after_ts


def extract_full_description(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    desc = soup.select_one('[data-marker="item-view/item-description"]')
    if desc:
        text = desc.get_text(separator=" ", strip=True)
        if text:
            return text
    # фолбэк: initial-data-view в JSON страницы
    m = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if m:
        try:
            return m.group(1).encode().decode("unicode_escape").encode("latin1").decode("utf-8")
        except Exception:
            return m.group(1)
    return None


def save_description(avito_id: int, description: str) -> None:
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE avito_original.ads SET description=%s WHERE avito_id=%s",
            (description, avito_id),
        )


def main() -> None:
    session = get_http_client()
    last_warmup = 0.0
    logger.info("enricher запущен: интервал {}с", INTERVAL)

    def warmup(force: bool = False) -> None:
        """Прогрев куки каталогом — без этого лот отдаёт 439."""
        nonlocal last_warmup
        if not force and time.time() - last_warmup < 600:
            return
        try:
            r = session.get(
                os.environ.get(
                    "AVITO_WARMUP_URL",
                    "https://www.avito.ru/sankt-peterburg/tovary_dlya_kompyutera",
                ),
                timeout=25,
            )
            last_warmup = time.time()
            logger.info("Прогрев: HTTP {}, куки в jar: {}", r.status_code, len(session.cookies.jar))
        except Exception as e:
            logger.warning("Прогрев не удался: {}", e)

    warmup(force=True)
    while True:
        ad = pick_ad({k for k, v in FAILED.items() if v > time.time()})
        if ad is None:
            logger.info("Нет лотов для обогащения — сплю")
            time.sleep(INTERVAL * 5)
            continue
        avito_id, url = ad
        try:
            warmup()  # раз в 10 минут
            r = session.get(url, timeout=25, allow_redirects=True)
            if r.status_code == 200:
                desc = extract_full_description(r.text)
                if desc and len(desc) > 260:
                    save_description(avito_id, desc)
                    logger.info("OK {}: {} симв.", avito_id, len(desc))
                else:
                    logger.info("SKIP {}: полное описание не извлеклось", avito_id)
            else:
                # неуспешный лот откладываем на 30 минут
                FAILED[avito_id] = time.time() + 1800
                logger.info(
                    "HTTP {} на лоте {} — отложен на 30 мин (в очереди failed: {})",
                    r.status_code, avito_id, len(FAILED),
                )
        except Exception as e:
            logger.warning("Ошибка лота {}: {}", avito_id, e)
        time.sleep(INTERVAL + random.uniform(0, 15))


if __name__ == "__main__":
    main()
