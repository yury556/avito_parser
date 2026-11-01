import os
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import psycopg2

import alerts_worker
import notification_outbox as outbox
import tg_alerts


DB = dict(
    host=os.environ.get("POSTGRES_HOST", "external-postgres"),
    port=os.environ.get("POSTGRES_PORT", "5432"),
    dbname=os.environ.get("POSTGRES_DB", "avito_outbox_test"),
    user=os.environ.get("POSTGRES_USER", "avito"),
    password=os.environ.get("POSTGRES_PASSWORD", ""),
)


def lot(avito_id=1001, kind="new"):
    value = {
        "avito_id": avito_id,
        "model": "Test model",
        "title": "Test lot",
        "price": 100.0,
        "url": f"https://www.avito.ru/test_{avito_id}",
        "location": "Москва",
        "description": "",
        "tags": [],
        "first_seen": datetime.now(timezone.utc),
        "image_keys": [],
    }
    if kind == "good":
        value.update(median=150.0, benefit_pct=-33)
    return value


class TelegramCompatibilityTests(unittest.TestCase):
    def setUp(self):
        tg_alerts._lot_msg_times.clear()

    def test_new_text_and_photo_share_budget_and_log(self):
        for photo in (None, ("url", "https://cdn.avito.st/test.jpg")):
            with self.subTest(photo=bool(photo)):
                with (
                    mock.patch.object(tg_alerts, "_photo_source", return_value=photo),
                    mock.patch.object(tg_alerts, "_send", return_value=True),
                    mock.patch.object(tg_alerts, "_send_photo", return_value=True),
                    mock.patch.object(tg_alerts, "_log_alert") as log,
                ):
                    self.assertTrue(tg_alerts.send_new_lot(lot()))
                    log.assert_called_once()
        self.assertEqual(len(tg_alerts._lot_msg_times), 2)

    def test_http_500_has_no_immediate_second_request(self):
        with (
            mock.patch.object(tg_alerts, "_photo_source", return_value=None),
            mock.patch.object(
                tg_alerts, "_send",
                return_value={"ok": False, "status": 500, "error": "HTTP 500"},
            ) as send,
        ):
            result = tg_alerts.deliver_lot(lot(), "new")
        self.assertFalse(result["ok"])
        send.assert_called_once()

    def test_retry_after_is_parsed(self):
        result = tg_alerts._response_result(
            429, {"ok": False, "parameters": {"retry_after": 120}},
        )
        self.assertEqual(result["retry_after"], 120)

    def test_temporary_mute_does_not_block_system_failure(self):
        with mock.patch.object(tg_alerts, "send_message", return_value=True) as send:
            self.assertTrue(tg_alerts.send_failure("test"))
        send.assert_called_once()

    def test_mute_all_stops_lot_sender(self):
        with (
            mock.patch.dict(os.environ, {"HEARTBEAT_FILE": "/tmp/outbox_test_heartbeat"}),
            mock.patch.object(alerts_worker, "_mute_all_on", return_value=True),
            mock.patch.object(alerts_worker, "send_pending") as send,
        ):
            alerts_worker.alert_tick(DB)
        send.assert_not_called()


@unittest.skipUnless(os.environ.get("OUTBOX_TEST_DB") == "1", "requires isolated test DB")
class OutboxIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DB["dbname"].endswith("_test"):
            raise RuntimeError("Refusing outbox tests outside a *_test database")
        with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS detail")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS detail.ads (
                    avito_id BIGINT PRIMARY KEY,
                    new_notified_at TIMESTAMPTZ,
                    alerted_at TIMESTAMPTZ,
                    alerted_price NUMERIC
                )
                """
            )

    def setUp(self):
        self.old_limits = (
            outbox.NEW_PER_HOUR, outbox.MAX_LOT_MSGS_PER_HOUR,
            outbox.RETRY_BASE, outbox.RETRY_MAX,
        )
        outbox.NEW_PER_HOUR = 100
        outbox.MAX_LOT_MSGS_PER_HOUR = 100
        outbox.RETRY_BASE = 30
        outbox.RETRY_MAX = 1800
        with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE avito_original.notification_outbox")
            cur.execute("TRUNCATE detail.ads")

    def tearDown(self):
        (
            outbox.NEW_PER_HOUR, outbox.MAX_LOT_MSGS_PER_HOUR,
            outbox.RETRY_BASE, outbox.RETRY_MAX,
        ) = self.old_limits

    def rows(self):
        with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, avito_id, kind, reason_key, status, attempt_count,
                       next_retry_at, telegram_message_id
                FROM avito_original.notification_outbox ORDER BY id
                """
            )
            return cur.fetchall()

    def test_parallel_scans_create_one_row(self):
        barrier = threading.Barrier(2)
        errors = []

        def scan():
            try:
                barrier.wait(timeout=5)
                outbox.enqueue_lot(DB, lot(), "new")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=scan) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len(self.rows()), 1)

    def test_parallel_senders_deliver_once(self):
        outbox.enqueue_lot(DB, lot(), "new")
        barrier = threading.Barrier(2)
        delivered = []

        def fake_deliver(payload, kind):
            delivered.append(payload["avito_id"])
            return {"ok": True, "message_id": 901}

        def sender():
            barrier.wait(timeout=5)
            outbox.send_pending(DB, limit=1)

        with (
            mock.patch.object(outbox, "deliver_lot", side_effect=fake_deliver),
            mock.patch.object(outbox.time, "sleep"),
        ):
            threads = [threading.Thread(target=sender) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual(delivered, [1001])
        self.assertEqual([r[4] for r in self.rows()], ["sent"])

    def test_repeat_tick_after_sent_creates_no_message(self):
        outbox.enqueue_lot(DB, lot(), "new")
        with (
            mock.patch.object(outbox, "deliver_lot", return_value={"ok": True, "message_id": 1}),
            mock.patch.object(outbox.time, "sleep"),
        ):
            outbox.send_pending(DB)
        outbox.enqueue_lot(DB, lot(), "new")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0][4], "sent")

    def test_http_500_and_timeout_use_backoff(self):
        results = (
            {"ok": False, "status": 500, "error": "HTTP 500"},
            {"ok": False, "error": "TimeoutError", "unknown_result": True},
        )
        for result in results:
            with self.subTest(result=result):
                self.setUp()
                outbox.enqueue_lot(DB, lot(), "new")
                with mock.patch.object(outbox, "deliver_lot", return_value=result):
                    outbox.send_pending(DB)
                row = self.rows()[0]
                self.assertEqual(row[4], "failed")
                self.assertEqual(row[5], 1)
                self.assertGreater(row[6], datetime.now(timezone.utc) + timedelta(seconds=20))

    def test_http_429_honors_retry_after(self):
        outbox.enqueue_lot(DB, lot(), "new")
        result = {"ok": False, "status": 429, "error": "Too Many Requests", "retry_after": 120}
        with mock.patch.object(outbox, "deliver_lot", return_value=result):
            outbox.send_pending(DB)
        row = self.rows()[0]
        self.assertEqual(row[4], "failed")
        self.assertGreater(row[6], datetime.now(timezone.utc) + timedelta(seconds=100))

    def test_stale_claim_recovers_after_restart(self):
        outbox.enqueue_lot(DB, lot(), "new")
        first = outbox.claim_next(DB)
        with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE avito_original.notification_outbox SET claimed_at = now() - interval '2 hours'"
            )
        self.assertEqual(outbox.recover_stale(DB), 1)
        with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
            cur.execute("UPDATE avito_original.notification_outbox SET next_retry_at = now()")
        second = outbox.claim_next(DB)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["attempt_count"], 2)

    def test_send_message_and_photo_store_ids_and_legacy_marks(self):
        new = lot(1001, "new")
        good = lot(1002, "good")
        with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO detail.ads (avito_id) VALUES (1001), (1002)")
        outbox.enqueue_lot(DB, new, "new")
        outbox.enqueue_lot(DB, good, "good")
        with (
            mock.patch.object(tg_alerts, "_photo_source", side_effect=lambda ad: (
                ("url", "https://cdn.avito.st/test.jpg") if ad["avito_id"] == 1002 else None
            )),
            mock.patch.object(tg_alerts, "_send", return_value={"ok": True, "message_id": 456}),
            mock.patch.object(tg_alerts, "_send_photo", return_value={"ok": True, "message_id": 457}),
            mock.patch.object(tg_alerts, "_log_alert"),
            mock.patch.object(outbox.time, "sleep"),
        ):
            self.assertEqual(outbox.send_pending(DB), 2)
        rows = {r[2]: r for r in self.rows()}
        self.assertEqual(rows["new"][7], 456)
        self.assertEqual(rows["good"][7], 457)
        with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
            cur.execute("SELECT new_notified_at IS NOT NULL, alerted_at IS NOT NULL, alerted_price FROM detail.ads ORDER BY avito_id")
            marks = cur.fetchall()
        self.assertTrue(marks[0][0])
        self.assertTrue(marks[1][1])
        self.assertEqual(float(marks[1][2]), 100.0)

    def test_photo_new_consumes_new_hour_budget(self):
        outbox.NEW_PER_HOUR = 1
        outbox.enqueue_lots(DB, [lot(1001), lot(1002)], "new")
        with (
            mock.patch.object(tg_alerts, "_photo_source", return_value=("url", "https://cdn.avito.st/test.jpg")),
            mock.patch.object(tg_alerts, "_send_photo", return_value={"ok": True, "message_id": 456}),
            mock.patch.object(tg_alerts, "_log_alert"),
        ):
            outbox.send_pending(DB)
        self.assertEqual([r[4] for r in self.rows()], ["sent", "pending"])

    def test_exhausted_total_limit_leaves_pending(self):
        outbox.MAX_LOT_MSGS_PER_HOUR = 1
        outbox.enqueue_lots(DB, [lot(1001), lot(1002)], "new")
        with (
            mock.patch.object(outbox, "deliver_lot", return_value={"ok": True, "message_id": 1}),
            mock.patch.object(outbox.time, "sleep"),
        ):
            outbox.send_pending(DB)
        self.assertEqual([r[4] for r in self.rows()], ["sent", "pending"])

    def test_good_repeat_requires_price_drop(self):
        self.assertEqual(outbox.enqueue_lot(DB, lot(1001, "good"), "good"), 1)
        unchanged = lot(1001, "good") | {
            "alerted_at": datetime.now(timezone.utc), "alerted_price": 100.0,
        }
        self.assertEqual(outbox.enqueue_lot(DB, unchanged, "good"), 0)
        dropped = unchanged | {"price": 97.0}
        self.assertEqual(outbox.enqueue_lot(DB, dropped, "good"), 1)
        self.assertEqual([r[3] for r in self.rows()], ["initial", "price:97"])

    def test_temporary_lot_mute_leaves_job_pending(self):
        outbox.enqueue_lot(DB, lot(), "new")
        with (
            mock.patch.object(outbox, "lots_muted", return_value=True),
            mock.patch.object(outbox, "deliver_lot") as deliver,
        ):
            self.assertEqual(outbox.send_pending(DB), 0)
        deliver.assert_not_called()
        self.assertEqual(self.rows()[0][4], "pending")


if __name__ == "__main__":
    unittest.main(verbosity=2)
