"""Единый Telegram-алерт-модуль: падения, выгодные лоты, системные события.

Один бот — один чат — типизированные сообщения:
  🚨 ПАДЕНИЕ / 🔥 ЛОТЫ / 🟢 ВОССТАНОВЛЕНИЕ / ℹ️ СИСТЕМА
send_message — тонкая обёртка (никогда не роняет вызывающий код).
Мьют-файл /shared/bot_mute_until.txt глушит ТОЛЬКО алерты лотов.
"""
import html as html_mod
import json
import os
import time
import urllib.error
import urllib.request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
MUTE_FILE = os.environ.get("BOT_MUTE_FILE", "/shared/bot_mute_until.txt")
MAX_LOT_MSGS_PER_HOUR = int(os.environ.get("ALERT_LOT_MSG_LIMIT", "10"))

_lot_msg_times: list[float] = []


def send_message(text: str, buttons: list[tuple[str, str]] | None = None) -> bool:
    """Отправка в чат. buttons: [(label, url)]. Возвращает True/False, не бросает."""
    if not TOKEN or not CHAT_ID:
        return False
    payload: dict = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": label, "url": url}] for label, url in buttons]
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


def send_lots_batch(items: list[dict]) -> bool:
    """Батч выгодных лотов одним сообщением с кнопками. Учитывает мьют и бюджет.

    items: [{"model","price","median","benefit_pct","avito_id","url","location"}]
    """
    if lots_muted() or not items:
        return False
    if lot_budget_left() <= 0:
        return False
    lines, buttons = [], []
    for ad in items[:5]:
        model = html_mod.escape((ad["model"] or "?")[:40])
        lines.append(
            f"<b>{model}</b> — {ad['price']:,.0f} ₽ "
            f"(<b>{ad['benefit_pct']:+d}%</b> от медианы {ad['median']:,.0f} ₽)\n"
            f"ID {ad['avito_id']} · {html_mod.escape(str(ad.get('location') or '—'))}"
        )
        buttons.append((f"🛒 {model}", ad["url"]))
    used = min(len(items), 5)
    text = (
        f"🔥 ЛОТЫ ({used}/{MAX_LOT_MSGS_PER_HOUR} за час)\n\n" + "\n\n".join(lines)
    )
    ok = send_message(text, buttons)
    if ok:
        _lot_msg_times.append(time.time())
    return ok


def send_system(text: str) -> bool:
    """ℹ️ системное сообщение (не глушится мьютом)."""
    return send_message(f"ℹ️ СИСТЕМА\n{text}")


def send_recovery(text: str) -> bool:
    return send_message(f"🟢 ВОССТАНОВЛЕНИЕ\n{text}")


def send_failure(text: str) -> bool:
    """🚨 падения — всегда, мьют не действует."""
    return send_message(f"🚨 ПАДЕНИЕ\n{text}")