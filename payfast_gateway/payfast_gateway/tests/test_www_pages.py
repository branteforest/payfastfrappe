import os

import frappe
from frappe.tests.utils import FrappeTestCase

from payfast_gateway.payfast_gateway.services import expiry as expiry_service


class TestWWWPages(FrappeTestCase):
    def test_pf_return_page_exists(self):
        path = os.path.join(
            frappe.get_app_path("payfast_gateway"),
            "payfast_gateway",
            "www",
            "pf-return",
            "index.html",
        )
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            html = f.read()
        self.assertIn("Payment submitted", html)
        self.assertNotIn("method=\"post\"", html.lower())

    def test_pf_cancel_page_exists(self):
        path = os.path.join(
            frappe.get_app_path("payfast_gateway"),
            "payfast_gateway",
            "www",
            "pf-cancel",
            "index.html",
        )
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            html = f.read()
        self.assertIn("Payment cancelled", html)
        self.assertNotIn("method=\"post\"", html.lower())


class TestExpiry(FrappeTestCase):
    def test_expire_stale_links_cancels_old_awaiting(self):
        log = frappe.get_doc({
            "doctype": "PayFast Payment Log",
            "m_payment_id": "PFM-EXPIRE-TEST",
            "reference_doctype": "Sales Invoice",
            "reference_docname": "",
            "amount": 10,
            "currency": "ZAR",
            "status": "Awaiting Payment",
        })
        log.insert(ignore_permissions=True)
        frappe.db.set_value(
            "PayFast Payment Log",
            log.name,
            "creation",
            frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-2),
        )
        expiry_service.expire_stale_links()
        self.assertEqual(
            frappe.db.get_value("PayFast Payment Log", log.name, "status"),
            "Cancelled",
        )
        frappe.delete_doc("PayFast Payment Log", log.name, force=True)
