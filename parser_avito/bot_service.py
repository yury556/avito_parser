"""Интерактивный бот @MyParser_ybirk_bot: алерты + пульт управления парсером.

Команды:
  /status            — сводка системы (парсер, enricher, данные)
  /top               — топ-5 самых выгодных активных лотов
  /add <url>         — добавить URL мониторинга в config.toml (с бэкапом)
  /urls              — список URL + сколько лотов собрал каждый за 24 ч
  /remove N confirm  — удалить URL по номеру из /urls
  /mute N            — пауза алертов лотов на N часов (падения не глушатся)
  /unmute            — снять паузу
  /help              — список команд

Long-polling через urllib — без внешних зависимостей.
"""
import html as html_mod
import json
import os
import time
import tomllib
import urllib.parse
import urllib.request

import psycopg2

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
CONFIG_PATH = os.environ.get("PARSER_CONFIG", "/app/config/config.toml")
BACKUP_DIR = os.environ.get("BOT_BACKUP_DIR", "/shared")
MUTE_FILE = os.environ.get("BOT_MUTE_FILE", "/shared/bot_mute_until.txt")
MAX_URLS = 30

DB = dict(
    host=os.environ.get("POSTGRES_HOST", "external-postgres"),
    port=os.environ.get("POSTGRES_PORT", "5432"),
    dbname=os.environ.get("POSTGRES_DB", "avito_dwh"),
    user=os.environ.get("POSTGRES_USER", "avito"),
    password=os.environ.get("POSTGRES_PASSWORD", ""),
)

HELP = (
    "Команды бота:\n"
    "/status — сводка системы\n"
    "/top — топ-5 самых выгодных активных лотов\n"
    "/lot <ID> — карточка объявления по ID\n"
    "/menu — настройки бота (регион, уведомления)\n"
    "/checks — последние 20 проверок лотов\n"
    "/tech <ID> — технические свойства лота (обогащение, запросы, цена)\n"
    "/add <url> — добавить URL мониторинга\n"
    "/urls — список URL и их сбор за 24 ч\n"
    "/remove N confirm — удалить URL номер N\n"
    "/mute N — пауза алертов лотов на N часов\n"
    "/unmute — снять паузу\n"
    "/off — отключить ВСЕ уведомления (и кнопка под каждым сообщением)\n"
    "/on — включить уведомления обратно"
)

RESULT_MARKS = {"жив": "✅", "закрыт": "⚫", "таймаут": "❌", "блокировка": "🚫"}


def _lot_card(avito_id: int, refresh: bool = False) -> str:
    """Карточка объявления из БД (обновляет через /fetch_lot при refresh)."""
    if refresh:
        try:
            req = urllib.request.Request(
                f"http://localhost:8000/fetch_lot" if False else
                f"{os.environ.get('PARSER_URL', 'http://avito-parser-service:8000')}/fetch_lot?avito_id={avito_id}")
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            if data.get("description") and len(data["description"]) > 260:
                with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
                    cur.execute("UPDATE avito_original.ads SET description=%s WHERE avito_id=%s",
                                (data["description"], avito_id))
                    cur.execute("UPDATE detail.ads SET last_checked_at=now() WHERE avito_id=%s", (avito_id,))
            fresh = "🔄 Свежая проверка: " + (
                f"цена {data['price']:,.0f} ₽, живой" if data.get("price") else
                f"{data.get('reason') or 'нет данных'}")
        except Exception as exc:
            fresh = f"🔄 Проверка не удалась: {exc}"
    else:
        fresh = None
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute("""
            WITH last AS (
                SELECT DISTINCT ON (avito_id) avito_id, price_rub, parsed_at
                FROM midraw.avito_ads WHERE price_rub > 0
                ORDER BY avito_id, parsed_at DESC)
            SELECT d.model, d.brand, d.state, l.price_rub, l.parsed_at,
                   m.description, m.url, m.location, d.category,
                   coalesce(NULLIF(a.title,''), m.title) AS lot_title
            FROM detail.ads d
            JOIN last l ON l.avito_id = d.avito_id
            JOIN midraw.avito_ads m ON m.avito_id = d.avito_id
            LEFT JOIN avito_original.ads a ON a.avito_id = d.avito_id
            WHERE d.avito_id = %s AND m.avito_id = %s
            LIMIT 1""", (avito_id, avito_id))
        row = cur.fetchone()
    if not row:
        return f"⚠️ Лот {avito_id} не найден в базе."
    model, brand, state, price, seen, desc, url, location, category, lot_title = row
    med = sql("""
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY price_rub)
        FROM detail.ads WHERE category = %s AND state='активно' AND price_rub > 0
    """, (category,))
    med = med[0][0] if med else None
    pct = f" ({(float(price)/float(med)-1)*100:+.0f}% от медианы {float(med):,.0f} ₽)" if med and price else ""
    desc_txt = (desc or "").strip()
    if len(desc_txt) > 2200:
        desc_txt = desc_txt[:2200] + "…"
    esc = lambda s: html_mod.escape(str(s or "?"))
    text = (
        f"📦 <b>{html_mod.escape((lot_title or brand or '')[:90])}</b>\n"
        + (f"<i>{html_mod.escape((brand or '') + ' ' + (model or ''))[:90]}</i>\n"
           if (brand or model) else "")
        + f"🟡 Категория: {esc(category)} · {esc(location)}\n"
        f"💰 Цена: <b>{float(price):,.0f} ₽</b>{pct}\n"
        f"🏷 Статус: {esc(state)} · видели {seen:%d.%m %H:%M}\n"
        f"\n📝 <b>Описание</b> ({len(desc or '')} симв.):\n"
        f"{html_mod.escape(desc_txt) if desc_txt else '<i>(описание ещё не собрано)</i>'}\n"
    )
    if fresh:
        text = fresh + "\n\n" + text
    return text, url


def cmd_checks() -> str:
    rows = sql("""
        SELECT c.avito_id, c.method, c.duration_s, c.result, c.price,
               to_char(c.ts, 'HH24:MI')
        FROM avito_original.check_log c
        ORDER BY c.ts DESC LIMIT 20
    """)
    if not rows:
        return "Журнал проверок пуст (check_log ещё не заполнялся)."
    lines = []
    for avito_id, method, dur, result, price, tm in rows:
        mark = RESULT_MARKS.get(result, "•")
        pr = f" · {float(price):,.0f} ₽" if price else ""
        lines.append(f"{mark} {avito_id} · {method} · {dur:.1f}с · {tm} · {result}{pr}")
    return "🔍 Последние проверки:\n\n" + "\n".join(lines)


def api_call(method: str, payload: dict | None = None) -> dict | None:
    data = json.dumps(payload).encode() if payload else None
    try:
        req = urllib.request.Request(
            API + "/" + method,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except Exception as exc:
        print(f"api_call {method} failed: {exc}", flush=True)
        return None


def reply(chat_id: int, text: str, mute_button: bool = False, url_button: str | None = None,
         markup: dict | None = None) -> None:
    payload: dict = {"chat_id": chat_id, "text": text,
                     "parse_mode": "HTML",
                     "disable_web_page_preview": True}
    payload = {k: v for k, v in payload.items() if v is not None}
    kb = []
    if url_button:
        kb.append([{"text": "🛒 Открыть на Авито", "url": url_button}])
    if mute_button:
        kb.append([{"text": "🔊 Включить уведомления", "callback_data": "unmute_all"}])
    if markup:
        kb = markup
    if kb:
        payload["reply_markup"] = {"inline_keyboard": kb}
    api_call("sendMessage", payload)


def sql(query: str, params=None):
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(query, params or None)
        return cur.fetchall()


# ---------------------------------------------------------------- /status
def cmd_status() -> str:
    parser_last, per_hour, enricher_last, active, total = sql("""
        SELECT (SELECT max(parsed_at) FROM avito_original.ads),
               (SELECT count(*) FROM avito_original.ads
                 WHERE parsed_at > now() - interval '1 hour'),
               (SELECT max(last_checked_at) FROM detail.ads),
               (SELECT count(*) FROM detail.ads WHERE state = 'активно'),
               (SELECT count(*) FROM detail.ads)
    """)[0]
    parser_min = f"{(time.time() - parser_last.timestamp()) / 60:.0f} мин назад" if parser_last else "?"
    enricher_min = f"{(time.time() - enricher_last.timestamp()) / 60:.0f} мин назад" if enricher_last else "?"
    parser_ok = "🟢" if parser_last and parser_last.timestamp() > time.time() - 5400 else "🔴"
    return (
        f"{parser_ok} Парсер: последняя запись {parser_min} ({parser_last:%H:%M})\n"
        f"   за час: {per_hour} лотов\n"
        f"🟡 Enricher: проверка {enricher_min} назад\n"
        f"📦 База: {total} лотов, активных {active}"
    )


def cmd_tech(avito_id: int) -> str:
    """Техническая сводка по лоту: обогащение, проверки, запросы, цена, просмотры."""
    row = sql("""
        SELECT d.model, d.model_ai, d.chip, d.category, d.brand, d.state,
               d.ai_model, d.ai_version, d.ai_processed_at, d.ai_status,
               d.ai_duration_ms, d.ai_attempts, d.price_rub, d.last_checked_at,
               a.published_at, a.views_total, a.views_today,
               length(coalesce(a.description,'')) AS desc_len,
               coalesce(NULLIF(a.title,''), d.input_title) AS lot_title
        FROM detail.ads d
        LEFT JOIN avito_original.ads a ON a.avito_id = d.avito_id
        WHERE d.avito_id = %s
    """, (avito_id,))
    if not row:
        return f"⚠️ Лот {avito_id} не найден в базе."
    (model, model_ai, chip, category, brand, state, ai_model, ai_version,
     ai_at, ai_status, ai_ms, ai_attempts, price, last_checked, published_at,
     v_total, v_today, desc_len, lot_title) = row[0]

    # проверки
    checks = sql("""
        SELECT count(*), max(ts) FROM avito_original.check_log WHERE avito_id = %s
    """, (avito_id,))[0]
    n_checks, last_check = checks
    last_one = sql("""
        SELECT method, duration_s, result FROM avito_original.check_log
        WHERE avito_id = %s ORDER BY ts DESC LIMIT 1
    """, (avito_id,))
    last_method, last_dur, last_result = (last_one[0] if last_one else (None, None, None))

    # запросы к странице лота
    fetches = sql("""
        SELECT count(*), count(*) FILTER (WHERE ok),
               count(*) FILTER (WHERE kind='description'),
               count(*) FILTER (WHERE kind='check'),
               max(ts), round(avg(duration_s)::numeric, 1)
        FROM avito_original.fetch_log WHERE avito_id = %s
    """, (avito_id,))[0]
    n_fetch, n_fetch_ok, n_desc, n_chk, last_fetch, avg_dur = fetches

    # сколько раз видели в выдаче + история цен
    seen = sql("""
        SELECT count(*), min(parsed_at), max(parsed_at)
        FROM midraw.avito_ads WHERE avito_id = %s
    """, (avito_id,))[0]
    n_seen, first_seen, last_seen = seen

    prices = sql("""
        SELECT min(price_rub), max(price_rub), count(*)
        FROM midraw.avito_ads WHERE avito_id = %s AND price_rub > 0
    """, (avito_id,))[0]
    p_min, p_max, n_prices = prices

    hist = sql("""
        SELECT price_rub FROM midraw.avito_ads
        WHERE avito_id = %s AND price_rub > 0
        ORDER BY parsed_at DESC LIMIT 5
    """, (avito_id,))
    hist_txt = " → ".join(f"{float(p):,.0f}" for p, in hist) if hist else "—"

    def ago(ts):
        if not ts:
            return "—"
        m = (time.time() - ts.timestamp()) / 60
        if m < 60:
            return f"{m:.0f} мин назад"
        if m < 1440:
            return f"{m / 60:.0f} ч назад"
        return f"{m / 1440:.0f} дн назад"

    def esc(s) -> str:
        return html_mod.escape(str(s or "—"))

    L = []
    if lot_title:
        L.append(f"📌 <b>{html_mod.escape(str(lot_title)[:100])}</b>")
    L.append(f"🔧 <b>Лот {avito_id}</b> — тех. свойства")
    L.append("")

    L.append("🤖 <b>Обогащение (AI)</b>")
    L.append(f"Модель AI: <b>{esc(ai_model)}</b> · промпт: <code>{esc(ai_version)}</code>")
    L.append(f"Обработано: {ai_at:%d.%m %H:%M} ({ago(ai_at)}) · статус: <b>{esc(ai_status)}</b>")
    if ai_ms:
        L.append(f"Длительность запроса: <b>{ai_ms / 1000:.1f} с</b> · попыток: {ai_attempts or 1}")
    else:
        L.append("Длительность запроса: <i>нет данных</i>")
    L.append(f"Модель товара: <b>{esc(model)}</b>"
             + (f" <i>(из slug: {esc(chip)})</i>" if chip else ""))
    if model_ai and model_ai != model:
        L.append(f"Было от AI: <s>{esc(model_ai)}</s>")
    L.append(f"Категория: {esc(category)} · Бренд: {esc(brand)}")
    L.append(f"Описание собрано: {desc_len or 0} симв.")

    L.append("")
    L.append("📡 <b>Проверки</b>")
    L.append(f"Всего проверок: <b>{n_checks}</b>")
    if n_checks:
        L.append(f"Последняя: {last_check:%d.%m %H:%M} ({ago(last_check)}) · "
                 f"{esc(last_method)} · {last_dur:.1f} с → <b>{esc(last_result)}</b>")
    L.append(f"Запросов к странице: <b>{n_fetch}</b> (успешных {n_fetch_ok}, "
             f"описаний {n_desc}, проверок {n_chk})")
    if last_fetch:
        L.append(f"Последний запрос: {last_fetch:%d.%m %H:%M} ({ago(last_fetch)})"
                 + (f" · в среднем {avg_dur} с" if avg_dur else ""))
    L.append(f"Видно в выдаче: <b>{n_seen}</b> раз"
             + (f" (первое {first_seen:%d.%m}, последнее {last_seen:%d.%m})"
                if n_seen else ""))

    L.append("")
    L.append("💰 <b>Цена</b>")
    L.append(f"Сейчас: <b>{float(price):,.0f} ₽</b>" if price else "Сейчас: —")
    L.append(f"Диапазон: {float(p_min):,.0f} – {float(p_max):,.0f} ₽ · замеров: {n_prices}"
             if p_min else "Диапазон: <i>нет данных</i>")
    L.append(f"История: {hist_txt}")

    L.append("")
    L.append("👁 <b>Просмотры</b>")
    if v_total is not None:
        L.append(f"Всего: <b>{v_total}</b>" + (f" (+{v_today} сегодня)" if v_today else ""))
    else:
        L.append("Нет данных (страница лота ещё не открывалась)")
    L.append(f"Статус: <b>{esc(state)}</b>"
             + (f" · проверено {ago(last_checked)}" if last_checked else ""))
    if published_at:
        L.append(f"Опубликовано: {published_at}")

    return "\n".join(L)


# ---------------------------------------------------------------- /top
def cmd_menu() -> str:
    """Меню бота: фильтр региона, уведомления, статус."""
    region = _get_setting("region_filter") or "all"
    muted = _get_setting("mute_all") == "1"
    region_txt = "📍 Только СПб" if region == "spb" else "🌐 Все города"
    mute_txt = "🔇 Уведомления выключены" if muted else "🔔 Уведомления включены"
    return (f"⚙️ <b>Настройки бота</b>\n\n"
            f"Регион: <b>{'📍 Только СПб' if region == 'spb' else '🌐 Все города'}</b>\n"
            f"Уведомления: <b>{'🔇 выключены' if muted else '🔔 включены'}</b>\n\n"
            f"Нажми кнопку, чтобы переключить:")


def _menu_markup() -> list:
    """Inline-кнопки для меню."""
    region = _get_setting("region_filter") or "all"
    muted = _get_setting("mute_all") == "1"
    region_btn = "🌐 Все города" if region == "spb" else "📍 Только СПб"
    mute_btn = "🔊 Включить уведомления" if muted else "🔇 Отключить уведомления"
    return [
        [{"text": region_btn, "callback_data": "toggle_region"}],
        [{"text": mute_btn, "callback_data": "toggle_mute" if muted else "mute_all"}],
    ]


def cmd_top() -> str:
    rows = sql("""
        WITH med AS (
            SELECT d2.category, percentile_cont(0.5)
                   WITHIN GROUP (ORDER BY d2.price_rub) AS med
            FROM detail.ads d2
            WHERE d2.state = 'активно' AND d2.price_rub > 0
            GROUP BY d2.category
        )
        SELECT d.model, m.price_rub, med.med, m.url, d.avito_id
        FROM detail.ads d
        JOIN midraw.avito_ads m ON m.avito_id = d.avito_id
        JOIN med ON med.category = d.category
        WHERE d.state = 'активно' AND d.price_rub > 0
          AND d.price_rub < med.med * 0.8
        ORDER BY (m.price_rub / med.med) ASC
        LIMIT 5
    """)
    if not rows:
        return "Сейчас нет активных лотов дешевле медианы на 20%+."
    lines, buttons = [], []
    for i, (model, price, med, url, avito_id) in enumerate(rows, 1):
        pct = int(round((float(price) / float(med) - 1) * 100))
        lines.append(f"{i}. <b>{(model or '?')[:40]}</b> — {price:,.0f} ₽ ({pct:+d}% от {med:,.0f} ₽)\n   ID {avito_id}")
        buttons.append([[{"text": f"🛒 {i}", "url": url}]])
    api_call("sendMessage", {
        "chat_id": ADMIN_CHAT_ID,
        "text": "🏆 Топ-5 выгодных активных лотов:\n\n" + "\n".join(lines),
        "reply_markup": {"inline_keyboard": buttons},
    })
    return ""  # сообщение уже отправлено с кнопками


# ---------------------------------------------------------------- config.toml
def _read_config_urls() -> list[str]:
    with open(CONFIG_PATH, "rb") as f:
        data = tomllib.load(f)
    return list(data.get("avito", {}).get("urls", []))


def _backup_config() -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BACKUP_DIR, f"config.toml.bot_{ts}.bak")
    with open(CONFIG_PATH, "rb") as src, open(path, "wb") as dst:
        dst.write(src.read())
    return path


def _rewrite_config_urls(new_urls: list[str]) -> None:
    """Перезаписывает блок urls = [ ... ] в секции [avito].

    ВАЖНО: записывает НА МЕСТЕ (truncate), без tmp+replace — контейнер
    парсера держит bind-mount файла по inode, замена файла оторвала бы
    маунт и парсер не увидел бы новые URL.
    """
    with open(CONFIG_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    start = next(i for i, l in enumerate(lines) if l.strip().startswith("urls = ["))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "]")
    block = ["urls = [\n"]
    for u in new_urls:
        block.append(f'    "{u}",\n')
    block.append("]\n")
    lines[start:end + 1] = block
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


def cmd_add(url: str) -> str:
    url = url.strip().strip('"')
    host = urllib.parse.urlparse(url).netloc
    if "avito.ru" not in host:
        return "❌ Это не ссылка на avito.ru."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    urls = _read_config_urls()
    if url in urls:
        return "⚠️ Такой URL уже есть в списке."
    if len(urls) >= MAX_URLS:
        return f"⚠️ Лимит {MAX_URLS} URL. Сначала /remove."
    backup = _backup_config()
    _rewrite_config_urls(urls + [url])
    return (
        f"✅ URL добавлен ({len(urls) + 1}-й).\n"
        f"Парсер перечитывает config.toml каждый цикл — подхватит сам.\n"
        f"Бэкап: {os.path.basename(backup)}"
    )


def cmd_remove(num: int, confirm: bool) -> str:
    urls = _read_config_urls()
    if not 1 <= num <= len(urls):
        return f"❌ Номер 1..{len(urls)}. Смотри /urls"
    if not confirm:
        host = urllib.parse.urlparse(urls[num - 1]).netloc
        return f"Удалить URL #{num} ({host})?\nПодтверди: /remove {num} confirm"
    backup = _backup_config()
    removed = urls.pop(num - 1)
    _rewrite_config_urls(urls)
    return f"✅ URL #{num} удалён ({len(urls)} осталось).\nБэкап: {os.path.basename(backup)}"


def cmd_urls() -> str:
    urls = _read_config_urls()
    stats = dict(sql("""
        SELECT source_url, count(*) FROM raw.avito_ads_csv
        WHERE loaded_at > now() - interval '24 hours'
        GROUP BY source_url
    """))
    lines = []
    for i, u in enumerate(urls, 1):
        n = stats.get(u, 0)
        q = urllib.parse.unquote_plus(
            urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("q", [""])[0]
        )
        label = q or urllib.parse.urlparse(u).path.rsplit("/", 1)[-1][:40]
        mark = "🟢" if n > 0 else "⚪"
        lines.append(f"{mark} {i}. {label} — {n} лотов/24ч")
    return f"📋 {len(urls)} URL (🟢 собирает, ⚪ нет за 24 ч):\n\n" + "\n".join(lines)


# ---------------------------------------------------------------- mute
def _set_mute_all(on: bool) -> None:
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS avito_original.bot_settings ("
            "key text PRIMARY KEY, value text)"
        )
        cur.execute(
            "INSERT INTO avito_original.bot_settings (key, value) VALUES ('mute_all', %s)"
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            ("1" if on else "0",),
        )


def _heartbeat() -> None:
    try:
        with open(os.environ.get("HEARTBEAT_FILE", "/shared/heartbeat_bot"), "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _get_setting(key: str) -> str | None:
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM avito_original.bot_settings WHERE key = %s", (key,))
        row = cur.fetchone()
    return row[0] if row else None


def _set_setting(key: str, value: str) -> None:
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS avito_original.bot_settings ("
            "key text PRIMARY KEY, value text)"
        )
        cur.execute(
            "INSERT INTO avito_original.bot_settings (key, value) VALUES (%s, %s)"
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )


DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "9"))


def build_digest() -> str:
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS avito_original.alerts_log (
                ts timestamptz NOT NULL DEFAULT now(), kind text, text text)
        """)
        cur.execute("""
            SELECT
              (SELECT count(*) FROM avito_original.ads
                WHERE parsed_at > now() - interval '24 hours'),
              (SELECT count(*) FROM midraw.avito_ads
                WHERE parsed_at > now() - interval '24 hours'),
              (SELECT count(*) FROM avito_original.ads WHERE length(description) > 260),
              (SELECT count(*) FROM detail.ads WHERE state = 'активно'),
              (SELECT count(*) FROM detail.ads WHERE state = 'закрыто'),
              (SELECT count(*) FROM raw.avito_ads_csv
                WHERE csv_header = 'browser_check' AND loaded_at > now() - interval '24 hours')
        """)
        collected, in_midraw, desc_total, active, closed, checked = cur.fetchone()
    incidents = sql("""
            SELECT kind, count(*) FROM avito_original.alerts_log
            WHERE ts > now() - interval '24 hours'
            GROUP BY kind ORDER BY kind
        """)
    prev_desc = int(_get_setting("prev_desc_total") or desc_total)
    prev_active = int(_get_setting("prev_active") or active)
    desc_delta = desc_total - prev_desc
    act_delta = active - prev_active
    _set_setting("prev_desc_total", str(desc_total))
    _set_setting("prev_active", str(active))

    inc_parts = {
        "failure": "падений", "lots": "лот-алертов", "recovery": "восстановлений",
        "system": "системных", "info": "инфо",
    }
    inc = ", ".join(f"{inc_parts.get(k, k)}: {n}" for k, n in incidents) or "не было"

    return (
        f"📰 СВОДКА за сутки\n\n"
        f"📦 Сбор: {collected} лотов записано, {in_midraw} прошло в midraw\n"
        f"🔄 Актуализация: {checked} лотов проверено напрямую\n"
        f"📝 Описания: полных {desc_total} ({'+' if desc_delta >= 0 else ''}{desc_delta} за сутки)\n"
        f"📊 Статусы: активных {active} ({'+' if act_delta >= 0 else ''}{act_delta}), закрыто {closed}\n"
        f"🚨 Инциденты: {inc}\n"
        f"🟢 Сейчас: парсер/enricher/бот на связи"
    )


def _maybe_digest() -> None:
    """Раз в сутки в DIGEST_HOUR:00."""
    today = time.strftime("%Y%m%d")
    if _get_setting("last_digest") == today:
        return
    if time.strftime("%H") != f"{DIGEST_HOUR:02d}":
        return
    if _get_setting("mute_all") == "1":
        return
    try:
        text = build_digest()
        api_call("sendMessage", {"chat_id": ADMIN_CHAT_ID, "text": text,
                                 "disable_web_page_preview": True})
        _set_setting("last_digest", today)
        print("дайджест отправлен", flush=True)
    except Exception as exc:
        print(f"digest failed: {exc}", flush=True)


def cmd_mute(hours: int) -> str:
    until = time.time() + hours * 3600
    with open(MUTE_FILE, "w") as f:
        f.write(str(until))
    return f"🔇 Алерты лотов выключены на {hours} ч (до {time.strftime('%H:%M', time.localtime(until))}).\nПадения продолжат приходить."


def cmd_unmute() -> str:
    try:
        os.remove(MUTE_FILE)
    except FileNotFoundError:
        pass
    return "🔊 Алерты лотов снова включены."


# ---------------------------------------------------------------- диспетчер
def handle(chat_id: int, text: str) -> None:
    if str(chat_id) != ADMIN_CHAT_ID:
        reply(chat_id, "⛔️ Этот бот только для владельца.")
        return
    parts = text.strip().split(maxsplit=2)
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""
    arg2 = parts[2] if len(parts) > 2 else ""
    try:
        if cmd == "/help" or cmd == "/start":
            reply(chat_id, HELP)
        elif cmd == "/status":
            reply(chat_id, cmd_status())
        elif cmd == "/top":
            out = cmd_top()
            if out:
                reply(chat_id, out)
        elif cmd == "/lot" and arg:
            out = _lot_card(int(arg), refresh=False)
            if isinstance(out, tuple):
                text, url = out
                reply(chat_id, text, url_button=url)
            else:
                reply(chat_id, out)
        elif cmd == "/checks":
            reply(chat_id, cmd_checks())
        elif cmd == "/menu":
            reply(chat_id, cmd_menu(), markup=_menu_markup())
        elif cmd == "/tech" and arg:
            reply(chat_id, cmd_tech(int(arg)))
        elif cmd == "/add" and arg:
            reply(chat_id, cmd_add(arg))
        elif cmd == "/urls":
            reply(chat_id, cmd_urls())
        elif cmd == "/remove" and arg:
            reply(chat_id, cmd_remove(int(arg), arg2 == "confirm"))
        elif cmd == "/mute" and arg:
            reply(chat_id, cmd_mute(max(1, int(arg))))
        elif cmd == "/unmute":
            reply(chat_id, cmd_unmute())
        elif cmd == "/off":
            _set_mute_all(True)
            reply(chat_id, "🔇 ВСЕ уведомления отключены (лоты, падения, системные).\n"
                           "Включить: /on или кнопка в этом сообщении.",
                  mute_button=True)
        elif cmd == "/on":
            _set_mute_all(False)
            reply(chat_id, "🔊 Уведомления включены.")
        else:
            reply(chat_id, "Не понял. " + HELP)
    except Exception as exc:
        reply(chat_id, f"⚠️ Ошибка команды: {exc}")


def _on_mute_all_callback(callback_id: str, chat_id: int) -> None:
    """Нажатие кнопки «Отключить уведомления» под любым сообщением."""
    _set_mute_all(True)
    api_call("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": "🔇 Все уведомления отключены. Включить: /on",
        "show_alert": False,
    })
    reply(chat_id, "🔇 ВСЕ уведомления отключены.\nВключить: /on")


def main() -> None:
    offset = 0
    print("bot_service запущен, polling...", flush=True)
    while True:
        try:
            _heartbeat()
            _maybe_digest()
            data = api_call(
                "getUpdates",
                {"offset": offset, "timeout": 50,
                 "allowed_updates": ["message", "callback_query"]},
            )
            if not data or not data.get("ok"):
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                cb = upd.get("callback_query")
                if cb and cb.get("data") in ("mute_all", "unmute_all"):
                    _set_mute_all(cb["data"] == "mute_all")
                    answer = ("🔇 Все уведомления отключены. Включить: /on"
                              if cb["data"] == "mute_all" else
                              "🔊 Уведомления включены.")
                    api_call("answerCallbackQuery", {
                        "callback_query_id": cb["id"], "text": answer,
                        "show_alert": False,
                    })
                    chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
                    if chat_id:
                        reply(chat_id, answer + ("\nПроверить: /status" if cb["data"] == "unmute_all" else ""),
                              mute_button=(cb["data"] == "mute_all"))
                    continue
                if cb and cb.get("data") in ("toggle_region", "toggle_mute"):
                    if cb["data"] == "toggle_region":
                        cur = _get_setting("region_filter") or "all"
                        new = "all" if cur == "spb" else "spb"
                        _set_setting("region_filter", new)
                        answer = "📍 Только СПб" if new == "spb" else "🌐 Все города"
                    else:
                        cur = _get_setting("mute_all") or "0"
                        new = "0" if cur == "1" else "1"
                        _set_setting("mute_all", new)
                        answer = "🔇 Уведомления выключены" if new == "1" else "🔔 Уведомления включены"
                    api_call("answerCallbackQuery", {
                        "callback_query_id": cb["id"], "text": answer,
                        "show_alert": False,
                    })
                    chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
                    if chat_id:
                        reply(chat_id, cmd_menu(), markup=_menu_markup())
                    continue
                if cb and cb.get("data", "").startswith("tech:"):
                    action, _, avito_id = cb["data"].partition(":")
                    api_call("answerCallbackQuery", {
                        "callback_query_id": cb["id"],
                        "text": "Собираю данные...",
                        "show_alert": False})
                    chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
                    if chat_id:
                        try:
                            reply(chat_id, cmd_tech(int(avito_id)))
                        except Exception as exc:
                            reply(chat_id, f"⚠️ Ошибка: {html_mod.escape(str(exc)[:100])}")
                    continue
                msg = upd.get("message") or {}
                text = msg.get("text") or ""
                chat = (msg.get("chat") or {}).get("id")
                if text.startswith("/") and chat:
                    handle(chat, text)
        except Exception as exc:
            print(f"polling error: {exc}", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()