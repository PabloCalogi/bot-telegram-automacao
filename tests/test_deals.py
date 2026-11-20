import os
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.amazon.models import Product
from app.config import Settings
from app.deals import analyze_discount


def make_product(
    original_price: Decimal | None,
    current_price: Decimal,
) -> Product:
    return Product(
        asin="B000000001",
        title="Produto de teste",
        original_price=original_price,
        current_price=current_price,
        image_url=None,
        source_url="https://www.amazon.com.br/dp/B000000001",
    )


class DealAnalysisTests(unittest.TestCase):
    def test_approves_only_discounts_strictly_above_threshold(self):
        exactly_at_threshold = analyze_discount(
            make_product(Decimal("100"), Decimal("85"))
        )
        above_threshold = analyze_discount(
            make_product(Decimal("100"), Decimal("84.99"))
        )

        self.assertFalse(exactly_at_threshold.approved)
        self.assertEqual(exactly_at_threshold.discount_percent, Decimal("15"))
        self.assertTrue(above_threshold.approved)
        self.assertEqual(above_threshold.discount_percent, Decimal("15.01"))

    def test_missing_original_price_is_not_approved(self):
        result = analyze_discount(make_product(None, Decimal("49.90")))

        self.assertFalse(result.approved)
        self.assertIsNone(result.discount_percent)

    def test_increased_price_is_not_approved(self):
        result = analyze_discount(
            make_product(Decimal("100"), Decimal("105"))
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.discount_percent, Decimal("-5"))

    def test_rejects_invalid_prices_and_threshold(self):
        with self.assertRaises(ValueError):
            analyze_discount(make_product(Decimal("0"), Decimal("1")))
        with self.assertRaises(ValueError):
            analyze_discount(
                make_product(Decimal("100"), Decimal("80")),
                Decimal("101"),
            )

    def test_loads_monitoring_settings_from_environment(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHANNEL_ID": "@test-channel",
            "MIN_DISCOUNT_PERCENT": "12.5",
            "AMAZON_ASSOCIATE_TAG": "associado-20",
            "PRODUCT_IDENTIFIERS": (
                "B000000001, https://www.amazon.com.br/dp/B000000002\n"
                "B000000003"
            ),
            "POLL_INTERVAL_SECONDS": "120",
        }
        with patch.dict(os.environ, environment):
            settings = Settings.from_env()

        self.assertEqual(settings.min_discount_percent, Decimal("12.5"))
        self.assertEqual(settings.amazon_associate_tag, "associado-20")
        self.assertEqual(
            settings.product_identifiers,
            (
                "B000000001",
                "https://www.amazon.com.br/dp/B000000002",
                "B000000003",
            ),
        )
        self.assertEqual(settings.poll_interval_seconds, 120)

    def test_loads_products_file_ignoring_comments_blanks_and_duplicates(self):
        products = (
            "# produtos para monitorar\n"
            "\n"
            "  B0D9KL3T8S # Nome do produto\n"
            "B000000002\n"
            "  B0D9KL3T8S # duplicado\n"
            "   # comentário sem produto\n"
            "B000000003\n"
        )
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHANNEL_ID": "@test-channel",
            "PRODUCT_IDENTIFIERS": "B000000004",
        }

        with TemporaryDirectory() as directory:
            products_file = Path(directory) / "products.txt"
            products_file.write_text(products, encoding="utf-8")
            with (
                patch("app.config.load_dotenv"),
                patch.dict(os.environ, environment, clear=True),
                patch("app.config.PRODUCTS_FILE", products_file),
            ):
                settings = Settings.from_env()

        self.assertEqual(
            settings.product_identifiers,
            ("B0D9KL3T8S", "B000000002", "B000000003"),
        )

    def test_falls_back_to_environment_when_products_file_is_missing_or_empty(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHANNEL_ID": "@test-channel",
            "PRODUCT_IDENTIFIERS": "B000000001, B000000002",
        }

        with TemporaryDirectory() as directory:
            products_file = Path(directory) / "products.txt"
            for content in (None, ""):
                with self.subTest(content=content):
                    if content is not None:
                        products_file.write_text(content, encoding="utf-8")
                    with (
                        patch("app.config.load_dotenv"),
                        patch.dict(os.environ, environment, clear=True),
                        patch("app.config.PRODUCTS_FILE", products_file),
                    ):
                        settings = Settings.from_env()
                    self.assertEqual(
                        settings.product_identifiers,
                        ("B000000001", "B000000002"),
                    )

    def test_rejects_poll_intervals_under_one_minute(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHANNEL_ID": "@test-channel",
            "POLL_INTERVAL_SECONDS": "59",
        }
        with patch.dict(os.environ, environment):
            with self.assertRaisesRegex(ValueError, "pelo menos 60"):
                Settings.from_env()

    def test_rejects_invalid_configured_threshold(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHANNEL_ID": "@test-channel",
            "MIN_DISCOUNT_PERCENT": "NaN",
        }
        with patch.dict(os.environ, environment):
            with self.assertRaisesRegex(ValueError, "entre 0 e 100"):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
