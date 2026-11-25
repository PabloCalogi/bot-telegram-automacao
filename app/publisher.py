import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from urllib.parse import urlencode

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import BadRequest, RetryAfter, TelegramError

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
PUBLISH_DELAY_SECONDS = 3.0
ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)


@dataclass
class ExecutionSummary:
    analyzed: int = 0
    with_image: int = 0
    published: int = 0
    updated: int = 0
    deleted: int = 0
    ignored: int = 0
    errors: int = 0
    error_reasons: dict[str, int] = field(default_factory=dict)

    def record_error(self, error: Exception) -> None:
        message = " ".join(str(error).split())
        status_match = re.search(r"\bHTTP\s+(\d{3})\b", message, re.IGNORECASE)
        if status_match:
            reason = f"HTTP {status_match.group(1)}"
        elif any(
            marker in message.casefold()
            for marker in ("captcha", "bloqueio", "bloqueada", "bloqueado")
        ):
            reason = "Captcha/Bloqueio"
        elif "timeout" in message.casefold() or "timed out" in message.casefold():
            reason = "Timeout"
        else:
            reason = type(error).__name__
            if message:
                reason = f"{reason}: {message[:80]}"
        self.error_reasons[reason] = self.error_reasons.get(reason, 0) + 1


class ProcessResult(Enum):
    PUBLISHED = "published"
    UPDATED = "updated"
    DELETED = "deleted"
    IGNORED = "ignored"


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
    is_lowest_price: bool = False,
) -> str:
    if (
        not analysis.approved
        or analysis.discount_percent is None
        or product.original_price is None
        or product.current_price is None
    ):
        raise ValueError("Não é possível formatar um produto não aprovado como oferta.")

    title = " ".join(product.title.split())
    if len(title) > 120:
        title = title[:117] + "..."
    discount = analysis.discount_percent.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    saved_amount = product.original_price - product.current_price

    lines = [
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
    ]
    if is_lowest_price:
        lines[0:0] = ["🚨 <b>MENOR PREÇO REGISTRADO</b>", ""]
    return "\n".join(lines)


def _build_reply_markup(product: Product, settings: Settings) -> InlineKeyboardMarkup:
    affiliate_url = build_affiliate_url(product.asin, settings.amazon_associate_tag)
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛒 Ver oferta na Amazon", url=affiliate_url)]]
    )


async def publish_offer(
    product: Product,
    analysis: DiscountAnalysis,
    bot: Bot,
    settings: Settings,
    is_lowest_price: bool = False,
) -> tuple[int, bool]:
    message = format_offer_message(product, analysis, is_lowest_price)
    reply_markup = _build_reply_markup(product, settings)
    is_photo = bool(product.image_url)
    published_message: Message | None = None

    if is_photo:
        for attempt in range(2):
            try:
                published_message = await bot.send_photo(
                    chat_id=settings.telegram_channel_id,
                    photo=product.image_url,
                    caption=message,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
                break
            except RetryAfter as exc:
                if attempt == 1:
                    logger.warning(
                        "Falha ao enviar foto da oferta de ASIN %s (%s); usando texto.",
                        product.asin,
                        type(exc).__name__,
                    )
                    is_photo = False
                    break
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramError as exc:
                logger.warning(
                    "Falha ao enviar foto da oferta de ASIN %s (%s); usando texto.",
                    product.asin,
                    type(exc).__name__,
                )
                is_photo = False
                break

    if not is_photo:
        try:
            published_message = await bot.send_message(
                chat_id=settings.telegram_channel_id,
                text=message,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            published_message = await bot.send_message(
                chat_id=settings.telegram_channel_id,
                text=message,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )

    if published_message is None or published_message.message_id is None:
        raise RuntimeError(
            f"O Telegram não retornou o ID da mensagem do ASIN {product.asin}."
        )
    return published_message.message_id, is_photo


async def update_offer_message(
    product: Product,
    analysis: DiscountAnalysis,
    message_id: int,
    is_photo: bool,
    bot: Bot,
    settings: Settings,
    is_lowest_price: bool = False,
) -> None:
    message = format_offer_message(product, analysis, is_lowest_price)
    reply_markup = _build_reply_markup(product, settings)
    while True:
        try:
            if is_photo:
                await bot.edit_message_caption(
                    chat_id=settings.telegram_channel_id,
                    message_id=message_id,
                    caption=message,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            else:
                await bot.edit_message_text(
                    chat_id=settings.telegram_channel_id,
                    message_id=message_id,
                    text=message,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            return
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except BadRequest as exc:
            if "message is not modified" in str(exc).casefold():
                logger.info("Mensagem %s já contém os dados atuais.", message_id)
                return
            raise


async def delete_offer_message(
    message_id: int,
    bot: Bot,
    settings: Settings,
) -> None:
    while True:
        try:
            await bot.delete_message(
                chat_id=settings.telegram_channel_id,
                message_id=message_id,
            )
            return
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except BadRequest as exc:
            logger.info(
                "Mensagem %s não pôde ser excluída (%s); removendo o registro local.",
                message_id,
                exc,
            )
            return


async def send_health_alert_if_needed(
    summary: ExecutionSummary,
    telegram_bot_token: str,
    admin_chat_id: str,
    *,
    fatal: bool = False,
) -> None:
    error_rate = summary.errors / summary.analyzed if summary.analyzed else 0
    if not admin_chat_id or not (
        fatal
        or summary.errors >= 3
        or (summary.analyzed >= 5 and error_rate >= 0.30)
    ):
        return

    top_errors = sorted(
        summary.error_reasons.items(),
        key=lambda item: (-item[1], item[0]),
    )[:3]
    error_details = ", ".join(
        f"{html.escape(reason)} ({count})" for reason, count in top_errors
    ) or "Nenhum motivo registrado"
    message = (
        "⚠️ <b>ALERTA DE SAÚDE DO CLUBE DO MAGO</b>\n"
        "Possível bloqueio da Amazon ou falha no bot.\n\n"
        f"Analisados: {summary.analyzed} | "
        f"Com imagem: {summary.with_image} | "
        f"Publicados: {summary.published} | "
        f"Atualizados: {summary.updated} | "
        f"Excluídos: {summary.deleted} | "
        f"Ignorados: {summary.ignored} | "
        f"Erros: {summary.errors}\n"
        f"Principais erros: {error_details}"
    )
    try:
        async with Bot(token=telegram_bot_token) as bot:
            await bot.send_message(
                chat_id=admin_chat_id,
                text=message,
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.warning(
            "Não foi possível enviar o alerta privado de saúde (%s).",
            type(exc).__name__,
        )


async def process_product(
    identifier: str,
    bot: Bot,
    store: SQLiteOfferStore,
    settings: Settings,
    *,
    summary: ExecutionSummary | None = None,
) -> ProcessResult:
    product = await asyncio.to_thread(fetch_product, identifier)
    if summary is not None and product.image_url:
        summary.with_image += 1
    stored_offer = store.get_offer(product.asin)
    if not product.in_stock:
        logger.info("Produto sem estoque: ASIN %s.", product.asin)
        if stored_offer is not None and stored_offer.message_id is not None:
            await delete_offer_message(stored_offer.message_id, bot, settings)
            store.remove_offer(product.asin)
            return ProcessResult.DELETED
        if stored_offer is not None:
            store.remove_offer(product.asin)
        return ProcessResult.IGNORED

    analysis = analyze_discount(product, settings.min_discount_percent)
    if not analysis.approved:
        logger.info("Oferta descartada para ASIN %s: %s", product.asin, analysis.reason)
        if stored_offer is not None and stored_offer.message_id is not None:
            await delete_offer_message(stored_offer.message_id, bot, settings)
            store.remove_offer(product.asin)
            return ProcessResult.DELETED
        if stored_offer is not None:
            store.remove_offer(product.asin)
        return ProcessResult.IGNORED
    if product.current_price is None:
        raise RuntimeError(
            f"Oferta aprovada sem preço atual para o ASIN {product.asin}."
        )
    is_lowest_price = store.record_price(
        product.asin,
        float(product.current_price),
    )
    if stored_offer is not None and stored_offer.message_id is not None:
        if stored_offer.price is None:
            logger.warning(
                "Oferta do ASIN %s não possui preço armazenado; mantendo mensagem.",
                product.asin,
            )
            return ProcessResult.IGNORED
        if Decimal(str(stored_offer.price)) == product.current_price:
            logger.info("Oferta já enviada; preço inalterado para ASIN %s.", product.asin)
            return ProcessResult.IGNORED
        if stored_offer.is_photo is None:
            raise RuntimeError(
                f"Oferta do ASIN {product.asin} não possui tipo de mensagem armazenado."
            )
        await update_offer_message(
            product,
            analysis,
            stored_offer.message_id,
            stored_offer.is_photo,
            bot,
            settings,
            is_lowest_price=is_lowest_price,
        )
        if not store.update_price(product.asin, product.current_price):
            raise RuntimeError(
                f"Não foi possível atualizar o preço do ASIN {product.asin}."
            )
        logger.info("Oferta atualizada: ASIN %s.", product.asin)
        return ProcessResult.UPDATED
    if stored_offer is not None:
        logger.info("Oferta já enviada; ignorando ASIN %s.", product.asin)
        return ProcessResult.IGNORED

    message_id, is_photo = await publish_offer(
        product,
        analysis,
        bot,
        settings,
        is_lowest_price=is_lowest_price,
    )
    await asyncio.sleep(PUBLISH_DELAY_SECONDS)
    if not store.record_sent(
        product.asin,
        message_id=message_id,
        price=product.current_price,
        is_photo=is_photo,
    ):
        raise RuntimeError(
            f"ASIN {product.asin} já estava registrado após uma publicação no Telegram."
        )
    logger.info("Oferta publicada e registrada: ASIN %s.", product.asin)
    return ProcessResult.PUBLISHED


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
                    result = await process_product(
                        identifier,
                        bot,
                        store,
                        settings,
                        summary=summary,
                    )
                    if result is ProcessResult.PUBLISHED:
                        summary.published += 1
                    elif result is ProcessResult.UPDATED:
                        summary.updated += 1
                    elif result is ProcessResult.DELETED:
                        summary.deleted += 1
                    else:
                        summary.ignored += 1
                except AmazonAccessBlocked as exc:
                    summary.errors += 1
                    summary.record_error(exc)
                    logger.error("Bloqueio da Amazon; interrompendo o monitoramento.")
                    raise
                except AmazonScrapingError as exc:
                    summary.errors += 1
                    summary.record_error(exc)
                    logger.warning("Consulta ignorada para %s: %s", identifier, exc)
                except TelegramError as exc:
                    summary.errors += 1
                    summary.record_error(exc)
                    logger.error(
                        "Erro do Telegram ao processar %s (%s).",
                        identifier,
                        type(exc).__name__,
                    )
                await asyncio.sleep(REQUEST_DELAY_SECONDS)

            if run_once:
                return summary
            await asyncio.sleep(settings.poll_interval_seconds)
