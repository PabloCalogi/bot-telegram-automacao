import unittest
from unittest.mock import call, patch

from bs4 import BeautifulSoup
from curl_cffi import requests

from app.amazon.client import (
    AmazonAccessBlocked,
    AmazonProductParseError,
    AmazonScrapingError,
    _get_image_url,
    fetch_product,
)


PRODUCT_HTML = """
<html>
  <span id="productTitle"> Produto de teste </span>
  <div id="corePriceDisplay_desktop_feature_div">
    <span class="priceToPay"><span class="a-offscreen">R$ 89,90</span></span>
    <span class="basisPrice"><span class="a-offscreen">R$ 119,90</span></span>
  </div>
  <span id="landingImage" src="/images/product.jpg">Imagem do produto</span>
</html>
"""


def make_response(status_code: int, html: str = "") -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://www.amazon.com.br/dp/B000000001"
    response._text = html
    return response


def extract_image_url(html: str) -> str | None:
    return _get_image_url(
        BeautifulSoup(html, "html.parser"),
        "https://www.amazon.com.br/dp/B000000001",
        html,
    )


class AmazonClientTests(unittest.TestCase):
    @patch("app.amazon.client.requests.get")
    def test_fetch_product_extracts_fields_with_chrome_impersonation(self, get):
        get.return_value = make_response(200, PRODUCT_HTML)

        product = fetch_product("B000000001")

        self.assertEqual(product.asin, "B000000001")
        self.assertEqual(product.title, "Produto de teste")
        self.assertEqual(str(product.current_price), "89.90")
        self.assertEqual(str(product.original_price), "119.90")
        self.assertEqual(
            product.image_url,
            "https://www.amazon.com.br/images/product.jpg",
        )
        self.assertEqual(product.source_url, "https://www.amazon.com.br/dp/B000000001")
        self.assertEqual(
            get.call_args.kwargs["headers"],
            {"Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"},
        )
        self.assertEqual(get.call_args.kwargs["impersonate"], "chrome124")
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    @patch("app.amazon.client.requests.get")
    def test_extracts_asin_from_input_url_when_response_url_omits_it(self, get):
        response = make_response(200, PRODUCT_HTML)
        response.url = "https://www.amazon.com.br/"
        get.return_value = response

        product = fetch_product(
            "https://www.amazon.com.br/gp/product/B000000001"
        )

        self.assertEqual(product.asin, "B000000001")

    @patch("app.amazon.client.requests.get")
    def test_fetch_product_stops_on_forbidden_or_unavailable(self, get):
        with (
            patch("app.amazon.client.time.sleep"),
            patch("app.amazon.client.random.uniform", return_value=2.5),
        ):
            for status_code in (403, 503):
                with self.subTest(status_code=status_code):
                    get.reset_mock()
                    get.side_effect = [
                        make_response(status_code)
                        for _ in range(3)
                    ]
                    with self.assertLogs("app.amazon.client", level="ERROR") as logs:
                        with self.assertRaises(AmazonAccessBlocked):
                            fetch_product("B000000001")
                    self.assertIn(f"HTTP {status_code}", logs.output[0])
                    self.assertEqual(get.call_count, 3)
                    self.assertEqual(
                        [
                            request.kwargs["impersonate"]
                            for request in get.call_args_list
                        ],
                        ["chrome124", "chrome120", "safari15_5"],
                    )

    @patch("app.amazon.client.requests.get")
    def test_missing_current_price_raises_parse_error(self, get):
        get.return_value = make_response(200, '<span id="productTitle">Produto</span>')

        with self.assertRaisesRegex(AmazonProductParseError, "preço atual"):
            fetch_product("https://www.amazon.com.br/dp/B000000001")

    @patch("app.amazon.client.time.sleep")
    @patch("app.amazon.client.requests.get")
    def test_retries_temporary_curl_ssl_error(self, get, sleep):
        get.side_effect = [
            requests.exceptions.SSLError("temporary TLS failure"),
            make_response(200, PRODUCT_HTML),
        ]

        product = fetch_product("B000000001")

        self.assertEqual(product.asin, "B000000001")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(2)

    @patch("app.amazon.client.time.sleep")
    @patch("app.amazon.client.requests.get")
    def test_wraps_request_exception_after_two_retries(self, get, sleep):
        get.side_effect = requests.exceptions.RequestException("connection failed")

        with self.assertRaisesRegex(AmazonScrapingError, "RequestException"):
            fetch_product("B000000001")

        self.assertEqual(get.call_count, 3)
        sleep.assert_has_calls([call(2), call(2)])

    @patch("app.amazon.client.requests.get")
    def test_parses_out_of_stock_product_without_a_current_price(self, get):
        get.return_value = make_response(
            200,
            """
            <span id="productTitle">Produto sem estoque</span>
            <div id="availability">
              <span>Não sabemos quando este produto estará disponível.</span>
            </div>
            """,
        )

        product = fetch_product("B000000001")

        self.assertFalse(product.in_stock)
        self.assertIsNone(product.current_price)

    def test_retries_captcha_and_missing_title_with_rotating_profiles(self):
        incomplete_pages = (
            '<html><title>validateCaptcha</title></html>',
            '<html><title>Robot Check</title></html>',
            '<html><body>Digite os caracteres</body></html>',
            "<html><body>Resposta sem título de produto</body></html>",
        )
        for incomplete_page in incomplete_pages:
            with self.subTest(incomplete_page=incomplete_page):
                with (
                    patch(
                        "app.amazon.client.requests.get",
                        side_effect=[
                            make_response(200, incomplete_page),
                            make_response(200, PRODUCT_HTML),
                        ],
                    ) as get,
                    patch("app.amazon.client.time.sleep") as sleep,
                    patch(
                        "app.amazon.client.random.uniform",
                        return_value=2.5,
                    ) as jitter,
                ):
                    product = fetch_product("B000000001")

                self.assertEqual(product.title, "Produto de teste")
                self.assertEqual(get.call_count, 2)
                self.assertEqual(
                    [request.kwargs["impersonate"] for request in get.call_args_list],
                    ["chrome124", "chrome120"],
                )
                jitter.assert_called_once_with(2, 5)
                sleep.assert_called_once_with(2.5)

    def test_stops_after_two_captcha_retries(self):
        with (
            patch(
                "app.amazon.client.requests.get",
                side_effect=[
                    make_response(200, '<html><title>captcha challenge</title></html>')
                    for _ in range(3)
                ],
            ) as get,
            patch("app.amazon.client.time.sleep") as sleep,
            patch("app.amazon.client.random.uniform", return_value=3),
            self.assertLogs("app.amazon.client", level="WARNING") as logs,
        ):
            with self.assertRaisesRegex(AmazonAccessBlocked, "CAPTCHA"):
                fetch_product("B000000001")

        self.assertIn("Bloqueio de CAPTCHA/Robot Check detectado", logs.output[0])
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch("app.amazon.client.requests.get")
    def test_missing_original_price_and_image_are_reported_as_optional(self, get):
        html = """
        <span id="productTitle">Produto sem desconto</span>
        <span id="priceblock_ourprice">R$ 49,90</span>
        """
        get.return_value = make_response(200, html)

        product = fetch_product("B000000001")

        self.assertIsNone(product.original_price)
        self.assertIsNone(product.image_url)

    def test_prefers_landing_image_hires_then_largest_dynamic_then_src(self):
        html = """
        <img id="landingImage"
             data-old-hires="https://images.example.test/high.jpg"
             data-a-dynamic-image='{"https://images.example.test/small.jpg":[200,200],
                                   "https://images.example.test/large.jpg":[1200,1200]}'
             src="https://images.example.test/thumbnail.jpg">
        """
        self.assertEqual(
            extract_image_url(html),
            "https://images.example.test/high.jpg",
        )

        html = """
        <img id="landingImage"
             data-a-dynamic-image='{"https://images.example.test/small.jpg":[200,200],
                                   "https://images.example.test/large.jpg":[1200,1200]}'
             src="https://images.example.test/thumbnail.jpg">
        """
        self.assertEqual(
            extract_image_url(html),
            "https://images.example.test/large.jpg",
        )

    def test_falls_back_to_book_images_and_ignores_placeholder_urls(self):
        for selector in ("imgBlkFront", "ebooksImgBlkFront"):
            with self.subTest(selector=selector):
                html = f"""
                <img id="landingImage"
                     src="https://images.example.test/transparent-pixel.gif">
                <img id="{selector}"
                     data-old-hires="https://images.example.test/book.jpg">
                """
                self.assertEqual(
                    extract_image_url(html),
                    "https://images.example.test/book.jpg",
                )

    def test_falls_back_to_open_graph_image(self):
        html = """
        <meta property="og:image" content="/images/open-graph.jpg">
        """
        self.assertEqual(
            extract_image_url(html),
            "https://www.amazon.com.br/images/open-graph.jpg",
        )

    def test_falls_back_to_hires_or_large_image_data_in_html(self):
        for key in ("hiRes", "large"):
            with self.subTest(key=key):
                html = (
                    f'<script>var image = {{"{key}":'
                    '"https://images.example.test/from-json.jpg"};</script>'
                )
                self.assertEqual(
                    extract_image_url(html),
                    "https://images.example.test/from-json.jpg",
                )

    def test_rejects_non_http_and_transparent_pixel_images(self):
        html = """
        <img id="landingImage" src="data:image/gif;base64,R0lGODlhAQABAAD/">
        """
        self.assertIsNone(extract_image_url(html))

    @patch("app.amazon.client.requests.get")
    def test_rejects_urls_outside_amazon_brazil_before_request(self, get):
        with self.assertRaises(AmazonScrapingError):
            fetch_product("https://amazon.com/dp/B000000001")
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
