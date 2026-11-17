import re
import sqlite3
from contextlib import closing
from pathlib import Path


DEFAULT_DB_PATH = Path("data/offers.sqlite3")
ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")


def _normalize_asin(asin: str) -> str:
    normalized = asin.strip().upper()
    if not ASIN_PATTERN.fullmatch(normalized):
        raise ValueError("ASIN deve conter exatamente 10 caracteres alfanuméricos.")
    return normalized


class SQLiteOfferStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = str(db_path)

    def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sent_offers (
                        asin TEXT PRIMARY KEY,
                        sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

    def has_been_sent(self, asin: str) -> bool:
        normalized_asin = _normalize_asin(asin)
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT 1 FROM sent_offers WHERE asin = ?",
                (normalized_asin,),
            ).fetchone()
        return row is not None

    def record_sent(self, asin: str) -> bool:
        normalized_asin = _normalize_asin(asin)
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO sent_offers (asin) VALUES (?)",
                    (normalized_asin,),
                )
        return cursor.rowcount == 1
