import hashlib

from frappe.tests.utils import FrappeTestCase

from payfast_gateway.payfast_gateway.services.signature import (
    build_parameter_string,
    generate_signature,
    normalize_itn_fields,
    verify_signature,
)


class TestSignature(FrappeTestCase):
    def test_field_order_preserved_not_alphabetical(self):
        items = [("merchant_id", "100001"), ("amount", "10.00"), ("name_first", "Jane")]
        sig = generate_signature(items, passphrase="secret")
        # Recompute in the SAME order to confirm non-alphabetical ordering is used.
        manual = hashlib.md5(
            ("merchant_id=100001&amount=10.00&name_first=Jane&passphrase=secret").encode()
        ).hexdigest().lower()
        self.assertEqual(sig, manual)

    def test_empty_passphrase_omitted_entirely(self):
        items = [("merchant_id", "100001"), ("amount", "10.00")]
        sig = generate_signature(items, passphrase="")
        manual = hashlib.md5(
            "merchant_id=100001&amount=10.00".encode()
        ).hexdigest().lower()
        self.assertEqual(sig, manual)
        self.assertNotIn("passphrase", build_parameter_string(items) + "")

    def test_empty_values_dropped(self):
        items = [("merchant_id", "100001"), ("name_first", ""), ("amount", "10.00")]
        ps = build_parameter_string(items)
        self.assertNotIn("name_first", ps)
        self.assertEqual(ps, "merchant_id=100001&amount=10.00")

    def test_url_encoding_uppercase_hex_and_plus_spaces(self):
        # value with a space and a slash -> space becomes +, slash -> %2F (uppercase hex)
        items = [("item_description", "a b/c")]
        ps = build_parameter_string(items)
        self.assertEqual(ps, "item_description=a+b%2Fc")

    def test_testing_param_excluded_from_signature(self):
        items = [("merchant_id", "100001"), ("amount", "10.00"), ("testing", "true")]
        ps = build_parameter_string(items)
        self.assertNotIn("testing", ps)

    def test_tilde_encoded_as_percent_7e(self):
        # PHP urlencode() encodes '~' to %7E; Python quote_plus leaves it bare.
        items = [("item_name", "a~b")]
        ps = build_parameter_string(items)
        self.assertEqual(ps, "item_name=a%7Eb")
        self.assertNotIn("~", ps)

    def test_lowercase_md5(self):
        sig = generate_signature([("merchant_id", "100001")], passphrase="s")
        self.assertEqual(sig, sig.lower())
        self.assertRegex(sig, r"^[0-9a-f]{32}$")

    def test_verify_roundtrip(self):
        items = [("merchant_id", "100001"), ("amount", "10.00")]
        sig = generate_signature(items, passphrase="secret")
        self.assertTrue(verify_signature(items, "secret", sig))
        self.assertFalse(verify_signature(items, "wrong", sig))
        self.assertFalse(verify_signature(items, "secret", ""))

    def test_itn_field_order_preserved(self):
        formdict = {"m_payment_id": "M1", "amount_gross": "10.00", "signature": "x"}
        items = normalize_itn_fields(formdict)
        names = [n for n, _ in items]
        self.assertEqual(names, ["m_payment_id", "amount_gross"])

    def test_payfast_official_example(self):
        # Mirror of PayFast's documented example ordering/values.
        items = [
            ("merchant_id", "10000100"),
            ("merchant_key", "46f0cd69b5816e2726fbe6b1"),
            ("return_url", "https://example.com/return"),
            ("cancel_url", "https://example.com/cancel"),
            ("notify_url", "https://example.com/notify"),
            ("name_first", "First Name"),
            ("name_last", "Last Name"),
            ("email_address", "test@example.com"),
            ("m_payment_id", "100"),
            ("amount", "20.00"),
            ("item_name", "store purchase"),
        ]
        sig = generate_signature(items, passphrase="")
        expected = hashlib.md5(
            "merchant_id=10000100&merchant_key=46f0cd69b5816e2726fbe6b1"
            "&return_url=https%3A%2F%2Fexample.com%2Freturn"
            "&cancel_url=https%3A%2F%2Fexample.com%2Fcancel"
            "&notify_url=https%3A%2F%2Fexample.com%2Fnotify"
            "&name_first=First+Name&name_last=Last+Name"
            "&email_address=test%40example.com"
            "&m_payment_id=100&amount=20.00&item_name=store+purchase"
            .encode()
        ).hexdigest().lower()
        self.assertEqual(sig, expected)
