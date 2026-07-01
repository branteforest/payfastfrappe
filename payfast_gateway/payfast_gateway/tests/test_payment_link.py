import frappe
from frappe.tests.utils import FrappeTestCase

from payfast_gateway.payfast_gateway import api
from payfast_gateway.payfast_gateway.www.pf import index as pf_index


class TestPaymentLink(FrappeTestCase):
    def setUp(self):
        self.settings = frappe.get_single("PayFast Settings")
        self._orig = {
            "environment": self.settings.environment,
            "sandbox_merchant_id": self.settings.sandbox_merchant_id,
            "sandbox_merchant_key": self.settings.sandbox_merchant_key,
            "sandbox_passphrase": self.settings.sandbox_passphrase,
            "notify_url": self.settings.notify_url,
            "return_url": self.settings.return_url,
            "cancel_url": self.settings.cancel_url,
            "currency": self.settings.currency,
            "enabled": self.settings.enabled,
        }
        self.settings.environment = "Sandbox"
        self.settings.sandbox_merchant_id = "10000100"
        self.settings.sandbox_merchant_key = "46f0cd69b5816e2726fbe6b1"
        self.settings.sandbox_passphrase = "testpass"
        self.settings.notify_url = "https://example.com/api/method/payfast_gateway.payfast_gateway.api.payfast_itn"
        self.settings.currency = "ZAR"
        self.settings.enabled = 1
        self.settings.save(ignore_permissions=True)

        if not frappe.db.exists("Role", "PayFast Agent"):
            frappe.get_doc({"doctype": "Role", "role_name": "PayFast Agent"}).insert(ignore_permissions=True)
        self._orig_user = frappe.session.user
        frappe.set_user("Administrator")

        self.si_name = self._make_submitted_sales_invoice()

    def tearDown(self):
        frappe.set_user(self._orig_user)
        for k, v in self._orig.items():
            self.settings.set(k, v)
        self.settings.save(ignore_permissions=True)
        for name in frappe.get_all("PayFast Payment Log", pluck="name"):
            frappe.delete_doc("PayFast Payment Log", name, force=True)
        if self.si_name and frappe.db.exists("Sales Invoice", self.si_name):
            si = frappe.get_doc("Sales Invoice", self.si_name)
            if si.docstatus == 1:
                si.cancel()
            frappe.delete_doc("Sales Invoice", self.si_name, force=True)

    def _make_submitted_sales_invoice(self):
        customer = frappe.get_all("Customer", pluck="name")
        if not customer:
            customer_doc = frappe.get_doc({"doctype": "Customer", "customer_name": "Test Customer"})
            customer_doc.insert(ignore_permissions=True)
            customer = [customer_doc.name]
        item = frappe.get_all("Item", filters={"is_sales_item": 1}, pluck="name")
        if not item:
            item_doc = frappe.get_doc({"doctype": "Item", "item_name": "Test Item", "is_sales_item": 1})
            item_doc.insert(ignore_permissions=True)
            item = [item_doc.name]
        si = frappe.get_doc({
            "doctype": "Sales Invoice",
            "customer": customer[0],
            "company": frappe.defaults.get_user_default("Company"),
            "items": [{"item_code": item[0], "qty": 1, "rate": 100.00}],
        })
        si.insert(ignore_permissions=True)
        si.submit()
        return si.name

    def test_create_payment_link_returns_url_and_expiry(self):
        result = api.create_payment_link(
            reference_doctype="Sales Invoice",
            reference_name=self.si_name,
            amount=100.00,
        )
        self.assertTrue(result["ok"])
        self.assertIn("/pf?token=", result["payment_url"])
        self.assertTrue(result["m_payment_id"])
        self.assertTrue(result["payment_log"])
        self.assertTrue(result["expires_at"])
        log = frappe.get_doc("PayFast Payment Log", result["payment_log"])
        self.assertEqual(log.status, "Awaiting Payment")
        self.assertTrue(log.signature)
        self.assertTrue(log.request_payload_json)

    def test_reference_docname_alias_still_accepted(self):
        result = api.create_payment_link(
            reference_doctype="Sales Invoice",
            reference_docname=self.si_name,
            amount=100.00,
        )
        self.assertTrue(result["ok"])

    def test_unsubmitted_reference_rejected(self):
        draft = frappe.get_doc({
            "doctype": "Sales Invoice",
            "customer": frappe.get_all("Customer", pluck="name")[0],
            "company": frappe.defaults.get_user_default("Company"),
            "items": [{"item_code": frappe.get_all("Item", {"is_sales_item": 1}, pluck="name")[0],
                       "qty": 1, "rate": 100.00}],
        })
        draft.insert(ignore_permissions=True)
        try:
            self.assertRaises(
                frappe.ValidationError, api.create_payment_link,
                reference_doctype="Sales Invoice", reference_name=draft.name, amount=100.00,
            )
        finally:
            frappe.delete_doc("Sales Invoice", draft.name, force=True)

    def test_create_reuses_existing_awaiting_log(self):
        first = api.create_payment_link(reference_doctype="Sales Invoice", reference_name=self.si_name, amount=100.00)
        second = api.create_payment_link(reference_doctype="Sales Invoice", reference_name=self.si_name, amount=100.00)
        self.assertEqual(first["m_payment_id"], second["m_payment_id"])
        self.assertEqual(first["payment_url"], second["payment_url"])

    def test_reuse_skips_expired_link(self):
        first = api.create_payment_link(reference_doctype="Sales Invoice", reference_name=self.si_name, amount=100.00)
        name = first["payment_log"]
        frappe.db.set_value(
            "PayFast Payment Log", name, "expires_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-5)
        )
        second = api.create_payment_link(reference_doctype="Sales Invoice", reference_name=self.si_name, amount=100.00)
        self.assertNotEqual(first["m_payment_id"], second["m_payment_id"])
        self.assertEqual(frappe.db.get_value("PayFast Payment Log", name, "status"), "Cancelled")

    def test_invalid_amount_rejected(self):
        self.assertRaises(frappe.ValidationError, api.create_payment_link,
                          reference_doctype="Sales Invoice", reference_name=self.si_name, amount=0)

    def test_non_zar_currency_rejected(self):
        self.assertRaises(frappe.ValidationError, api.create_payment_link,
                          reference_doctype="Sales Invoice", reference_name=self.si_name,
                          amount=100.00, currency="USD")

    def test_empty_currency_defaults_to_zar(self):
        result = api.create_payment_link(
            reference_doctype="Sales Invoice", reference_name=self.si_name,
            amount=100.00, currency="",
        )
        log = frappe.get_doc("PayFast Payment Log", result["payment_log"])
        self.assertEqual(log.currency, "ZAR")

    def test_disabled_gateway_rejects_create(self):
        self.settings.enabled = 0
        self.settings.save(ignore_permissions=True)
        try:
            self.assertRaises(frappe.ValidationError, api.create_payment_link,
                              reference_doctype="Sales Invoice", reference_name=self.si_name, amount=100.00)
        finally:
            self.settings.enabled = 1
            self.settings.save(ignore_permissions=True)

    def test_cancel_payment_request(self):
        created = api.create_payment_link(reference_doctype="Sales Invoice", reference_name=self.si_name, amount=100.00)
        out = api.cancel_payment_request(created["m_payment_id"], reason="customer changed mind")
        self.assertEqual(out["status"], "Cancelled")
        self.assertTrue(out["ok"])
        log = frappe.get_doc("PayFast Payment Log", out["payment_log"])
        self.assertIn("customer changed mind", log.review_reason)

    def test_regenerate_payment_link(self):
        created = api.create_payment_link(reference_doctype="Sales Invoice", reference_name=self.si_name, amount=100.00)
        regenerated = api.regenerate_payment_link(created["m_payment_id"])
        self.assertTrue(regenerated["ok"])
        self.assertNotEqual(created["m_payment_id"], regenerated["m_payment_id"])
        self.assertEqual(
            frappe.db.get_value("PayFast Payment Log", {"m_payment_id": created["m_payment_id"]}, "status"),
            "Cancelled",
        )

    def test_get_payment_status_by_log_and_mpid(self):
        created = api.create_payment_link(reference_doctype="Sales Invoice", reference_name=self.si_name, amount=100.00)
        by_mpid = api.get_payment_status(m_payment_id=created["m_payment_id"])
        by_log = api.get_payment_status(payment_log=created["payment_log"])
        self.assertTrue(by_mpid["ok"])
        self.assertIn("booking_status", by_mpid)
        self.assertIn("paid_at", by_mpid)
        self.assertEqual(by_log["payment_log"], created["payment_log"])

    def test_expired_link_page_shows_error(self):
        created = api.create_payment_link(reference_doctype="Sales Invoice", reference_name=self.si_name, amount=100.00)
        name = created["payment_log"]
        token = frappe.db.get_value("PayFast Payment Log", name, "redirect_token")
        frappe.db.set_value(
            "PayFast Payment Log", name, "expires_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-5)
        )
        frappe.form_dict = frappe._dict({"token": token})
        try:
            context = frappe._dict()
            pf_index.get_context(context)
            self.assertFalse(context.show_form)
            self.assertIn("expired", context.error.lower())
            self.assertEqual(frappe.db.get_value("PayFast Payment Log", name, "status"), "Cancelled")
        finally:
            frappe.form_dict = frappe._dict()

    def test_role_gating(self):
        if not frappe.db.exists("User", "pf-no-role@example.com"):
            frappe.get_doc({
                "doctype": "User",
                "email": "pf-no-role@example.com",
                "first_name": "NoRole",
                "enabled": 1,
            }).insert(ignore_permissions=True)
        frappe.set_user("pf-no-role@example.com")
        try:
            self.assertRaises(frappe.PermissionError, api.create_payment_link,
                              reference_doctype="Sales Invoice", reference_name=self.si_name, amount=100.00)
        finally:
            frappe.set_user("Administrator")
            if frappe.db.exists("User", "pf-no-role@example.com"):
                frappe.delete_doc("User", "pf-no-role@example.com", force=True)
