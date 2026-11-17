from dataclasses import dataclass
from decimal import Decimal

from app.amazon.models import Product


@dataclass(frozen=True)
class DiscountAnalysis:
    discount_percent: Decimal | None
    approved: bool
    reason: str | None


def analyze_discount(
    product: Product,
    minimum_discount_percent: Decimal = Decimal("15"),
) -> DiscountAnalysis:
    if (
        not minimum_discount_percent.is_finite()
        or minimum_discount_percent < 0
        or minimum_discount_percent > 100
    ):
        raise ValueError("O desconto mínimo deve estar entre 0 e 100.")

    original_price = product.original_price
    current_price = product.current_price
    if original_price is None:
        return DiscountAnalysis(
            discount_percent=None,
            approved=False,
            reason="Preço original indisponível.",
        )
    if (
        not original_price.is_finite()
        or not current_price.is_finite()
        or original_price <= 0
        or current_price <= 0
    ):
        raise ValueError("Os preços devem ser valores decimais finitos maiores que zero.")

    difference = original_price - current_price
    discount_percent = difference * Decimal("100") / original_price
    approved = difference * Decimal("100") > (
        original_price * minimum_discount_percent
    )
    if approved:
        return DiscountAnalysis(discount_percent, True, None)

    if difference <= 0:
        reason = "O preço atual não é menor que o preço original."
    else:
        reason = "O desconto não supera o mínimo configurado."
    return DiscountAnalysis(discount_percent, False, reason)
