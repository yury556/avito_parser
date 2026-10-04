FROM python:3.13-slim

WORKDIR /client

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen

COPY . .

ENV PYTHONUNBUFFERED=1

# Цикл: пройти антибот → сохранить own_cookies.json → спать COOKIE_REFRESH_INTERVAL
CMD ["bash", "-c", "while true; do uv run python main.py || true; echo '=== cookie refresh sleep ==='; sleep ${COOKIE_REFRESH_INTERVAL:-3600}; done"]
