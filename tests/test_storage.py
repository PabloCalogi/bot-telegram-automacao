import tempfile
import sqlite3
import unittest
from decimal import Decimal
from contextlib import closing
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

    def test_migrates_existing_database_and_stores_message_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "offers.sqlite3"
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE sent_offers (
                        asin TEXT PRIMARY KEY,
                        sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO sent_offers (asin) VALUES (?)",
                    ("B000000001",),
                )

            store = SQLiteOfferStore(db_path)
            store.initialize()
            legacy_offer = store.get_offer("B000000001")
            self.assertIsNotNone(legacy_offer)
            self.assertIsNone(legacy_offer.message_id)

            self.assertTrue(
                store.record_sent(
                    "B000000002",
                    message_id=456,
                    price=Decimal("79.90"),
                    is_photo=True,
                )
            )
            saved_offer = store.get_offer("B000000002")
            self.assertIsNotNone(saved_offer)
            self.assertEqual(saved_offer.message_id, 456)
            self.assertEqual(saved_offer.price, 79.9)
            self.assertTrue(saved_offer.is_photo)

            self.assertTrue(store.update_price("B000000002", Decimal("74.50")))
            self.assertEqual(store.get_offer("B000000002").price, 74.5)
            self.assertTrue(store.remove_offer("B000000002"))
            self.assertIsNone(store.get_offer("B000000002"))


if __name__ == "__main__":
    unittest.main()
