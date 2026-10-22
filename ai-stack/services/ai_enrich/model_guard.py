"""Детерминированная валидация модели, извлечённой LLM.

Проблема: LLM иногда называет не ту модель, что в объявлении
(например, i5-13600KF -> «Core i5-14400F», хотя в URL-slug — 13600kf).
Такие ошибки портят медианы и реестры.

Идея: URL-slug Авито генерируется из заголовка продавца, поэтому в нём
именно то, что реально продаётся. Первый «чип» (i5-13600kf, rtx 4060ti,
ddr4 32gb) в slug — главный товар объявления.

Правила:
  * чип в slug и чип в модели одного типа, но разные -> модель исправляется
    на slug-версию (чип в названии заменяется, бренд/суффиксы сохраняются);
  * в модели чипа нет, а в slug есть -> модель дополняется: «Fury Beast (DDR4 32GB)»;
  * типы разные (комплекты: i5 + плата + 32GB) -> модель не трогаем;
  * в slug чипа нет -> модель не трогаем.

Ничего не зависит от LLM: работает на slug'е, который приходит из БД.
"""

import re
from typing import NamedTuple, Optional

# (тип, regex). Порядок важен: GPU -> CPU -> RAM
_CHIP_PATTERNS = (
    ("gpu-nv", re.compile(r"\b(rtx|gtx)[\s_-]*(\d{4})(?:\s?(ti|super))?\b")),
    ("gpu-amd", re.compile(r"\brx[\s_-]*(\d{4})(?:\s?xt)?\b")),
    ("cpu-intel", re.compile(r"\b(i[3579])[\s_-]*(\d{4,5})([a-z]{1,2})?\b")),
    ("cpu-amd", re.compile(r"\bryzen[\s_-]*([579])[\s_-]*(\d{4})(?:\s?([a-z0-9]{0,4}))?\b")),
    ("ram", re.compile(r"\bddr([345])[\s_-]*(\d{1,2})[\s_-]*gb\b")),
)

# Человекочитаемые названия типов (для логов)
_CHIP_KIND_RU = {
    "gpu-nv": "видеокарта NVIDIA",
    "gpu-amd": "видеокарта AMD",
    "cpu-intel": "процессор Intel",
    "cpu-amd": "процессор AMD",
    "ram": "оперативная память",
}


class Chip(NamedTuple):
    kind: str
    raw: str      # как написано в тексте
    key: str      # нормализованный ключ для сравнения


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def find_chips(text: str) -> list[Chip]:
    """Все «чипы» в тексте, по порядку появления."""
    if not text:
        return []
    low = text.lower()
    hits = []
    for kind, pat in _CHIP_PATTERNS:
        for m in pat.finditer(low):
            hits.append((m.start(), m.end(), Chip(kind, m.group(0), _key(m.group(0)))))
    hits.sort()
    # убираем пересечения (например, один чип найден двумя паттернами)
    out: list[Chip] = []
    last_end = -1
    for start, end, chip in hits:
        if start < last_end:
            continue
        out.append(chip)
        last_end = end
    return out


def slug_text(url: str) -> str:
    """Человекочитаемый текст из URL-слага: без категории, без id и query."""
    if not url:
        return ""
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    seg = path.rsplit("/", 1)[-1]
    # отрезаем хвостовой id объявления
    seg = re.sub(r"_\d{6,}$", "", seg)
    # подчёркивания -> пробелы; дефисы сохраняем (i5-13600kf)
    return seg.replace("_", " ").lower()


def first_chip_from_slug(url: str) -> Optional[Chip]:
    """Первый чип в slug'е — главный товар объявления."""
    chips = find_chips(slug_text(url))
    return chips[0] if chips else None


def validate_model(model: Optional[str], url: Optional[str]) -> tuple[str, str, str, str]:
    """Возвращает (model, model_ai, chip, note).

    model    — итоговая модель (исправленная при необходимости)
    model_ai — модель как отдал LLM (для аудита)
    chip     — канонический чип товара (для группировок)
    note     — что именно поправили (пусто, если всё чисто)
    """
    model_ai = (model or "").strip()
    chip = ""
    note = ""

    slug_chip = first_chip_from_slug(url or "")
    model_chips = find_chips(model_ai)

    if slug_chip is None:
        chip = model_chips[0].raw if model_chips else ""
        return model_ai, model_ai, chip, note

    if not model_chips:
        # модель без чипа (например «Fury Beast») -> дополняем чипом из slug
        if model_ai:
            new_model = f"{model_ai} ({slug_chip.raw})"
        else:
            new_model = slug_chip.raw
        note = f"дополнено чипом из slug: {slug_chip.raw}"
        return new_model, model_ai, slug_chip.raw, note

    m_chip = model_chips[0]
    if m_chip.kind == slug_chip.kind and m_chip.key != slug_chip.key:
        # LLM назвал другой чип того же типа -> заменяем чип в модели,
        # сохраняя бренд и прочие слова названия
        new_model = re.sub(re.escape(m_chip.raw), slug_chip.raw, model_ai, count=1, flags=re.I)
        note = (f"модель исправлена по slug: {m_chip.raw} -> {slug_chip.raw} "
                f"({_CHIP_KIND_RU.get(m_chip.kind, m_chip.kind)})")
        return new_model, model_ai, slug_chip.raw, note

    # чипы совпали или типы разные (комплект: i5 + плата + 32GB) -> не трогаем
    chip = m_chip.raw
    return model_ai, model_ai, chip, note
