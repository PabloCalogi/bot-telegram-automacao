import asyncio
import html
import logging
import re
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode

from telegram import Bot
from telegram.error import TelegramError

from app.amazon.client import (
    AmazonAccessBlocked,
    AmazonScrapingError,
    fetch_product,
)
from app.amazon.models import Product
from app.config import Settings
from app.deals import DiscountAnalysis, analyze_discount
from app.storage import SQLiteOfferStore


logger = logging.getLogger(__name__)
REQUEST_DELAY_SECONDS = 1
ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)


def build_affiliate_url(asin: str, associate_tag: str) -> str:
    if not ASIN_PATTERN.fullmatch(asin):
        raise ValueError("ASIN inválido para gerar link de afiliado.")
    if not associate_tag.strip():
        raise ValueError("Configure AMAZON_ASSOCIATE_TAG para publicar ofertas.")
    return (
        f"https://www.amazon.com.br/dp/{asin.upper()}?"
        f"{urlencode({'tag': associate_tag.strip()})}"
    )


def _format_brl(value: Decimal) -> str:
    formatted = format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ",.2f")
    return formatted.replace(",", "_").replace(".", ",").replace("_", ".")


def format_offer_message(
    product: Product,
    analysis: DiscountAnalysis,
    affiliate_url: str,
) -> str:
    if (
        not analysis.approved
        or analysis.discount_percent is None
        or product.original_price is None
    ):
        raise ValueError("Não é possível formatar um produto não aprovado como oferta.")

    title = " ".join(product.title.split())
    if len(title) > 300:
        title = title[:297] + "..."
    discount = analysis.discount_percent.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )

    return "\n".join(
        (
            "<b>🚨 OFERTA DETECTADA! 🚨</b>",
            "",
            html.escape(title),
            "",
            f"❌ De: R$ {_format_brl(product.original_price)}",
            f"✅ Por: R$ <b>{_format_brl(product.current_price)}</b>",
            f"🔥 Desconto de: <b>{str(discount).replace('.', ',')}%</b>",
            "",
            f"🛒 Compre aqui: {html.escape(affiliate_url)}",
        )
    )


async def process_product(
    identifier: str,
    bot: Bot,
    store: SQLiteOfferStore,
    settings: Settings,
) -> bool:
    product = await asyncio.to_thread(fetch_product, identifier)
    analysis = analyze_discount(product, settings.min_discount_percent)
    if not analysis.approved:
        logger.info("Oferta descartada para ASIN %s: %s", product.asin, analysis.reason)
        return False
    if store.has_been_sent(product.asin):
        logger.info("Oferta já enviada; ignorando ASIN %s.", product.asin)
        return False

    affiliate_url = build_affiliate_url(product.asin, settings.amazon_associate_tag)
    message = format_offer_message(product, analysis, affiliate_url)
    if product.image_url:
        await bot.send_photo(
            chat_id=settings.telegram_channel_id,
            photo=product.image_url,
            caption=message,
            parse_mode="HTML",
        )
    else:
        await bot.send_message(
            chat_id=settings.telegram_channel_id,
            text=message,
            parse_mode="HTML",
        )

    if not store.record_sent(product.asin):
        raise RuntimeError(
            f"ASIN {product.asin} já estava registrado após uma publicação no Telegram."
        )
    logger.info("Oferta publicada e registrada: ASIN %s.", product.asin)
    return True


async def monitor_offers(
    settings: Settings,
    *,
    run_once: bool = False,
) -> None:
    if not settings.product_identifiers:
        raise ValueError("Configure PRODUCT_IDENTIFIERS no arquivo .env.")
    if not settings.amazon_associate_tag:
        raise ValueError("Configure AMAZON_ASSOCIATE_TAG no arquivo .env.")

    store = SQLiteOfferStore()
    store.initialize()

    async with Bot(token=settings.telegram_bot_token) as bot:
        while True:
            for identifier in settings.product_identifiers:
                try:
                    await process_product(identifier, bot, store, settings)
                except AmazonAccessBlocked:
                    logger.error("Bloqueio da Amazon; interrompendo o monitoramento.")
                    raise
                except AmazonScrapingError as exc:
                    logger.warning("Consulta ignorada para %s: %s", identifier, exc)
                except TelegramError as exc:
                    logger.error(
                        "Erro do Telegram ao processar %s (%s).",
                        identifier,
                        type(exc).__name__,
                    )
                await asyncio.sleep(REQUEST_DELAY_SECONDS)

            if run_once:
                return
            await asyncio.sleep(settings.poll_interval_seconds)
