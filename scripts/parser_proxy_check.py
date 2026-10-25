"""Проверка: ходит ли парсер Авито через локальный прокси.

Запуск (на неттопе):
    sudo docker cp scripts/parser_proxy_check.py \
        airflow-avito-project-avito-parser-service-1:/tmp/t.py
    sudo docker exec -e PYTHONPATH=/app/src -e PROXY=172.17.0.1:8888 \
        airflow-avito-project-avito-parser-service-1 \
        sh -c "cd /app && python /tmp/t.py"

Ожидаемо: HTTP 200, 40-50 объявлений, «Доступ ограничен»: False
"""
import os
import re
import sys

sys.path.insert(0, '/app/src')

from avito_parser.config import load_avito_config
from avito_parser.cookies.factory import build_cookies_provider
from avito_parser.fetchers.client import HttpClient
from avito_parser.proxies.proxy import ServerProxy

CFG = os.environ.get('PARSER_CFG', '/app/config/config.toml')
PROXY = os.environ.get('PROXY', '172.17.0.1:8888')

cfg = load_avito_config(CFG)
cookies = build_cookies_provider(config=cfg)
http = HttpClient(proxy=ServerProxy(PROXY), cookies=cookies,
                  timeout=60, max_retries=2, retry_delay=3, block_threshold=10)

url = None
with open(CFG, encoding='utf-8') as f:
    for line in f:
        m = re.search(r'https://www\.avito\.ru\S+', line)
        if m:
            url = m.group(0).strip('"')
            break

print('прокси:', PROXY)
print('URL:', (url or '')[:90], '...')
try:
    r = http.request('GET', url)
    text = r.text
    print('HTTP:', r.status_code, 'размер:', len(text))
    print('«Доступ ограничен»:', 'Доступ ограничен' in text)
    print('объявлений:', len(re.findall(r'data-marker="item"', text)))
except Exception as e:
    print('ОШИБКА:', type(e).__name__, str(e)[:300])
