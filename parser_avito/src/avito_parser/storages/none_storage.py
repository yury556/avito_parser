from avito_parser.storages.base import ImageStorage


class NoneStorage(ImageStorage):
    def upload(self, image_bytes: bytes, key: str, content_type: str = "image/jpeg") -> str:
        return ""

    def get_url(self, key: str) -> str:
        return ""

    def delete(self, key: str) -> None:
        pass