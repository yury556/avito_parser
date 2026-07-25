import os

from cachelib.redis import RedisCache
from sqlalchemy.engine import URL


SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]

SQLALCHEMY_DATABASE_URI = str(
    URL.create(
        "postgresql+psycopg2",
        username=os.environ["SUPERSET_META_DB_USER"],
        password=os.environ["SUPERSET_META_DB_PASSWORD"],
        host=os.environ["SUPERSET_META_DB_HOST"],
        port=int(os.environ["SUPERSET_META_DB_PORT"]),
        database=os.environ["SUPERSET_META_DB_NAME"],
    )
)

REDIS_HOST = os.environ["SUPERSET_REDIS_HOST"]
REDIS_PORT = int(os.environ["SUPERSET_REDIS_PORT"])


class CeleryConfig:
    broker_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
    result_backend = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
    imports = ("superset.sql_lab",)
    worker_prefetch_multiplier = 10
    task_acks_late = True


CELERY_CONFIG = CeleryConfig
RESULTS_BACKEND = RedisCache(
    host=REDIS_HOST,
    port=REDIS_PORT,
    key_prefix="superset_results",
)

# Keep the embedded BI UI protected when it is exposed outside localhost later.
WTF_CSRF_ENABLED = True