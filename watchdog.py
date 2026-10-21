"""Внешний watchdog (вне docker): контейнеры Up, heartbeat'ы, свежесть БД.

Алерты в тот же бот. Запускается systemd timer'ом каждые 5 минут от root.
Дедуп: состояние в /var/lib/avito-watchdog/state.json; восстановление -> 🟢.
"""
import json
import os
import subprocess
import time
import urllib.request

TOKEN = None
CHAT_ID = None


def _load_env():
    global TOKEN, CHAT_ID
    if TOKEN:
        return
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "airflow-avito-project", ".env")
    with open(env_path) as f:
        for line in f:
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                TOKEN = line.strip().split("=", 1)[1]
            elif line.startswith("TELEGRAM_ALERT_CHAT_ID="):
                CHAT_ID = line.strip().split("=", 1)[1]


def notify(text: str) -> None:
    _load_env()
    payload = {"chat_id": CHAT_ID,
               "text": f"🚨 WATCHDOG\n{text}", "disable_web_page_preview": True}
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:
        print(f"notify failed: {exc}", flush=True)


def notify_recovered(text: str) -> None:
    _load_env()
    payload = {"chat_id": CHAT_ID, "text": f"🟢 WATCHDOG\n{text}"}
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:
        print(f"notify failed: {exc}", flush=True)


def containers_status() -> dict:
    out = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}} {{.Status}}"],
        capture_output=True, text=True, timeout=30).stdout
    status = {}
    for line in out.splitlines():
        name = line.split(" ")[0]
        status[name] = line
    return status


def check():
    base = os.path.dirname(os.path.abspath(__file__))
    storage = os.path.join(base, "parser_avito", "storage")
    state_path = "/var/lib/avito-watchdog/state.json"
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    state = {}
    try:
        state = json.load(open(state_path))
    except Exception:
        pass

    problems = {}

    # 1) контейнеры
    cs = containers_status()
    for name in ("airflow-avito-project-avito-parser-service-1",
                 "airflow-avito-project-description-enricher-1",
                 "airflow-avito-project-parser-bot-1"):
        if name not in cs or "Up" not in cs[name]:
            problems[name.split("avito-project-")[-1]] = "контейнер не Up"

    # 2) heartbeat'ы
    checks = [("heartbeat_enricher", 60, "enricher"), ("heartbeat_bot", 8, "бот")]
    for fname, max_age_min, label in checks:
        path = os.path.join(storage, fname)
        try:
            age = (time.time() - os.path.getmtime(path)) / 60
            if age > max_age_min:
                problems[label] = f"heartbeat старый: {age:.0f} мин"
        except FileNotFoundError:
            problems[label] = "heartbeat-файла нет"

    # 3) свежесть БД (через docker exec psql)
    try:
        out = subprocess.run(
            ["docker", "exec", "airflow-avito-project-external-postgres-1",
             "psql", "-U", "avito", "-d", "avito_dwh", "-t", "-A", "-c",
             "SELECT extract(epoch FROM now() - max(parsed_at))/60 FROM avito_original.ads"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        age = float(out or 0)
        if age > 120:
            problems["парсер"] = f"нет записей {age:.0f} мин"
    except Exception as exc:
        print(f"db check failed: {exc}", flush=True)

    # алерты / восстановление
    for key, msg in problems.items():
        if not state.get(key):
            notify(f"{key}: {msg}")
            state[key] = msg
    for key in list(state):
        if key not in problems:
            notify_recovered(f"{key}: восстановилось")
            del state[key]

    json.dump(state, open(state_path, "w"))
    print(f"watchdog run: problems={list(problems)}", flush=True)


if __name__ == "__main__":
    check()