import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from payfast_gateway.payfast_gateway.services import itn as itn_service
from payfast_gateway.payfast_gateway.services.signature import generate_signature


def _itn_payload(log, passphrase="", *, payment_status="COMPLETE",
                 amount_gross="100.00", amount_fee="0.00", amount_net="100.00",
                 pf_payment_id="PF12345", extra=None):
    base = {
        "m_payment_id": log.m_payment_id,
        "pf_payment_id": pf_payment_id,
        "payment_status": payment_status,
        "amount_gross": amount_gross,
        "amount_fee": amount_fee,
        "amount_net": amount_net,
        "merchant_id": "10000100",
        "item_name": "Test item",
    }
    if extra:
        base.update(extra)
    items = [(k, v) for k, v in base.items() if k != "signature"]
    base["signature"] = generate_signature(items, passphrase)
    return base


class TestITN(FrappeTestCase):
    def setUp(self):
        self.settings = frappe.get_single("PayFast Settings")
        self._orig = {
            "environment": self.settings.environment,
            "sandbox_merchant_id": self.settings.sandbox_merchant_id,
            "sandbox_passphrase": self.settings.sandbox_passphrase,
            "allowed_source_hosts": self.settings.allowed_source_hosts,
            "clearing_account": self.settings.clearing_account,
            "mode_of_payment": self.settings.mode_of_payment,
            "currency": self.settings.currency,
        }
        self.settings.environment = "Sandbox"
        self.settings.sandbox_merchant_id = "10000100"
        self.settings.sandbox_passphrase = "testpass"
        self.settings.allowed_source_hosts = "www.payfast.co.za\nsandbox.payfast.co.za"
        self.settings.currency = "ZAR"
        if not self.settings.mode_of_payment:
            if not frappe.db.exists("Mode of Payment", "PayFast"):
                frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": "PayFast"}).insert(
                    ignore_permissions=True
                )
            self.settings.mode_of_payment = "PayFast"
        if not self.settings.clearing_account:
            self.settings.clearing_account = frappe.get_all(
                "Account", filters={"account_type": "Cash", "is_group": 0}, pluck="name"
            )[0]
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
        c = frappe.get_doc({"doctype": "Customer", "customer_name": "ITN Test Customer"})
        c.insert(ignore_permissions=True)
        return c.name

    def _make_submitted_sales_invoice(self):
        item = frappe.get_all("Item", filters={"is_sales_item": 1}, pluck="name")
        item_code = item[0] if item else None
        if not item_code:
            it = frappe.get_doc({"doctype": "Item", "item_name": "ITN Test Item", "is_sales_item": 1})
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

    def _make_log(self, amount=100.00, m_payment_id="PFM-TEST-001", status="Awaiting Payment"):
        if frappe.db.exists("PayFast Payment Log", {"m_payment_id": m_payment_id}):
            name = frappe.db.get_value("PayFast Payment Log", {"m_payment_id": m_payment_id})
            frappe.delete_doc("PayFast Payment Log", name, force=True)
        log = frappe.get_doc({
            "doctype": "PayFast Payment Log",
            "m_payment_id": m_payment_id,
            "reference_doctype": "Sales Invoice",
            "reference_docname": self.si_name,
            "customer": self.customer,
            "amount": amount,
            "currency": "ZAR",
            "status": status,
        })
        log.insert(ignore_permissions=True)
        return log

    def _process(self, log, payload, source_host="www.payfast.co.za"):
        itn_service.process_itn(
            log.name,
            raw_payload_json=json.dumps(payload),
            source_host=payload.get("source_host") or source_host,
        )
        return frappe.get_doc("PayFast Payment Log", log.name)

    def test_all_checks_pass_complete(self):
        with patch.object(itn_service, "_server_validate", return_value=(True, {"status": "VALID"})):
            with patch.object(itn_service, "_create_payment_entry", return_value="PE-TEST-1"):
                log = self._make_log()
                payload = _itn_payload(log, passphrase="testpass")
                result = self._process(log, payload)
                self.assertEqual(result.status, "Complete")
                self.assertTrue(result.processed)
                self.assertTrue(result.signature_valid)
                self.assertTrue(result.source_valid)
                self.assertTrue(result.amount_valid)
                self.assertTrue(result.server_valid)
                self.assertEqual(result.payment_entry, "PE-TEST-1")
                self.assertTrue(result.paid_at)
                # amount_* persisted from the ITN (fix #9).
                self.assertEqual(float(result.amount_gross), 100.00)
                self.assertEqual(float(result.amount_net), 100.00)

    def test_signature_invalid_goes_to_review(self):
        with patch.object(itn_service, "_server_validate", return_value=(True, {})):
            log = self._make_log()
            payload = _itn_payload(log, passphrase="testpass")
            payload["signature"] = "deadbeef" * 4
            result = self._process(log, payload)
            self.assertEqual(result.status, "Manual Review")
            self.assertFalse(result.processed)
            self.assertFalse(result.signature_valid)
            self.assertIn("signature", result.review_reason)

    def test_source_invalid_goes_to_review(self):
        with patch.object(itn_service, "_server_validate", return_value=(True, {})):
            log = self._make_log()
            payload = _itn_payload(log, passphrase="testpass")
            itn_service.process_itn(
                log.name, json.dumps(payload), source_host="evil.example.com"
            )
            result = frappe.get_doc("PayFast Payment Log", log.name)
            self.assertEqual(result.status, "Manual Review")
            self.assertFalse(result.source_valid)
            self.assertFalse(result.processed)

    def test_amount_mismatch_goes_to_review(self):
        with patch.object(itn_service, "_server_validate", return_value=(True, {})):
            log = self._make_log(amount=100.00)
            payload = _itn_payload(log, passphrase="testpass", amount_gross="99.00", amount_net="99.00")
            result = self._process(log, payload)
            self.assertEqual(result.status, "Manual Review")
            self.assertFalse(result.amount_valid)

    def test_server_validate_not_valid_goes_to_review(self):
        with patch.object(itn_service, "_server_validate", return_value=(False, {"body": "INVALID"})):
            log = self._make_log()
            payload = _itn_payload(log, passphrase="testpass")
            result = self._process(log, payload)
            self.assertEqual(result.status, "Manual Review")
            self.assertFalse(result.server_valid)

    def test_failed_status(self):
        with patch.object(itn_service, "_server_validate", return_value=(True, {})):
            log = self._make_log()
            payload = _itn_payload(log, passphrase="testpass", payment_status="FAILED")
            result = self._process(log, payload)
            self.assertEqual(result.status, "Failed")
            self.assertTrue(result.processed)

    def test_cancelled_status(self):
        with patch.object(itn_service, "_server_validate", return_value=(True, {})):
            log = self._make_log()
            payload = _itn_payload(log, passphrase="testpass", payment_status="CANCELLED")
            result = self._process(log, payload)
            self.assertEqual(result.status, "Cancelled")
            self.assertTrue(result.processed)

    def test_duplicate_itn_does_not_double_process(self):
        with patch.object(itn_service, "_server_validate", return_value=(True, {})):
            with patch.object(itn_service, "_create_payment_entry", return_value="PE-TEST-1") as mock_pe:
                log = self._make_log()
                payload = _itn_payload(log, passphrase="testpass")
                self._process(log, payload)
                self._process(log, payload)  # duplicate
                self.assertEqual(mock_pe.call_count, 1)

    def test_conflicting_pf_payment_id_goes_to_review(self):
        log = self._make_log()
        log.pf_payment_id = "PF-A"
        log.save(ignore_permissions=True)
        payload = _itn_payload(log, passphrase="testpass", pf_payment_id="PF-B")
        with patch.object(itn_service, "_server_validate", return_value=(True, {})):
            result = self._process(log, payload)
            self.assertEqual(result.status, "Manual Review")
            self.assertIn("Conflicting pf_payment_id", result.review_reason)

    def test_pending_then_complete_sequence(self):
        """PENDING then COMPLETE must both parse (fix #1) and finish Complete."""
        with patch.object(itn_service, "_server_validate", return_value=(True, {"status": "VALID"})):
            with patch.object(itn_service, "_create_payment_entry", return_value="PE-SEQ-1"):
                log = self._make_log()
                pending = _itn_payload(log, passphrase="testpass", payment_status="PENDING")
                self._process(log, pending)
                mid = frappe.get_doc("PayFast Payment Log", log.name)
                self.assertEqual(mid.status, "Awaiting Payment")
                self.assertFalse(mid.processed)

                complete = _itn_payload(log, passphrase="testpass", payment_status="COMPLETE")
                result = self._process(log, complete)
                self.assertEqual(result.status, "Complete")
                self.assertTrue(result.processed)

    def test_unsubmitted_reference_does_not_create_unallocated_pe(self):
        """A verified COMPLETE against a non-submitted reference must not yield a
        submitted-but-unallocated Payment Entry (fix #4)."""
        draft = frappe.get_doc({
            "doctype": "Sales Invoice",
            "customer": self.customer,
            "company": frappe.defaults.get_user_default("Company"),
            "items": [{"item_code": frappe.get_all("Item", {"is_sales_item": 1}, pluck="name")[0],
                       "qty": 1, "rate": 100.00}],
        })
        draft.insert(ignore_permissions=True)  # left in draft (docstatus 0)
        log = self._make_log(m_payment_id="PFM-DRAFT-1")
        log.reference_docname = draft.name
        log.save(ignore_permissions=True)
        payload = _itn_payload(log, passphrase="testpass")
        with patch.object(itn_service, "_server_validate", return_value=(True, {"status": "VALID"})):
            with patch.object(itn_service, "_create_payment_entry") as mock_pe:
                result = self._process(log, payload)
                self.assertEqual(result.status, "Manual Review")
                self.assertFalse(result.processed)
                mock_pe.assert_not_called()
        frappe.delete_doc("Sales Invoice", draft.name, force=True)

    def test_erp_sync_failure_flags_retry_state(self):
        """Verified COMPLETE but PE creation fails -> ERP Sync Failed + retry_count,
        raw payload retained (fix #12)."""
        with patch.object(itn_service, "_server_validate", return_value=(True, {"status": "VALID"})):
            with patch.object(itn_service, "_create_payment_entry", side_effect=Exception("boom")):
                log = self._make_log(m_payment_id="PFM-ERP-1")
                payload = _itn_payload(log, passphrase="testpass")
                result = self._process(log, payload)
                self.assertEqual(result.status, "ERP Sync Failed")
                self.assertFalse(result.processed)
                self.assertEqual(result.retry_count, 1)
                self.assertTrue(result.raw_payload_json)

        # Scheduler retry succeeds once PE creation works.
        with patch.object(itn_service, "_server_validate", return_value=(True, {"status": "VALID"})):
            with patch.object(itn_service, "_create_payment_entry", return_value="PE-RETRY-1"):
                itn_service.retry_erp_sync()
                result = frappe.get_doc("PayFast Payment Log", log.name)
                self.assertEqual(result.status, "Complete")
                self.assertTrue(result.processed)
                self.assertEqual(result.payment_entry, "PE-RETRY-1")

    def test_ip_based_source_resolves(self):
        """A client IP resolved from a PayFast notify host passes source validation;
        a spoofed IP fails (fix #2)."""
        import socket
        try:
            infos = socket.getaddrinfo("www.payfast.co.za", None)
        except socket.gaierror:
            self.skipTest("DNS resolution unavailable in this environment")
        payfast_ip = next((i[4][0] for i in infos if i[4][0]), None)
        if not payfast_ip:
            self.skipTest("No resolvable PayFast IP")
        allowed = ["www.payfast.co.za", "sandbox.payfast.co.za"]
        self.assertTrue(itn_service._source_valid(payfast_ip, allowed))
        self.assertFalse(itn_service._source_valid("203.0.113.7", allowed))
