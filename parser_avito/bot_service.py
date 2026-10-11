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
CONFIG_PATH = os.environ.get("PARSER_CONFIG", "/app/config.toml")
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
    "/add <url> — добавить URL мониторинга\n"
    "/urls — список URL и их сбор за 24 ч\n"
    "/remove N confirm — удалить URL номер N\n"
    "/mute N — пауза алертов лотов на N часов\n"
    "/unmute — снять паузу"
)


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


def reply(chat_id: int, text: str) -> None:
    api_call("sendMessage", {"chat_id": chat_id, "text": text,
                             "disable_web_page_preview": True})


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


# ---------------------------------------------------------------- /top
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
    """Атомарно заменяет блок urls = [ ... ] в секции [avito]."""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    start = next(i for i, l in enumerate(lines) if l.strip().startswith("urls = ["))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "]")
    block = ["urls = [\n"]
    for u in new_urls:
        block.append(f'    "{u}",\n')
    block.append("]\n")
    lines[start:end + 1] = block
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp, CONFIG_PATH)


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
        else:
            reply(chat_id, "Не понял. " + HELP)
    except Exception as exc:
        reply(chat_id, f"⚠️ Ошибка команды: {exc}")


def main() -> None:
    offset = 0
    print("bot_service запущен, polling...", flush=True)
    while True:
        try:
            data = api_call(
                "getUpdates",
                {"offset": offset, "timeout": 50,
                 "allowed_updates": ["message"]},
            )
            if not data or not data.get("ok"):
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
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