import importlib.util
import os
import sys
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2

ROOT = Path(os.environ.get("AUDIT05_ROOT", str(Path(__file__).resolve().parents[1])))
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

from parser_runs import finish_parser_run, sanitize_error, start_parser_run


class SourceContractTests(unittest.TestCase):
    def test_both_upserts_always_seen_and_keep_legacy_parsed_at(self):
        for name in ("avito_service.py", "entrypoint.sh"):
            source = (ROOT / name).read_text()
            self.assertIn("last_seen_at = EXCLUDED.last_seen_at", source)
            self.assertIn("first_seen_at = COALESCE(", source)
            self.assertIn("content_updated_at = CASE", source)
            self.assertIn("last_price_changed_at = CASE", source)
            self.assertIn("ELSE avito_original.ads.parsed_at", source)
            self.assertNotIn("WHERE avito_original.ads.price_rub", source)

    def test_consumers_use_observation_and_run_signals(self):
        postgres_io = (REPO / "airflow-avito-project/include/avito_pipeline/postgres_io.py").read_text()
        self.assertIn("WHERE last_seen_at > %s", postgres_io)
        self.assertIn("parsed_at=row[7]", postgres_io)

        watchdog = (REPO / "watchdog.py").read_text()
        self.assertIn("avito_original.parser_runs", watchdog)
        self.assertIn("max(last_seen_at)", watchdog)
        self.assertNotIn("max(parsed_at)", watchdog)

        alerts = (ROOT / "alerts_worker.py").read_text()
        self.assertIn("min(parsed_at) OVER (PARTITION BY avito_id) AS first_seen", alerts)
        self.assertIn("max(parsed_at) OVER (PARTITION BY avito_id) AS last_seen", alerts)
        self.assertNotIn('cur.execute("SELECT max(parsed_at) FROM avito_original.ads")', alerts)

        bot = (ROOT / "bot_service.py").read_text()
        self.assertIn("последний успешный цикл", bot)
        self.assertIn("max(content_updated_at)", bot)

    def test_empty_and_partial_cycles_have_explicit_statuses(self):
        for name in ("avito_service.py", "entrypoint.sh"):
            source = (ROOT / name).read_text()
            self.assertIn('"partial" if partial else "success"', source.replace("'", '"'))
            self.assertIn('finish_parser_run', source)
            self.assertIn('new_run_id', source)

    def test_error_is_truncated_and_secrets_are_masked(self):
        secret = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        error = sanitize_error(
            f"postgresql://user:dbpass@host/db password=supersecret token={secret} " + "x" * 2000
        )
        self.assertLessEqual(len(error), 1000)
        for value in ("dbpass", "supersecret", secret):
            self.assertNotIn(value, error)


class TimestampDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dbname = os.environ.get("TEST_POSTGRES_DB")
        if not cls.dbname:
            raise unittest.SkipTest("TEST_POSTGRES_DB не задан")
        if not cls.dbname.startswith("avito_tz05_test_"):
            raise RuntimeError("Тесты разрешены только в отдельной БД avito_tz05_test_*")
        cls.pg = {
            "host": os.environ.get("POSTGRES_HOST", "external-postgres"),
            "port": int(os.environ.get("POSTGRES_PORT", "5432")),
            "dbname": cls.dbname,
            "user": os.environ.get("POSTGRES_USER", "avito"),
            "password": os.environ.get("POSTGRES_PASSWORD", ""),
        }
        with psycopg2.connect(**cls.pg) as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA avito_original; CREATE SCHEMA midraw;")
            cur.execute("""
                CREATE TABLE avito_original.ads (
                    avito_id BIGINT PRIMARY KEY,
                    title TEXT,
                    price_rub INTEGER,
                    url TEXT,
                    location VARCHAR(256),
                    seller VARCHAR(64),
                    description TEXT,
                    image_keys JSONB DEFAULT '[]'::jsonb,
                    is_reserved BOOLEAN,
                    is_promotion BOOLEAN,
                    total_views INTEGER,
                    today_views INTEGER,
                    parsed_at TIMESTAMPTZ,
                    published_at TIMESTAMPTZ,
                    views_total INTEGER,
                    views_today INTEGER,
                    source_url TEXT,
                    new_notified_at TIMESTAMP
                );
                CREATE TABLE midraw.avito_ads (
                    avito_id BIGINT,
                    parsed_at TIMESTAMPTZ
                );
                CREATE TABLE avito_original.image_jobs (
                    avito_id BIGINT,
                    original_url TEXT,
                    position INTEGER,
                    job_type TEXT,
                    UNIQUE (avito_id, original_url)
                );
                CREATE TABLE avito_original.lot_pipeline_log (
                    avito_id BIGINT,
                    stage TEXT,
                    meta JSONB,
                    ts TIMESTAMPTZ DEFAULT now()
                );
            """)
            old = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
            cur.execute(
                "INSERT INTO avito_original.ads (avito_id, parsed_at) VALUES (910000, %s)",
                (old,),
            )
            cur.execute(
                "INSERT INTO midraw.avito_ads VALUES (910000, %s), (910000, %s)",
                (old - timedelta(days=2), old - timedelta(days=1)),
            )
            cur.execute((ROOT / "migrations/20261101_05_seen_timestamp_model.sql").read_text())
        spec = importlib.util.spec_from_file_location("audit05_avito_service", ROOT / "avito_service.py")
        cls.service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.service)

    def _row(self, avito_id):
        with psycopg2.connect(**self.pg) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT parsed_at, first_seen_at, last_seen_at,
                       content_updated_at, last_price_changed_at
                FROM avito_original.ads WHERE avito_id = %s
            """, (avito_id,))
            return cur.fetchone()

    @staticmethod
    def _ad(avito_id=910001, price=1000, description="описание"):
        return {
            "avito_id": avito_id,
            "title": "Тестовый лот",
            "price_rub": price,
            "url": f"https://www.avito.ru/test_{avito_id}",
            "location": "Тест",
            "seller": "test",
            "description": description,
            "image_keys": [],
            "image_urls": [],
            "is_reserved": False,
            "is_promotion": False,
            "total_views": 10,
            "today_views": 1,
        }

    def _write(self, ad, observed_at):
        return self.service._write_ads_to_postgres(
            self.pg, f"test_{observed_at.timestamp()}", [], [ad], observed_at=observed_at)

    def test_01_backfill_uses_midraw_and_does_not_invent_price_change(self):
        row = self._row(910000)
        self.assertEqual(row[1], datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(row[2], datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(row[0], row[3])
        self.assertIsNone(row[4])

    def test_02_new_repeat_price_and_description_scenarios(self):
        t1 = datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc)
        t2 = t1 + timedelta(minutes=10)
        t3 = t1 + timedelta(minutes=20)
        t4 = t1 + timedelta(minutes=30)

        self.assertEqual(self._write(self._ad(), t1), 1)
        self.assertEqual(self._row(910001), (t1, t1, t1, t1, None))

        self._write(self._ad(), t2)
        self.assertEqual(self._row(910001), (t1, t1, t2, t1, None))

        self._write(self._ad(price=900), t3)
        self.assertEqual(self._row(910001), (t3, t1, t3, t3, t3))

        self._write(self._ad(price=900, description="новое описание"), t4)
        self.assertEqual(self._row(910001), (t4, t1, t4, t4, t3))

        t5 = t1 + timedelta(minutes=40)
        changed_title = self._ad(price=900, description="новое описание")
        changed_title["title"] = "Новый заголовок"
        self._write(changed_title, t5)
        # Заголовок — контент, но parsed_at сохраняет прежние legacy-правила.
        self.assertEqual(self._row(910001), (t4, t1, t5, t5, t3))

    def test_03_empty_partial_and_failed_runs(self):
        for run_id, status, found, written in (
            ("empty_success", "success", 0, 0),
            ("partial_harvest", "partial", 3, 2),
        ):
            start_parser_run(self.pg, run_id, 2, "test")
            finish_parser_run(self.pg, run_id, status, found, written)

        start_parser_run(self.pg, "failed_secret", 1, "test")
        finish_parser_run(
            self.pg, "failed_secret", "failed", 0, 0,
            "password=supersecret token=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ " + "x" * 1500,
        )
        with psycopg2.connect(**self.pg) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT run_id, status, found_count, written_count, error,
                       started_at IS NOT NULL, finished_at IS NOT NULL
                FROM avito_original.parser_runs ORDER BY run_id
            """)
            rows = {row[0]: row[1:] for row in cur.fetchall()}
        self.assertEqual(rows["empty_success"][:3], ("success", 0, 0))
        self.assertEqual(rows["partial_harvest"][:3], ("partial", 3, 2))
        self.assertEqual(rows["failed_secret"][0], "failed")
        self.assertNotIn("supersecret", rows["failed_secret"][3])
        self.assertLessEqual(len(rows["failed_secret"][3]), 1000)
        self.assertTrue(all(row[4] and row[5] for row in rows.values()))

    def test_04_recent_empty_success_prevents_stale_and_silence(self):
        spec = importlib.util.spec_from_file_location("audit05_alerts", ROOT / "alerts_worker.py")
        alerts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(alerts)
        self.assertFalse(alerts._parser_stale(self.pg))
        with unittest.mock.patch.object(alerts, "send_failure") as failure:
            alerts._parser_silence_check(self.pg)
        failure.assert_not_called()

    def test_05_indexes_exist(self):
        with psycopg2.connect(**self.pg) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT indexname FROM pg_indexes
                WHERE schemaname = 'avito_original'
                  AND indexname IN ('ix_ads_last_seen_at', 'ix_parser_runs_finished_at')
            """)
            self.assertEqual({row[0] for row in cur.fetchall()},
                             {"ix_ads_last_seen_at", "ix_parser_runs_finished_at"})

    def test_06_entrypoint_upsert_matches_timestamp_model(self):
        source = (ROOT / "entrypoint.sh").read_text()
        embedded = source.split('python3 -c "', 1)[1].rsplit('\n"', 1)[0]
        namespace = {}
        exec(embedded.split("\nwhile True:", 1)[0], namespace)
        t1 = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
        ad = self._ad(avito_id=910002)
        with unittest.mock.patch.dict(os.environ, {"POSTGRES_DB": self.dbname}):
            namespace["write_to_postgres"]([ad], t1)
            namespace["write_to_postgres"]([ad], t1 + timedelta(minutes=10))
            changed_price = self._ad(avito_id=910002, price=800)
            namespace["write_to_postgres"]([changed_price], t1 + timedelta(minutes=20))
            changed_description = self._ad(avito_id=910002, price=800, description="другое")
            namespace["write_to_postgres"]([changed_description], t1 + timedelta(minutes=30))
        self.assertEqual(
            self._row(910002),
            (t1 + timedelta(minutes=30), t1, t1 + timedelta(minutes=30),
             t1 + timedelta(minutes=30), t1 + timedelta(minutes=20)),
        )

    def test_07_parse_endpoint_records_empty_partial_and_failed(self):
        body = {"urls": [], "postgres": self.pg}

        empty_parser = unittest.mock.Mock()
        empty_parser.parse.return_value = []
        with (
            unittest.mock.patch.object(self.service, "AvitoParse", return_value=empty_parser),
            unittest.mock.patch.object(self.service, "new_run_id", return_value="svc_test_empty"),
        ):
            self.assertEqual(self.service.parse_ads(body)["count"], 0)

        partial_parser = unittest.mock.Mock()
        partial_parser.parse.side_effect = RuntimeError("часть страниц недоступна")
        partial_parser.collected = [object()]
        with (
            unittest.mock.patch.object(self.service, "AvitoParse", return_value=partial_parser),
            unittest.mock.patch.object(self.service, "new_run_id", return_value="svc_test_partial"),
            unittest.mock.patch.object(self.service, "_item_to_dict", return_value=self._ad(910003)),
            unittest.mock.patch.object(self.service, "_write_ads_to_postgres", return_value=1),
        ):
            self.assertEqual(self.service.parse_ads(body)["count"], 1)

        failed_parser = unittest.mock.Mock()
        failed_parser.parse.side_effect = RuntimeError("password=supersecret")
        failed_parser.collected = []
        with (
            unittest.mock.patch.object(self.service, "AvitoParse", return_value=failed_parser),
            unittest.mock.patch.object(self.service, "new_run_id", return_value="svc_test_failed"),
        ):
            with self.assertRaises(RuntimeError):
                self.service.parse_ads(body)

        with psycopg2.connect(**self.pg) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT run_id, status, found_count, written_count, error
                FROM avito_original.parser_runs
                WHERE run_id LIKE 'svc_test_%'
            """)
            rows = {row[0]: row[1:] for row in cur.fetchall()}
        self.assertEqual(rows["svc_test_empty"][:3], ("success", 0, 0))
        self.assertEqual(rows["svc_test_partial"][:3], ("partial", 1, 1))
        self.assertEqual(rows["svc_test_failed"][:3], ("failed", 0, 0))
        self.assertNotIn("supersecret", rows["svc_test_failed"][3])


if __name__ == "__main__":
    unittest.main(verbosity=2)
