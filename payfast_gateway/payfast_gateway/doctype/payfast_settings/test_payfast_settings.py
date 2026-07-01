import frappe
from frappe.tests.utils import FrappeTestCase


class TestPayFastSettings(FrappeTestCase):
    def test_single_exists(self):
        s = frappe.get_single("PayFast Settings")
        self.assertEqual(s.doctype, "PayFast Settings")
