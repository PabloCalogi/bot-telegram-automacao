import re
from decimal import Decimal

from bs4 import BeautifulSoup, Tag
from curl_cffi import CurlError, requests

from app.amazon.client import (
    AmazonScrapingError,
    _get_response,
)


DISCOVERY_MAX_ITEMS = 10
DISCOVERY_MIN_PRICE = Decimal("30.0")
DISCOVERY_MAX_PRICE = Decimal("200.0")
DISCOVERY_CATEGORY_URLS = (
    "https://www.amazon.com.br/gp/bestsellers/electronics",
    "https://www.amazon.com.br/gp/bestsellers/kitchen",
    "https://www.amazon.com.br/gp/bestsellers/beauty",
)

ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)
ASIN_URL_PATTERN = re.compile(r"/dp/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)
PRICE_PATTERN = re.compile(r"(?:R\$\s*)?([\d.]+,\d{2})")


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


def extract_discovered_asins(
    page_html: str,
    min_price: Decimal = DISCOVERY_MIN_PRICE,
    max_price: Decimal = DISCOVERY_MAX_PRICE,
) -> list[str]:
    soup = BeautifulSoup(page_html, "html.parser")
    discovered: list[str] = []
    seen: set[str] = set()

    for card in soup.select("[data-asin]"):
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
        price = _price_from_card(card)
        if price is None or not min_price <= price <= max_price:
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
        ):
            if asin in seen:
                continue
            seen.add(asin)
            discovered.append(asin)
            if len(discovered) >= max_items:
                return tuple(discovered)
    return tuple(discovered)
