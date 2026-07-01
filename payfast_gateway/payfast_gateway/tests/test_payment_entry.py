import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from payfast_gateway.payfast_gateway.services import itn as itn_service
from payfast_gateway.payfast_gateway.services.signature import generate_signature


def _make_log(amount=100.00, m_payment_id="PFM-PE-001"):
    name = frappe.db.get_value("PayFast Payment Log", {"m_payment_id": m_payment_id})
    if name:
        frappe.delete_doc("PayFast Payment Log", name, force=True)
    log = frappe.get_doc({
        "doctype": "PayFast Payment Log",
        "m_payment_id": m_payment_id,
        "reference_doctype": "Sales Invoice",
        "reference_docname": None,
        "amount": amount,
        "currency": "ZAR",
        "status": "Awaiting Payment",
    })
    log.insert(ignore_permissions=True)
    return log


class TestPaymentEntry(FrappeTestCase):
    def setUp(self):
        self.settings = frappe.get_single("PayFast Settings")
        self._orig = {
            "environment": self.settings.environment,
            "sandbox_merchant_id": self.settings.sandbox_merchant_id,
            "sandbox_passphrase": self.settings.sandbox_passphrase,
            "clearing_account": self.settings.clearing_account,
            "mode_of_payment": self.settings.mode_of_payment,
            "currency": self.settings.currency,
        }
        self.settings.environment = "Sandbox"
        self.settings.sandbox_merchant_id = "10000100"
        self.settings.sandbox_passphrase = "testpass"
        self.settings.currency = "ZAR"
        if not self.settings.clearing_account:
            self.settings.clearing_account = frappe.get_all(
                "Account", filters={"account_type": "Cash", "is_group": 0}, pluck="name"
            )[0]
        if not self.settings.mode_of_payment:
            if not frappe.db.exists("Mode of Payment", "PayFast"):
                frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": "PayFast"}).insert(ignore_permissions=True)
            self.settings.mode_of_payment = "PayFast"
        self.settings.save(ignore_permissions=True)

        self._orig_user = frappe.session.user
        frappe.set_user("Administrator")

        self.customer = self._ensure_customer()
        self.si_name = self._make_submitted_sales_invoice()

    def tearDown(self):
        frappe.set_user(self._orig_user)
        for k, v in self._orig.items():
            self.settings.set(k, v)
        self.settings.save(ignore_permissions=True)
        for n in frappe.get_all("Payment Entry", {"reference_no": "PF12345"}, pluck="name"):
            try:
                pe = frappe.get_doc("Payment Entry", n)
                if pe.docstatus == 1:
                    pe.cancel()
                frappe.delete_doc("Payment Entry", n, force=True)
            except Exception:
                pass
        for n in frappe.get_all("PayFast Payment Log", pluck="name"):
            frappe.delete_doc("PayFast Payment Log", n, force=True)
        if self.si_name and frappe.db.exists("Sales Invoice", self.si_name):
            si = frappe.get_doc("Sales Invoice", self.si_name)
            if si.docstatus == 1:
                si.cancel()
            frappe.delete_doc("Sales Invoice", self.si_name, force=True)

    def _ensure_customer(self):
        name = frappe.get_all("Customer", pluck="name")
        if name:
            return name[0]
        c = frappe.get_doc({"doctype": "Customer", "customer_name": "PE Test Customer"})
        c.insert(ignore_permissions=True)
        return c.name

    def _make_submitted_sales_invoice(self):
        item = frappe.get_all("Item", filters={"is_sales_item": 1}, pluck="name")
        item_code = item[0] if item else None
        if not item_code:
            it = frappe.get_doc({"doctype": "Item", "item_name": "PE Test Item", "is_sales_item": 1})
            it.insert(ignore_permissions=True)
            item_code = it.name
        si = frappe.get_doc({
            "doctype": "Sales Invoice",
            "customer": self.customer,
            "company": frappe.defaults.get_user_default("Company"),
            "items": [{"item_code": item_code, "qty": 1, "rate": 100.00}],
        })
        si.insert(ignore_permissions=True)
        si.submit()
        return si.name

    def test_complete_creates_one_payment_entry_and_allocates(self):
        log = _make_log()
        log.reference_docname = self.si_name
        log.customer = self.customer
        log.save(ignore_permissions=True)

        payload = {
            "m_payment_id": log.m_payment_id,
            "pf_payment_id": "PF12345",
            "payment_status": "COMPLETE",
            "amount_gross": "100.00",
            "amount_fee": "0.00",
            "amount_net": "100.00",
            "merchant_id": "10000100",
            "item_name": "Test",
        }
        items = [(k, v) for k, v in payload.items()]
        payload["signature"] = generate_signature(items, "testpass")

        with patch.object(itn_service, "_server_validate", return_value=(True, {"status": "VALID"})):
            itn_service.process_itn(log.name, raw_payload_json=json.dumps(payload), source_host="www.payfast.co.za")

        log.reload()
        self.assertEqual(log.status, "Complete")
        self.assertTrue(log.processed)
        self.assertTrue(log.payment_entry)

        pe = frappe.get_doc("Payment Entry", log.payment_entry)
        self.assertEqual(pe.docstatus, 1)
        self.assertEqual(pe.reference_no, "PF12345")
        self.assertEqual(pe.payment_entry_type, "Receive")
        # Allocated against the Sales Invoice.
        refs = [r for r in pe.references if r.reference_doctype == "Sales Invoice" and r.reference_name == self.si_name]
        self.assertEqual(len(refs), 1)

        # Only one Payment Entry exists for this pf_payment_id.
        count = frappe.db.count("Payment Entry", {"reference_no": "PF12345"})
        self.assertEqual(count, 1)

        # Duplicate ITN must not create a second PE.
        with patch.object(itn_service, "_server_validate", return_value=(True, {"status": "VALID"})):
            itn_service.process_itn(log.name, raw_payload_json=json.dumps(payload), source_host="www.payfast.co.za")
        count = frappe.db.count("Payment Entry", {"reference_no": "PF12345"})
        self.assertEqual(count, 1)

        # Sales Invoice confirmed paid.
        si = frappe.get_doc("Sales Invoice", self.si_name)
        self.assertEqual(si.status, "Paid")

    def test_stale_draft_pe_is_submitted_on_retry_not_falsely_marked_paid(self):
        """If a prior run inserted the Payment Entry but submit() failed, the log
        must NOT be marked Complete with an unsubmitted PE. The retry must submit
        the stale draft and never create a second Payment Entry.
        """
        from frappe.model.document import Document

        log = _make_log()
        log.reference_docname = self.si_name
        log.customer = self.customer
        log.save(ignore_permissions=True)

        payload = {
            "m_payment_id": log.m_payment_id,
            "pf_payment_id": "PF12345",
            "payment_status": "COMPLETE",
            "amount_gross": "100.00",
            "amount_fee": "0.00",
            "amount_net": "100.00",
            "merchant_id": "10000100",
            "item_name": "Test",
        }
        items = [(k, v) for k, v in payload.items()]
        payload["signature"] = generate_signature(items, "testpass")
        raw = json.dumps(payload)

        orig_submit = Document.submit
        state = {"failed_once": False}

        def flaky_submit(doc, *a, **k):
            if doc.doctype == "Payment Entry" and not state["failed_once"]:
                state["failed_once"] = True
                raise frappe.ValidationError("simulated submit failure")
            return orig_submit(doc, *a, **k)

        # First delivery: insert succeeds, submit fails -> ERP Sync Failed + a draft PE.
        with patch.object(itn_service, "_server_validate", return_value=(True, {"status": "VALID"})), \
                patch.object(Document, "submit", flaky_submit):
            itn_service.process_itn(log.name, raw_payload_json=raw, source_host="www.payfast.co.za")

        log.reload()
        self.assertEqual(log.status, "ERP Sync Failed")
        self.assertFalse(log.processed)
        drafts = frappe.get_all("Payment Entry", {"reference_no": "PF12345", "docstatus": 0}, pluck="name")
        self.assertEqual(len(drafts), 1)

        # Retry: the stale draft must be submitted, not treated as done.
        itn_service.retry_erp_sync()

        log.reload()
        self.assertEqual(log.status, "Complete")
        self.assertTrue(log.processed)
        self.assertTrue(log.payment_entry)
        pe = frappe.get_doc("Payment Entry", log.payment_entry)
        self.assertEqual(pe.docstatus, 1)
        # Exactly one Payment Entry for this payment (no duplicate created on retry).
        self.assertEqual(frappe.db.count("Payment Entry", {"reference_no": "PF12345"}), 1)

    def test_itn_only_marks_paid_after_all_checks(self):
        log = _make_log()
        log.reference_docname = self.si_name
        log.customer = self.customer
        log.save(ignore_permissions=True)
        payload = {
            "m_payment_id": log.m_payment_id,
            "pf_payment_id": "PF12345",
            "payment_status": "COMPLETE",
            "amount_gross": "100.00",
            "amount_fee": "0.00",
            "amount_net": "100.00",
            "merchant_id": "10000100",
            "item_name": "Test",
        }
        items = [(k, v) for k, v in payload.items()]
        payload["signature"] = generate_signature(items, "WRONGPASS")

        with patch.object(itn_service, "_server_validate", return_value=(True, {"status": "VALID"})):
            itn_service.process_itn(log.name, raw_payload_json=json.dumps(payload), source_host="www.payfast.co.za")

        log.reload()
        self.assertEqual(log.status, "Manual Review")
        self.assertFalse(log.processed)
        self.assertEqual(frappe.db.count("Payment Entry", {"reference_no": "PF12345"}), 0)

    def test_sales_order_complete_creates_advance_payment_entry(self):
        try:
            from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry  # noqa: F401
        except ImportError:
            self.skipTest("ERPNext not available")

        customer = self.customer
        item = frappe.get_all("Item", filters={"is_sales_item": 1}, pluck="name")[0]
        so = frappe.get_doc({
            "doctype": "Sales Order",
            "customer": customer,
            "company": frappe.defaults.get_user_default("Company"),
            "items": [{"item_code": item, "qty": 1, "rate": 100.00}],
        })
        so.insert(ignore_permissions=True)
        so.submit()

        log = _make_log(m_payment_id="PFM-SO-001")
        log.reference_doctype = "Sales Order"
        log.reference_docname = so.name
        log.customer = customer
        log.save(ignore_permissions=True)

        payload = {
            "m_payment_id": log.m_payment_id,
            "pf_payment_id": "PF-SO-001",
            "payment_status": "COMPLETE",
            "amount_gross": "100.00",
            "amount_fee": "0.00",
            "amount_net": "100.00",
            "merchant_id": "10000100",
            "item_name": "Test",
        }
        items = [(k, v) for k, v in payload.items()]
        payload["signature"] = generate_signature(items, "testpass")

        try:
            with patch.object(itn_service, "_server_validate", return_value=(True, {"status": "VALID"})):
                itn_service.process_itn(
                    log.name,
                    raw_payload_json=json.dumps(payload),
                    raw_body="m_payment_id=x",
                    source_host="www.payfast.co.za",
                )
        finally:
            log.reload()
            if log.payment_entry and frappe.db.exists("Payment Entry", log.payment_entry):
                pe = frappe.get_doc("Payment Entry", log.payment_entry)
                if pe.docstatus == 1:
                    pe.cancel()
                frappe.delete_doc("Payment Entry", log.payment_entry, force=True)
            if so.docstatus == 1:
                so.cancel()
            frappe.delete_doc("Sales Order", so.name, force=True)

        self.assertEqual(log.status, "Complete")
        self.assertTrue(log.processed)
        self.assertTrue(log.payment_entry)
