import json
import logging
import random
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlparse

from curl_cffi import CurlError, requests
from bs4 import BeautifulSoup, Tag

from app.amazon.models import Product


logger = logging.getLogger(__name__)


REQUEST_TIMEOUT_SECONDS = 15
REQUEST_RETRIES = 2
REQUEST_RETRY_DELAY_SECONDS = 2
BLOCK_PAGE_RETRIES = 2
BLOCK_PAGE_RETRY_PROFILES = ("chrome124", "chrome120", "safari15_5")
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
BOOK_IMAGE_SELECTORS = (
    "#imgBlkFront",
    "#ebooksImgBlkFront",
)
LEGACY_IMAGE_SELECTORS = ("#main-image-container img",)
IMAGE_DATA_PATTERN = re.compile(
    r'"(?:hiRes|large)"\s*:\s*"(https://[^"]+)"',
    re.IGNORECASE,
)
PLACEHOLDER_IMAGE_MARKERS = (
    "transparent",
    "pixel",
    "placeholder",
    "spacer.gif",
    "blank.gif",
)


class AmazonScrapingError(Exception):
    """Raised when an Amazon product page cannot be retrieved or parsed."""


class AmazonAccessBlocked(AmazonScrapingError):
    """Raised when Amazon refuses or temporarily blocks the request."""


class AmazonProductParseError(AmazonScrapingError):
    """Raised when required product data is missing or invalid."""


class _AmazonProductTitleMissing(AmazonProductParseError):
    """Raised when the product title is missing and a retry may be useful."""


def _is_amazon_block_page(page_html: str) -> bool:
    lowered_html = page_html.casefold()
    return any(
        marker in lowered_html
        for marker in (
            "validatecaptcha",
            "robot check",
            "digite os caracteres",
            "captcha",
        )
    )


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


def _get_response(
    url: str,
    *,
    impersonate: str = "chrome120",
) -> requests.Response:
    current_url = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        for attempt in range(REQUEST_RETRIES + 1):
            try:
                response = requests.get(
                    current_url,
                    impersonate=impersonate,
                    headers={"Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    allow_redirects=False,
                )
                break
            except (requests.exceptions.RequestException, CurlError) as exc:
                if attempt == REQUEST_RETRIES:
                    raise
                logger.warning(
                    "Falha temporária na conexão com a Amazon (%s); "
                    "nova tentativa %s/%s em %s segundos.",
                    type(exc).__name__,
                    attempt + 1,
                    REQUEST_RETRIES,
                    REQUEST_RETRY_DELAY_SECONDS,
                )
                time.sleep(REQUEST_RETRY_DELAY_SECONDS)

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
    logger.debug("Texto bruto do preço capturado: %r", text)
    match = PRICE_PATTERN.search(" ".join(text.split()))
    
    if match is None:
        raise AmazonProductParseError("O preço encontrado não está em formato BRL reconhecido.")

    value = match.group(1).replace(".", "").replace(",", ".")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise AmazonProductParseError("O preço encontrado é inválido.") from exc


def _validated_image_url(image_url: str | None, page_url: str) -> str | None:
    if not image_url:
        return None

    resolved_url = urljoin(page_url, image_url.strip())
    parsed_url = urlparse(resolved_url)
    if (
        parsed_url.scheme.lower() not in ("http", "https")
        or not parsed_url.netloc
        or any(
            marker in parsed_url.path.casefold()
            for marker in PLACEHOLDER_IMAGE_MARKERS
        )
    ):
        return None
    return resolved_url


def _dynamic_image_urls(image: Tag) -> list[str]:
    dynamic_images = image.get("data-a-dynamic-image")
    if not dynamic_images:
        return []

    try:
        image_data = json.loads(dynamic_images)
    except json.JSONDecodeError:
        logger.warning("Atributo de imagens dinâmicas não pôde ser interpretado.")
        return []

    if not isinstance(image_data, dict):
        return []

    resolutions = []
    for image_url, dimensions in image_data.items():
        if not isinstance(image_url, str):
            continue
        if not isinstance(dimensions, (list, tuple)) or len(dimensions) < 2:
            resolution = 0
        else:
            try:
                resolution = float(dimensions[0]) * float(dimensions[1])
            except (OverflowError, TypeError, ValueError):
                resolution = 0
        resolutions.append((resolution, image_url))
    resolutions.sort(key=lambda item: item[0], reverse=True)
    return [image_url for _, image_url in resolutions]


def _image_element_candidates(image: Tag) -> list[str | None]:
    return [
        image.get("data-old-hires"),
        *_dynamic_image_urls(image),
        image.get("src"),
    ]


def _first_valid_image_url(
    candidates: list[str | None],
    page_url: str,
) -> str | None:
    for candidate in candidates:
        image_url = _validated_image_url(candidate, page_url)
        if image_url is not None:
            return image_url
    return None


def _get_image_url(
    soup: BeautifulSoup,
    page_url: str,
    page_html: str,
) -> str | None:
    landing_image = soup.select_one("#landingImage")
    if landing_image is not None:
        image_url = _first_valid_image_url(
            _image_element_candidates(landing_image),
            page_url,
        )
        if image_url is not None:
            return image_url

    for selector in BOOK_IMAGE_SELECTORS:
        image = soup.select_one(selector)
        if image is not None:
            image_url = _first_valid_image_url(
                _image_element_candidates(image),
                page_url,
            )
            if image_url is not None:
                return image_url

    og_image = soup.select_one('meta[property="og:image"]')
    if og_image is not None:
        image_url = _validated_image_url(og_image.get("content"), page_url)
        if image_url is not None:
            return image_url

    for match in IMAGE_DATA_PATTERN.finditer(page_html):
        image_url = _validated_image_url(match.group(1), page_url)
        if image_url is not None:
            return image_url

    for selector in LEGACY_IMAGE_SELECTORS:
        image = soup.select_one(selector)
        if image is not None:
            image_url = _first_valid_image_url(
                _image_element_candidates(image),
                page_url,
            )
            if image_url is not None:
                return image_url

    logger.warning("Imagem do produto não encontrada ou inválida.")
    return None


def _parse_product(html: str, page_url: str, asin: str) -> Product:
    if _is_amazon_block_page(html):
        logger.warning("Bloqueio de CAPTCHA/Robot Check detectado.")
        raise AmazonAccessBlocked("Bloqueio de CAPTCHA detectado.")

    soup = BeautifulSoup(html, "html.parser")

    title_element = soup.select_one("#productTitle")
    if title_element is None or not title_element.get_text(strip=True):
        raise _AmazonProductTitleMissing("Tag de título do produto não encontrada.")
    title = " ".join(title_element.stripped_strings)

    availability_element = soup.select_one(
        "#availability, #availabilityInsideBuyBox_feature_div"
    )
    availability = (
        availability_element.get_text(" ", strip=True).casefold()
        if availability_element is not None
        else ""
    )
    out_of_stock = any(
        phrase in availability
        for phrase in (
            "atualmente indisponível",
            "não disponível",
            "indisponível",
            "sem estoque",
            "fora de estoque",
            "não sabemos quando",
            "não temos previsão",
            "currently unavailable",
            "out of stock",
        )
    )
    current_price_element = _first_element(soup, CURRENT_PRICE_SELECTORS)
    if current_price_element is None and not out_of_stock:
        raise AmazonProductParseError("Tag do preço atual não encontrada.")
    current_price = (
        _parse_brl_price(current_price_element.get_text(" ", strip=True))
        if current_price_element is not None
        else None
    )

    original_price = None
    original_price_element = _first_element(soup, ORIGINAL_PRICE_SELECTORS)
    if original_price_element is None:
        logger.debug("Tag do preço original não encontrada.")
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
        image_url=_get_image_url(soup, page_url, html),
        source_url=page_url,
        in_stock=not out_of_stock,
    )


def fetch_product(identifier: str) -> Product:
    """Fetch a product, retrying blocked or incomplete HTML with rotating profiles."""
    url = _normalize_identifier(identifier)
    for attempt in range(BLOCK_PAGE_RETRIES + 1):
        response = None
        retry_reason = ""
        try:
            response = _get_response(
                url,
                impersonate=BLOCK_PAGE_RETRY_PROFILES[
                    attempt % len(BLOCK_PAGE_RETRY_PROFILES)
                ],
            )
            if _is_amazon_block_page(response.text):
                logger.warning("Bloqueio de CAPTCHA/Robot Check detectado.")
                raise AmazonAccessBlocked(
                    f"Bloqueio/CAPTCHA detectado na resposta HTTP "
                    f"{response.status_code}."
                )
            response.raise_for_status()
            asin = _extract_asin(response.url) or _extract_asin(url)
            if asin is None:
                raise AmazonProductParseError("ASIN não encontrado na URL do produto.")
            return _parse_product(response.text, response.url, asin)
        except (AmazonAccessBlocked, _AmazonProductTitleMissing) as exc:
            if attempt == BLOCK_PAGE_RETRIES:
                logger.error(
                    "Bloqueio ou título ausente persistiu após %s tentativas (%s).",
                    attempt + 1,
                    type(exc).__name__,
                )
                raise
            retry_reason = str(exc)
            logger.warning(
                "Resposta bloqueada/incompleta da Amazon; tentativa %s/%s será "
                "repetida com outro perfil.",
                attempt + 1,
                BLOCK_PAGE_RETRIES,
            )
        except AmazonProductParseError as exc:
            logger.error("Não foi possível interpretar o produto em %s: %s", url, exc)
            raise
        except AmazonScrapingError:
            raise
        except (requests.exceptions.RequestException, CurlError) as exc:
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

        if retry_reason:
            delay = random.uniform(2, 5)
            logger.info(
                "Nova tentativa após resposta incompleta (%s), aguardando %.2f "
                "segundos.",
                retry_reason,
                delay,
            )
            time.sleep(delay)

    raise AmazonScrapingError("Não foi possível concluir a consulta à Amazon.")
