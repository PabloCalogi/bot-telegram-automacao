import unittest
from decimal import Decimal
from unittest.mock import call, patch

from app.amazon.discovery import discover_products, extract_discovered_asins


def product_card(asin: str, price: str) -> str:
    return (
        f'<div data-asin="{asin}">'
        f'<a href="/dp/{asin}">Produto</a>'
        f'<span class="a-price"><span class="a-offscreen">R$ {price}</span></span>'
        "</div>"
    )


class AmazonDiscoveryTests(unittest.TestCase):
    def test_extracts_unique_asins_and_filters_listing_price(self):
        page_html = (
            "<html><body>"
            + product_card("B000000001", "30,00")
            + product_card("B000000002", "200,00")
            + product_card("B000000003", "29,99")
            + product_card("B000000001", "50,00")
            + "</body></html>"
        )

        self.assertEqual(
            extract_discovered_asins(
                page_html,
                min_price=Decimal("30"),
                max_price=Decimal("200"),
            ),
            ["B000000001", "B000000002"],
        )

    def test_excludes_fixed_asins_deduplicates_and_obeys_max_items(self):
        first_page = (
            product_card("B000000001", "50,00")
            + product_card("B000000002", "29,99")
            + product_card("B000000003", "90,00")
            + product_card("B000000003", "90,00")
        )
        second_page = (
            product_card("B000000004", "201,00")
            + product_card("B000000005", "150,00")
            + product_card("B000000006", "40,00")
        )

        with patch(
            "app.amazon.discovery._fetch_page",
            side_effect=(first_page, second_page),
        ) as fetch_page:
            products = discover_products(
                ("https://www.amazon.com.br/dp/B000000001",),
                max_items=2,
                min_price=Decimal("30"),
                max_price=Decimal("200"),
                category_urls=("page-one", "page-two"),
            )

        self.assertEqual(products, ("B000000003", "B000000005"))
        fetch_page.assert_has_calls([call("page-one"), call("page-two")])


if __name__ == "__main__":
    unittest.main()
