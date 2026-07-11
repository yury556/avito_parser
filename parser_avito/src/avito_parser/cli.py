"""Console (headless) entry point for the parser."""
import time

from loguru import logger

from avito_parser.config import load_avito_config
from avito_parser.core import AvitoParse
from avito_parser.version import VERSION


def main():
    """Run the parser in headless (console) mode."""
    try:
        config = load_avito_config("config.toml")
    except Exception as err:
        logger.error(f"Ошибка загрузки конфига: {err}")
        exit(1)

    logger.info(f"Parser Avito v{VERSION} — консольный режим запущен")

    while True:
        try:
            parser = AvitoParse(config)
            parser.parse()
            if config.one_time_start:
                logger.info("Парсинг завершён (one_time_start)")
                break
            logger.info(f"Пауза {config.pause_general} сек")
            time.sleep(config.pause_general)
        except Exception as err:
            logger.exception(err)
            logger.error(f"Ошибка {err}. Повтор через 30 сек.")
            time.sleep(30)


if __name__ == "__main__":
    main()
