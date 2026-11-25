import logging
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from bs4 import BeautifulSoup, Tag
from curl_cffi import CurlError, requests

from app.amazon.client import (
    AmazonAccessBlocked,
    AmazonScrapingError,
    _get_response,
    _is_amazon_block_page,
)


logger = logging.getLogger(__name__)


DISCOVERY_MAX_ITEMS = 10
DISCOVERY_MIN_PRICE = Decimal("30.0")
DISCOVERY_MAX_PRICE = Decimal("200.0")
DISCOVERY_MIN_RATING = Decimal("4.3")
DISCOVERY_MIN_REVIEWS = 50
DISCOVERY_CATEGORY_URLS = (
    "https://www.amazon.com.br/gp/bestsellers/electronics",
    "https://www.amazon.com.br/gp/bestsellers/kitchen",
    "https://www.amazon.com.br/gp/bestsellers/beauty",
)

ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)
ASIN_URL_PATTERN = re.compile(r"/dp/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)
PRICE_PATTERN = re.compile(r"(?:R\$\s*)?([\d.]+,\d{2})")
VOLTAGE_PATTERN = re.compile(r"\b(?:220|127|110)\s*v\b", re.IGNORECASE)
CLOTHING_PATTERN = re.compile(
    r"\btamanho\s+(?:p|m|g|gg)\b|"
    r"\b(?:camisetas?|camisas?|calças?|bermudas?|vestidos?|tênis|"
    r"sapatos?|chinelos?|sandálias?|botas?|calçados?)\b",
    re.IGNORECASE,
)
RATING_PATTERN = re.compile(
    r"(\d+(?:[,.]\d+)?)\s*(?:de\s+5\s+estrelas|out\s+of\s+5\s+stars)",
    re.IGNORECASE,
)
REVIEW_COUNT_PATTERN = re.compile(
    r"([\d][\d.,\s]*)\s*(?:avaliações?|reviews?|ratings?)\b",
    re.IGNORECASE,
)
HTTP_STATUS_PATTERN = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)


@dataclass
class _CategoryStats:
    cards_found: int = 0
    discarded: Counter[str] = field(default_factory=Counter)
    approved: int = 0
    captcha_detected: bool = False
    status_code: int | None = None


def _normalize_asin(identifier: str) -> str | None:
    value = identifier.strip()
    if ASIN_PATTERN.fullmatch(value):
        return value.upper()
    match = ASIN_URL_PATTERN.search(value)
    return match.group(1).upper() if match else None


def _price_from_card(card: Tag) -> Decimal | None:
    for element in card.select(".a-price .a-offscreen, .a-price-whole"):
        match = PRICE_PATTERN.search(element.get_text(" ", strip=True))
        if match is not None:
            return Decimal(match.group(1).replace(".", "").replace(",", "."))
    return None


def _title_from_card(card: Tag) -> str:
    for selector in ("#title", "h2", ".p13n-sc-truncate", "[class*='title']"):
        element = card.select_one(selector)
        if element is not None:
            title = element.get_text(" ", strip=True)
            if title:
                return title
    for image in card.select("img[alt]"):
        title = image.get("alt", "").strip()
        if title:
            return title
    for link in card.select('a[href*="/dp/"]'):
        title = link.get_text(" ", strip=True)
        if title:
            return title
    return ""


def _rating_from_card(card: Tag) -> Decimal | None:
    for element in card.select(".a-icon-alt, [aria-label]"):
        values = (
            element.get_text(" ", strip=True),
            str(element.get("aria-label", "")),
        )
        for value in values:
            match = RATING_PATTERN.search(value)
            if match is not None:
                return Decimal(match.group(1).replace(",", "."))
    return None


def _review_count_from_card(card: Tag) -> int | None:
    candidates = card.select(".a-size-small, [aria-label], [title]")
    for element in candidates:
        values = (
            element.get_text(" ", strip=True),
            str(element.get("aria-label", "")),
            str(element.get("title", "")),
        )
        for value in values:
            match = REVIEW_COUNT_PATTERN.search(value)
            if match is not None:
                return int(re.sub(r"\D", "", match.group(1)))

    for element in candidates:
        if element.select_one(".a-icon-alt") is not None:
            continue
        value = element.get_text(" ", strip=True)
        if re.fullmatch(r"[\d][\d.,\s]*", value):
            return int(re.sub(r"\D", "", value))
    return None


def _quality_rejection_reason(
    card: Tag,
    min_rating: Decimal,
    min_reviews: int,
) -> tuple[str, str] | None:
    title = _title_from_card(card)
    if "bivolt" not in title.casefold() and VOLTAGE_PATTERN.search(title):
        return "voltagem", "voltagem no título"
    if CLOTHING_PATTERN.search(title):
        return "tamanho/vestuário", "tamanho/vestuário no título"

    rating = _rating_from_card(card)
    if rating is None:
        return "nota ausente", "nota ausente no card"
    if rating < min_rating:
        return "nota abaixo do mínimo", f"nota {rating} abaixo de {min_rating}"

    reviews = _review_count_from_card(card)
    if reviews is None:
        return "avaliações ausentes", "avaliações ausentes no card"
    if reviews < min_reviews:
        return "avaliações abaixo do mínimo", f"{reviews} avaliações; mínimo {min_reviews}"
    return None


def _record_rejection(
    stats: _CategoryStats,
    asin: str,
    category: str,
    detail: str,
) -> None:
    stats.discarded[category] += 1
    logger.info("Produto %s descartado: %s.", asin, detail)


def _extract_discovered_asins(
    page_html: str,
    min_price: Decimal = DISCOVERY_MIN_PRICE,
    max_price: Decimal = DISCOVERY_MAX_PRICE,
    min_rating: Decimal = DISCOVERY_MIN_RATING,
    min_reviews: int = DISCOVERY_MIN_REVIEWS,
    *,
    stats: _CategoryStats,
) -> list[str]:
    soup = BeautifulSoup(page_html, "html.parser")
    discovered: list[str] = []
    seen: set[str] = set()
    processed_cards: set[int] = set()
    asin_cards = soup.select("[data-asin]")
    stats.cards_found = len(asin_cards)

    for card in asin_cards:
        processed_cards.add(id(card))
        asin_value = card.get("data-asin")
        asin = _normalize_asin(asin_value) if isinstance(asin_value, str) else None
        if asin is None:
            for link in card.select('a[href*="/dp/"]'):
                match = ASIN_URL_PATTERN.search(link.get("href", ""))
                if match is not None:
                    asin = match.group(1).upper()
                    break
        if asin is None:
            stats.discarded["ASIN inválido"] += 1
            continue
        if asin in seen:
            continue

        price = _price_from_card(card)
        if price is None or not min_price <= price <= max_price:
            _record_rejection(
                stats,
                asin,
                "preço",
                "preço ausente ou fora da faixa configurada",
            )
            continue
        rejection = _quality_rejection_reason(card, min_rating, min_reviews)
        if rejection is not None:
            _record_rejection(stats, asin, *rejection)
            continue
        seen.add(asin)
        discovered.append(asin)

    for link in soup.select('a[href*="/dp/"]'):
        match = ASIN_URL_PATTERN.search(link.get("href", ""))
        if match is None:
            continue
        asin = match.group(1).upper()
        if asin in seen:
            continue
        card = link.find_parent(attrs={"data-asin": True})
        if card is None:
            card = link.parent
        if not isinstance(card, Tag):
            continue
        if id(card) in processed_cards:
            continue
        processed_cards.add(id(card))
        stats.cards_found += 1
        price = _price_from_card(card)
        if price is None or not min_price <= price <= max_price:
            _record_rejection(
                stats,
                asin,
                "preço",
                "preço ausente ou fora da faixa configurada",
            )
            continue
        rejection = _quality_rejection_reason(card, min_rating, min_reviews)
        if rejection is not None:
            _record_rejection(stats, asin, *rejection)
            continue
        seen.add(asin)
        discovered.append(asin)

    stats.approved = len(discovered)
    return discovered


def extract_discovered_asins(
    page_html: str,
    min_price: Decimal = DISCOVERY_MIN_PRICE,
    max_price: Decimal = DISCOVERY_MAX_PRICE,
    min_rating: Decimal = DISCOVERY_MIN_RATING,
    min_reviews: int = DISCOVERY_MIN_REVIEWS,
) -> list[str]:
    return _extract_discovered_asins(
        page_html,
        min_price,
        max_price,
        min_rating,
        min_reviews,
        stats=_CategoryStats(),
    )


def _fetch_page(url: str) -> tuple[str, int]:
    response = None
    try:
        response = _get_response(url)
        if _is_amazon_block_page(response.text):
            raise AmazonAccessBlocked(
                f"Bloqueio/CAPTCHA detectado na resposta HTTP "
                f"{response.status_code}."
            )
        response.raise_for_status()
        return response.text, response.status_code
    except AmazonScrapingError:
        raise
    except (requests.exceptions.RequestException, CurlError) as exc:
        status = (
            f" HTTP {response.status_code}"
            if response is not None and response.status_code >= 400
            else ""
        )
        raise AmazonScrapingError(
            f"Falha ao consultar página de descoberta{status} "
            f"({type(exc).__name__})."
        ) from exc
    finally:
        if response is not None:
            response.close()


def _log_category_summary(
    phase: str,
    url: str,
    stats: _CategoryStats,
) -> None:
    if phase == "Início":
        status = "pendente"
        captcha = "pendente"
        cards_found = "pendente"
        discarded = "pendente"
        approved = "pendente"
    else:
        discarded = ", ".join(
            f"{reason}: {count}" for reason, count in sorted(stats.discarded.items())
        ) or "nenhum"
        status = (
            str(stats.status_code)
            if stats.status_code is not None
            else "não recebido"
        )
        captcha = "sim" if stats.captcha_detected else "não detetado"
        cards_found = str(stats.cards_found)
        approved = str(stats.approved)
    logger.info(
        "%s categoria Mais Vendidos %s | status HTTP: %s | CAPTCHA/bloqueio: %s | "
        "cards encontrados: %s | descartados por motivo: %s | ASINs aprovados: %s",
        phase,
        url,
        status,
        captcha,
        cards_found,
        discarded,
        approved,
    )


def _record_error_status(error: AmazonScrapingError, stats: _CategoryStats) -> None:
    status_match = HTTP_STATUS_PATTERN.search(str(error))
    if status_match is not None:
        stats.status_code = int(status_match.group(1))


def discover_products(
    fixed_identifiers: tuple[str, ...] | list[str],
    *,
    max_items: int = DISCOVERY_MAX_ITEMS,
    min_price: Decimal = DISCOVERY_MIN_PRICE,
    max_price: Decimal = DISCOVERY_MAX_PRICE,
    min_rating: Decimal = DISCOVERY_MIN_RATING,
    min_reviews: int = DISCOVERY_MIN_REVIEWS,
    category_urls: tuple[str, ...] = DISCOVERY_CATEGORY_URLS,
) -> tuple[str, ...]:
    if max_items < 1:
        raise ValueError("max_items deve ser um inteiro positivo.")
    if (
        not min_price.is_finite()
        or not max_price.is_finite()
        or min_price < 0
        or max_price < min_price
    ):
        raise ValueError("A faixa de preços da descoberta é inválida.")
    if not min_rating.is_finite() or not Decimal("0") <= min_rating <= Decimal("5"):
        raise ValueError("min_rating deve estar entre 0 e 5.")
    if min_reviews < 0:
        raise ValueError("min_reviews não pode ser negativo.")

    excluded = {
        asin
        for identifier in fixed_identifiers
        if (asin := _normalize_asin(identifier)) is not None
    }
    discovered: list[str] = []
    seen = set(excluded)

    for url in category_urls:
        stats = _CategoryStats()
        _log_category_summary("Início", url, stats)
        try:
            fetched_page = _fetch_page(url)
            if isinstance(fetched_page, tuple):
                page_html, stats.status_code = fetched_page
            else:
                page_html = fetched_page

            stats.captcha_detected = _is_amazon_block_page(page_html)
            if stats.captcha_detected:
                raise AmazonAccessBlocked("Bloqueio/CAPTCHA detectado na categoria.")

            category_asins = _extract_discovered_asins(
                page_html,
                min_price=min_price,
                max_price=max_price,
                min_rating=min_rating,
                min_reviews=min_reviews,
                stats=stats,
            )
            stats.approved = 0
            for asin in category_asins:
                if asin in seen:
                    _record_rejection(
                        stats,
                        asin,
                        "ASIN fixo/duplicado",
                        "ASIN fixo ou já encontrado em outra categoria",
                    )
                    continue
                seen.add(asin)
                discovered.append(asin)
                stats.approved += 1
                if len(discovered) >= max_items:
                    break
        except AmazonAccessBlocked as exc:
            stats.captcha_detected = True
            _record_error_status(exc, stats)
            logger.warning(
                "Categoria Mais Vendidos %s bloqueada (%s); tentando as próximas.",
                url,
                exc,
            )
        except AmazonScrapingError as exc:
            _record_error_status(exc, stats)
            logger.warning(
                "Falha na categoria Mais Vendidos %s (%s); tentando as próximas.",
                url,
                exc,
            )
        finally:
            _log_category_summary("Fim", url, stats)

        if len(discovered) >= max_items:
            break
    if not discovered:
        logger.warning(
            "Falha total da descoberta automática: nenhuma categoria retornou "
            "ASINs válidos."
        )
        raise AmazonScrapingError(
            "Nenhuma categoria da descoberta retornou ASINs válidos."
        )
    return tuple(discovered)
