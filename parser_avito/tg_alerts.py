"""Единый Telegram-алерт-модуль: падения, выгодные лоты, системные события.

Один бот — один чат — типизированные сообщения:
  🚨 ПАДЕНИЕ / 🔥 ЛОТЫ / 🟢 ВОССТАНОВЛЕНИЕ / ℹ️ СИСТЕМА
send_message — тонкая обёртка (никогда не роняет вызывающий код).
Мьют-файл /shared/bot_mute_until.txt глушит ТОЛЬКО алерты лотов.
"""
import html as html_mod
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
MUTE_FILE = os.environ.get("BOT_MUTE_FILE", "/shared/bot_mute_until.txt")
MAX_LOT_MSGS_PER_HOUR = int(os.environ.get("ALERT_LOT_MSG_LIMIT", "10"))

_lot_msg_times: list[float] = []


def _log_alert(kind: str, text: str) -> None:
    """История алертов в БД (для дайджеста). Ошибки — молча."""
    try:
        import psycopg2
        conf = dict(
            host=os.environ.get("POSTGRES_HOST", "external-postgres"),
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ.get("POSTGRES_DB", "avito_dwh"),
            user=os.environ.get("POSTGRES_USER", "avito"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
        )
        with psycopg2.connect(**conf) as conn, conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS avito_original.alerts_log ("
                "ts timestamptz NOT NULL DEFAULT now(), kind text, text text)"
            )
            cur.execute("INSERT INTO avito_original.alerts_log (kind, text) VALUES (%s, %s)",
                        (kind, text[:500]))
    except Exception as exc:
        print(f"tg_alerts: log failed: {exc}", flush=True)


def send_message(text: str, buttons: list[tuple[str, str]] | None = None, kind: str = "info") -> bool:
    """Отправка в чат. buttons: [(label, url)]. Возвращает True/False, не бросает."""
    _log_alert(kind, text)
    if not TOKEN or not CHAT_ID:
        return False
    keyboard: list[list[dict]] = []
    if buttons:
        keyboard = [[{"text": label, "url": url} for label, url in buttons]]
    # кнопка полного отключения уведомлений под каждым сообщением
    keyboard.append([{"text": "🔇 Отключить уведомления", "callback_data": "mute_all"}])
    payload: dict = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": keyboard},
    }
    try:
        req = urllib.request.Request(
            API + "/sendMessage",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError as exc:
        print(f"tg_alerts: send failed: HTTP {exc.code}: {exc.read().decode()[:200]}", flush=True)
        return False
    except Exception as exc:  # алерты не должны ломать основную работу
        print(f"tg_alerts: send failed: {exc}", flush=True)
        return False


def lots_muted() -> bool:
    """Истина, пока действует /mute (глушит только алерты лотов)."""
    try:
        with open(MUTE_FILE) as f:
            return float(f.read().strip()) > time.time()
    except Exception:
        return False


def lot_budget_left() -> int:
    """Сколько лот-сообщений ещё можно отправить в течение часа."""
    now = time.time()
    _lot_msg_times[:] = [t for t in _lot_msg_times if now - t < 3600]
    return max(0, MAX_LOT_MSGS_PER_HOUR - len(_lot_msg_times))


def _city(ad: dict) -> str:
    """Город: из location, а если он пуст — по URL."""
    loc = str(ad.get("location") or "").strip()
    if loc:
        return loc
    url = (ad.get("url") or "").lower()
    if "sankt-peterburg" in url:
        return "Санкт-Петербург"
    if "leningradskaya" in url:
        return "Ленобласть"
    if "moskva" in url:
        return "Москва"
    return "—"


def _fmt_published(published_at) -> str:
    """Дата создания объявления -> человекочитаемый вид."""
    if not published_at:
        return "дата публикации неизвестна"
    try:
        dt = datetime.fromisoformat(str(published_at))
    except Exception:
        return str(published_at)
    months = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря")
    age_days = (datetime.now(timezone.utc) - dt).days
    stamp = f"{dt.day} {months[dt.month - 1]} в {dt:%H:%M}"
    if age_days <= 0:
        return f"сегодня в {dt:%H:%M}"
    if age_days == 1:
        return f"вчера в {dt:%H:%M}"
    if age_days < 7:
        return f"{age_days} дн. назад ({dt.day} {months[dt.month - 1]})"
    return stamp


def send_lot(ad: dict) -> bool:
    """Одно сообщение на один лот: название, цена, медиана, локация, теги,
    описание, дата создания. Кнопки: Авито / тех. свойства / карточка."""
    model = html_mod.escape((ad["model"] or "?")[:60])
    loc = html_mod.escape(_city(ad))
    title = html_mod.escape((ad.get("title") or "").strip()[:120])

    lines = []
    if title and title.lower() != model.lower():
        lines.append(f"🔥 <b>{title}</b>")
        if ad.get("model") and ad["model"] != "?":
            lines.append(f"<i>{model}</i>")
    else:
        lines.append(f"🔥 <b>{model}</b>")
    lines += [
        f"💰 {ad['price']:,.0f} ₽ — на <b>{ad['benefit_pct']:+d}%</b> "
        f"ниже медианы ({ad['median']:,.0f} ₽)",
        f"📍 {loc} · ID {ad['avito_id']}",
    ]
    if ad.get("published_at"):
        lines.append(f"🕐 Опубликовано: {html_mod.escape(_fmt_published(ad['published_at']))}")

    tags = ad.get("tags") or []
    if tags:
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = [tags]
        clean = [html_mod.escape(str(t))[:30] for t in tags if str(t).strip()][:6]
        if clean:
            lines.append("🏷 " + " · ".join(clean))

    desc = (ad.get("description") or "").strip()
    if desc:
        snippet = desc[:300].rstrip()
        if len(desc) > 300:
            snippet += "…"
        lines.append(f"\n📝 {html_mod.escape(snippet)}")

    text = "\n".join(lines)
    payload: dict = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": [
            [{"text": "🛒 Открыть на Авито", "url": ad["url"]}],
            [{"text": "🔧 Тех. свойства", "callback_data": f"tech:{ad['avito_id']}"}],
            [{"text": "🔇 Отключить уведомления", "callback_data": "mute_all"}],
        ]},
    }
    if _send(payload) is False:
        # Telegram не понял HTML (например, в тексте остался «<»):
        # отправляем без разметки, чтобы лот не потерялся
        plain = re.sub(r"<[^>]+>", "", text)
        payload["text"] = plain
        payload["parse_mode"] = None
        if _send(payload) is False:
            return False
    _lot_msg_times.append(time.time())
    _log_alert("lots", f"{model} — {ad['price']:.0f} ₽ ({ad['benefit_pct']:+d}%)")
    return True


def _send(payload: dict) -> bool | None:
    """Отправка одного сообщения. True — ок, False — ошибка, None — сетевая."""
    try:
        req = urllib.request.Request(
            API + "/sendMessage",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:300]
        print(f"tg_alerts: send failed: HTTP {exc.code}: {body}", flush=True)
        if exc.code == 400:
            print(f"tg_alerts: текст сообщения: {payload.get('text', '')[:300]!r}", flush=True)
        return False
    except Exception as exc:
        print(f"tg_alerts: send failed: {exc}", flush=True)
        return None


def send_new_lot(ad: dict) -> bool:
    """Уведомление о НОВОМ объявлении (появилось в выдаче) — без сравнения
    с медианой. Название, цена, локация, теги, описание, дата создания."""
    title = html_mod.escape((ad.get("title") or ad.get("model") or "?").strip()[:120])
    model = html_mod.escape((ad.get("model") or "")[:60])
    loc = html_mod.escape(_city(ad))

    lines = [f"🆕 <b>{title}</b>"]
    if model and model.lower() != title.lower():
        lines.append(f"<i>{model}</i>")
    lines.append(f"💰 {ad['price']:,.0f} ₽")
    lines.append(f"📍 {loc} · ID {ad['avito_id']}")
    if ad.get("search_name"):
        lines.append(f"🔎 Найдено в поиске: <b>{html_mod.escape(str(ad['search_name'])[:40])}</b>")
    if ad.get("published_at"):
        lines.append(f"🕐 Опубликовано: {html_mod.escape(_fmt_published(ad['published_at']))}")

    tags = ad.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = [tags]
    clean = [html_mod.escape(str(t))[:30] for t in tags if str(t).strip()][:6]
    if clean:
        lines.append("🏷 " + " · ".join(clean))

    desc = (ad.get("description") or "").strip()
    if desc:
        snippet = desc[:300].rstrip()
        if len(desc) > 300:
            snippet += "…"
        lines.append(f"\n📝 {html_mod.escape(snippet)}")

    text = "\n".join(lines)
    payload: dict = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": [
            [{"text": "🛒 Открыть на Авито", "url": ad["url"]}],
            [{"text": "🔧 Тех. свойства", "callback_data": f"tech:{ad['avito_id']}"}],
            [{"text": "🔇 Отключить уведомления", "callback_data": "mute_all"}],
        ]},
    }
    if _send(payload) is False:
        plain = re.sub(r"<[^>]+>", "", text)
        payload["text"] = plain
        payload["parse_mode"] = None
        if _send(payload) is False:
            return False
    _log_alert("new", f"{title} — {ad['price']:.0f} ₽")
    return True


def send_lots_batch(items: list[dict]) -> list[int]:
    """Каждый лот — отдельным сообщением. Возвращает ID отправленных.

    items: [{"model","price","median","benefit_pct","avito_id","url","location"}]
    """
    if lots_muted() or not items:
        return []
    sent: list[int] = []
    for ad in items:
        if lot_budget_left() <= 0:
            print(f"tg_alerts: бюджет исчерпан ({MAX_LOT_MSGS_PER_HOUR}/час)", flush=True)
            break
        if send_lot(ad):
            sent.append(ad["avito_id"])
            time.sleep(0.7)  # пауза между сообщениями, чтобы не ловить 429
    return sent


def send_system(text: str) -> bool:
    """ℹ️ системное сообщение (не глушится мьютом)."""
    return send_message(f"ℹ️ СИСТЕМА\n{text}", kind="system")


def send_recovery(text: str) -> bool:
    return send_message(f"🟢 ВОССТАНОВЛЕНИЕ\n{text}", kind="recovery")


def send_failure(text: str) -> bool:
    """🚨 падения — всегда, мьют не действует."""
    return send_message(f"🚨 ПАДЕНИЕ\n{text}", kind="failure")