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

# --- нормализация обфусцированного текста для показа в уведомлениях ---
_LOOK_CLEAN = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х",
    "y": "у", "k": "к", "m": "м", "h": "н", "t": "т", "b": "в",
    "i": "и", "s": "с", "j": "й", "d": "д",
    "A": "А", "C": "С", "E": "Е", "O": "О", "P": "Р", "X": "Х",
    "Y": "У", "K": "К", "M": "М", "H": "Н", "T": "Т", "B": "В",
    "ο": "о", "α": "а",
}


def _clean_display(text):
    return "".join(_LOOK_CLEAN.get(ch, ch) for ch in (text or ""))
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


# ──── фото лотов ────
PHOTO_ENABLED = os.environ.get("PHOTO_ENABLED", "1") == "1"
PHOTO_MAX_BYTES = int(os.environ.get("PHOTO_MAX_BYTES", str(8 * 1024 * 1024)))

_minio_state = {"client": None, "bucket": None, "failed_at": 0.0}


def _minio():
    """Ленивый клиент MinIO из конфига парсера. None → недоступен (не чаще раза в час)."""
    if _minio_state["client"] is not None:
        return _minio_state["client"], _minio_state["bucket"]
    if time.time() - _minio_state["failed_at"] < 3600:
        return None
    try:
        import tomllib
        from minio import Minio
        path = os.environ.get("PARSER_CONFIG", "/app/config/config.toml")
        with open(path, "rb") as f:
            st = tomllib.load(f).get("storage", {})
        endpoint = (st.get("endpoint") or "").replace("http://", "").replace("https://", "")
        client = Minio(endpoint, access_key=st.get("access_key", ""),
                       secret_key=st.get("secret_key", ""), secure=False)
        _minio_state["client"] = client
        _minio_state["bucket"] = st.get("bucket", "avito-images")
        return _minio_state["client"], _minio_state["bucket"]
    except Exception as e:
        _minio_state["failed_at"] = time.time()
        print(f"tg_alerts: MinIO недоступен ({e}), фото временно выключены", flush=True)
        return None


def _photo_entries(image_keys) -> list[dict]:
    if isinstance(image_keys, str):
        try:
            image_keys = json.loads(image_keys)
        except Exception:
            image_keys = []
    out = []
    for e in image_keys or []:
        if isinstance(e, dict) and e.get("key"):
            out.append(e)
        elif isinstance(e, str):
            out.append({"key": e})
    return out


def _pick_photo_key(image_keys) -> str | None:
    """Первое настоящее фото: иконки Авито и мусор отбрасываем."""
    entries = _photo_entries(image_keys)
    if not entries:
        return None
    good = [e for e in entries if "avito.st" in (e.get("original_url") or "")]
    pool = good or [e for e in entries if "icon" not in (e.get("original_url") or "")]
    # Telegram надёжнее принимает jpg/png; webp — запасной вариант
    jpg_png = [e for e in pool
               if (e.get("key") or "").lower().endswith((".jpg", ".jpeg", ".png"))]
    return (jpg_png or pool or entries)[0].get("key")


def _photo_source(ad: dict):
    """('bytes', имя, данные) | ('url', ссылка) | None. Ошибки — тихо, с логом."""
    if not PHOTO_ENABLED:
        return None
    keys = ad.get("image_keys")
    mc = _minio()
    key = _pick_photo_key(keys)
    if mc and key:
        client, bucket = mc
        try:
            resp = client.get_object(bucket, key)
            data = resp.read()
            resp.close()
            resp.release_conn()
            if data and len(data) <= PHOTO_MAX_BYTES:
                return ("bytes", key.rsplit("/", 1)[-1] or "lot.jpg", data)
        except Exception as e:
            print(f"tg_alerts: фото {ad.get('avito_id')}: {e}", flush=True)
    # запасной путь: публичная ссылка на оригинал с CDN Авито
    for e in _photo_entries(keys):
        orig = e.get("original_url") or ""
        if "avito.st" in orig:
            return ("url", orig)
    return None


def _retry_after(data: dict) -> int | None:
    try:
        return int((data.get("parameters") or {}).get("retry_after"))
    except (TypeError, ValueError):
        return None


def _as_result(value) -> dict:
    """Совместимость со старыми моками _send/_send_photo (True/False/None)."""
    if isinstance(value, dict):
        return value
    if value is True:
        return {"ok": True, "message_id": None}
    return {"ok": False, "error": "Telegram delivery failed"}


def _response_result(status: int, data: dict, body: str = "") -> dict:
    ok = status == 200 and bool(data.get("ok", False))
    result = {
        "ok": ok,
        "status": status,
        "message_id": (data.get("result") or {}).get("message_id") if ok else None,
        "retry_after": _retry_after(data),
    }
    if not ok:
        result["error"] = f"HTTP {status}: {data.get('description') or body[:300]}"
    return result


def _send_photo(caption: str, reply_markup: dict, photo, detailed: bool = False):
    """sendPhoto с подписью и кнопками. False → вызывающий код шлёт текст."""
    import requests
    fields: dict = {
        "chat_id": CHAT_ID,
        "caption": caption[:1024],
        "parse_mode": "HTML",
        "reply_markup": json.dumps(reply_markup),
    }
    try:
        if photo[0] == "bytes":
            files = {"photo": (photo[1], photo[2])}
            r = requests.post(f"{API}/sendPhoto", data=fields, files=files, timeout=30)
        else:
            fields["photo"] = photo[1]
            r = requests.post(f"{API}/sendPhoto", data=fields, timeout=30)
        data = r.json()
        if r.status_code == 400:  # Telegram не понял HTML в подписи → без разметки
            fields["caption"] = re.sub(r"<[^>]+>", "", caption)[:1024]
            fields.pop("parse_mode", None)
            if photo[0] == "bytes":
                files = {"photo": (photo[1], photo[2])}
                r = requests.post(f"{API}/sendPhoto", data=fields, files=files, timeout=30)
            else:
                r = requests.post(f"{API}/sendPhoto", data=fields, timeout=30)
            data = r.json()
        result = _response_result(r.status_code, data, r.text)
        if not result["ok"]:
            print(f"tg_alerts: sendPhoto: {result['error']}", flush=True)
        return result if detailed else result["ok"]
    except Exception as e:
        print(f"tg_alerts: sendPhoto failed: {e}", flush=True)
        result = {"ok": False, "error": f"{type(e).__name__}: {e}", "unknown_result": True}
        return result if detailed else False


def _deliver_good_lot(ad: dict) -> dict:
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
    elif ad.get("first_seen"):
        fs = ad["first_seen"]
        lines.append(f"📥 В базе с: {fs:%d.%m %H:%M}" if hasattr(fs, "strftime") else f"📥 В базе с: {fs}")

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
        snippet = _clean_display(desc[:300].rstrip())
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
    # фото: подпись ≤1024 симв.; длинный текст уходит отдельным сообщением
    photo = _photo_source(ad)
    if photo:
        cap = text if len(text) <= 1000 else ""
        photo_result = _as_result(_send_photo(cap, payload["reply_markup"], photo, detailed=True))
        if not photo_result["ok"]:
            return photo_result
        if not cap:
            # Фото уже доставлено: не повторяем его из-за сбоя дополнительного текста.
            _as_result(_send(payload, detailed=True))
        _log_alert("lots", f"{model} — {ad['price']:.0f} ₽ ({ad['benefit_pct']:+d}%)")
        return photo_result
    result = _as_result(_send(payload, detailed=True))
    if not result["ok"] and result.get("status") == 400:
        # Только HTTP 400: при 429/500/timeout немедленного повтора нет.
        plain = re.sub(r"<[^>]+>", "", text)
        payload["text"] = plain
        payload["parse_mode"] = None
        result = _as_result(_send(payload, detailed=True))
    if not result["ok"]:
        return result
    _log_alert("lots", f"{model} — {ad['price']:.0f} ₽ ({ad['benefit_pct']:+d}%)")
    return result


def _send(payload: dict, detailed: bool = False):
    """Отправка одного сообщения; detailed добавляет message_id и retry_after."""
    try:
        req = urllib.request.Request(
            API + "/sendMessage",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode()
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {}
            result = _response_result(r.status, data, body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:2000]
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}
        result = _response_result(exc.code, data, body)
        print(f"tg_alerts: send failed: {result['error']}", flush=True)
        if exc.code == 400:
            print(f"tg_alerts: текст сообщения: {payload.get('text', '')[:300]!r}", flush=True)
    except Exception as exc:
        print(f"tg_alerts: send failed: {exc}", flush=True)
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "unknown_result": True}
    if detailed:
        return result
    if result["ok"]:
        return True
    return None if result.get("unknown_result") else False


def _deliver_new_lot(ad: dict) -> dict:
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
    elif ad.get("first_seen"):
        fs = ad["first_seen"]
        lines.append(f"📥 В базе с: {fs:%d.%m %H:%M}" if hasattr(fs, "strftime") else f"📥 В базе с: {fs}")

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
        snippet = _clean_display(desc[:300].rstrip())
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
    # фото: подпись ≤1024 симв.; длинный текст уходит отдельным сообщением
    photo = _photo_source(ad)
    if photo:
        cap = text if len(text) <= 1000 else ""
        photo_result = _as_result(_send_photo(cap, payload["reply_markup"], photo, detailed=True))
        if not photo_result["ok"]:
            return photo_result
        if not cap:
            # Не повторяем уже доставленное фото из-за дополнительного текста.
            _as_result(_send(payload, detailed=True))
        _log_alert("new", f"{title} — {ad['price']:.0f} ₽")
        return photo_result
    result = _as_result(_send(payload, detailed=True))
    if not result["ok"] and result.get("status") == 400:
        payload["text"] = re.sub(r"<[^>]+>", "", text)
        payload["parse_mode"] = None
        result = _as_result(_send(payload, detailed=True))
    if not result["ok"]:
        return result
    _log_alert("new", f"{title} — {ad['price']:.0f} ₽")
    return result


def deliver_lot(ad: dict, kind: str) -> dict:
    """Сетевой вызов для outbox; бюджет уже зарезервирован sender-ом."""
    if kind == "new":
        return _deliver_new_lot(ad)
    if kind == "good":
        return _deliver_good_lot(ad)
    raise ValueError(f"unknown notification kind: {kind}")


def _send_legacy_with_budget(ad: dict, kind: str) -> bool:
    if lots_muted() or lot_budget_left() <= 0:
        return False
    result = deliver_lot(ad, kind)
    if result["ok"]:
        # Общий учёт для текстового и фото-пути, в том числе send_new_lot().
        _lot_msg_times.append(time.time())
    return result["ok"]


def send_lot(ad: dict) -> bool:
    return _send_legacy_with_budget(ad, "good")


def send_new_lot(ad: dict) -> bool:
    return _send_legacy_with_budget(ad, "new")


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