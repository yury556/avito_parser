from typing import Optional
from loguru import logger
import psycopg2

from models import Item
from parser.export.base import ResultStorage


class PostgresStorage(ResultStorage):
    """
    Сохранение результатов парсинга в PostgreSQL с историей цен.
    """
    name = "postgres"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5433,
        dbname: str = "avito",
        user: str = "avito",
        password: str = "avito123",
    ):
        self.conn_params = dict(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
        )
        self._conn: Optional[psycopg2.extensions.connection] = None

    def _get_conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(**self.conn_params)
        return self._conn

    def save(self, ads: list[Item]) -> None:
        if not ads:
            return

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                for ad in ads:
                    avito_id = self._resolve_id(ad)
                    if avito_id is None:
                        continue

                    # 1. Upsert в ads
                    cur.execute("""
                        INSERT INTO ads (avito_id, url, title, price, seller, location, coords,
                                         description, total_views, today_views, is_promoted, phone,
                                         last_seen)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (avito_id) DO UPDATE SET
                            title        = EXCLUDED.title,
                            price        = EXCLUDED.price,
                            seller       = EXCLUDED.seller,
                            location     = EXCLUDED.location,
                            coords       = EXCLUDED.coords,
                            description  = EXCLUDED.description,
                            total_views  = EXCLUDED.total_views,
                            today_views  = EXCLUDED.today_views,
                            is_promoted  = EXCLUDED.is_promoted,
                            phone        = EXCLUDED.phone,
                            last_seen    = CURRENT_TIMESTAMP,
                            is_active    = TRUE
                    """, (
                        avito_id,
                        f"https://www.avito.ru/{ad.urlPath}" if ad.urlPath else "",
                        ad.title,
                        ad.priceDetailed.value if ad.priceDetailed else None,
                        ad.sellerId,
                        ad.location.name if ad.location else None,
                        self._get_coords(ad),
                        ad.description,
                        ad.total_views,
                        ad.today_views,
                        ad.isPromotion,
                        ad.phone,
                    ))

                    # 2. Если цена изменилась — записываем в price_history
                    cur.execute("""
                        SELECT price FROM ads WHERE avito_id = %s
                    """, (avito_id,))
                    row = cur.fetchone()

                    # row[0] — это уже НОВАЯ цена после upsert'а.
                    # Сравниваем: если в истории нет записи с такой ценой — пишем
                    cur.execute("""
                        SELECT 1 FROM price_history
                        WHERE avito_id = %s AND price = %s
                        LIMIT 1
                    """, (avito_id, ad.priceDetailed.value if ad.priceDetailed else None))
                    if cur.fetchone() is None:
                        cur.execute("""
                            INSERT INTO price_history (avito_id, price)
                            VALUES (%s, %s)
                        """, (avito_id, ad.priceDetailed.value if ad.priceDetailed else None))

                conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("PostgresStorage save failed")
            raise

    @staticmethod
    def _resolve_id(ad: Item) -> int | None:
        if isinstance(ad.id, int):
            return ad.id
        if isinstance(ad.id, dict):
            return ad.id.get("id") if isinstance(ad.id.get("id"), int) else None
        return None

    @staticmethod
    def _get_coords(ad: Item) -> str:
        if ad.coords and "lat" in ad.coords and "lng" in ad.coords:
            return f"{ad.coords['lat']};{ad.coords['lng']}"
        return ""