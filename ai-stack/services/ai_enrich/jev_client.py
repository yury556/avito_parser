# -*- coding: utf-8 -*-
"""Jev-клиент: быстрая разметка лота через Decisions API (TypeSafe).
Choice: категория + бренд. Noul: теги состояния и сигналы.
Теги берутся из словаря avito_original.tag_vocabulary (status='active').

Оплата только за входные токены (~$0.00004/лот). Output бесплатен.
"""

import os
import urllib.request

JEV_MODEL = os.environ.get("JEV_MODEL", "typesafe/jev-1.14")
JEV_URL = "https://openrouter.ai/api/alpha/decisions"

# категория в Jev → канон в detail.ads (как сложилось исторически)
_CATEGORIES = {
    "videokarta": "видеокарта",
    "operativnaya_pamyat": "оперативная_память",
    "protsessor": "процессор",
    "smartfon": "смартфон",
    "blok_pitaniya": "блок_питания",
    "materinskaya_plata": "материнская_плата",
    "kulery": "кулер",
    "korpus": "корпус",
    "ssd": "ssd",
    "hdd": "hdd",
    "monitor": "монитор",
    "noutbuk": "ноутбук",
    "pritsep": "прицеп",
    "drugoe": "другое",
}

_BRANDS = {
    "nvidia": "NVIDIA", "amd": "AMD", "intel": "Intel",
    "palit": "Palit", "msi": "MSI", "gigabyte": "Gigabyte",
    "asus": "ASUS", "asrock": "ASRock", "gainward": "Gainward",
    "inno3d": "Inno3D", "zotac": "Zotac", "sapphire": "Sapphire",
    "xfx": "XFX", "powercolor": "PowerColor", "colorful": "Colorful",
    "kingston": "Kingston", "corsair": "Corsair", "gskill": "G.Skill",
    "samsung": "Samsung", "crucial": "Crucial", "netac": "Netac",
    "hyperx": "HyperX", "be_quiet": "be quiet!", "chieftec": "Chieftec",
    "thermaltake": "Thermaltake", "deepcool": "Deepcool",
    "aerocool": "Aerocool", "zalman": "Zalman", "noctua": "Noctua",
    "apple": "Apple", "xiaomi": "Xiaomi", "honor": "Honor",
    "huawei": "Huawei", "google": "Google", "oneplus": "OnePlus",
    "realme": "Realme", "poco": "Poco", "vivo": "Vivo",
    "oppo": "Oppo", "noyau": "без бренда/другой",
}

# Noul-вопросы: слаг тега → (вопрос, критерий true, критерий false)
# Точные формулировки важны: Noul-модель отвечает буквально.
_NOULS = {
    "novyy": ("Товар новый и ни разу не использовался?",
              "Товар ни разу не использовался, не распаковывался или не имеет следов эксплуатации",
              "Товар использовался, имеет следы эксплуатации или наработку"),
    "kak-novyy": ("Товар использовался минимально и находится в состоянии как новый?",
                  "Небольшая наработка или тесты, состояние практически нового",
                  "Заметное использование, дефекты или долгий срок службы"),
    "zapechatan": ("Товар запечатан в заводскую упаковку?",
                   "Заводская упаковка не вскрывалась",
                   "Упаковка вскрыта или её нет"),
    "zapchasti": ("Товар продаётся на запчасти или не работает?",
                  "Явно указано: на запчасти, не работает, дефектный",
                  "Товар рабочий и продаётся для использования"),
    "neskolko-shtuk": ("В объявлении продаётся несколько экземпляров товара?",
                       "Указано количество: несколько штук, есть в наличии в нескольких экземплярах",
                       "Продаётся один экземпляр"),
    "garantiya-magazina": ("На ЭТОТ товар есть гарантия от магазина (не от продавца-физлица)?",
                           "Гарантия от магазина или чек на этот товар",
                           "Гарантии нет, гарантия на другой товар или только слова продавца"),
    "sravnenie-s-drugoy": ("Продавец сравнивает этот товар с другой моделью как более выгодной?",
                           "Описание упоминает другую модель как аналог или сравнение в пользу этой",
                           "Сравнения с другими моделями нет"),
    "torg": ("Продавец готов торговаться?",
             "Указано: торг уместен, цена обсуждаема",
             "Торга нет или цена фиксированная"),
    "samovyvoz": ("Возможен самовывоз?",
                  "Самовывоз указан как основной или доступный способ",
                  "Самовывоз не упомянут или невозможен"),
    "dostavka": ("Доступна доставка?",
                 "Доставка указана как доступная",
                 "Доставка не упомянута или продавец только самовывоз"),
}

# приоритет определения condition из Noul-вероятностей
_CONDITION_RULES = [
    ("zapchasti", 0.80, "запчасти"),
    ("novyy", 0.75, "новый"),
    ("kak-novyy", 0.75, "как новый"),
]


def _build_questions(active_tags):
    """Собирает вопросы: 2 Choice + Noul-ы по активным тегам словаря."""
    questions = {
        "category": {
            "type": "choice",
            "instructions": "К какой категории относится ГЛАВНЫЙ товар объявления?",
            "criteria": {k: v.replace("_", " ") for k, v in _CATEGORIES.items()},
        },
        "brand": {
            "type": "choice",
            "instructions": "Какой бренд у главного товара?",
            "criteria": _BRANDS,
        },
    }
    for tag_key in active_tags:
        if tag_key in _NOULS:
            q, ct, cf = _NOULS[tag_key]
            questions[f"tag_{tag_key}"] = {
                "type": "noul", "instructions": q,
                "criteria": {"true": ct, "false": cf},
            }
    return questions


def call_jev(api_key, title, description, active_tags, timeout=25):
    """Один вызов Decisions API. Возвращает dict:
    {ok, category, brand, confidence (min из Choice), tags: {key: p}, usage}
    """
    body = {
        "model": JEV_MODEL,
        "state": {"title": title or "", "description": (description or "")[:3000]},
        "questions": _build_questions(active_tags),
    }
    req = urllib.request.Request(
        JEV_URL,
        data=json_dumps(body),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json_loads(resp.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}
    answers = data.get("answers", {})
    cat_a = answers.get("category", {})
    brand_a = answers.get("brand", {})
    if not cat_a or not brand_a:
        return {"ok": False, "error": "нет ответа Choice", "usage": data.get("usage", {})}
    conf = min(float(cat_a.get("confidence", 0)), float(brand_a.get("confidence", 0)))
    tags = {}
    for tag_key in active_tags:
        a = answers.get(f"tag_{tag_key}")
        if a and "noul" in a:
            tags[tag_key] = float(a["noul"])
    return {
        "ok": True,
        "category": _CATEGORIES.get(cat_a.get("choice"), "другое"),
        "brand": _BRANDS.get(brand_a.get("choice")),
        "confidence": conf,
        "tags": tags,
        "usage": data.get("usage", {}),
    }


def condition_from_tags(tags):
    """Канонический condition из Noul-вероятностей (первое совпавшее правило)."""
    for key, thr, label in _CONDITION_RULES:
        if tags.get(key, 0) >= thr:
            return label
    return "б/у"


def universal_tags(tags, obfuscated, threshold=0.75):
    """Теги ≥ порога + детекция обфускации (модуль normalize)."""
    out = [k for k, p in tags.items() if p >= threshold]
    if obfuscated:
        out.append("obfuskatsiya")
    return out


# маленькие обёртки, чтобы не тянуть json в сигнатуры выше
def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False).encode()


def json_loads(b):
    import json
    return json.loads(b)
