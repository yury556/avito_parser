FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
ENV PLAYWRIGHT_BROWSERS_PATH=0
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir fastapi uvicorn

COPY src/ ./src/
COPY avito_service.py .
COPY image_downloader_service.py .
COPY enricher.py .

# Chromium для description-enricher (живой браузер: JS-челлендж исполняется сам)
RUN python -m playwright install --with-deps chromium || \
    (apt-get update && apt-get install -y --no-install-recommends \
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
      libgbm1 libpango-1.0-0 libcairo2 libasound2 && \
     python -m playwright install chromium)

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "avito_service:app", "--host", "0.0.0.0", "--port", "8000"]
