from __future__ import annotations

from typing import Optional

import requests
from loguru import logger

from avito_parser.settings import StorageConfig
from avito_parser.storages.base import ImageStorage


class ImageDownloader:
    def __init__(self, storage: ImageStorage, config: StorageConfig):
        self.storage = storage
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/140.0.0.0 Safari/537.36",
        })

    def download_and_store(
        self,
        avito_id: int,
        image_urls: list[str],
    ) -> list[dict]:
        results: list[dict] = []
        max_images = self.config.max_images_per_ad
        timeout = self.config.download_timeout
        max_size = self.config.max_image_size_mb * 1024 * 1024

        for index, url in enumerate(image_urls):
            if index >= max_images:
                logger.warning("Skipping image {} for ad {} (max {} per ad)", index, avito_id, max_images)
                break

            try:
                resp = self.session.get(url, timeout=timeout, stream=True)
                resp.raise_for_status()

                content_type = resp.headers.get("Content-Type", "")
                ext = self._ext_to_extension(content_type, url)

                content = resp.content
                if len(content) > max_size:
                    logger.warning("Image {} for ad {} exceeds max size ({} > {} MB), skipping",
                                   index, avito_id, len(content) // (1024 * 1024), self.config.max_image_size_mb)
                    continue

                key = f"{avito_id}/{index}.{ext}"
                stored_url = self.storage.upload(content, key, content_type=content_type)

                results.append({
                    "key": key,
                    "url": stored_url,
                    "original_url": url,
                    "size_bytes": len(content),
                    "content_type": content_type,
                })
            except requests.RequestException as e:
                logger.warning("Failed to download image {} for ad {}: {}", index, avito_id, e)
                continue
            except Exception as e:
                logger.error("Unexpected error processing image {} for ad {}: {}", index, avito_id, e)
                continue

        return results

    @staticmethod
    def _ext_to_extension(content_type: str, url: str) -> str:
        mapping = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }
        ext = mapping.get(content_type)
        if ext:
            return ext
        if "." in url:
            return url.rsplit(".", 1)[-1].split("?")[0].split("/")[0][:6].lower()
        return "jpg"