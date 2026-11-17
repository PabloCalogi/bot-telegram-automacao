import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from app.amazon.client import AmazonAccessBlocked
from app.amazon.models import Product
from app.config import Settings
from app.deals import analyze_discount
from app.main import send_test_message
from app.publisher import (
    build_affiliate_url,
    format_offer_message,
    monitor_offers,
    process_product,
)
from app.storage import SQLiteOfferStore


def make_product(
    image_url: str | None = None,
    current_price: Decimal = Decimal("80"),
) -> Product:
    return Product(
        asin="B000000001",
        title="Produto de teste",
        original_price=Decimal("100"),
        current_price=current_price,
        image_url=image_url,
        source_url="https://www.amazon.com.br/dp/B000000001",
    )


class PublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_test_message_sends_to_numeric_telegram_chat_id(self):
        settings = Settings(
            telegram_bot_token="token",
            telegram_channel_id="-1001234567890",
        )

        with patch("app.main.Bot") as bot_class:
            bot = bot_class.return_value
            bot.__aenter__ = AsyncMock(return_value=bot)
            bot.__aexit__ = AsyncMock(return_value=False)
            bot.send_message = AsyncMock()

            await send_test_message(settings)

        bot.send_message.assert_awaited_once()
        self.assertEqual(
            bot.send_message.await_args.kwargs["chat_id"],
            "-1001234567890",
        )

    async def test_publishes_approved_offer_once_and_records_asin(self):
        product = make_product()
        settings = Settings(
            telegram_bot_token="token",
            telegram_channel_id="@canal",
            amazon_associate_tag="exemplo-20",
        )
        bot = AsyncMock()

        with TemporaryDirectory() as directory:
            store = SQLiteOfferStore(Path(directory) / "offers.sqlite3")
            store.initialize()
            with patch("app.publisher.fetch_product", return_value=product):
                first_result = await process_product(
                    "B000000001",
                    bot,
                    store,
                    settings,
                )
                duplicate_result = await process_product(
                    "B000000001",
                    bot,
                    store,
                    settings,
                )

        self.assertTrue(first_result)
        self.assertFalse(duplicate_result)
        bot.send_message.assert_awaited_once()
        message = bot.send_message.await_args.kwargs["text"]
        self.assertIn("R$ 100,00", message)
        self.assertIn("R$ 80,00", message)
        self.assertIn("20,00% OFF", message)
        self.assertIn("tag=exemplo-20", message)

    async def test_uses_photo_when_product_image_is_available(self):
        product = make_product("https://images.example.test/product.jpg")
        settings = Settings(
            telegram_bot_token="token",
            telegram_channel_id="@canal",
            amazon_associate_tag="exemplo-20",
        )
        bot = AsyncMock()

        with TemporaryDirectory() as directory:
            store = SQLiteOfferStore(Path(directory) / "offers.sqlite3")
            store.initialize()
            with patch("app.publisher.fetch_product", return_value=product):
                await process_product("B000000001", bot, store, settings)

        bot.send_photo.assert_awaited_once()
        bot.send_message.assert_not_awaited()

    async def test_does_not_publish_discount_at_threshold(self):
        product = make_product(current_price=Decimal("85"))
        settings = Settings(
            telegram_bot_token="token",
            telegram_channel_id="@canal",
            amazon_associate_tag="exemplo-20",
        )
        bot = AsyncMock()

        with TemporaryDirectory() as directory:
            store = SQLiteOfferStore(Path(directory) / "offers.sqlite3")
            store.initialize()
            with patch("app.publisher.fetch_product", return_value=product):
                result = await process_product("B000000001", bot, store, settings)

            self.assertFalse(store.has_been_sent(product.asin))

        self.assertFalse(result)
        bot.send_message.assert_not_awaited()
        bot.send_photo.assert_not_awaited()

    async def test_monitor_stops_when_amazon_returns_a_block(self):
        settings = Settings(
            telegram_bot_token="token",
            telegram_channel_id="@canal",
            amazon_associate_tag="exemplo-20",
            product_identifiers=("B000000001",),
        )
        with (
            patch("app.publisher.Bot") as bot_class,
            patch("app.publisher.SQLiteOfferStore") as store_class,
            patch(
                "app.publisher.process_product",
                new_callable=AsyncMock,
                side_effect=AmazonAccessBlocked("HTTP 403"),
            ),
        ):
            bot_context = bot_class.return_value
            bot_context.__aenter__ = AsyncMock(return_value=AsyncMock())
            bot_context.__aexit__ = AsyncMock(return_value=False)

            with self.assertRaises(AmazonAccessBlocked):
                await monitor_offers(settings, run_once=True)

        store_class.return_value.initialize.assert_called_once()


class PublisherFormattingTests(unittest.TestCase):
    def test_builds_tagged_amazon_brazil_link(self):
        self.assertEqual(
            build_affiliate_url("b000000001", "afiliado-20"),
            "https://www.amazon.com.br/dp/B000000001?tag=afiliado-20",
        )

    def test_formats_approved_offer(self):
        product = make_product()
        analysis = analyze_discount(product, Decimal("15"))

        message = format_offer_message(
            product,
            analysis,
            "https://www.amazon.com.br/dp/B000000001?tag=afiliado-20",
        )

        self.assertIn("🔥 OFERTA AMAZON!", message)
        self.assertIn("📉 Desconto: 20,00% OFF", message)


if __name__ == "__main__":
    unittest.main()
