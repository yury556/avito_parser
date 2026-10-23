"""
ai_enrich — микросервис AI-обогащения товарных объявлений.

Принимает текст объявления, шлёт в OpenRouter, возвращает структурированный JSON.
Запуск: uvicorn app:app --host 0.0.0.0 --port 8000
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import psycopg2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from model_guard import validate_model

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

# ──── Параллелизм внутри батча ────
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "5"))

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
    price_rub: float | None = None


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
        SELECT a.avito_id, a.title, a.description, a.price_rub, a.url
        FROM midraw.avito_ads a
        LEFT JOIN detail.ads d ON a.avito_id = d.avito_id
        WHERE d.avito_id IS NULL
        LIMIT %s
    """, (req.limit,))
    rows = cur.fetchall()

    # --- журнал конвейера: midraw (первое появление) + enrich_start ---
    if rows:
        ids = [r[0] for r in rows]
        id_list = ", ".join(str(i) for i in ids)
        cur.execute(f"""
            INSERT INTO avito_original.lot_pipeline_log (avito_id, stage)
            SELECT v.avito_id, 'midraw'
            FROM (VALUES ({id_list})) AS v(avito_id)
            WHERE NOT EXISTS (
                SELECT 1 FROM avito_original.lot_pipeline_log p
                WHERE p.avito_id = v.avito_id AND p.stage = 'midraw')
        """)
        cur.execute(f"""
            INSERT INTO avito_original.lot_pipeline_log (avito_id, stage)
            SELECT v.avito_id, 'enrich_start'
            FROM (VALUES ({id_list})) AS v(avito_id)
        """)
        conn.commit()

    processed = 0
    errors = 0
    results = []

    def _process_one(job):
        """Worker — тянет данные в OpenRouter (БД не касается)."""
        avito_id, title, desc, price_rub, url = job
        try:
            messages = _build_messages(title or "", desc or "")
            result, duration_ms, attempts = _call_openrouter(messages)
            if isinstance(result, dict) and "category" in result:
                return {"ok": True, "avito_id": avito_id, "data": result,
                        "title": title, "desc": desc, "price_rub": price_rub, "url": url,
                        "duration_ms": duration_ms, "attempts": attempts}
            return {"ok": False, "avito_id": avito_id, "error": "invalid AI response",
                    "duration_ms": duration_ms, "attempts": attempts}
        except Exception as e:
            return {"ok": False, "avito_id": avito_id, "error": str(e)}

    jobs = [(r[0], r[1], r[2], r[3], r[4]) for r in rows]
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
        futures = [executor.submit(_process_one, job) for job in jobs]
        for fut in as_completed(futures):
            out = fut.result()
            if out["ok"]:
                try:
                    # Детерминированная валидация модели по URL-slug: LLM иногда
                    # называет не тот чип (i5-13600KF -> "i5-14400F").
                    model_fixed, model_ai, chip, note = validate_model(
                        out["data"].get("model"), out.get("url"))
                    if note:
                        print(f"ai_enrich: {out['avito_id']}: {note} "
                              f"('{model_ai}' -> '{model_fixed}')", flush=True)
                    cur.execute("""
                        INSERT INTO detail.ads
                            (avito_id, category, brand, model, model_ai, chip, tags,
                             input_title, input_description,
                             price_rub,
                             ai_model, ai_version, ai_processed_at, ai_status,
                             ai_duration_ms, ai_attempts)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 'success',
                                %s, %s)
                        ON CONFLICT (avito_id) DO UPDATE SET
                            category=EXCLUDED.category, brand=EXCLUDED.brand,
                            model=EXCLUDED.model, model_ai=EXCLUDED.model_ai,
                            chip=EXCLUDED.chip, tags=EXCLUDED.tags,
                            price_rub=EXCLUDED.price_rub,
                            ai_processed_at=NOW(), ai_status='success',
                            ai_duration_ms=EXCLUDED.ai_duration_ms,
                            ai_attempts=EXCLUDED.ai_attempts
                    """, (
                        out["avito_id"],
                        out["data"].get("category"),
                        out["data"].get("brand"),
                        model_fixed,
                        model_ai,
                        chip,
                        json.dumps(out["data"].get("tags", [])),
                        out["title"], out["desc"],
                        out["data"].get("price_rub") if out["data"].get("price_rub") is not None else out["price_rub"],
                        AI_MODEL, PROMPT_VERSION,
                        out.get("duration_ms"), out.get("attempts"),
                    ))
                    cur.execute(
                        "INSERT INTO avito_original.lot_pipeline_log "
                        "(avito_id, stage, duration_ms, meta) VALUES (%s, 'enrich_done', %s, %s)",
                        (out["avito_id"], out.get("duration_ms"),
                         json.dumps({"tier": "deepseek", "attempts": out.get("attempts")})),
                    )
                    conn.commit()
                    processed += 1
                    results.append({"avito_id": out["avito_id"], "status": "ok", "data": out["data"]})
                except Exception as e:
                    conn.rollback()
                    errors += 1
                    results.append({"avito_id": out["avito_id"], "status": "error", "error": str(e)})
            else:
                errors += 1
                results.append({"avito_id": out["avito_id"], "status": "error", "error": out["error"]})

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


def _call_openrouter(messages: list[dict], retries: int = 5) -> tuple[dict | None, int, int]:
    """Возвращает (данные, длительность_мс, попыток) — для метрик в detail.ads."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": AI_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 4096,
        "reasoning": {"effort": "low"},
    }

    t0 = time.time()
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
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning") or ""
            text = (content or reasoning).strip()
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                text = text[start:end + 1]
            if not text:
                continue
            return json.loads(text), int((time.time() - t0) * 1000), attempt + 1
        except Exception:
            if attempt == retries - 1:
                return None, int((time.time() - t0) * 1000), attempt + 1
            time.sleep(2 ** (attempt + 1))
    return None, int((time.time() - t0) * 1000), retries