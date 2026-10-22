"""Entry point for python -m avito_parser.

GUI-режим удалён (мёртвый код в docker-режиме). Осталась консольная
сборка объявлений по URL-конфигу.
"""
import logging
import sys

from avito_parser.config import load_avito_config
from avito_parser.core import AvitoParse

DEFAULT_CONFIG = "/app/config/config.toml"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG
    config = load_avito_config(path)
    parser = AvitoParse(config)
    items = parser.parse()
    print(f"Собрано объявлений: {len(items)}")
    for item in items[:20]:
        print(f"  {getattr(item, 'avito_id', '?')} · {getattr(item, 'price', '?')} ₽ · "
              f"{str(getattr(item, 'title', ''))[:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
