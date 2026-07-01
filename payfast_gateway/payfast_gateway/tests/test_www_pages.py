from pathlib import Path

from frappe.tests.utils import FrappeTestCase


class TestWWWPages(FrappeTestCase):
    def test_pf_return_and_cancel_pages_exist(self):
        base = Path(__file__).resolve().parents[1] / "www"
        for route in ("pf-return", "pf-cancel"):
            html = (base / route / "index.html").read_text(encoding="utf-8")
            self.assertNotIn('method="post"', html.lower())
            self.assertNotIn("payfast.co.za", html.lower())
            index_py = (base / route / "index.py").read_text(encoding="utf-8")
            self.assertIn("no_cache", index_py)
