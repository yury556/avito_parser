# -*- coding: utf-8 -*-
"""Нормализация юникод-двойников: продавцы Авито намешивают в слова
кириллицу и латинских двойников (Пeрвая = кириллица + латинская 'e'),
чтобы обойти поиск и дедуп.

Правило: обрабатываем ТОЛЬКО слова, где кириллица намешана с латиницей.
Чистые латинские слова (RTX, Palit, DDR4, i5) не трогаем — иначе сломаем
названия техники. Чистые кириллические слова тоже не трогаем.
"""

# Латинские буквы, визуально неотличимые от кириллицы
_LOOKALIKE = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х",
    "y": "у", "k": "к", "m": "м", "h": "н", "t": "т", "b": "в",
    "A": "А", "C": "С", "E": "Е", "O": "О", "P": "Р", "X": "Х",
    "Y": "У", "K": "К", "M": "М", "H": "Н", "T": "Т", "B": "В",
    # греческие и украинские двойники (уже не латиница, тоже чистим)
    "ο": "о", "Ο": "О", "α": "а", "Α": "А", "ρ": "р", "Ρ": "Р",
    "χ": "х", "Χ": "Х", "κ": "к", "Κ": "К", "τ": "т", "Τ": "Т",
    "і": "и", "І": "И", "ѕ": "з", "ј": "й",
}

_CYR = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
           "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")


def _fix_word(word):
    """Слово с намешанной кириллицей и латиницей → чистая кириллица."""
    has_cyr = any(ch in _CYR for ch in word)
    has_lookalike = any(ch in _LOOKALIKE for ch in word)
    if not (has_cyr and has_lookalike):
        return word, False  # чистое слово — не трогаем
    fixed = "".join(_LOOKALIKE.get(ch, ch) or ch for ch in word)
    return fixed, True


def normalize_text(s):
    """Разбиваем на слова, чиним только гибридные слова.
    Возвращает (исправленный_текст, была_ли_обфускация)."""
    if not s:
        return s, False
    out, obfuscated = [], False
    for w in s.split(" "):
        fixed, ob = _fix_word(w)
        out.append(fixed)
        obfuscated = obfuscated or ob
    return " ".join(out), obfuscated


def normalize_lot(title, description):
    """Нормализует заголовок и описание лота.
    Возвращает (title, description, obfuscated: bool)."""
    t, ob1 = normalize_text(title or "")
    d, ob2 = normalize_text(description or "")
    return t, d, (ob1 or ob2)
