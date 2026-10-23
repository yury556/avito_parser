-- ============================================================
-- Migration 010: словарь тегов tag_vocabulary
-- Источник истины для универсальных тегов Jev/DeepSeek.
-- Теги лота хранятся в detail.ads.tags_universal TEXT[] (ключи словаря).
-- ============================================================

CREATE TABLE IF NOT EXISTS avito_original.tag_vocabulary (
    tag_id SERIAL PRIMARY KEY,
    tag_key TEXT UNIQUE NOT NULL,
    grp TEXT NOT NULL,
    label_ru TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'seed',
    status TEXT NOT NULL DEFAULT 'active',
    usage_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tag_voc_grp ON avito_original.tag_vocabulary (grp, status);

-- --- стартовый словарь (seed) ---
INSERT INTO avito_original.tag_vocabulary (tag_key, grp, label_ru) VALUES
    -- состояние
    ('novyy',        'состояние',  'новый'),
    ('kak-novyy',    'состояние',  'как новый'),
    ('bu',           'состояние',  'б/у'),
    ('zapechatan',   'состояние',  'запечатан'),
    ('zapchasti',    'состояние',  'на запчасти'),
    ('narabotka-malaya', 'состояние', 'малая наработка'),
    ('ispolzovalsya', 'состояние', 'использовался'),
    -- дефекты
    ('skol',         'дефект',     'скол'),
    ('treschina',    'дефект',     'трещина'),
    ('ne-rabotaet',  'дефект',     'не работает'),
    ('bityy-piksel', 'дефект',     'битый пиксель'),
    ('vmyatiny',     'дефект',     'вмятины'),
    ('poter-pokrytie', 'дефект',   'потёртости'),
    -- комплектация
    ('polnyy-komplekt', 'комплект', 'полный комплект'),
    ('bez-korobki',  'комплект',   'без коробки'),
    ('v-korobke',    'комплект',   'в коробке'),
    ('oem',          'комплект',   'OEM (без коробки, новый)'),
    -- условия
    ('torg',         'условия',    'торг'),
    ('obmen',        'условия',    'обмен'),
    ('samovyvoz',    'условия',    'самовывоз'),
    ('dostavka',     'условия',    'доставка'),
    ('proverka',     'условия',    'проверка при покупке'),
    -- происхождение
    ('original',     'происхождение', 'оригинал'),
    ('globalnaya-versiya', 'происхождение', 'глобальная версия'),
    ('refurbished',  'происхождение', 'refurbished (восстановлен)'),
    ('kitay-versiya', 'происхождение', 'китайская версия'),
    -- сигналы
    ('neskolko-shtuk', 'сигнал',   'несколько штук'),
    ('obfuskatsiya', 'сигнал',     'обфускация текста'),
    ('garantiya-magazina', 'сигнал', 'гарантия магазина'),
    ('sravnenie-s-drugoy', 'сигнал', 'сравнение с другой моделью'),
    ('sborka-na-zakaz', 'сигнал',  'сборка на заказ'),
    ('komplekt-s-drugim', 'сигнал', 'продаётся комплектом с другим')
ON CONFLICT (tag_key) DO NOTHING;