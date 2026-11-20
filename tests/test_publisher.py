import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, call, patch

from telegram.error import RetryAfter, TelegramError

from app.amazon.client import AmazonAccessBlocked, AmazonScrapingError
from app.amazon.models import Product
from app.config import Settings
from app.deals import analyze_discount
from app.main import main, send_test_message
from app.publisher import (
    ExecutionSummary,
    PUBLISH_DELAY_SECONDS,
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
    def setUp(self):
        self.sleep_patcher = patch(
            "app.publisher.asyncio.sleep",
            new_callable=AsyncMock,
        )
        self.sleep_mock = self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

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
        send_args = bot.send_message.await_args.kwargs
        message = send_args["text"]
        self.assertEqual(
            message,
            "🔥 <b>OFERTA NO CLUBE DO MAGO</b>\n\n"
            "📦 <b>Produto de teste</b>\n\n"
            "❌ De: <s>R$ 100,00</s>\n"
            "✅ <b>Por: R$ 80,00</b>\n"
            "📉 <b>20% OFF</b> (Economize R$ 20,00)\n\n"
            "⚡ <i>Preço e estoque sujeitos a alteração pela Amazon.</i>",
        )
        self.assertNotIn("https://", message)
        self.assertEqual(send_args["parse_mode"], "HTML")
        button = send_args["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, "🛒 Ver oferta na Amazon")
        self.assertEqual(
            button.url,
            "https://www.amazon.com.br/dp/B000000001?tag=exemplo-20",
        )

    async def test_uses_photo_when_product_image_is_available(self):
        product = make_product("https://images.example.test/product.jpg")
        summary = ExecutionSummary()
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
                await process_product(
                    "B000000001",
                    bot,
                    store,
                    settings,
                    summary=summary,
                )

        bot.send_photo.assert_awaited_once()
        bot.send_message.assert_not_awaited()
        self.assertEqual(summary.with_image, 1)
        send_args = bot.send_photo.await_args.kwargs
        self.assertEqual(send_args["parse_mode"], "HTML")
        self.assertNotIn("<a href=", send_args["caption"])
        self.assertIn("📦 <b>Produto de teste</b>", send_args["caption"])
        self.assertIn("(Economize R$ 20,00)", send_args["caption"])
        self.assertEqual(
            send_args["reply_markup"].inline_keyboard[0][0].url,
            "https://www.amazon.com.br/dp/B000000001?tag=exemplo-20",
        )

    async def test_retries_photo_after_retry_after(self):
        product = make_product("https://images.example.test/product.jpg")
        settings = Settings(
            telegram_bot_token="token",
            telegram_channel_id="@canal",
            amazon_associate_tag="exemplo-20",
        )
        bot = AsyncMock()
        bot.send_photo.side_effect = [RetryAfter(2), None]

        with TemporaryDirectory() as directory:
            store = SQLiteOfferStore(Path(directory) / "offers.sqlite3")
            store.initialize()
            with patch("app.publisher.fetch_product", return_value=product):
                result = await process_product(
                    "B000000001",
                    bot,
                    store,
                    settings,
                )

        self.assertTrue(result)
        self.assertEqual(bot.send_photo.await_count, 2)
        bot.send_message.assert_not_awaited()
        self.assertEqual(
            self.sleep_mock.await_args_list,
            [call(3), call(PUBLISH_DELAY_SECONDS)],
        )

    async def test_retries_fallback_message_after_retry_after(self):
        product = make_product("https://images.example.test/rejected.jpg")
        settings = Settings(
            telegram_bot_token="token",
            telegram_channel_id="@canal",
            amazon_associate_tag="exemplo-20",
        )
        bot = AsyncMock()
        bot.send_photo.side_effect = TelegramError("foto rejeitada")
        bot.send_message.side_effect = [RetryAfter(4), None]

        with TemporaryDirectory() as directory:
            store = SQLiteOfferStore(Path(directory) / "offers.sqlite3")
            store.initialize()
            with patch("app.publisher.fetch_product", return_value=product):
                result = await process_product(
                    "B000000001",
                    bot,
                    store,
                    settings,
                )

        self.assertTrue(result)
        bot.send_photo.assert_awaited_once()
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(
            self.sleep_mock.await_args_list,
            [call(5), call(PUBLISH_DELAY_SECONDS)],
        )

    async def test_waits_after_each_successful_publication(self):
        products = (
            make_product(),
            Product(
                asin="B000000002",
                title="Segundo produto",
                original_price=Decimal("100"),
                current_price=Decimal("80"),
                image_url=None,
                source_url="https://www.amazon.com.br/dp/B000000002",
            ),
        )
        settings = Settings(
            telegram_bot_token="token",
            telegram_channel_id="@canal",
            amazon_associate_tag="exemplo-20",
        )
        bot = AsyncMock()

        with TemporaryDirectory() as directory:
            store = SQLiteOfferStore(Path(directory) / "offers.sqlite3")
            store.initialize()
            with patch("app.publisher.fetch_product", side_effect=products):
                first_result = await process_product(
                    "B000000001",
                    bot,
                    store,
                    settings,
                )
                second_result = await process_product(
                    "B000000002",
                    bot,
                    store,
                    settings,
                )

        self.assertTrue(first_result)
        self.assertTrue(second_result)
        self.assertEqual(
            self.sleep_mock.await_args_list,
            [
                call(PUBLISH_DELAY_SECONDS),
                call(PUBLISH_DELAY_SECONDS),
            ],
        )
        self.assertEqual(bot.send_message.await_count, 2)

    async def test_falls_back_to_formatted_text_when_photo_send_fails(self):
        product = make_product("https://images.example.test/rejected.jpg")
        settings = Settings(
            telegram_bot_token="token",
            telegram_channel_id="@canal",
            amazon_associate_tag="exemplo-20",
        )
        bot = AsyncMock()
        bot.send_photo.side_effect = TelegramError("foto rejeitada")

        with TemporaryDirectory() as directory:
            store = SQLiteOfferStore(Path(directory) / "offers.sqlite3")
            store.initialize()
            with patch("app.publisher.fetch_product", return_value=product):
                result = await process_product(
                    "B000000001",
                    bot,
                    store,
                    settings,
                )

        self.assertTrue(result)
        bot.send_photo.assert_awaited_once()
        bot.send_message.assert_awaited_once()
        photo_args = bot.send_photo.await_args.kwargs
        message_args = bot.send_message.await_args.kwargs
        self.assertEqual(photo_args["parse_mode"], "HTML")
        self.assertEqual(message_args["parse_mode"], "HTML")
        self.assertEqual(photo_args["caption"], message_args["text"])
        self.assertEqual(
            photo_args["reply_markup"].inline_keyboard[0][0].text,
            "🛒 Ver oferta na Amazon",
        )
        self.assertEqual(
            message_args["reply_markup"].inline_keyboard[0][0].text,
            "🛒 Ver oferta na Amazon",
        )
        self.assertEqual(
            photo_args["reply_markup"].inline_keyboard[0][0].url,
            "https://www.amazon.com.br/dp/B000000001?tag=exemplo-20",
        )
        self.assertEqual(
            message_args["reply_markup"].inline_keyboard[0][0].url,
            "https://www.amazon.com.br/dp/B000000001?tag=exemplo-20",
        )

    async def test_monitor_returns_execution_counters(self):
        settings = Settings(
            telegram_bot_token="token",
            telegram_channel_id="@canal",
            amazon_associate_tag="exemplo-20",
            product_identifiers=("one", "two", "three"),
        )
        summary = ExecutionSummary()
        with (
            patch("app.publisher.Bot") as bot_class,
            patch("app.publisher.SQLiteOfferStore") as store_class,
            patch(
                "app.publisher.process_product",
                new_callable=AsyncMock,
                side_effect=[
                    True,
                    False,
                    AmazonScrapingError("consulta falhou"),
                ],
            ) as process_mock,
        ):
            bot_context = bot_class.return_value
            bot_context.__aenter__ = AsyncMock(return_value=AsyncMock())
            bot_context.__aexit__ = AsyncMock(return_value=False)

            result = await monitor_offers(settings, run_once=True, summary=summary)

        self.assertIs(result, summary)
        self.assertEqual(summary.analyzed, 3)
        self.assertEqual(summary.published, 1)
        self.assertEqual(summary.ignored, 1)
        self.assertEqual(summary.errors, 1)
        self.assertEqual(process_mock.await_count, 3)
        store_class.return_value.initialize.assert_called_once()

    async def test_main_logs_summary_when_once_finishes(self):
        settings = Settings(
            telegram_bot_token="token",
            telegram_channel_id="@canal",
        )
        summary = ExecutionSummary(
            analyzed=3,
            with_image=1,
            published=1,
            ignored=1,
            errors=1,
        )

        with (
            patch("sys.argv", ["app.main", "--once"]),
            patch("app.main.Settings.from_env", return_value=settings),
            patch("app.main.ExecutionSummary", return_value=summary),
            patch(
                "app.main.monitor_offers",
                new_callable=AsyncMock,
            ) as monitor_offers_mock,
            patch(
                "app.main.asyncio.run",
                side_effect=lambda coroutine: coroutine.close(),
            ),
            self.assertLogs("app.main", level="INFO") as logs,
        ):
            main()

        monitor_offers_mock.assert_called_once_with(
            settings,
            run_once=True,
            summary=summary,
        )
        self.assertIn(
            "Resumo da execução | Analisados: 3 | Com imagem: 1 | Publicados: 1 | "
            "Ignorados/Já enviados: 1 | Erros: 1",
            logs.output[-1],
        )

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

        message = format_offer_message(product, analysis)

        self.assertIn(
            "🔥 <b>OFERTA NO CLUBE DO MAGO</b>\n\n"
            "📦 <b>Produto de teste</b>",
            message,
        )
        self.assertIn("❌ De: <s>R$ 100,00</s>", message)
        self.assertIn("✅ <b>Por: R$ 80,00</b>", message)
        self.assertIn("📉 <b>20% OFF</b> (Economize R$ 20,00)", message)
        self.assertTrue(
            message.endswith(
                "⚡ <i>Preço e estoque sujeitos a alteração pela Amazon.</i>"
            )
        )

    def test_rounds_discount_to_an_integer_and_keeps_brazilian_prices(self):
        product = make_product(current_price=Decimal("74.50"))
        analysis = analyze_discount(product, Decimal("15"))

        message = format_offer_message(product, analysis)

        self.assertIn("<b>26% OFF</b>", message)
        self.assertIn("<s>R$ 100,00</s>", message)
        self.assertIn("<b>Por: R$ 74,50</b>", message)

    def test_escapes_dynamic_fields_for_html_parse_mode(self):
        product = Product(
            asin="B000000001",
            title="Produto <especial> & único",
            original_price=Decimal("100"),
            current_price=Decimal("80"),
            image_url=None,
            source_url="https://www.amazon.com.br/dp/B000000001",
        )
        analysis = analyze_discount(product, Decimal("15"))

        message = format_offer_message(product, analysis)

        self.assertIn("Produto &lt;especial&gt; &amp; único", message)
        self.assertNotIn("https://", message)

    def test_truncates_long_titles_and_formats_large_savings_in_brl(self):
        product = Product(
            asin="B000000001",
            title="Produto" * 20,
            original_price=Decimal("2000"),
            current_price=Decimal("765.50"),
            image_url=None,
            source_url="https://www.amazon.com.br/dp/B000000001",
        )
        analysis = analyze_discount(product, Decimal("15"))

        message = format_offer_message(product, analysis)

        self.assertIn(f"📦 <b>{'Produto' * 16}Produ...</b>", message)
        self.assertIn("❌ De: <s>R$ 2.000,00</s>", message)
        self.assertIn("✅ <b>Por: R$ 765,50</b>", message)
        self.assertIn("(Economize R$ 1.234,50)", message)


if __name__ == "__main__":
    unittest.main()
