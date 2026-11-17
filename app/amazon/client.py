import json
import logging
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlparse

from curl_cffi import requests
from bs4 import BeautifulSoup

from app.amazon.models import Product


logger = logging.getLogger(__name__)


REQUEST_TIMEOUT_SECONDS = 15
MAX_REDIRECTS = 3
ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)
ASIN_URL_PATTERN = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)
PRICE_PATTERN = re.compile(r"(?:R\$?\s*)?([\d.]+,\d{2})")   
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}

CURRENT_PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
    "#tmmSwatches .swatchElement.selected .a-color-price",
    "#tmmSwatches .a-color-price",
    "#priceblock_dealprice",
    "#priceblock_ourprice",
    "#price_inside_buybox",
    ".a-price .a-offscreen",
)
ORIGINAL_PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen",
    "#priceblock_listprice",
)
IMAGE_SELECTORS = ("#landingImage", "#imgBlkFront", "#main-image-container img")


class AmazonScrapingError(Exception):
    """Raised when an Amazon product page cannot be retrieved or parsed."""


class AmazonAccessBlocked(AmazonScrapingError):
    """Raised when Amazon refuses or temporarily blocks the request."""


class AmazonProductParseError(AmazonScrapingError):
    """Raised when required product data is missing or invalid."""


def _validate_amazon_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not hostname
        or not (hostname == "amazon.com.br" or hostname.endswith(".amazon.com.br"))
    ):
        raise AmazonScrapingError("A URL deve pertencer ao domínio amazon.com.br.")
    return url


def _normalize_identifier(identifier: str) -> str:
    value = identifier.strip()
    if ASIN_PATTERN.fullmatch(value):
        return f"https://www.amazon.com.br/dp/{value.upper()}"
    return _validate_amazon_url(value)


def _extract_asin(url: str) -> str | None:
    match = ASIN_URL_PATTERN.search(urlparse(url).path)
    return match.group(1).upper() if match else None


def _get_response(url: str) -> requests.Response:
    current_url = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        response = requests.get(
            current_url,
            impersonate="chrome120",
            headers={"Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"},
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )

        if response.status_code in (403, 503):
            logger.error(
                "A Amazon recusou ou temporariamente bloqueou a consulta (HTTP %s): %s",
                response.status_code,
                current_url,
            )
            response.close()
            raise AmazonAccessBlocked(
                f"A Amazon respondeu HTTP {response.status_code}; consulta interrompida."
            )

        if response.status_code not in REDIRECT_STATUS_CODES:
            return response

        location = response.headers.get("Location")
        response.close()
        if not location:
            raise AmazonScrapingError("Redirecionamento sem cabeçalho Location.")
        if redirect_count == MAX_REDIRECTS:
            raise AmazonScrapingError("Limite de redirecionamentos da Amazon excedido.")
        current_url = _validate_amazon_url(urljoin(current_url, location))

    raise AmazonScrapingError("Não foi possível concluir a consulta à Amazon.")


def _first_element(soup: BeautifulSoup, selectors: tuple[str, ...]):
    for selector in selectors:
        element = soup.select_one(selector)
        if element is not None and element.get_text(strip=True):
            return element
    return None


def _parse_brl_price(text: str) -> Decimal:
    logger.warning(f"Texto bruto do preço capturado: {repr(text)}")
    match = PRICE_PATTERN.search(" ".join(text.split()))
    
    if match is None:
        raise AmazonProductParseError("O preço encontrado não está em formato BRL reconhecido.")

    value = match.group(1).replace(".", "").replace(",", ".")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise AmazonProductParseError("O preço encontrado é inválido.") from exc


def _get_image_url(soup: BeautifulSoup, page_url: str) -> str | None:
    image = _first_element(soup, IMAGE_SELECTORS)
    if image is None:
        logger.warning("Tag de imagem do produto não encontrada.")
        return None

    image_url = (
        image.get("data-old-hires")
        or image.get("src")
    )
    if not image_url:
        dynamic_images = image.get("data-a-dynamic-image")
        if dynamic_images:
            try:
                image_data = json.loads(dynamic_images)
                if isinstance(image_data, dict):
                    image_url = next(iter(image_data), None)
            except json.JSONDecodeError:
                logger.warning("Atributo de imagens dinâmicas não pôde ser interpretado.")

    if not image_url:
        logger.warning("A tag de imagem não contém um endereço utilizável.")
        return None
    return urljoin(page_url, image_url)


def _parse_product(html: str, page_url: str, asin: str) -> Product:
    soup = BeautifulSoup(html, "html.parser")

    title_element = soup.select_one("#productTitle")
    if title_element is None or not title_element.get_text(strip=True):
        if "captcha" in html.casefold():
            logger.warning("Bloqueio de CAPTCHA detectado; consulta interrompida.")
            raise AmazonAccessBlocked("Bloqueio de CAPTCHA detectado.")
        raise AmazonProductParseError("Tag de título do produto não encontrada.")
    title = " ".join(title_element.stripped_strings)

    current_price_element = _first_element(soup, CURRENT_PRICE_SELECTORS)
    if current_price_element is None:
        raise AmazonProductParseError("Tag do preço atual não encontrada.")
    current_price = _parse_brl_price(current_price_element.get_text(" ", strip=True))

    original_price = None
    original_price_element = _first_element(soup, ORIGINAL_PRICE_SELECTORS)
    if original_price_element is None:
        logger.warning("Tag do preço original não encontrada.")
    else:
        try:
            original_price = _parse_brl_price(
                original_price_element.get_text(" ", strip=True)
            )
        except AmazonProductParseError:
            logger.warning("Preço original encontrado, mas não pôde ser interpretado.")

    return Product(
        asin=asin,
        title=title,
        original_price=original_price,
        current_price=current_price,
        image_url=_get_image_url(soup, page_url),
        source_url=page_url,
    )


def fetch_product(identifier: str) -> Product:
    """Fetch a product by ASIN or Amazon Brasil URL, without retrying blocked requests."""
    url = _normalize_identifier(identifier)
    response = None
    try:
        response = _get_response(url)
        response.raise_for_status()
        asin = _extract_asin(response.url) or _extract_asin(url)
        if asin is None:
            raise AmazonProductParseError("ASIN não encontrado na URL do produto.")
        return _parse_product(response.text, response.url, asin)
    except AmazonProductParseError as exc:
        logger.error("Não foi possível interpretar o produto em %s: %s", url, exc)
        raise
    except AmazonScrapingError:
        raise
    except requests.RequestException as exc:
        logger.error(
            "Falha na requisição à Amazon (%s): %s",
            type(exc).__name__,
            url,
        )
        raise AmazonScrapingError(
            f"Falha na requisição à Amazon ({type(exc).__name__})."
        ) from exc
    finally:
        if response is not None:
            response.close()
