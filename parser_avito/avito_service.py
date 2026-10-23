import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fastapi import FastAPI, HTTPException
from loguru import logger

from avito_parser.settings import AvitoConfig
from avito_parser.core import AvitoParse
from avito_parser.config import load_avito_config
from avito_parser.image_downloader import ImageDownloader
from avito_parser.storages import create_storage

app = FastAPI(title="Avito Parser Service", version="3.2.16")


def _build_config(body: dict) -> AvitoConfig:
    return AvitoConfig(
        urls=body.get("urls", []),
        count=body.get("max_pages", body.get("count", 1)),
        timeout=body.get("request_timeout", body.get("timeout", 20)),
        proxy_string=body.get("proxy_string"),
        proxy_change_url=body.get("proxy_change_url"),
        keys_word_white_list=body.get("keys_word_white_list", body.get("white_list", [])),
        keys_word_black_list=body.get("keys_word_black_list", body.get("black_list", [])),
        seller_black_list=body.get("seller_black_list", []),
        max_price=body.get("max_price", 999_999_999),
        min_price=body.get("min_price", 0),
        geo=body.get("geo"),
        max_age=body.get("max_age", 24 * 60 * 60),
        pause_between_links=body.get("pause_between_links", 5),
        max_count_of_retry=body.get("max_count_of_retry", 5),
        ignore_reserv=body.get("ignore_reserv", True),
        ignore_promotion=body.get("ignore_promotion", False),
        one_time_start=body.get("one_time_start", True),
        one_file_for_link=body.get("one_file_for_link", False),
        parse_views=body.get("parse_views", False),
        save_xlsx=False,
        save_json=body.get("save_json", True),
        use_webdriver=False,
        use_bypass_api=body.get("use_bypass_api", False),
        cookies_api_key=body.get("cookies_api_key"),
        output_dir=Path(body.get("output_dir", "result")),
        use_own_cookies=body.get("use_own_cookies", False),
        parse_phone=body.get("parse_phone", False),
        retry_delay=body.get("retry_delay", 5),
        block_threshold=body.get("block_threshold", 3),
        debug_mode=body.get("debug_mode", 0),
        user_agent=body.get("user_agent",
                           "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/140.0.0.0 Safari/537.36"),
    )


def _extract_image_urls(item) -> list[str]:
    if not item.images:
        return []
    urls = []
    for img in item.images:
        try:
            best_key = max(
                img.root.keys(),
                key=lambda k: int(k.split("x")[0]) * int(k.split("x")[1])
            )
            urls.append(str(img.root[best_key]))
        except Exception:
            continue
    return urls


def _item_to_dict(item) -> dict:
    price_raw = item.priceDetailed.value if item.priceDetailed else None
    description_raw = item.description or ""
    if not description_raw and item.iva:
        steps = item.iva.get("DescriptionStep", [])
        if steps and steps[0].payload:
            description_raw = steps[0].payload.get("description", "") or ""
    description_clean = " ".join(description_raw.replace("\n", " ").replace("\r", " ").split())[:10000] or None
    return {
        "avito_id": item.id if isinstance(item.id, int) else None,
        "title": item.title or "",
        "price_rub": int(price_raw) if price_raw is not None else None,
        "url": f"https://www.avito.ru{item.urlPath}" if item.urlPath else None,
        "location": (item.location.name or "")[:256] if item.location else None,
        "seller": (item.sellerId or "")[:64] if item.sellerId else None,
        "description": description_clean,
        "is_reserved": item.isReserved if item.isReserved is not None else False,
        "is_promotion": item.isPromotion if item.isPromotion is not None else False,
        "total_views": item.total_views,
        "today_views": item.today_views,
        "image_urls": _extract_image_urls(item),
    }


def _get_postgres_config(body: dict) -> dict | None:
    pg = body.get("postgres", {})
    if not pg.get("host") and not os.environ.get("POSTGRES_HOST"):
        return None
    return {
        "host": pg.get("host", os.environ.get("POSTGRES_HOST", "external-postgres")),
        "port": pg.get("port", int(os.environ.get("POSTGRES_PORT", 5432))),
        "dbname": pg.get("dbname", os.environ.get("POSTGRES_DB", "avito_dwh")),
        "user": pg.get("user", os.environ.get("POSTGRES_USER", "avito")),
        "password": pg.get("password", os.environ.get("POSTGRES_PASSWORD", "avito123")),
    }


@contextmanager
def _postgres_conn(pg_config: dict):
    import psycopg2
    conn = psycopg2.connect(**pg_config)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _write_ads_to_postgres(pg_config: dict, run_id: str, source_urls: list[str], ads: list[dict]) -> int:
    with _postgres_conn(pg_config) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE avito_original.ads
                ADD COLUMN IF NOT EXISTS description TEXT
            """)
            cur.execute("""
                ALTER TABLE avito_original.ads
                ADD COLUMN IF NOT EXISTS image_keys JSONB DEFAULT '[]'::jsonb
            """)
            if ads:
                from psycopg2.extras import execute_values
                execute_values(
                    cur,
                    """
                    INSERT INTO avito_original.ads
                        (avito_id, title, price_rub, url, location, seller,
                         description, image_keys,
                         is_reserved, is_promotion, total_views, today_views, parsed_at)
                    VALUES %s
                    ON CONFLICT (avito_id) DO UPDATE SET
                        title = EXCLUDED.title,
                        price_rub = EXCLUDED.price_rub,
                        url = EXCLUDED.url,
                        location = EXCLUDED.location,
                        seller = EXCLUDED.seller,
                        description = EXCLUDED.description,
                        image_keys = EXCLUDED.image_keys,
                        is_reserved = EXCLUDED.is_reserved,
                        is_promotion = EXCLUDED.is_promotion,
                        total_views = EXCLUDED.total_views,
                        today_views = EXCLUDED.today_views,
                        parsed_at = EXCLUDED.parsed_at
                    WHERE avito_original.ads.price_rub     IS DISTINCT FROM EXCLUDED.price_rub
                       OR avito_original.ads.is_reserved   IS DISTINCT FROM EXCLUDED.is_reserved
                       OR avito_original.ads.is_promotion  IS DISTINCT FROM EXCLUDED.is_promotion
                       OR avito_original.ads.total_views   IS DISTINCT FROM EXCLUDED.total_views
                       OR avito_original.ads.today_views   IS DISTINCT FROM EXCLUDED.today_views
                       OR avito_original.ads.description IS DISTINCT FROM EXCLUDED.description
                    """,
                    [
                        (
                            ad["avito_id"],
                            ad.get("title", ""),
                            ad.get("price_rub"),
                            ad.get("url", ""),
                            ad.get("location"),
                            ad.get("seller"),
                            ad.get("description"),
                            json.dumps(ad.get("image_keys", [])),
                            ad.get("is_reserved", False),
                            ad.get("is_promotion", False),
                            ad.get("total_views"),
                            ad.get("today_views"),
                            datetime.now(timezone.utc),
                        )
                        for ad in ads
                        if ad.get("avito_id")
                    ],
                )

    # --- журнал конвейера: этап 'raw' только для новых лотов ---
                new_ids = [ad["avito_id"] for ad in ads if ad.get("avito_id")]
                if new_ids:
                    values = ", ".join(
                        f"({avito_id}, 'raw', '{json.dumps({'run_id': run_id})}'::jsonb)"
                        for avito_id in new_ids
                    )
                    cur.execute(f"""
                        INSERT INTO avito_original.lot_pipeline_log (avito_id, stage, meta)
                        SELECT v.avito_id, v.stage, v.meta
                        FROM (VALUES {values}) AS v(avito_id, stage, meta)
                        WHERE NOT EXISTS (
                            SELECT 1 FROM avito_original.lot_pipeline_log p
                            WHERE p.avito_id = v.avito_id AND p.stage = 'raw'
                        )
                    """)

    # --- синк именованных поисков из [url_names] в БД ---
                try:
                    url_names = {}
                    config_path = os.environ.get("PARSER_CONFIG", "/app/config/config.toml")
                    try:
                        import tomllib as _tomllib
                        with open(config_path, "rb") as tf:
                            _cfg = _tomllib.load(tf)
                        url_names = _cfg.get("url_names", {})
                    except Exception:
                        pass
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS avito_original.search_urls (
                            url TEXT PRIMARY KEY, name TEXT,
                            active BOOLEAN DEFAULT TRUE, added_at TIMESTAMPTZ DEFAULT now())
                    """)
                    seen_urls = set()
                    for su in (source_urls or []):
                        if not su or su in seen_urls:
                            continue
                        seen_urls.add(su)
                        cur.execute("""
                            INSERT INTO avito_original.search_urls (url, name)
                            VALUES (%s, %s)
                            ON CONFLICT (url) DO UPDATE
                                SET name = COALESCE(EXCLUDED.name, search_urls.name),
                                    active = TRUE
                        """, (su, url_names.get(su)))
                except Exception as sync_err:
                    logger.warning(f"sync search_urls failed: {sync_err}")
    return len([ad for ad in ads if ad.get("avito_id")])


@app.post("/parse")
def parse_ads(body: dict):
    logger.info(f"Получен запрос на парсинг: {len(body.get('urls', []))} URL, "
                f"max_pages={body.get('max_pages', 1)}")

    config = _build_config(body)
    parser = AvitoParse(config)
    try:
        items = parser.parse()
        partial = False
    except Exception as parse_err:
        # Частичный урожай уже собранных страниц — не теряем при падении.
        items = getattr(parser, "collected", None) or []
        partial = True
        if not items:
            raise
        logger.warning(f"Парсинг упал ({parse_err}); отдаю частичный урожай: {len(items)} объявлений")
    logger.info(f"Парсинг завершён: {len(items)} объявлений{' (частично)' if partial else ''}")

    ads = [_item_to_dict(item) for item in items]

    storage = create_storage(config.storage)
    downloader = ImageDownloader(storage, config.storage)
    for ad in ads:
        if ad.get("image_urls"):
            image_keys = downloader.download_and_store(ad["avito_id"], ad["image_urls"])
            ad["image_keys"] = image_keys
        else:
            ad["image_keys"] = []

    pg_config = _get_postgres_config(body)
    if pg_config and ads:
        run_id = datetime.now(timezone.utc).strftime("svc_%Y%m%d_%H%M%S")
        count = _write_ads_to_postgres(pg_config, run_id, config.urls, ads)
        logger.info(f"Записано в Postgres: {count} строк, run_id={run_id}")

    return {"status": "ok", "count": len(ads), "ads": ads}


@app.get("/health")
def health():
    return {"status": "ok", "service": "avito-parser", "version": "3.2.16"}


# ---------------------------------------------------------------- /fetch_lot
_lot_parser = None


def _extract_description(html: str) -> str | None:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    desc_block = soup.select_one('[data-marker="item-view/item-description"]')
    if desc_block:
        text = desc_block.get_text(separator=" ", strip=True)
        if text:
            return text
    import html as html_lib
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(html_lib.unescape(script.text))
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                if isinstance(entry, dict) and entry.get("description"):
                    return entry["description"]
        except Exception:
            continue
    for script in soup.select('script'):
        if script.get('type') == 'mime/invalid' and script.get('data-mfe-state') == 'true':
            try:
                data = json.loads(html_lib.unescape(script.text))
                item_view = data.get("loaderData", {}).get("data", {}).get("item", {})
                desc = item_view.get("description") or item_view.get("fullDescription")
                if desc:
                    return desc
            except Exception:
                continue
    return None


def _extract_lot_meta(html: str) -> dict:
    """Дата создания объявления и просмотры со страницы лота.

    data-marker="item-view/item-date"      -> "23 сентября в 14:25"
    data-marker="item-view/total-views"    -> "1251 просмотр"
    data-marker="item-view/today-views"    -> "(+7 сегодня)"
    """
    from bs4 import BeautifulSoup
    out: dict = {"published_at": None, "views_total": None, "views_today": None}
    soup = BeautifulSoup(html, "html.parser")

    node = soup.select_one('[data-marker="item-view/item-date"]')
    if node:
        out["published_at"] = _parse_avito_date(node.get_text(" ", strip=True))

    node = soup.select_one('[data-marker="item-view/total-views"]')
    if node:
        m = re.search(r"(\d[\d\s ]*)", node.get_text(" ", strip=True))
        if m:
            out["views_total"] = int(re.sub(r"\D", "", m.group(1)))

    node = soup.select_one('[data-marker="item-view/today-views"]')
    if node:
        m = re.search(r"\+(\d+)", node.get_text(" ", strip=True))
        if m:
            out["views_today"] = int(m.group(1))
    return out


_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
}


def _parse_avito_date(text: str) -> str | None:
    """'23 сентября в 14:25' -> ISO-строка (год угадывается по месяцу)."""
    if not text:
        return None
    low = text.lower().replace("·", " ")
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+в\s+(\d{1,2}):(\d{2})", low)
    if not m:
        m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", low)
        if not m:
            return None
        day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
        hh = mm = 0
    else:
        day, month_name = int(m.group(1)), m.group(2)
        hh, mm = int(m.group(3)), int(m.group(4))
        now = datetime.now(timezone.utc)
        year = now.year
        # если месяц в будущем — значит прошлый год
        if month_name in _MONTHS and _MONTHS[month_name] > now.month:
            year -= 1
    if month_name not in _MONTHS:
        return None
    try:
        dt = datetime(year, _MONTHS[month_name], day, hh, mm, tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.isoformat()


@app.get("/fetch_lot")
def fetch_lot(avito_id: int):
    """Описание лота через HTTP-клиент парсера (прогретая сессия + solver).

    В 10-20 раз быстрее Playwright; используется enricher'ом как основной
    путь, Playwright остаётся fallback. Возвращает ok=False при блокировке.
    """
    global _lot_parser
    pg = _get_postgres_config({})
    if pg is None:
        raise HTTPException(status_code=503, detail="postgres config missing")
    with _postgres_conn(pg) as conn, conn.cursor() as cur:
        cur.execute("SELECT url FROM avito_original.ads WHERE avito_id = %s", (avito_id,))
        row = cur.fetchone()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail=f"avito_id {avito_id} не найден")
    url = row[0]

    if _lot_parser is None:
        from avito_parser.core import AvitoParse
        from avito_parser.config import load_avito_config
        _lot_parser = AvitoParse(load_avito_config(
            os.environ.get("PARSER_CONFIG", "/app/config/config.toml")))
        logger.info("/fetch_lot: сессия парсера инициализирована")

    html_code = _lot_parser.fetch_data(url=url)
    if not html_code:
        return {"avito_id": avito_id, "ok": False, "description": None,
                "price": None, "title": None,
                "published_at": None, "views_total": None, "views_today": None,
                "reason": "запрос не прошёл (блокировка/ретраи)"}
    desc = _extract_description(html_code)
    price = _extract_price(html_code)
    title = _extract_title(html_code)
    meta = _extract_lot_meta(html_code)

    # сохраняем дату публикации и просмотры в БД (для карточки и тех. сводки)
    try:
        with _postgres_conn(pg) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE avito_original.ads SET published_at=COALESCE(published_at, %s), "
                "views_total=COALESCE(views_total, %s), views_today=COALESCE(views_today, %s) "
                "WHERE avito_id=%s",
                (meta["published_at"], meta["views_total"], meta["views_today"], avito_id))
            conn.commit()
    except Exception as exc:
        logger.warning(f"/fetch_lot: не сохранил метаданные лота: {exc}")

    return {"avito_id": avito_id, "ok": True, "description": desc,
            "price": price, "title": title,
            "published_at": meta["published_at"],
            "views_total": meta["views_total"],
            "views_today": meta["views_today"],
            "reason": None if desc else "страница получена, описание не извлеклось"}


def _extract_price(html: str) -> float | None:
    from bs4 import BeautifulSoup
    el = BeautifulSoup(html, "html.parser").select_one(
        '[data-marker="item-view/item-price"], [data-marker="item-price"], [itemprop="price"]')
    if el:
        digits = "".join(ch for ch in el.get_text() if ch.isdigit())
        if digits:
            return float(digits)
    return None


def _extract_title(html: str) -> str | None:
    from bs4 import BeautifulSoup
    el = BeautifulSoup(html, "html.parser").select_one(
        '[data-marker="item-view/item-title"], h1[data-marker="item-view/title"], h1')
    if el:
        text = el.get_text(separator=" ", strip=True)
        return text or None
    return None