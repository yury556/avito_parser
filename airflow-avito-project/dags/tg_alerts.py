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
        # глобальный мьют из bot_settings (ставится кнопкой бота или /off);
        # если таблицы нет/БД недоступна — шлём как обычно
        try:
            import psycopg2
            conn_conf = dict(
                host=os.environ.get("POSTGRES_HOST", "external-postgres"),
                port=os.environ.get("POSTGRES_PORT", "5432"),
                dbname=os.environ.get("POSTGRES_DB", "avito_dwh"),
                user=os.environ.get("POSTGRES_USER", "avito"),
                password=os.environ.get("POSTGRES_PASSWORD", ""),
            )
            with psycopg2.connect(**conn_conf) as conn, conn.cursor() as cur:
                cur.execute("SELECT value FROM avito_original.bot_settings WHERE key='mute_all'")
                row = cur.fetchone()
                if row and row[0] == "1":
                    return
        except Exception:
            pass
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