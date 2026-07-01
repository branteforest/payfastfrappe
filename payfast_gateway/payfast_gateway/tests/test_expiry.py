import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from payfast_gateway.payfast_gateway.services.expiry import expire_stale_links


class TestLinkExpiry(FrappeTestCase):
    def setUp(self):
        self._orig_user = frappe.session.user
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.set_user(self._orig_user)
        for name in frappe.get_all("PayFast Payment Log", pluck="name"):
            frappe.delete_doc("PayFast Payment Log", name, force=True, ignore_on_trash=True)

    def test_expire_stale_links_cancels_old_awaiting_logs(self):
        log = frappe.get_doc({
            "doctype": "PayFast Payment Log",
            "m_payment_id": "PFM-EXPIRE-1",
            "reference_doctype": "Sales Invoice",
            "reference_docname": "",
            "amount": 100.00,
            "currency": "ZAR",
            "status": "Awaiting Payment",
        })
        log.insert(ignore_permissions=True)
        frappe.db.set_value(
            "PayFast Payment Log",
            log.name,
            "creation",
            add_to_date(now_datetime(), minutes=-120),
        )
        expire_stale_links()
        self.assertEqual(
            frappe.db.get_value("PayFast Payment Log", log.name, "status"),
            "Cancelled",
        )

    def test_expire_stale_links_ignores_recent_logs(self):
        log = frappe.get_doc({
            "doctype": "PayFast Payment Log",
            "m_payment_id": "PFM-EXPIRE-2",
            "reference_doctype": "Sales Invoice",
            "reference_docname": "",
            "amount": 50.00,
            "currency": "ZAR",
            "status": "Awaiting Payment",
        })
        log.insert(ignore_permissions=True)
        expire_stale_links()
        self.assertEqual(
            frappe.db.get_value("PayFast Payment Log", log.name, "status"),
            "Awaiting Payment",
        )
