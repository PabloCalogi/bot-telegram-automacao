import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


DEFAULT_DB_PATH = Path("data/offers.sqlite3")
ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")


def _normalize_asin(asin: str) -> str:
    normalized = asin.strip().upper()
    if not ASIN_PATTERN.fullmatch(normalized):
        raise ValueError("ASIN deve conter exatamente 10 caracteres alfanuméricos.")
    return normalized


@dataclass(frozen=True)
class StoredOffer:
    asin: str
    message_id: int | None
    price: float | None
    is_photo: bool | None


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
                        sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        message_id INTEGER,
                        price REAL,
                        is_photo INTEGER
                    )
                    """
                )
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(sent_offers)")
                }
                for column, definition in (
                    ("message_id", "INTEGER"),
                    ("price", "REAL"),
                    ("is_photo", "INTEGER"),
                ):
                    if column not in columns:
                        connection.execute(
                            f"ALTER TABLE sent_offers ADD COLUMN {column} {definition}"
                        )

    def has_been_sent(self, asin: str) -> bool:
        normalized_asin = _normalize_asin(asin)
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT 1 FROM sent_offers WHERE asin = ?",
                (normalized_asin,),
            ).fetchone()
        return row is not None

    def get_offer(self, asin: str) -> StoredOffer | None:
        normalized_asin = _normalize_asin(asin)
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                """
                SELECT asin, message_id, price, is_photo
                FROM sent_offers
                WHERE asin = ?
                """,
                (normalized_asin,),
            ).fetchone()
        if row is None:
            return None
        return StoredOffer(
            asin=row[0],
            message_id=row[1],
            price=row[2],
            is_photo=None if row[3] is None else bool(row[3]),
        )

    def record_sent(
        self,
        asin: str,
        message_id: int | None = None,
        price: Decimal | float | None = None,
        is_photo: bool | None = None,
    ) -> bool:
        normalized_asin = _normalize_asin(asin)
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO sent_offers
                        (asin, message_id, price, is_photo)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        normalized_asin,
                        message_id,
                        None if price is None else float(price),
                        None if is_photo is None else int(is_photo),
                    ),
                )
        return cursor.rowcount == 1

    def update_price(self, asin: str, price: Decimal | float) -> bool:
        normalized_asin = _normalize_asin(asin)
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                cursor = connection.execute(
                    "UPDATE sent_offers SET price = ? WHERE asin = ?",
                    (float(price), normalized_asin),
                )
        return cursor.rowcount == 1

    def remove_offer(self, asin: str) -> bool:
        normalized_asin = _normalize_asin(asin)
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM sent_offers WHERE asin = ?",
                    (normalized_asin,),
                )
        return cursor.rowcount == 1
