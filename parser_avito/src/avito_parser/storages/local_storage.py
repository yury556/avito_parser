from pathlib import Path

from avito_parser.storages.base import ImageStorage


class LocalStorage(ImageStorage):
    def __init__(self, base_path: str = "images"):
        self.base_dir = Path(base_path)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def upload(self, image_bytes: bytes, key: str, content_type: str = "image/jpeg") -> str:
        file_path = self.base_dir / key
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(image_bytes)
        return str(file_path)

    def get_url(self, key: str) -> str:
        file_path = self.base_dir / key
        return str(file_path) if file_path.exists() else ""

    def delete(self, key: str) -> None:
        file_path = self.base_dir / key
        if file_path.exists():
            file_path.unlink()