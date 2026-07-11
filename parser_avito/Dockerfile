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

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "avito_service:app", "--host", "0.0.0.0", "--port", "8000"]
