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
    avito = dict(data["avito"])
    # временно выключенные поиски (секция [url_muted], управляется ботом)
    muted = set(data.get("url_muted", {}).keys())
    if muted and isinstance(avito.get("urls"), list):
        avito["urls"] = [u for u in avito["urls"] if u not in muted]
    return AvitoConfig(storage=storage, **avito)


def save_avito_config(config: dict):
    with Path("config.toml").open("wb") as f:
        tomli_w.dump(config, f)