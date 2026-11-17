import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_channel_id: str
    min_discount_percent: Decimal = Decimal("15")
    amazon_associate_tag: str = ""
    product_identifiers: tuple[str, ...] = ()
    poll_interval_seconds: int = 3600

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
        )
