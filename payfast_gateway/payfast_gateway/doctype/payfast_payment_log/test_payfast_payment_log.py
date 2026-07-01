import frappe
from frappe.tests.utils import FrappeTestCase


class TestPayFastPaymentLog(FrappeTestCase):
    def test_unique_fields(self):
        meta = frappe.get_meta("PayFast Payment Log")
        for fname in ("m_payment_id", "redirect_token"):
            self.assertTrue(meta.get_field(fname).unique, f"{fname} must be unique")

    def _make_log(self, m_payment_id, **kwargs):
        if frappe.db.exists("PayFast Payment Log", {"m_payment_id": m_payment_id}):
            frappe.delete_doc(
                "PayFast Payment Log",
                frappe.db.get_value("PayFast Payment Log", {"m_payment_id": m_payment_id}),
                force=True,
            )
        vals = {
            "doctype": "PayFast Payment Log",
            "m_payment_id": m_payment_id,
            "reference_doctype": "Sales Invoice",
            "reference_docname": None,
            "amount": 100.00,
            "currency": "ZAR",
            "status": "Awaiting Payment",
        }
        vals.update(kwargs)
        log = frappe.get_doc(vals)
        log.insert(ignore_permissions=True)
        return log

    def tearDown(self):
        for n in frappe.get_all("PayFast Payment Log", {"m_payment_id": ["like", "PFM-GUARD-%"]}, pluck="name"):
            frappe.delete_doc("PayFast Payment Log", n, force=True, ignore_on_trash=True)

    def test_manual_complete_without_payment_entry_rejected(self):
        """A manual edit cannot mark a log Complete without a linked Payment
        Entry -- only the ITN pipeline is allowed to do that, and it always
        sets both fields together."""
        log = self._make_log("PFM-GUARD-1")
        log.status = "Complete"
        self.assertRaises(frappe.ValidationError, log.save, ignore_permissions=True)

    def test_complete_with_payment_entry_allowed(self):
        log = self._make_log("PFM-GUARD-2")
        log.status = "Complete"
        log.payment_entry = "not-a-real-pe-but-present"
        log.flags.ignore_links = True  # only asserting the Complete-requires-PE guard here
        log.save(ignore_permissions=True)
        self.assertEqual(log.status, "Complete")

    def test_cannot_delete_manual_review_log(self):
        log = self._make_log("PFM-GUARD-3", status="Manual Review", review_reason="test")
        self.assertRaises(frappe.ValidationError, log.delete)

    def test_cannot_delete_erp_sync_failed_log(self):
        log = self._make_log("PFM-GUARD-4", status="ERP Sync Failed")
        self.assertRaises(frappe.ValidationError, log.delete)

    def test_can_delete_awaiting_payment_log(self):
        log = self._make_log("PFM-GUARD-5", status="Awaiting Payment")
        log.delete()
        self.assertFalse(frappe.db.exists("PayFast Payment Log", log.name))
