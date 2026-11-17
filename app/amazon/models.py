from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Product:
    asin: str
    title: str
    original_price: Decimal | None
    current_price: Decimal
    image_url: str | None
    source_url: str
