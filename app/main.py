import argparse
import asyncio
import logging

from telegram import Bot
from telegram.error import TelegramError

from app.amazon.client import AmazonAccessBlocked
from app.config import Settings
from app.publisher import ExecutionSummary, monitor_offers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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
        "Ignorados/Já enviados: %s | Erros: %s",
        summary.analyzed,
        summary.with_image,
        summary.published,
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

    try:
        settings = Settings.from_env()
        if args.test_telegram:
            asyncio.run(send_test_message(settings))
        else:
            asyncio.run(
                monitor_offers(
                    settings,
                    run_once=args.once,
                    summary=summary,
                )
            )
    except ValueError as exc:
        if args.once:
            summary.errors += 1
        raise SystemExit(str(exc)) from exc
    except AmazonAccessBlocked as exc:
        logger.error("Monitoramento encerrado após bloqueio da Amazon: %s", exc)
        raise SystemExit(1) from None
    except TelegramError as exc:
        if args.once:
            summary.errors += 1
        logger.error(
            "Falha na comunicação com o Telegram (%s).",
            type(exc).__name__,
        )
        raise SystemExit(1) from None
    finally:
        if args.once:
            log_execution_summary(summary)

    if args.test_telegram:
        logger.info("Mensagem de teste enviada com sucesso.")


if __name__ == "__main__":
    main()
