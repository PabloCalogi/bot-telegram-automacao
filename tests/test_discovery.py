import unittest
from decimal import Decimal
from unittest.mock import call, patch

from curl_cffi import requests

from app.amazon.client import (
    AmazonAccessBlocked,
    AmazonScrapingError,
    close_amazon_session,
)
from app.amazon.discovery import discover_products, extract_discovered_asins


def product_card(
    asin: str,
    price: str,
    title: str = "Produto universal",
    rating: str | None = "4,5",
    reviews: str | None = "100",
) -> str:
    rating_html = (
        f'<span class="a-icon-alt">{rating} de 5 estrelas</span>'
        if rating is not None
        else ""
    )
    reviews_html = (
        f'<span class="a-size-small">{reviews}</span>'
        if reviews is not None
        else ""
    )
    return (
        f'<div data-asin="{asin}">'
        f'<a href="/dp/{asin}"><span class="product-title">{title}</span></a>'
        f"{rating_html}{reviews_html}"
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

    def test_rejects_voltage_titles_except_bivolt(self):
        page_html = (
            product_card("B000000007", "80,00", title="Aparelho 220V")
            + product_card(
                "B000000008",
                "80,00",
                title="Aparelho 127V/220V Bivolt",
            )
        )

        self.assertEqual(extract_discovered_asins(page_html), ["B000000008"])

    def test_rejects_clothing_and_shoe_titles(self):
        page_html = (
            product_card("B000000009", "80,00", title="Tênis esportivo")
            + product_card(
                "B000000010",
                "80,00",
                title="Camiseta Tamanho M",
            )
        )

        self.assertEqual(extract_discovered_asins(page_html), [])

    def test_rejects_low_rating_or_insufficient_reviews(self):
        page_html = (
            product_card("B000000011", "80,00", rating="4,2", reviews="100")
            + product_card("B000000012", "80,00", rating="4,5", reviews="49")
        )

        with self.assertLogs("app.amazon.discovery", level="INFO") as logs:
            products = extract_discovered_asins(page_html)

        self.assertEqual(products, [])
        self.assertIn("nota 4.2 abaixo de 4.3", logs.output[0])
        self.assertIn("49 avaliações; mínimo 50", logs.output[1])

    def test_accepts_universal_product_meeting_quality_and_price_limits(self):
        page_html = product_card(
            "B000000013",
            "80,00",
            title="Garrafa térmica universal",
            rating="4,3",
            reviews="50",
        )

        self.assertEqual(extract_discovered_asins(page_html), ["B000000013"])

    def test_logs_category_status_discard_reasons_and_approved_count(self):
        page_html = (
            product_card("B000000014", "80,00")
            + product_card("B000000015", "80,00", title="Aparelho 220V")
            + product_card("B000000016", "29,99")
            + product_card("B000000017", "80,00", rating=None)
            + product_card("B000000018", "80,00", reviews=None)
            + product_card("B000000019", "80,00", rating="4,2")
            + product_card("B000000020", "80,00", reviews="49")
        )

        with (
            patch(
                "app.amazon.discovery._fetch_page",
                return_value=(page_html, 200),
            ),
            self.assertLogs("app.amazon.discovery", level="INFO") as logs,
        ):
            products = discover_products(
                (),
                category_urls=("bestsellers-test",),
            )

        self.assertEqual(products, ("B000000014",))
        self.assertIn("status HTTP: pendente", logs.output[0])
        category_summary = logs.output[-1]
        self.assertIn("Fim categoria Mais Vendidos bestsellers-test", category_summary)
        self.assertIn("status HTTP: 200", category_summary)
        self.assertIn("CAPTCHA/bloqueio: não detetado", category_summary)
        self.assertIn("cards encontrados: 7", category_summary)
        self.assertIn("preço: 1", category_summary)
        self.assertIn("voltagem: 1", category_summary)
        self.assertIn("nota ausente: 1", category_summary)
        self.assertIn("nota abaixo do mínimo: 1", category_summary)
        self.assertIn("avaliações ausentes: 1", category_summary)
        self.assertIn("avaliações abaixo do mínimo: 1", category_summary)
        self.assertIn("ASINs aprovados: 1", category_summary)

    def test_categories_reuse_the_client_session_and_close_it(self):
        first_page = product_card("B000000021", "80,00")
        second_page = product_card("B000000022", "80,00")
        first_response = requests.Response()
        first_response.status_code = 200
        first_response.url = "https://www.amazon.com.br/gp/bestsellers/one"
        first_response._text = first_page
        second_response = requests.Response()
        second_response.status_code = 200
        second_response.url = "https://www.amazon.com.br/gp/bestsellers/two"
        second_response._text = second_page

        with (
            patch("app.amazon.client.requests.Session") as session_class,
            patch("app.amazon.client.REQUEST_DELAY_RANGE_SECONDS", (0, 0)),
        ):
            session = session_class.return_value
            session.get.side_effect = [first_response, second_response]

            products = discover_products(
                (),
                category_urls=("category-one", "category-two"),
            )
            close_amazon_session()

        self.assertEqual(products, ("B000000021", "B000000022"))
        session_class.assert_called_once_with(
            headers={"Accept-Language": "pt-BR,pt;q=0.9"}
        )
        self.assertEqual(session.get.call_count, 2)
        session.close.assert_called_once()

    def test_continues_to_next_category_after_blocked_category(self):
        valid_page = product_card("B000000023", "80,00")
        with (
            patch(
                "app.amazon.discovery._fetch_page",
                side_effect=[
                    AmazonAccessBlocked("HTTP 503"),
                    (valid_page, 200),
                ],
            ) as fetch_page,
            self.assertLogs("app.amazon.discovery", level="WARNING") as logs,
        ):
            products = discover_products(
                (),
                category_urls=("blocked-category", "working-category"),
            )

        self.assertEqual(products, ("B000000023",))
        fetch_page.assert_has_calls(
            [call("blocked-category"), call("working-category")]
        )
        self.assertTrue(
            any("bloqueada" in message for message in logs.output)
        )

    def test_category_reuses_client_retry_for_transient_network_error(self):
        response = requests.Response()
        response.status_code = 200
        response.url = "https://www.amazon.com.br/gp/bestsellers/test"
        response._text = product_card("B000000025", "80,00")
        with (
            patch("app.amazon.client.requests.Session") as session_class,
            patch("app.amazon.client.REQUEST_DELAY_RANGE_SECONDS", (0, 0)),
            patch("app.amazon.client.time.sleep") as sleep,
        ):
            session = session_class.return_value
            session.get.side_effect = [
                requests.exceptions.RequestException("temporary network error"),
                response,
            ]

            products = discover_products(
                (),
                category_urls=("working-after-network-retry",),
            )
            close_amazon_session()

        self.assertEqual(products, ("B000000025",))
        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once_with(2)
        session_class.assert_called_once()

    def test_persistent_category_failure_does_not_prevent_next_category(self):
        valid_page = product_card("B000000024", "80,00")
        with (
            patch(
                "app.amazon.discovery._fetch_page",
                side_effect=[
                    AmazonScrapingError("Falha de rede após retries"),
                    (valid_page, 200),
                ],
            ) as fetch_page,
            self.assertLogs("app.amazon.discovery", level="WARNING") as logs,
        ):
            products = discover_products(
                (),
                category_urls=("failed-category", "working-category"),
            )

        self.assertEqual(products, ("B000000024",))
        fetch_page.assert_has_calls(
            [call("failed-category"), call("working-category")]
        )
        self.assertTrue(
            any("tentando as próximas" in message for message in logs.output)
        )

    def test_reports_total_failure_only_when_no_category_returns_products(self):
        with (
            patch(
                "app.amazon.discovery._fetch_page",
                side_effect=AmazonScrapingError("Falha de rede após retries"),
            ),
            self.assertLogs("app.amazon.discovery", level="WARNING") as logs,
        ):
            with self.assertRaisesRegex(AmazonScrapingError, "Nenhuma categoria"):
                discover_products((), category_urls=("failed-category",))

        self.assertTrue(any("Falha total" in message for message in logs.output))

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
