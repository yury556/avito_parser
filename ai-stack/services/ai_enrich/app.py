"""
ai_enrich — микросервис AI-обогащения товарных объявлений.

Принимает текст объявления, шлёт в OpenRouter, возвращает структурированный JSON.
Запуск: uvicorn app:app --host 0.0.0.0 --port 8000
"""
import json
import os
import re
import time
from pathlib import Path

import httpx
import psycopg2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="AI Enrich", version="1.0.0")

# ──── Конфигурация ────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
AI_MODEL = os.getenv("AI_MODEL", "google/gemma-4-31b-it:free")
PROMPT_VERSION = os.getenv("AI_PROMPT_VERSION", "v1")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5433"))
DB_NAME = os.getenv("DB_NAME", "avito_dwh")
DB_USER = os.getenv("DB_USER", "avito")
DB_PASSWORD = os.getenv("DB_PASSWORD", "avito123")

# ──── Загрузка промпта ────
PROMPT_DIR = Path(__file__).parent / "prompts"
prompt_path = PROMPT_DIR / f"{PROMPT_VERSION}.txt"
if not prompt_path.exists():
    raise RuntimeError(f"Prompt file not found: {prompt_path}")
PROMPT_TEMPLATE = prompt_path.read_text(encoding="utf-8")


# ──── Модели ────
class EnrichRequest(BaseModel):
    title: str
    description: str = ""


class EnrichResponse(BaseModel):
    category: str | None = None
    brand: str | None = None
    model: str | None = None
    tags: list[str] = []
    attributes: dict = {}


# ──── Health check ────
@app.get("/health")
def health():
    return {"status": "ok", "model": AI_MODEL, "prompt": PROMPT_VERSION, "backend": "openrouter"}


# ──── Основной эндпоинт ────
@app.post("/enrich", response_model=EnrichResponse)
def enrich(req: EnrichRequest):
    messages = _build_messages(req.title or "", req.description or "")
    data = _call_openrouter(messages)
    if data is None:
        raise HTTPException(status_code=502, detail="AI call failed after retries")
    return EnrichResponse(**data)


# ──── Batch-эндпоинт для Airflow ────
class BatchRequest(BaseModel):
    limit: int = 10


class BatchItem(BaseModel):
    avito_id: int
    title: str | None
    description: str | None


class BatchResult(BaseModel):
    processed: int
    errors: int
    items: list[dict]


@app.post("/batch", response_model=BatchResult)
def batch_process(req: BatchRequest):
    """
    Читает из midraw.avito_ads, шлёт в OpenRouter, пишет в detail.ads.
    """
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
    )
    cur = conn.cursor()

    cur.execute("""
        SELECT a.avito_id, a.title, a.description
        FROM midraw.avito_ads a
        LEFT JOIN detail.ads d ON a.avito_id = d.avito_id
        WHERE d.avito_id IS NULL
        LIMIT %s
    """, (req.limit,))
    rows = cur.fetchall()

    processed = 0
    errors = 0
    results = []

    for avito_id, title, desc in rows:
        time.sleep(0.5)
        messages = _build_messages(title or "", desc or "")
        result = _call_openrouter(messages)
        if isinstance(result, dict) and "category" in result:
            try:
                cur.execute("""
                    INSERT INTO detail.ads
                        (avito_id, category, brand, model, tags,
                         input_title, input_description,
                         ai_model, ai_version, ai_processed_at, ai_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 'success')
                    ON CONFLICT (avito_id) DO UPDATE SET
                        category=EXCLUDED.category, brand=EXCLUDED.brand,
                        model=EXCLUDED.model, tags=EXCLUDED.tags,
                        ai_processed_at=NOW(), ai_status='success'
                """, (
                    avito_id,
                    result.get("category"),
                    result.get("brand"),
                    result.get("model"),
                    json.dumps(result.get("tags", [])),
                    title, desc, AI_MODEL, PROMPT_VERSION,
                ))
                conn.commit()
                processed += 1
                results.append({"avito_id": avito_id, "status": "ok", "data": result})
            except Exception as e:
                conn.rollback()
                errors += 1
                results.append({"avito_id": avito_id, "status": "error", "error": str(e)})
        else:
            errors += 1
            results.append({"avito_id": avito_id, "status": "error", "error": "invalid AI response"})

    conn.close()
    return BatchResult(processed=processed, errors=errors, items=results)


# ──── OpenRouter API ────

def _build_messages(title: str, description: str) -> list[dict]:
    system_prompt = PROMPT_TEMPLATE.format(
        title=title or "",
        description=(description or "")[:1000],
    )
    return [
        {"role": "system", "content": "Ты — ассистент по нормализации товарных объявлений. Отвечай только JSON."},
        {"role": "user", "content": system_prompt},
    ]


def _call_openrouter(messages: list[dict], retries: int = 5) -> dict | None:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": AI_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 1024,
    }

    for attempt in range(retries):
        try:
            resp = httpx.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            )
            if resp.status_code == 429:
                wait = 2 ** (attempt + 2)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
            text = msg.get("content") or msg.get("reasoning")
            if not text:
                continue
            text = text.strip()
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            return json.loads(text)
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(2 ** (attempt + 1))
    return None