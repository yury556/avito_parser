from abc import ABC, abstractmethod


class ImageStorage(ABC):
    @abstractmethod
    def upload(self, image_bytes: bytes, key: str, content_type: str = "image/jpeg") -> str:
        ...

    @abstractmethod
    def get_url(self, key: str) -> str:
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        ...