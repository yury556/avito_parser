"""
Отдельный сервис для скачивания картинок объявлений.
Читает avito_original.ads, где image_keys пустой,
парсит детальную страницу, извлекает URL картинок,
скачивает их в MinIO, обновляет image_keys в БД.
"""

import json
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests
from minio import Minio
from minio.error import S3Error
from loguru import logger


POSTGRES_CONFIG = {
    "host": os.environ.get("POSTGRES_HOST", "external-postgres"),
    "port": int(os.environ.get("POSTGRES_PORT", 5432)),
    "dbname": os.environ.get("POSTGRES_DB", "avito_dwh"),
    "user": os.environ.get("POSTGRES_USER", "avito"),
    "password": os.environ.get("POSTGRES_PASSWORD", "avito123"),
}

MINIO_CONFIG = {
    "endpoint": os.environ.get("MINIO_ENDPOINT", "minio:9000"),
    "access_key": os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
    "secret_key": os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
    "bucket": os.environ.get("MINIO_BUCKET", "avito-images"),
    "secure": False,
}

SETTINGS = {
    "batch_size": int(os.environ.get("BATCH_SIZE", "20")),
    "delay_between_ads": float(os.environ.get("DELAY_BETWEEN_ADS", "1.0")),
    "delay_between_batches": float(os.environ.get("DELAY_BETWEEN_BATCHES", "30.0")),
    "max_images_per_ad": int(os.environ.get("MAX_IMAGES_PER_AD", "10")),
    "max_image_size_mb": int(os.environ.get("MAX_IMAGE_SIZE_MB", "10")),
    "download_timeout": int(os.environ.get("DOWNLOAD_TIMEOUT", "30")),
    "request_timeout": int(os.environ.get("REQUEST_TIMEOUT", "20")),
}


@contextmanager
def postgres_conn():
    import psycopg2

    conn = psycopg2.connect(**POSTGRES_CONFIG)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_bucket():
    try:
        client = Minio(
            MINIO_CONFIG["endpoint"],
            access_key=MINIO_CONFIG["access_key"],
            secret_key=MINIO_CONFIG["secret_key"],
            secure=MINIO_CONFIG["secure"],
        )
        if not client.bucket_exists(MINIO_CONFIG["bucket"]):
            client.make_bucket(MINIO_CONFIG["bucket"])
            logger.info("Created MinIO bucket: {}", MINIO_CONFIG["bucket"])
        return client
    except (S3Error, Exception) as e:
        logger.warning("MinIO init warning: {}", e)
        return None


def fetch_page(url: str, timeout: int) -> str | None:
    impersonate = random.choice(["chrome", "edge", "firefox", "safari"])
    version = str(random.randint(142, 147))
    try:
        session = requests.Session(impersonate=impersonate)
        session.headers.update({
            "User-Agent": (
                f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{version}.0.0.0 Safari/537.36"
            ),
        })
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        if resp.status_code in (401, 403, 429):
            logger.warning("Blocked {} for {}", resp.status_code, url)
            return None
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning("Failed to fetch {}: {}", url, e)
        return None


def extract_image_urls(html: str) -> list[str]:
    urls = set()
    soup = BeautifulSoup(html, "html.parser")

    # Avito images in div[data-marker="image-frame/image"]
    for img in soup.select('[data-marker*="image"] img'):
        for attr in ("src", "data-src"):
            src = img.get(attr)
            if src and src.startswith("https://") and "avito" in src:
                urls.add(src)

    # All img tags on the page
    for img in soup.find_all("img"):
        for attr in ("src", "data-src"):
            src = img.get(attr)
            if src and "avito" in src.lower():
                urls.add(src)

    # Og-image meta
    meta = soup.select_one('meta[property="og:image"]')
    if meta and meta.get("content"):
        urls.add(meta["content"])

    # Filter avito image URLs
    result = [u for u in urls if re.search(r"avito\.(st|ru)", u)]
    return result


def download_and_upload(
    minio_client: Minio | None,
    ad_id: int,
    image_urls: list[str],
) -> list[dict]:
    results = []
    max_images = SETTINGS["max_images_per_ad"]
    max_size = SETTINGS["max_image_size_mb"] * 1024 * 1024

    for idx, url in enumerate(image_urls[:max_images]):
        try:
            resp = requests.get(url, impersonate="chrome", timeout=SETTINGS["download_timeout"])
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "image/jpeg")
            content = resp.content

            if len(content) > max_size:
                logger.warning("Image {} for ad {} too large ({} MB)", idx, ad_id, len(content) // (1024 * 1024))
                continue

            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(content_type, "jpg")
            key = f"{ad_id}/{idx}.{ext}"

            if minio_client:
                minio_client.put_object(
                    MINIO_CONFIG["bucket"],
                    key,
                    BytesIO(content),
                    len(content),
                    content_type=content_type,
                )

            results.append({
                "key": key,
                "url": f"http://{MINIO_CONFIG['endpoint']}/{MINIO_CONFIG['bucket']}/{key}" if minio_client else "",
                "original_url": url,
                "size_bytes": len(content),
                "content_type": content_type,
            })

            time.sleep(random.uniform(0.1, 0.3))
        except Exception as e:
            logger.warning("Failed to download image {} for ad {}: {}", idx, ad_id, e)
            continue

    return results


def process_batch() -> int:
    minio_client = ensure_bucket()

    with postgres_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT avito_id, url
                FROM avito_original.ads
                WHERE (image_keys IS NULL OR image_keys = '[]'::jsonb)
                  AND url IS NOT NULL
                LIMIT %s
            """, (SETTINGS["batch_size"],))
            rows = cur.fetchall()

    if not rows:
        return 0

    processed = 0
    for ad_id, ad_url in rows:
        logger.info("Processing ad {}: {}", ad_id, ad_url)
        time.sleep(SETTINGS["delay_between_ads"])

        html = fetch_page(ad_url, SETTINGS["request_timeout"])
        if not html:
            logger.warning("Could not fetch {}, skipping", ad_url)
            continue

        image_urls = extract_image_urls(html)
        if not image_urls:
            logger.info("No images found for ad {}", ad_id)
            image_keys = []
        else:
            logger.info("Found {} images for ad {}", len(image_urls), ad_id)
            image_keys = download_and_upload(minio_client, ad_id, image_urls)

        with postgres_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE avito_original.ads
                    SET image_keys = %s::jsonb
                    WHERE avito_id = %s
                """, (json.dumps(image_keys), ad_id))

        processed += 1
        logger.info("Completed ad {} ({} images)", ad_id, len(image_keys))

    return processed


def main():
    logger.info("Image downloader service started")
    logger.info("Settings: {}", SETTINGS)

    while True:
        try:
            count = process_batch()
            if count > 0:
                logger.info("Processed {} ads in this batch", count)
            else:
                logger.info("No pending ads, sleeping {}s", SETTINGS["delay_between_batches"])
        except Exception as e:
            logger.error("Batch error: {}", e)

        time.sleep(SETTINGS["delay_between_batches"])


if __name__ == "__main__":
    main()