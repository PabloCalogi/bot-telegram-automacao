import asyncio
import html
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
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


@dataclass
class ExecutionSummary:
    analyzed: int = 0
    with_image: int = 0
    published: int = 0
    ignored: int = 0
    errors: int = 0


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
) -> str:
    if (
        not analysis.approved
        or analysis.discount_percent is None
        or product.original_price is None
    ):
        raise ValueError("Não é possível formatar um produto não aprovado como oferta.")

    title = " ".join(product.title.split())
    if len(title) > 120:
        title = title[:117] + "..."
    discount = analysis.discount_percent.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    saved_amount = product.original_price - product.current_price

    return "\n".join(
        (
            "🔥 <b>OFERTA NO CLUBE DO MAGO</b>",
            "",
            f"📦 <b>{html.escape(title)}</b>",
            "",
            f"❌ De: <s>R$ {_format_brl(product.original_price)}</s>",
            f"✅ <b>Por: R$ {_format_brl(product.current_price)}</b>",
            f"📉 <b>{discount}% OFF</b> "
            f"(Economize R$ {_format_brl(saved_amount)})",
            "",
            "⚡ <i>Preço e estoque sujeitos a alteração pela Amazon.</i>",
        )
    )


async def process_product(
    identifier: str,
    bot: Bot,
    store: SQLiteOfferStore,
    settings: Settings,
    *,
    summary: ExecutionSummary | None = None,
) -> bool:
    product = await asyncio.to_thread(fetch_product, identifier)
    if summary is not None and product.image_url:
        summary.with_image += 1
    analysis = analyze_discount(product, settings.min_discount_percent)
    if not analysis.approved:
        logger.info("Oferta descartada para ASIN %s: %s", product.asin, analysis.reason)
        return False
    if store.has_been_sent(product.asin):
        logger.info("Oferta já enviada; ignorando ASIN %s.", product.asin)
        return False

    affiliate_url = build_affiliate_url(product.asin, settings.amazon_associate_tag)
    has_image = bool(product.image_url)
    message = format_offer_message(product, analysis)
    reply_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛒 Ver oferta na Amazon", url=affiliate_url)]]
    )
    if has_image:
        try:
            await bot.send_photo(
                chat_id=settings.telegram_channel_id,
                photo=product.image_url,
                caption=message,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            logger.warning(
                "Falha ao enviar foto da oferta de ASIN %s (%s); usando texto.",
                product.asin,
                type(exc).__name__,
            )
            has_image = False
    if not has_image:
        await bot.send_message(
            chat_id=settings.telegram_channel_id,
            text=message,
            parse_mode="HTML",
            reply_markup=reply_markup,
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
    summary: ExecutionSummary | None = None,
) -> ExecutionSummary:
    if not settings.product_identifiers:
        raise ValueError("Configure PRODUCT_IDENTIFIERS no arquivo .env.")
    if not settings.amazon_associate_tag:
        raise ValueError("Configure AMAZON_ASSOCIATE_TAG no arquivo .env.")

    if summary is None:
        summary = ExecutionSummary()

    store = SQLiteOfferStore()
    store.initialize()

    async with Bot(token=settings.telegram_bot_token) as bot:
        while True:
            for identifier in settings.product_identifiers:
                summary.analyzed += 1
                try:
                    published = await process_product(
                        identifier,
                        bot,
                        store,
                        settings,
                        summary=summary,
                    )
                    if published:
                        summary.published += 1
                    else:
                        summary.ignored += 1
                except AmazonAccessBlocked:
                    summary.errors += 1
                    logger.error("Bloqueio da Amazon; interrompendo o monitoramento.")
                    raise
                except AmazonScrapingError as exc:
                    summary.errors += 1
                    logger.warning("Consulta ignorada para %s: %s", identifier, exc)
                except TelegramError as exc:
                    summary.errors += 1
                    logger.error(
                        "Erro do Telegram ao processar %s (%s).",
                        identifier,
                        type(exc).__name__,
                    )
                await asyncio.sleep(REQUEST_DELAY_SECONDS)

            if run_once:
                return summary
            await asyncio.sleep(settings.poll_interval_seconds)
