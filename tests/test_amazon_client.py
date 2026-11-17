import unittest
from io import BytesIO
from unittest.mock import patch

import requests

from app.amazon.client import (
    USER_AGENT,
    AmazonAccessBlocked,
    AmazonProductParseError,
    AmazonScrapingError,
    fetch_product,
)


PRODUCT_HTML = """
<html>
  <span id="productTitle"> Produto de teste </span>
  <div id="corePriceDisplay_desktop_feature_div">
    <span class="priceToPay"><span class="a-offscreen">R$ 89,90</span></span>
    <span class="basisPrice"><span class="a-offscreen">R$ 119,90</span></span>
  </div>
  <img id="landingImage" src="/images/product.jpg">
</html>
"""


def make_response(status_code: int, html: str = "") -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://www.amazon.com.br/dp/B000000001"
    response._content = html.encode("utf-8")
    response.encoding = "utf-8"
    response.raw = BytesIO()
    return response


class AmazonClientTests(unittest.TestCase):
    @patch("app.amazon.client.requests.get")
    def test_fetch_product_extracts_fields_with_only_fixed_user_agent(self, get):
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
            {"User-Agent": USER_AGENT},
        )
        self.assertIn("Chrome/", USER_AGENT)
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
        for status_code in (403, 503):
            with self.subTest(status_code=status_code):
                get.reset_mock()
                get.return_value = make_response(status_code)
                with self.assertLogs("app.amazon.client", level="ERROR") as logs:
                    with self.assertRaises(AmazonAccessBlocked):
                        fetch_product("B000000001")
                self.assertIn(f"HTTP {status_code}", logs.output[0])
                get.assert_called_once()

    @patch("app.amazon.client.requests.get")
    def test_missing_current_price_raises_parse_error(self, get):
        get.return_value = make_response(200, '<span id="productTitle">Produto</span>')

        with self.assertRaisesRegex(AmazonProductParseError, "preço atual"):
            fetch_product("https://www.amazon.com.br/dp/B000000001")

    @patch("app.amazon.client.requests.get")
    def test_detects_captcha_and_stops_without_retrying(self, get):
        get.return_value = make_response(
            200,
            '<html><title>captcha challenge</title></html>',
        )

        with self.assertLogs("app.amazon.client", level="WARNING") as logs:
            with self.assertRaisesRegex(AmazonAccessBlocked, "CAPTCHA"):
                fetch_product("B000000001")

        self.assertIn("Bloqueio de CAPTCHA detectado", logs.output[0])
        get.assert_called_once()

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

    @patch("app.amazon.client.requests.get")
    def test_rejects_urls_outside_amazon_brazil_before_request(self, get):
        with self.assertRaises(AmazonScrapingError):
            fetch_product("https://amazon.com/dp/B000000001")
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
