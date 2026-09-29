from avito_parser.settings import StorageConfig
from avito_parser.storages.base import ImageStorage
from avito_parser.storages.local_storage import LocalStorage
from avito_parser.storages.none_storage import NoneStorage


def create_storage(config: StorageConfig) -> ImageStorage:
    if config.type == "minio" or config.type == "s3":
        from avito_parser.storages.minio_storage import MinIOStorage
        return MinIOStorage(
            endpoint=config.endpoint,
            access_key=config.access_key,
            secret_key=config.secret_key,
            bucket=config.bucket,
            region=config.region,
        )
    if config.type == "local":
        return LocalStorage(base_path=config.base_path)
    return NoneStorage()