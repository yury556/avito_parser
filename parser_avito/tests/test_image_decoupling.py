import importlib.util
import os
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(os.environ.get("AUDIT03_ROOT", str(Path(__file__).resolve().parents[1])))


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


downloader = load_module("audit03_image_downloader", "image_downloader_service.py")
tg_alerts = load_module("audit03_tg_alerts", "tg_alerts.py")


class FakeResponse:
    def __init__(self, chunks, content_type="image/jpeg", headers=None):
        self.chunks = chunks
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.status_code = 200
        self.closed = False
        self.chunks_read = 0

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            self.chunks_read += 1
            yield chunk

    def close(self):
        self.closed = True


class FakeCursor:
    def __init__(self, description=None, rows=()):
        self.description = description or []
        self.rows = rows
        self.calls = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor

    def cursor(self):
        return self.fake_cursor


class ImageDecouplingTests(unittest.TestCase):
    def test_parser_contains_no_image_network_download(self):
        entrypoint = (ROOT / "entrypoint.sh").read_text()
        service = (ROOT / "avito_service.py").read_text()
        for forbidden in ("Minio(", "_requests.get", "download_and_store", "create_storage("):
            self.assertNotIn(forbidden, entrypoint)
            self.assertNotIn(forbidden, service)
        self.assertIn("image_jobs", entrypoint)
        self.assertIn("image_jobs", service)

    def test_stream_size_limit_stops_before_storing(self):
        response = FakeResponse([b"x" * (512 * 1024)] * 3)
        client = mock.Mock()
        old_limit = downloader.SETTINGS["max_image_size_mb"]
        downloader.SETTINGS["max_image_size_mb"] = 1
        try:
            with mock.patch.object(downloader.requests, "get", return_value=response):
                with self.assertRaises(downloader.ImageProcessingError) as caught:
                    downloader.download_image(client, 123, 0, "https://cdn.avito.st/image.jpg")
            self.assertTrue(caught.exception.permanent)
            self.assertEqual(response.chunks_read, 3)
            self.assertTrue(response.closed)
            client.put_object.assert_not_called()
        finally:
            downloader.SETTINGS["max_image_size_mb"] = old_limit

    def test_content_length_and_type_are_validated(self):
        response = FakeResponse([], headers={"Content-Length": str(11 * 1024 * 1024)})
        with mock.patch.object(downloader.requests, "get", return_value=response):
            with self.assertRaises(downloader.ImageProcessingError):
                downloader.download_image(mock.Mock(), 123, 0, "https://cdn.avito.st/image.jpg")
        self.assertEqual(response.chunks_read, 0)

        wrong_type = FakeResponse([], content_type="text/html")
        with mock.patch.object(downloader.requests, "get", return_value=wrong_type):
            with self.assertRaises(downloader.ImageProcessingError) as caught:
                downloader.download_image(mock.Mock(), 123, 0, "https://cdn.avito.st/image.jpg")
        self.assertTrue(caught.exception.permanent)
        self.assertEqual(wrong_type.chunks_read, 0)

    def test_one_image_failure_does_not_stop_others(self):
        entry = {"key": "123/1.jpg", "original_url": "https://cdn.avito.st/2.jpg"}
        with mock.patch.object(
            downloader,
            "download_image",
            side_effect=[downloader.ImageProcessingError("failed"), entry],
        ):
            result = downloader.download_and_upload(
                mock.Mock(), 123, ["https://cdn.avito.st/1.jpg", "https://cdn.avito.st/2.jpg"]
            )
        self.assertEqual(result, [entry])

    def test_retry_backoff_is_delayed_capped_and_finite(self):
        first = downloader.retry_at(1)
        second = downloader.retry_at(2)
        capped = downloader.retry_at(100)
        now = datetime.now(timezone.utc)
        self.assertGreaterEqual((first - now).total_seconds(), downloader.SETTINGS["retry_base_seconds"] - 1)
        self.assertGreater(second, first)
        self.assertLessEqual(
            (capped - now).total_seconds(), downloader.SETTINGS["retry_max_seconds"] + 1
        )

        cursor = FakeCursor()
        conn = FakeConnection(cursor)

        @contextmanager
        def fake_conn():
            yield conn

        job = {"id": 1, "attempt_count": downloader.SETTINGS["max_attempts"]}
        with mock.patch.object(downloader, "postgres_conn", fake_conn):
            downloader.finish_job(job, "retry", "still unavailable")
        params = cursor.calls[0][1]
        self.assertEqual(params[0], "permanent_failure")
        self.assertIsNone(params[2])

    def test_failed_job_does_not_block_remaining_queue(self):
        jobs = [
            {"id": 1, "avito_id": 10, "job_type": "image"},
            {"id": 2, "avito_id": 20, "job_type": "image"},
        ]
        with (
            mock.patch.object(downloader, "enqueue_missing_detail_jobs", return_value=0),
            mock.patch.object(downloader, "ensure_bucket", return_value=mock.Mock()),
            mock.patch.object(downloader, "claim_jobs", return_value=jobs),
            mock.patch.object(
                downloader,
                "process_image_job",
                side_effect=[downloader.ImageProcessingError("failed"), None],
            ) as process,
            mock.patch.object(downloader, "finish_job") as finish,
        ):
            self.assertEqual(downloader.process_batch(), 2)
        self.assertEqual(process.call_count, 2)
        finish.assert_called_once()

    def test_ready_query_uses_skip_locked_and_next_retry_at(self):
        cursor = FakeCursor()
        conn = FakeConnection(cursor)

        @contextmanager
        def fake_conn():
            yield conn

        with mock.patch.object(downloader, "postgres_conn", fake_conn):
            self.assertEqual(downloader.claim_jobs(20), [])
        sql = cursor.calls[0][0]
        self.assertIn("FOR UPDATE OF j SKIP LOCKED", sql)
        self.assertIn("j.next_retry_at <= now()", sql)
        self.assertIn("a.parsed_at DESC", sql)

    def test_telegram_without_photo_sends_text(self):
        ad = {
            "avito_id": 123,
            "title": "Test lot",
            "price": 1000,
            "url": "https://www.avito.ru/test_123",
            "location": "Москва",
        }
        with (
            mock.patch.object(tg_alerts, "_photo_source", return_value=None),
            mock.patch.object(tg_alerts, "_send", return_value=True) as send,
            mock.patch.object(tg_alerts, "_log_alert"),
        ):
            self.assertTrue(tg_alerts.send_new_lot(ad))
        self.assertIn("Test lot", send.call_args.args[0]["text"])

    def test_missing_keys_and_original_fallback_send_text_or_url(self):
        with mock.patch.object(tg_alerts, "_minio", return_value=None):
            self.assertIsNone(tg_alerts._photo_source({"avito_id": 123, "image_keys": []}))
        with mock.patch.object(tg_alerts, "_minio", return_value=None):
            source = tg_alerts._photo_source({
                "avito_id": 123,
                "image_keys": [{"key": "123/0.jpg", "original_url": "https://cdn.avito.st/1.jpg"}],
            })
        self.assertEqual(source, ("url", "https://cdn.avito.st/1.jpg"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
