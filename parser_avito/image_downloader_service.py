"""
Отдельный сервис для скачивания картинок объявлений.

Parser помещает исходные URL в avito_original.image_jobs. Для старых лотов
используются durable-задания типа detail: сервис открывает страницу лота и
ставит найденные изображения в ту же очередь. Сетевые ошибки не блокируют
остальную очередь, а retry управляется через next_retry_at.
"""

import json
import os
import random
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO

from bs4 import BeautifulSoup
from curl_cffi import requests
from loguru import logger
from minio import Minio
from minio.error import S3Error


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
    "max_attempts": int(os.environ.get("IMAGE_JOB_MAX_ATTEMPTS", "8")),
    "retry_base_seconds": int(os.environ.get("IMAGE_RETRY_BASE_SECONDS", "30")),
    "retry_max_seconds": int(os.environ.get("IMAGE_RETRY_MAX_SECONDS", "1800")),
    "job_lease_seconds": int(os.environ.get("IMAGE_JOB_LEASE_SECONDS", "300")),
}

CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


class ImageProcessingError(Exception):
    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


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
    except (S3Error, Exception) as exc:
        logger.warning("MinIO init warning: {}", exc)
        return None


def fetch_page(url: str, timeout: int) -> str:
    """Открытие detail page остаётся последовательным и только для legacy jobs."""
    impersonate = random.choice(["chrome", "edge", "firefox", "safari"])
    version = str(random.randint(142, 147))
    session = requests.Session(impersonate=impersonate)
    try:
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{version}.0.0.0 Safari/537.36"
            ),
        })
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        if resp.status_code in (404, 410):
            raise ImageProcessingError(f"detail page HTTP {resp.status_code}", permanent=True)
        if resp.status_code in (401, 403, 429, 439):
            raise ImageProcessingError(f"detail page blocked: HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp.text
    except ImageProcessingError:
        raise
    except Exception as exc:
        raise ImageProcessingError(f"detail page fetch failed: {exc}") from exc
    finally:
        session.close()


def extract_image_urls(html: str) -> list[str]:
    urls = set()
    soup = BeautifulSoup(html, "html.parser")

    for img in soup.select('[data-marker*="image"] img'):
        for attr in ("src", "data-src"):
            src = img.get(attr)
            if src and src.startswith("https://") and "avito" in src:
                urls.add(src)

    for img in soup.find_all("img"):
        for attr in ("src", "data-src"):
            src = img.get(attr)
            if src and "avito" in src.lower():
                urls.add(src)

    meta = soup.select_one('meta[property="og:image"]')
    if meta and meta.get("content"):
        urls.add(meta["content"])

    return sorted(u for u in urls if re.search(r"avito\.(st|ru)", u))


def download_image(minio_client: Minio, ad_id: int, position: int, url: str) -> dict:
    """Скачивает один CDN-файл, ограничивая размер во время потокового чтения."""
    if minio_client is None:
        raise ImageProcessingError("MinIO is unavailable")

    max_size = SETTINGS["max_image_size_mb"] * 1024 * 1024
    resp = None
    try:
        resp = requests.get(
            url,
            impersonate="chrome",
            timeout=SETTINGS["download_timeout"],
            stream=True,
        )
        if resp.status_code in (404, 410):
            raise ImageProcessingError(f"image HTTP {resp.status_code}", permanent=True)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        ext = CONTENT_TYPE_EXTENSIONS.get(content_type)
        if not ext:
            raise ImageProcessingError(f"unsupported Content-Type: {content_type or 'missing'}", permanent=True)

        content_length = resp.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > max_size:
                    raise ImageProcessingError(f"image Content-Length exceeds {max_size} bytes", permanent=True)
            except ValueError:
                pass

        content = bytearray()
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            if len(content) + len(chunk) > max_size:
                raise ImageProcessingError(f"image stream exceeds {max_size} bytes", permanent=True)
            content.extend(chunk)

        if not content:
            raise ImageProcessingError("empty image response", permanent=True)

        key = f"{ad_id}/{position}.{ext}"
        minio_client.put_object(
            MINIO_CONFIG["bucket"],
            key,
            BytesIO(content),
            len(content),
            content_type=content_type,
        )
        time.sleep(random.uniform(0.1, 0.3))
        return {
            "key": key,
            "url": f"http://{MINIO_CONFIG['endpoint']}/{MINIO_CONFIG['bucket']}/{key}",
            "original_url": url,
            "size_bytes": len(content),
            "content_type": content_type,
        }
    except ImageProcessingError:
        raise
    except Exception as exc:
        raise ImageProcessingError(f"image download/upload failed: {exc}") from exc
    finally:
        if resp is not None:
            resp.close()


def download_and_upload(minio_client: Minio | None, ad_id: int, image_urls: list[str]) -> list[dict]:
    """Совместимый helper: ошибка одного изображения не отменяет остальные."""
    results = []
    for position, url in enumerate(image_urls[:SETTINGS["max_images_per_ad"]]):
        try:
            results.append(download_image(minio_client, ad_id, position, url))
        except ImageProcessingError as exc:
            logger.warning("Failed image {} for ad {}: {}", position, ad_id, exc)
    return results


def enqueue_missing_detail_jobs() -> int:
    """Гарантирует durable fallback для лотов, где parser не получил image_urls."""
    with postgres_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO avito_original.image_jobs
                (avito_id, original_url, position, job_type)
            SELECT a.avito_id, a.url, 0, 'detail'
            FROM avito_original.ads a
            WHERE (a.image_keys IS NULL OR a.image_keys = '[]'::jsonb)
              AND a.url IS NOT NULL AND a.url <> ''
              AND NOT EXISTS (
                  SELECT 1 FROM avito_original.image_jobs j
                  WHERE j.avito_id = a.avito_id
              )
            ON CONFLICT (avito_id, original_url) DO NOTHING
        """)
        return cur.rowcount


def claim_jobs(limit: int) -> list[dict]:
    """Атомарно резервирует ready jobs; lease защищает от зависшего worker."""
    lease_until = datetime.now(timezone.utc) + timedelta(seconds=SETTINGS["job_lease_seconds"])
    with postgres_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT j.id, j.avito_id, j.original_url, j.position, j.job_type,
                   j.attempt_count + 1 AS attempt_count
            FROM avito_original.image_jobs j
            JOIN avito_original.ads a ON a.avito_id = j.avito_id
            WHERE j.status = 'retry'
              AND j.next_retry_at IS NOT NULL
              AND j.next_retry_at <= now()
            ORDER BY a.parsed_at DESC NULLS LAST,
                     j.avito_id DESC, j.position ASC, j.id ASC
            LIMIT %s
            FOR UPDATE OF j SKIP LOCKED
        """, (limit,))
        columns = [desc[0] for desc in cur.description]
        jobs = [dict(zip(columns, row)) for row in cur.fetchall()]
        if jobs:
            cur.execute("""
                UPDATE avito_original.image_jobs
                SET attempt_count = attempt_count + 1,
                    next_retry_at = %s,
                    updated_at = now()
                WHERE id = ANY(%s)
            """, (lease_until, [job["id"] for job in jobs]))
    return jobs


def retry_at(attempt_count: int) -> datetime:
    delay = min(
        SETTINGS["retry_max_seconds"],
        SETTINGS["retry_base_seconds"] * (2 ** max(0, attempt_count - 1)),
    )
    return datetime.now(timezone.utc) + timedelta(seconds=delay)


def finish_job(job: dict, status: str, error: Exception | str | None = None) -> None:
    if status == "retry" and job["attempt_count"] >= SETTINGS["max_attempts"]:
        status = "permanent_failure"
    next_retry_at = retry_at(job["attempt_count"]) if status == "retry" else None
    last_error = str(error)[:2000] if error else None
    with postgres_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE avito_original.image_jobs
            SET status = %s, last_error = %s, next_retry_at = %s, updated_at = now()
            WHERE id = %s AND attempt_count = %s
        """, (status, last_error, next_retry_at, job["id"], job["attempt_count"]))


def process_image_job(minio_client: Minio, job: dict) -> None:
    entry = download_image(minio_client, job["avito_id"], job["position"], job["original_url"])
    entry_json = json.dumps([entry])
    with postgres_conn() as conn, conn.cursor() as cur:
        # Ключ и URL идемпотентны: повтор после сбоя не дублирует image_keys.
        cur.execute("""
            WITH current_ad AS (
                SELECT image_keys FROM avito_original.ads
                WHERE avito_id = %s FOR UPDATE
            ), kept AS (
                SELECT COALESCE(jsonb_agg(item.value ORDER BY item.ord), '[]'::jsonb) AS image_keys
                FROM current_ad
                LEFT JOIN LATERAL jsonb_array_elements(
                    COALESCE(current_ad.image_keys, '[]'::jsonb)
                ) WITH ORDINALITY AS item(value, ord) ON TRUE
                WHERE item.value IS NULL
                   OR (COALESCE(item.value->>'key', '') <> %s
                       AND COALESCE(item.value->>'original_url', '') <> %s)
            )
            UPDATE avito_original.ads a
            SET image_keys = kept.image_keys || %s::jsonb
            FROM kept
            WHERE a.avito_id = %s
        """, (job["avito_id"], entry["key"], job["original_url"], entry_json, job["avito_id"]))
        if cur.rowcount != 1:
            raise ImageProcessingError(f"ad {job['avito_id']} not found")
        cur.execute("""
            UPDATE avito_original.image_jobs
            SET status = 'success', last_error = NULL, next_retry_at = NULL, updated_at = now()
            WHERE id = %s AND attempt_count = %s
        """, (job["id"], job["attempt_count"]))


def process_detail_job(job: dict) -> None:
    time.sleep(SETTINGS["delay_between_ads"])
    html = fetch_page(job["original_url"], SETTINGS["request_timeout"])
    image_urls = extract_image_urls(html)[:SETTINGS["max_images_per_ad"]]
    with postgres_conn() as conn, conn.cursor() as cur:
        if image_urls:
            from psycopg2.extras import execute_values
            execute_values(cur, """
                INSERT INTO avito_original.image_jobs
                    (avito_id, original_url, position, job_type)
                VALUES %s
                ON CONFLICT (avito_id, original_url) DO NOTHING
            """, [(job["avito_id"], url, position, "image")
                  for position, url in enumerate(image_urls)])
        cur.execute("""
            UPDATE avito_original.image_jobs
            SET status = 'success', last_error = NULL, next_retry_at = NULL, updated_at = now()
            WHERE id = %s AND attempt_count = %s
        """, (job["id"], job["attempt_count"]))
    logger.info("Discovered {} images for ad {}", len(image_urls), job["avito_id"])


def process_batch() -> int:
    discovered = enqueue_missing_detail_jobs()
    if discovered:
        logger.info("Created {} missing detail jobs", discovered)
    minio_client = ensure_bucket()
    if minio_client is None:
        return 0

    jobs = claim_jobs(SETTINGS["batch_size"])
    for job in jobs:
        try:
            if job["job_type"] == "detail":
                process_detail_job(job)
            else:
                process_image_job(minio_client, job)
            logger.info("Completed {} job {} for ad {}", job["job_type"], job["id"], job["avito_id"])
        except ImageProcessingError as exc:
            status = "permanent_failure" if exc.permanent else "retry"
            finish_job(job, status, exc)
            logger.warning("Job {} for ad {}: {} ({})", job["id"], job["avito_id"], status, exc)
        except Exception as exc:
            finish_job(job, "retry", exc)
            logger.exception("Unexpected job {} failure: {}", job["id"], exc)
    return len(jobs)


def main():
    logger.info("Image downloader service started")
    logger.info("Settings: {}", SETTINGS)
    while True:
        try:
            count = process_batch()
            if count == 0:
                logger.info("No ready jobs, sleeping {}s", SETTINGS["delay_between_batches"])
            # Сохраняем прежний темп: никакого роста частоты detail-запросов.
            time.sleep(SETTINGS["delay_between_batches"])
        except Exception as exc:
            logger.error("Batch error: {}", exc)
            time.sleep(SETTINGS["delay_between_batches"])


if __name__ == "__main__":
    main()
