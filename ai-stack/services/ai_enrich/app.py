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
from normalize import normalize_lot
from jev_client import call_jev, condition_from_tags, universal_tags, JEV_MODEL

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

    # --- активные теги словаря для Noul-вопросов Jev ---
    active_tags = []
    try:
        cur.execute("SELECT tag_key FROM avito_original.tag_vocabulary WHERE status = 'active'")
        active_tags = [r[0] for r in cur.fetchall()]
    except Exception as tag_err:
        print(f"ai_enrich: словарь тегов недоступен: {tag_err}", flush=True)

    jev_enabled = os.environ.get("JEV_ENABLED", "1") == "1"
    jev_conf = float(os.environ.get("JEV_CONF", "0.70"))
    jev_tag_thr = float(os.environ.get("JEV_TAG_THRESHOLD", "0.75"))

    def _process_one(job):
        """Worker: нормализация → Jev (быстро) → fallback DeepSeek (БД не касается)."""
        avito_id, title, desc, price_rub, url = job
        t, d, obfuscated = normalize_lot(title or "", desc or "")
        # --- тир 1: Jev ---
        if jev_enabled and active_tags:
            try:
                t0 = time.monotonic()
                jr = call_jev(OPENROUTER_API_KEY, t, d, active_tags)
                jdur = int((time.monotonic() - t0) * 1000)
                if jr.get("ok"):
                    if jr["confidence"] >= jev_conf:
                        tags_uni = universal_tags(jr["tags"], obfuscated, jev_tag_thr)
                        return {"ok": True, "avito_id": avito_id,
                                "tier": "jev", "duration_ms": jdur, "attempts": 1,
                                "data": {
                                    "category": jr["category"],
                                    "brand": jr["brand"],
                                    "model": None,  # чип ставит slug-валидатор
                                    "tags": tags_uni,
                                    "price_rub": price_rub,
                                },
                                "condition": condition_from_tags(jr["tags"]),
                                "tags_universal": tags_uni,
                                "jev_confidence": jr["confidence"],
                                "title": title, "desc": desc, "price_rub": price_rub,
                                "url": url}
                    print(f"ai_enrich: {avito_id}: Jev conf {jr['confidence']:.2f} "
                          f"< {jev_conf} → карантин", flush=True)
                    return {"ok": True, "avito_id": avito_id,
                            "tier": "quarantine", "duration_ms": jdur, "attempts": 1,
                            "quarantine_reason": f"conf {jr['confidence']:.2f} < {jev_conf}",
                            "jev_confidence": jr["confidence"],
                            "title": title, "desc": desc, "price_rub": price_rub, "url": url}
                else:
                    print(f"ai_enrich: {avito_id}: Jev fail ({jr.get('error')}) → карантин",
                          flush=True)
                    return {"ok": True, "avito_id": avito_id,
                            "tier": "quarantine", "duration_ms": jdur, "attempts": 1,
                            "quarantine_reason": f"jev error: {str(jr.get('error'))[:120]}",
                            "title": title, "desc": desc, "price_rub": price_rub, "url": url}
            except Exception as je:
                print(f"ai_enrich: {avito_id}: Jev exception {je} → карантин", flush=True)
                return {"ok": True, "avito_id": avito_id,
                        "tier": "quarantine", "duration_ms": 0, "attempts": 1,
                        "quarantine_reason": f"jev exception: {str(je)[:120]}",
                        "title": title, "desc": desc, "price_rub": price_rub, "url": url}
        # --- тир 2: DeepSeek (как раньше, на нормализованном тексте) ---
        try:
            messages = _build_messages(t or "", d or "")
            result, duration_ms, attempts = _call_openrouter(messages)
            if isinstance(result, dict) and "category" in result:
                return {"ok": True, "avito_id": avito_id, "data": result,
                        "tier": "deepseek",
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
                    if out.get("tier") == "quarantine":
                        cur.execute("""
                            INSERT INTO avito_original.quarantine
                                (avito_id, reason, jev_confidence)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (avito_id) DO UPDATE
                                SET reason=EXCLUDED.reason,
                                    jev_confidence=EXCLUDED.jev_confidence
                        """, (out["avito_id"], out.get("quarantine_reason"),
                              out.get("jev_confidence")))
                        cur.execute("""
                            INSERT INTO avito_original.lot_pipeline_log
                                (avito_id, stage, meta) VALUES (%s, 'quarantine_in', %s)
                        """, (out["avito_id"], json.dumps(
                            {"reason": out.get("quarantine_reason")})))
                        conn.commit()
                        results.append({"avito_id": out["avito_id"],
                                        "status": "quarantined",
                                        "reason": out.get("quarantine_reason")})
                    elif out.get("tier") == "jev":
                        cur.execute("""
                            INSERT INTO detail.ads
                                (avito_id, category, brand, model, model_ai, chip, tags,
                                 input_title, input_description, price_rub,
                                 ai_model, ai_version, ai_processed_at, ai_status,
                                 ai_duration_ms, ai_attempts,
                                 condition, tags_universal, jev_confidence, tier)
                            VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s, %s,
                                    %s, 'jev', NOW(), 'success', %s, 1,
                                    %s, %s, %s, 'jev')
                            ON CONFLICT (avito_id) DO UPDATE SET
                                category=EXCLUDED.category, brand=EXCLUDED.brand,
                                model=EXCLUDED.model, model_ai=NULL,
                                chip=EXCLUDED.chip, tags=EXCLUDED.tags,
                                price_rub=EXCLUDED.price_rub,
                                ai_processed_at=NOW(), ai_status='success',
                                ai_duration_ms=EXCLUDED.ai_duration_ms,
                                ai_attempts=1,
                                condition=EXCLUDED.condition,
                                tags_universal=EXCLUDED.tags_universal,
                                jev_confidence=EXCLUDED.jev_confidence,
                                tier='jev'
                        """, (
                            out["avito_id"],
                            out["data"].get("category"),
                            out["data"].get("brand"),
                            model_fixed,
                            chip,
                            json.dumps(out["tags_universal"]),
                            out["title"], out["desc"], out["price_rub"],
                            JEV_MODEL,
                            out.get("duration_ms"),
                            out.get("condition"),
                            out.get("tags_universal"),
                            out.get("jev_confidence"),
                        ))
                    else:
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
                            ai_attempts=EXCLUDED.ai_attempts,
                            tier=COALESCE(detail.ads.tier, 'deepseek')
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
                         json.dumps({"tier": out.get("tier", "deepseek"),
                                     "attempts": out.get("attempts")})),
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


# ──── карантин ────

def _slug_tag(ru_text: str) -> str:
    """Слаг тега из русского текста DeepSeek: lowercase, дефисы."""
    s = (ru_text or "").strip().lower()
    s = re.sub(r"[^\wа-яё0-9]+", "-", s, flags=re.IGNORECASE)
    s = s.strip("-")[:40]
    return s if s else "прочее"


@app.post("/quarantine_process")
def quarantine_process(req: BatchRequest):
    """Воркер: пачка из карантина → DeepSeek → detail.ads + новые теги в словарь."""
    limit = min(req.limit or 10, 50)
    tag_autoapprove = os.environ.get("TAG_AUTOAPPROVE", "1") == "1"
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
    )
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (q.avito_id)
               q.avito_id, q.reason, m.title, m.price_rub, m.url,
               coalesce(d2.input_description, '') AS desc
        FROM avito_original.quarantine q
        JOIN midraw.avito_ads m ON m.avito_id = q.avito_id
        LEFT JOIN detail.ads d2 ON d2.avito_id = q.avito_id
        WHERE q.status = 'waiting'
        ORDER BY q.avito_id, m.parsed_at DESC
        LIMIT %s
    """, (limit,))
    jobs = cur.fetchall()
    done = failed = new_tags = 0
    items = []
    for avito_id, reason, title, price_rub, url, desc in jobs:
        t0 = time.monotonic()
        t, d, _ob = normalize_lot(title or "", desc or "")
        result, duration_ms, attempts = _call_openrouter(_build_messages(t or "", d or ""))
        if not isinstance(result, dict) or "category" not in result:
            cur.execute("UPDATE avito_original.quarantine SET status='failed' WHERE avito_id=%s",
                        (avito_id,))
            conn.commit()
            failed += 1
            items.append({"avito_id": avito_id, "status": "failed"})
            continue
        try:
            model_fixed, model_ai, chip, note = validate_model(
                result.get("model"), url)
            # --- обратная связь: новые теги → словарь ---
            for tag in (result.get("tags") or []):
                key = _slug_tag(tag)
                if not key:
                    continue
                cur.execute("""
                    INSERT INTO avito_original.tag_vocabulary (tag_key, grp, label_ru, created_by, status)
                    VALUES (%s, 'сигнал', %s, 'deepseek', %s)
                    ON CONFLICT (tag_key) DO NOTHING
                """, (key, tag, 'active' if tag_autoapprove else 'pending'))
                if cur.rowcount:
                    new_tags += 1
            tags_uni = [_slug_tag(tag) for tag in (result.get("tags") or []) if _slug_tag(tag)]
            cur.execute("""
                INSERT INTO detail.ads
                    (avito_id, category, brand, model, model_ai, chip, tags,
                     input_title, input_description, price_rub,
                     ai_model, ai_version, ai_processed_at, ai_status,
                     ai_duration_ms, ai_attempts,
                     tags_universal, tier)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, NOW(), 'success', %s, %s, %s, 'quarantine_ds')
                ON CONFLICT (avito_id) DO UPDATE SET
                    category=EXCLUDED.category, brand=EXCLUDED.brand,
                    model=EXCLUDED.model, model_ai=EXCLUDED.model_ai,
                    chip=EXCLUDED.chip, tags=EXCLUDED.tags,
                    price_rub=EXCLUDED.price_rub,
                    ai_processed_at=NOW(), ai_status='success',
                    ai_duration_ms=EXCLUDED.ai_duration_ms,
                    ai_attempts=EXCLUDED.ai_attempts,
                    tags_universal=EXCLUDED.tags_universal,
                    tier='quarantine_ds'
            """, (
                avito_id, result.get("category"), result.get("brand"),
                model_fixed, model_ai, chip,
                json.dumps(result.get("tags", [])),
                title, desc, price_rub or result.get("price_rub"),
                AI_MODEL, PROMPT_VERSION,
                duration_ms, attempts,
                tags_uni,
            ))
            cur.execute("""
                UPDATE avito_original.quarantine
                SET status='done', processed_at=NOW(), result=%s
                WHERE avito_id=%s
            """, (f"категория={result.get('category')}, новых тегов={new_tags}", avito_id))
            cur.execute("""
                INSERT INTO avito_original.lot_pipeline_log
                    (avito_id, stage, duration_ms, meta)
                VALUES (%s, 'quarantine_done', %s, %s)
            """, (avito_id, duration_ms,
                  json.dumps({"attempts": attempts, "new_tags": new_tags,
                              "category": result.get("category")})))
            conn.commit()
            done += 1
            items.append({"avito_id": avito_id, "status": "done",
                          "category": result.get("category")})
        except Exception as e:
            conn.rollback()
            cur = conn.cursor()
            cur.execute("UPDATE avito_original.quarantine SET status='failed' WHERE avito_id=%s",
                        (avito_id,))
            conn.commit()
            failed += 1
            items.append({"avito_id": avito_id, "status": "failed", "error": str(e)[:120]})
    # инкремент usage_count у использованных тегов
    try:
        cur.execute("""
            UPDATE avito_original.tag_vocabulary v
            SET usage_count = usage_count + 1
            WHERE tag_key IN (
                SELECT unnest(tags_universal) FROM detail.ads
                WHERE tier IN ('jev','quarantine_ds')
                  AND ai_processed_at > now() - interval '10 minutes')
        """)
        conn.commit()
    except Exception:
        conn.rollback()
    conn.close()
    return {"done": done, "failed": failed, "new_tags": new_tags, "items": items}