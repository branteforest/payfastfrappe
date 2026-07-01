import frappe
from frappe.tests.utils import FrappeTestCase


class TestPayFastPaymentLog(FrappeTestCase):
    def test_unique_fields(self):
        meta = frappe.get_meta("PayFast Payment Log")
        for fname in ("m_payment_id", "redirect_token"):
            self.assertTrue(meta.get_field(fname).unique, f"{fname} must be unique")
