import csv
import io
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "include"))

from avito_pipeline.csv_contract import AvitoAd, CSV_COLUMNS, ad_to_csv_row, csv_header


class CsvContractTest(unittest.TestCase):
    def test_header_matches_columns(self):
        self.assertEqual(csv_header(), ",".join(CSV_COLUMNS))

    def test_ad_to_csv_row_is_parseable(self):
        ad = AvitoAd(
            avito_id=1,
            title="Phone, with comma",
            price_rub=100,
            url="https://example.test/ad_1",
            location="Moscow",
            seller="seller",
            parsed_at=datetime(2026, 7, 5, tzinfo=timezone.utc),
        )

        row = ad_to_csv_row(ad)
        parsed = next(csv.reader(io.StringIO(row)))

        self.assertEqual(len(parsed), len(CSV_COLUMNS))
        self.assertEqual(parsed[0], "1")
        self.assertEqual(parsed[1], "Phone with comma")


if __name__ == "__main__":
    unittest.main()
