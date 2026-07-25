#!/bin/bash
set +e

cd /app
uvicorn avito_service:app --host 0.0.0.0 --port 8000 2>/dev/null &
sleep 2

cd /app
python3 -c "
import sys, time, os
sys.path.insert(0, '/app/src')
from avito_parser.config import load_avito_config
from avito_parser.core import AvitoParse

CONFIG_PATH = os.environ.get('PARSER_CONFIG', '/app/config.toml')

while True:
    try:
        config = load_avito_config(CONFIG_PATH)
        config.save_json = True
        config.one_time_start = False
        config.count = config.count or 1
        parser = AvitoParse(config)
        print('=== Запускаю парсинг ===', flush=True)
        parser.parse()
        print('=== Пауза ===', flush=True)
        time.sleep(config.pause_general)
    except Exception as e:
        print(f'Ошибка: {e}', flush=True)
        time.sleep(30)
" &
echo $! > /tmp/parser.pid
