from datetime import datetime
from pathlib import Path
from avito_parser.settings import AvitoConfig

from avito_parser.sheets.base import ResultStorage
from avito_parser.sheets.excel import ExcelStorage
from avito_parser.sheets.composite import CompositeResultStorage, NullResultStorage


def build_result_storage(
    config: AvitoConfig,
    *,
    link_index: int | None = None,
) -> ResultStorage:
    storages: list[ResultStorage] = []

    if config.save_xlsx:
        file_path = _build_excel_path(config, link_index)
        storages.append(ExcelStorage(file_path))

    if not storages:
        return NullResultStorage()

    return CompositeResultStorage(storages)


def _build_excel_path(config: AvitoConfig, link_index: int | None) -> Path:
    base_dir = Path(config.output_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    if config.one_file_for_link and link_index is not None:
        # отдельный файл для каждой ссылки
        return base_dir / f"avito_link_{link_index + 1}_{ts}.xlsx"

    # один файл для всего парсинга
    return base_dir / "avito.xlsx"
