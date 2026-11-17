import tempfile
import unittest
from pathlib import Path

from app.storage import SQLiteOfferStore


class SQLiteOfferStoreTests(unittest.TestCase):
    def test_records_asin_once_and_normalizes_case(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteOfferStore(Path(directory) / "offers.sqlite3")
            store.initialize()

            self.assertFalse(store.has_been_sent("b000000001"))
            self.assertTrue(store.record_sent("b000000001"))
            self.assertTrue(store.has_been_sent("B000000001"))
            self.assertFalse(store.record_sent("B000000001"))

    def test_rejects_invalid_asin(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteOfferStore(Path(directory) / "offers.sqlite3")
            store.initialize()

            with self.assertRaises(ValueError):
                store.record_sent("not-an-asin")


if __name__ == "__main__":
    unittest.main()
