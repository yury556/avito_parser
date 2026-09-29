from io import BytesIO

from loguru import logger
from minio import Minio
from minio.error import S3Error

from avito_parser.storages.base import ImageStorage


class MinIOStorage(ImageStorage):
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str = "avito-images",
        region: str = "us-east-1",
        secure: bool = False,
    ):
        self.endpoint = endpoint.replace('http://', '').replace('https://', '')
        self.bucket = bucket
        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            secure=secure,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
                logger.info("Created bucket: {}", self.bucket)
        except S3Error as e:
            logger.warning("Failed to ensure bucket {}: {}", self.bucket, e)

    def upload(self, image_bytes: bytes, key: str, content_type: str = "image/jpeg") -> str:
        try:
            self.client.put_object(
                self.bucket,
                key,
                data=BytesIO(image_bytes),
                length=len(image_bytes),
                content_type=content_type,
            )
            return self.get_url(key)
        except S3Error as e:
            logger.error("MinIO upload failed for key {}: {}", key, e)
            raise

    def get_url(self, key: str) -> str:
        return f"{self.endpoint}/{self.bucket}/{key}"

    def delete(self, key: str) -> None:
        try:
            self.client.remove_object(self.bucket, key)
        except S3Error as e:
            logger.warning("MinIO delete failed for key {}: {}", key, e)