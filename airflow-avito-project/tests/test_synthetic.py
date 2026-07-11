import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "include"))

from avito_pipeline.synthetic import synthetic_ads


class SyntheticAdsTest(unittest.TestCase):
    def test_synthetic_ads_have_stable_shape(self):
        ads = synthetic_ads()

        self.assertGreaterEqual(len(ads), 3)
        self.assertTrue(all(ad.avito_id for ad in ads))
        self.assertTrue(all(ad.title for ad in ads))
        self.assertTrue(all(ad.url.startswith("https://www.avito.ru/") for ad in ads))


if __name__ == "__main__":
    unittest.main()
