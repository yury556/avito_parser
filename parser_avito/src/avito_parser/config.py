import tomllib
from pathlib import Path

import tomli_w

from avito_parser.settings import AvitoConfig, StorageConfig


def load_avito_config(path: str = "config.toml") -> AvitoConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    storage = StorageConfig()
    if "storage" in data:
        storage = StorageConfig(**data["storage"])
    return AvitoConfig(storage=storage, **data["avito"])


def save_avito_config(config: dict):
    with Path("config.toml").open("wb") as f:
        tomli_w.dump(config, f)