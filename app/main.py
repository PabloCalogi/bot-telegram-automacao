import argparse
import asyncio
import logging
import os

from telegram import Bot
from telegram.error import TelegramError

from app.amazon.client import (
    AmazonAccessBlocked,
    AmazonScrapingError,
    close_amazon_session,
)
from app.amazon.discovery import _normalize_asin, discover_products
from app.config import Settings
from app.publisher import (
    ExecutionSummary,
    monitor_offers,
    send_health_alert_if_needed,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def send_test_message(settings: Settings) -> None:
    async with Bot(token=settings.telegram_bot_token) as bot:
        await bot.send_message(
            chat_id=settings.telegram_channel_id,
            text="✅ Bot conectado ao Telegram. Mensagem de teste enviada.",
        )


def log_execution_summary(summary: ExecutionSummary) -> None:
    logger.info(
        "Resumo da execução | Analisados: %s | Com imagem: %s | Publicados: %s | "
        "Atualizados: %s | Excluídos: %s | Ignorados/Já enviados: %s | Erros: %s",
        summary.analyzed,
        summary.with_image,
        summary.published,
        summary.updated,
        summary.deleted,
        summary.ignored,
        summary.errors,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot de ofertas da Amazon Brasil.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--test-telegram",
        action="store_true",
        help="Envia uma mensagem simples para testar o canal.",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="Processa a lista de produtos uma única vez e encerra.",
    )
    args = parser.parse_args()
    summary = ExecutionSummary()
    settings: Settings | None = None
    fatal = False

    try:
        settings = Settings.from_env()
        if args.test_telegram:
            asyncio.run(send_test_message(settings))
        else:
            if settings.enable_auto_discovery:
                def discover_after_fixed_products() -> tuple[str, ...]:
                    try:
                        discovered = discover_products(
                            settings.product_identifiers,
                            max_items=settings.discovery_max_items,
                            min_price=settings.discovery_min_price,
                            max_price=settings.discovery_max_price,
                            min_rating=settings.discovery_min_rating,
                            min_reviews=settings.discovery_min_reviews,
                        )
                    except AmazonScrapingError as exc:
                        logger.warning(
                            "Descoberta automática falhou (%s); mantendo os "
                            "resultados da lista fixa.",
                            type(exc).__name__,
                        )
                        return ()

                    known_asins = {
                        asin
                        for identifier in settings.product_identifiers
                        if (asin := _normalize_asin(identifier)) is not None
                    }
                    new_products = []
                    for identifier in discovered:
                        asin = _normalize_asin(identifier)
                        if asin is not None and asin not in known_asins:
                            known_asins.add(asin)
                            new_products.append(asin)
                    if not new_products:
                        logger.warning(
                            "Descoberta automática não retornou novos ASINs; "
                            "mantendo os resultados da lista fixa."
                        )
                    return tuple(new_products)

                asyncio.run(
                    monitor_offers(
                        settings,
                        run_once=args.once,
                        summary=summary,
                        after_fixed_products=discover_after_fixed_products,
                    )
                )
            else:
                asyncio.run(
                    monitor_offers(
                        settings,
                        run_once=args.once,
                        summary=summary,
                    )
                )
    except ValueError as exc:
        fatal = True
        summary.errors += 1
        summary.record_error(exc)
        raise SystemExit(str(exc)) from exc
    except AmazonAccessBlocked as exc:
        logger.warning(
            "Monitoramento encerrado pelo circuit breaker após bloqueios "
            "consecutivos da Amazon; possível bloqueio temporário de IP: %s",
            exc,
        )
    except TelegramError as exc:
        fatal = True
        summary.errors += 1
        summary.record_error(exc)
        logger.error(
            "Falha na comunicação com o Telegram (%s).",
            type(exc).__name__,
        )
        raise SystemExit(1) from None
    except Exception as exc:
        fatal = True
        summary.errors += 1
        summary.record_error(exc)
        logger.error("Falha fatal no bot (%s).", type(exc).__name__)
        raise SystemExit(1) from exc
    finally:
        try:
            if args.once:
                log_execution_summary(summary)
            bot_token = (
                settings.telegram_bot_token
                if settings is not None
                else os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            )
            admin_chat_id = (
                settings.telegram_admin_chat_id
                if settings is not None
                else os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()
            )
            if bot_token and admin_chat_id:
                asyncio.run(
                    send_health_alert_if_needed(
                        summary,
                        bot_token,
                        admin_chat_id,
                        fatal=fatal,
                    )
                )
        finally:
            close_amazon_session()

    if args.test_telegram:
        logger.info("Mensagem de teste enviada com sucesso.")


if __name__ == "__main__":
    main()
