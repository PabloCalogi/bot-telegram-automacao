import logging
import re
from decimal import Decimal

from bs4 import BeautifulSoup, Tag
from curl_cffi import CurlError, requests

from app.amazon.client import (
    AmazonScrapingError,
    _get_response,
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
) -> str | None:
    title = _title_from_card(card)
    if "bivolt" not in title.casefold() and VOLTAGE_PATTERN.search(title):
        return "voltagem no título"
    if CLOTHING_PATTERN.search(title):
        return "tamanho/vestuário"

    rating = _rating_from_card(card)
    if rating is None or rating < min_rating:
        return f"nota abaixo da mínima ({rating if rating is not None else 'ausente'})"

    reviews = _review_count_from_card(card)
    if reviews is None or reviews < min_reviews:
        return (
            "avaliações abaixo do mínimo "
            f"({reviews if reviews is not None else 'ausentes'})"
        )
    return None


def extract_discovered_asins(
    page_html: str,
    min_price: Decimal = DISCOVERY_MIN_PRICE,
    max_price: Decimal = DISCOVERY_MAX_PRICE,
    min_rating: Decimal = DISCOVERY_MIN_RATING,
    min_reviews: int = DISCOVERY_MIN_REVIEWS,
) -> list[str]:
    soup = BeautifulSoup(page_html, "html.parser")
    discovered: list[str] = []
    seen: set[str] = set()
    processed_cards: set[int] = set()

    for card in soup.select("[data-asin]"):
        processed_cards.add(id(card))
        asin_value = card.get("data-asin")
        asin = _normalize_asin(asin_value) if isinstance(asin_value, str) else None
        if asin is None:
            for link in card.select('a[href*="/dp/"]'):
                match = ASIN_URL_PATTERN.search(link.get("href", ""))
                if match is not None:
                    asin = match.group(1).upper()
                    break
        if asin is None or asin in seen:
            continue

        price = _price_from_card(card)
        if price is None or not min_price <= price <= max_price:
            continue
        rejection_reason = _quality_rejection_reason(card, min_rating, min_reviews)
        if rejection_reason is not None:
            logger.info("Produto %s descartado: %s.", asin, rejection_reason)
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
        price = _price_from_card(card)
        if price is None or not min_price <= price <= max_price:
            continue
        rejection_reason = _quality_rejection_reason(card, min_rating, min_reviews)
        if rejection_reason is not None:
            logger.info("Produto %s descartado: %s.", asin, rejection_reason)
            continue
        seen.add(asin)
        discovered.append(asin)

    return discovered


def _fetch_page(url: str) -> str:
    response = None
    try:
        response = _get_response(url)
        response.raise_for_status()
        return response.text
    except AmazonScrapingError:
        raise
    except (requests.exceptions.RequestException, CurlError) as exc:
        raise AmazonScrapingError(
            f"Falha ao consultar página de descoberta ({type(exc).__name__})."
        ) from exc
    finally:
        if response is not None:
            response.close()


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
        for asin in extract_discovered_asins(
            _fetch_page(url),
            min_price=min_price,
            max_price=max_price,
            min_rating=min_rating,
            min_reviews=min_reviews,
        ):
            if asin in seen:
                continue
            seen.add(asin)
            discovered.append(asin)
            if len(discovered) >= max_items:
                return tuple(discovered)
    return tuple(discovered)
