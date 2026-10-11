"""Telegram-алерты падений для DAG'ов: on_failure_callback.

Отправка через urllib; любая ошибка — залогирована, таск не роняется.
Токен из env TELEGRAM_BOT_TOKEN / TELEGRAM_ALERT_CHAT_ID (compose .env).
"""
import json
import logging
import os
import urllib.request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()

log = logging.getLogger(__name__)


def send_alert(text: str) -> None:
    if not TOKEN or not CHAT_ID:
        return
    try:
        payload = {
            "chat_id": CHAT_ID,
            "text": f"🚨 ПАДЕНИЕ\n{text}",
            "disable_web_page_preview": True,
        }
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:
        log.warning("tg alert failed: %s", exc)


def dag_failure_callback(context):
    """Вызывается Airflow при финальном провале таска (после ретраев)."""
    ti = context.get("task_instance")
    dag = context.get("dag")
    exception = context.get("exception")
    send_alert(
        f"DAG {getattr(dag, 'dag_id', '?')} · task {getattr(ti, 'task_id', '?')} упал\n"
        f"попытки исчерпаны. Ошибка: {str(exception)[:200] or '—'}"
    )