import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

from app.amazon.discovery import (
    DISCOVERY_MAX_ITEMS,
    DISCOVERY_MAX_PRICE,
    DISCOVERY_MIN_RATING,
    DISCOVERY_MIN_PRICE,
    DISCOVERY_MIN_REVIEWS,
)


PRODUCTS_FILE = Path(__file__).resolve().parent.parent / "products.txt"


def _read_products_file() -> tuple[str, ...]:
    if not PRODUCTS_FILE.is_file():
        return ()

    identifiers = []
    seen = set()
    with PRODUCTS_FILE.open(encoding="utf-8-sig") as products_file:
        for line in products_file:
            identifier = line.split("#", 1)[0].strip()
            if identifier and identifier not in seen:
                identifiers.append(identifier)
                seen.add(identifier)
    return tuple(identifiers)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_channel_id: str
    min_discount_percent: Decimal = Decimal("15")
    amazon_associate_tag: str = ""
    product_identifiers: tuple[str, ...] = ()
    poll_interval_seconds: int = 3600
    telegram_admin_chat_id: str = ""
    enable_auto_discovery: bool = False
    discovery_max_items: int = DISCOVERY_MAX_ITEMS
    discovery_min_price: Decimal = DISCOVERY_MIN_PRICE
    discovery_max_price: Decimal = DISCOVERY_MAX_PRICE
    discovery_min_rating: Decimal = DISCOVERY_MIN_RATING
    discovery_min_reviews: int = DISCOVERY_MIN_REVIEWS

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        values = {
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            "TELEGRAM_CHANNEL_ID": os.getenv("TELEGRAM_CHANNEL_ID", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(
                "Configure as seguintes variáveis no arquivo .env: "
                + ", ".join(missing)
            )

        try:
            min_discount_percent = Decimal(
                os.getenv("MIN_DISCOUNT_PERCENT", "15").strip()
            )
        except InvalidOperation as exc:
            raise ValueError(
                "MIN_DISCOUNT_PERCENT deve ser um número entre 0 e 100."
            ) from exc
        if (
            not min_discount_percent.is_finite()
            or min_discount_percent < 0
            or min_discount_percent > 100
        ):
            raise ValueError("MIN_DISCOUNT_PERCENT deve estar entre 0 e 100.")

        try:
            poll_interval_seconds = int(os.getenv("POLL_INTERVAL_SECONDS", "3600"))
        except ValueError as exc:
            raise ValueError(
                "POLL_INTERVAL_SECONDS deve ser um número inteiro de pelo menos 60."
            ) from exc
        if poll_interval_seconds < 60:
            raise ValueError("POLL_INTERVAL_SECONDS deve ser de pelo menos 60.")

        enable_auto_discovery = (
            os.getenv("ENABLE_AUTO_DISCOVERY", "false").strip().casefold()
            in {"true", "1"}
        )
        try:
            discovery_max_items = int(
                os.getenv("DISCOVERY_MAX_ITEMS", str(DISCOVERY_MAX_ITEMS)).strip()
            )
        except ValueError as exc:
            raise ValueError(
                "DISCOVERY_MAX_ITEMS deve ser um inteiro positivo."
            ) from exc
        if discovery_max_items < 1:
            raise ValueError("DISCOVERY_MAX_ITEMS deve ser um inteiro positivo.")

        try:
            discovery_min_price = Decimal(
                os.getenv("DISCOVERY_MIN_PRICE", str(DISCOVERY_MIN_PRICE)).strip()
            )
            discovery_max_price = Decimal(
                os.getenv("DISCOVERY_MAX_PRICE", str(DISCOVERY_MAX_PRICE)).strip()
            )
        except InvalidOperation as exc:
            raise ValueError(
                "DISCOVERY_MIN_PRICE e DISCOVERY_MAX_PRICE devem ser números válidos."
            ) from exc
        if (
            not discovery_min_price.is_finite()
            or not discovery_max_price.is_finite()
            or discovery_min_price < 0
            or discovery_max_price < discovery_min_price
        ):
            raise ValueError(
                "A faixa de descoberta deve ter preços finitos e não negativos, "
                "com DISCOVERY_MAX_PRICE maior ou igual a DISCOVERY_MIN_PRICE."
            )

        try:
            discovery_min_rating = Decimal(
                os.getenv(
                    "DISCOVERY_MIN_RATING",
                    str(DISCOVERY_MIN_RATING),
                ).strip()
            )
        except InvalidOperation as exc:
            raise ValueError(
                "DISCOVERY_MIN_RATING deve ser um número entre 0 e 5."
            ) from exc
        if (
            not discovery_min_rating.is_finite()
            or discovery_min_rating < 0
            or discovery_min_rating > 5
        ):
            raise ValueError("DISCOVERY_MIN_RATING deve estar entre 0 e 5.")

        try:
            discovery_min_reviews = int(
                os.getenv(
                    "DISCOVERY_MIN_REVIEWS",
                    str(DISCOVERY_MIN_REVIEWS),
                ).strip()
            )
        except ValueError as exc:
            raise ValueError(
                "DISCOVERY_MIN_REVIEWS deve ser um inteiro não negativo."
            ) from exc
        if discovery_min_reviews < 0:
            raise ValueError("DISCOVERY_MIN_REVIEWS deve ser um inteiro não negativo.")

        product_identifiers = _read_products_file()
        if not product_identifiers:
            product_identifiers = tuple(
                item.strip()
                for item in re.split(
                    r"[,\r\n]+", os.getenv("PRODUCT_IDENTIFIERS", "")
                )
                if item.strip()
            )

        return cls(
            telegram_bot_token=values["TELEGRAM_BOT_TOKEN"],
            telegram_channel_id=values["TELEGRAM_CHANNEL_ID"],
            min_discount_percent=min_discount_percent,
            amazon_associate_tag=os.getenv("AMAZON_ASSOCIATE_TAG", "").strip(),
            product_identifiers=product_identifiers,
            poll_interval_seconds=poll_interval_seconds,
            telegram_admin_chat_id=os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip(),
            enable_auto_discovery=enable_auto_discovery,
            discovery_max_items=discovery_max_items,
            discovery_min_price=discovery_min_price,
            discovery_max_price=discovery_max_price,
            discovery_min_rating=discovery_min_rating,
            discovery_min_reviews=discovery_min_reviews,
        )
