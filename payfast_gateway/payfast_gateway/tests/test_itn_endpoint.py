"""Integration tests that drive the REAL ``api.payfast_itn`` whitelisted method.

These require a live Frappe bench (DB, cache, request context). They mock the
HTTP request (``frappe.request`` / ``frappe.form_dict``) and run the enqueued
``process_itn`` inline so the full guest-endpoint path is exercised.
"""
import json
import socket
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import urlencode

import frappe
from frappe.tests.utils import FrappeTestCase

from payfast_gateway.payfast_gateway import api
from payfast_gateway.payfast_gateway.services import itn as itn_service
from payfast_gateway.payfast_gateway.services.signature import generate_signature


class _FakeRequest:
    def __init__(self, body, headers=None, remote_addr="127.0.0.1"):
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = headers or {}
        self.environ = {"REMOTE_ADDR": remote_addr}

    def get_data(self):
        return self._body


def _signed_body(pairs, passphrase="testpass"):
    sig = generate_signature(pairs, passphrase)
    return urlencode(list(pairs) + [("signature", sig)])


def _pairs(m_payment_id, *, payment_status="COMPLETE", pf_payment_id="PF-END-1",
           amount_gross="100.00", amount_fee="0.00", amount_net="100.00"):
    return [
        ("m_payment_id", m_payment_id),
        ("pf_payment_id", pf_payment_id),
        ("payment_status", payment_status),
        ("amount_gross", amount_gross),
        ("amount_fee", amount_fee),
        ("amount_net", amount_net),
        ("merchant_id", "10000100"),
        ("item_name", "Test item"),
    ]


class TestITNEndpoint(FrappeTestCase):
    def setUp(self):
        self.settings = frappe.get_single("PayFast Settings")
        self._orig = {
            "environment": self.settings.environment,
            "sandbox_merchant_id": self.settings.sandbox_merchant_id,
            "sandbox_merchant_key": self.settings.sandbox_merchant_key,
            "sandbox_passphrase": self.settings.sandbox_passphrase,
            "allowed_source_hosts": self.settings.allowed_source_hosts,
            "clearing_account": self.settings.clearing_account,
            "mode_of_payment": self.settings.mode_of_payment,
            "currency": self.settings.currency,
            "enabled": self.settings.enabled,
        }
        self.settings.environment = "Sandbox"
        self.settings.sandbox_merchant_id = "10000100"
        self.settings.sandbox_merchant_key = "46f0cd69b5816e2726fbe6b1"
        self.settings.sandbox_passphrase = "testpass"
        self.settings.allowed_source_hosts = "www.payfast.co.za\nsandbox.payfast.co.za"
        self.settings.currency = "ZAR"
        self.settings.enabled = 1
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
        frappe.local.request_ip = "127.0.0.1"

    def tearDown(self):
        frappe.set_user(self._orig_user)
        self.settings.reload()
        for k, v in self._orig.items():
            if k in ("clearing_account", "mode_of_payment") and not v:
                # reqd on the doctype; can't restore to empty on a fresh site.
                continue
            self.settings.set(k, v)
        try:
            self.settings.save(ignore_permissions=True)
        except frappe.ValidationError:
            self.settings.enabled = 0
            self.settings.save(ignore_permissions=True)
        for n in frappe.get_all("Payment Entry", pluck="name"):
            try:
                pe = frappe.get_doc("Payment Entry", n)
                if pe.reference_no and pe.reference_no.startswith("PF-END"):
                    if pe.docstatus == 1:
                        pe.cancel()
                    frappe.delete_doc("Payment Entry", n, force=True)
            except Exception:
                pass
        for n in frappe.get_all("PayFast Payment Log", pluck="name"):
            frappe.delete_doc("PayFast Payment Log", n, force=True, ignore_on_trash=True)
        if self.si_name and frappe.db.exists("Sales Invoice", self.si_name):
            si = frappe.get_doc("Sales Invoice", self.si_name)
            if si.docstatus == 1:
                si.cancel()
            frappe.delete_doc("Sales Invoice", self.si_name, force=True)

    def _ensure_customer(self):
        name = frappe.get_all("Customer", pluck="name")
        if name:
            return name[0]
        c = frappe.get_doc({"doctype": "Customer", "customer_name": "Endpoint Customer"})
        c.insert(ignore_permissions=True)
        return c.name

    def _make_submitted_sales_invoice(self):
        item = frappe.get_all(
            "Item",
            filters={"is_sales_item": 1, "has_variants": 0, "disabled": 0, "is_fixed_asset": 0},
            pluck="name",
        )
        item_code = item[0] if item else None
        if not item_code:
            it = frappe.get_doc({"doctype": "Item", "item_name": "Endpoint Item", "is_sales_item": 1})
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

    def _make_log(self, m_payment_id):
        log = frappe.get_doc({
            "doctype": "PayFast Payment Log",
            "m_payment_id": m_payment_id,
            "reference_doctype": "Sales Invoice",
            "reference_docname": self.si_name,
            "customer": self.customer,
            "amount": 100.00,
            "currency": "ZAR",
            "status": "Awaiting Payment",
        })
        log.insert(ignore_permissions=True)
        return log

    @contextmanager
    def _request(self, body, headers=None, remote_addr="127.0.0.1"):
        fake = _FakeRequest(body, headers=headers, remote_addr=remote_addr)
        orig = getattr(frappe.local, "request", None)
        orig_form = frappe.local.form_dict
        frappe.local.request = fake
        frappe.local.form_dict = frappe._dict({"cmd": "payfast_gateway.payfast_gateway.api.payfast_itn"})
        try:
            yield
        finally:
            frappe.local.request = orig
            frappe.local.form_dict = orig_form

    def _inline_enqueue(self):
        def runner(method, **kwargs):
            itn_service.process_itn(
                log_name=kwargs.get("log_name"),
                raw_payload_json=kwargs.get("raw_payload_json"),
                raw_body=kwargs.get("raw_body"),
                source_host=kwargs.get("source_host"),
            )
        return runner

    def test_endpoint_pending_then_complete(self):
        log = self._make_log("PFM-END-SEQ")
        pending = _signed_body(_pairs(log.m_payment_id, payment_status="PENDING"))
        complete = _signed_body(_pairs(log.m_payment_id, payment_status="COMPLETE"))

        with patch.object(itn_service, "_server_validate", return_value=(True, {"status": "VALID"})), \
                patch.object(itn_service, "_source_valid", return_value=True), \
                patch.object(itn_service, "_create_payment_entry", return_value="PE-END-SEQ"), \
                patch.object(frappe, "enqueue", self._inline_enqueue()):
            with self._request(pending):
                self.assertEqual(api.payfast_itn(), "OK")
            mid = frappe.get_doc("PayFast Payment Log", log.name)
            self.assertEqual(mid.status, "Awaiting Payment")
            self.assertFalse(mid.processed)

            with self._request(complete):
                self.assertEqual(api.payfast_itn(), "OK")
            done = frappe.get_doc("PayFast Payment Log", log.name)
            self.assertEqual(done.status, "Complete")
            self.assertTrue(done.processed)

        # Fix #1: latest raw payload is valid JSON and the audit array holds both ITNs.
        json.loads(done.raw_payload_json)
        audit = json.loads(done.raw_payload_audit_json)
        self.assertEqual(len(audit), 2)

    def test_endpoint_ip_source_validates(self):
        try:
            infos = socket.getaddrinfo("www.payfast.co.za", None)
        except socket.gaierror:
            self.skipTest("DNS resolution unavailable")
        payfast_ip = next((i[4][0] for i in infos if i[4][0]), None)
        if not payfast_ip:
            self.skipTest("No resolvable PayFast IP")

        log = self._make_log("PFM-END-IP")
        body = _signed_body(_pairs(log.m_payment_id))
        headers = {"X-Forwarded-For": payfast_ip}
        with patch.object(itn_service, "_server_validate", return_value=(True, {"status": "VALID"})), \
                patch.object(itn_service, "_create_payment_entry", return_value="PE-END-IP"), \
                patch.object(frappe, "enqueue", self._inline_enqueue()):
            with self._request(body, headers=headers, remote_addr="10.0.0.5"):
                self.assertEqual(api.payfast_itn(), "OK")
        result = frappe.get_doc("PayFast Payment Log", log.name)
        self.assertTrue(result.source_valid)
        self.assertEqual(result.source_host, payfast_ip)

    def test_endpoint_duplicate_single_payment_entry(self):
        log = self._make_log("PFM-END-DUP")
        body = _signed_body(_pairs(log.m_payment_id, pf_payment_id="PF-END-DUP"))
        with patch.object(itn_service, "_server_validate", return_value=(True, {"status": "VALID"})), \
                patch.object(itn_service, "_source_valid", return_value=True), \
                patch.object(frappe, "enqueue", self._inline_enqueue()):
            with self._request(body):
                self.assertEqual(api.payfast_itn(), "OK")
            with self._request(body):
                self.assertEqual(api.payfast_itn(), "OK")  # retry
        self.assertEqual(frappe.db.count("Payment Entry", {"reference_no": "PF-END-DUP"}), 1)
        result = frappe.get_doc("PayFast Payment Log", log.name)
        self.assertEqual(result.status, "Complete")

    def test_endpoint_itn_while_disabled_routes_to_manual_review(self):
        """An ITN received while the master kill switch is off must never be
        silently dropped in Awaiting Payment/Processing -- it should surface
        in Manual Review so ops don't lose track of it."""
        log = self._make_log("PFM-END-DISABLED")
        body = _signed_body(_pairs(log.m_payment_id, pf_payment_id="PF-END-DISABLED"))
        self.settings.enabled = 0
        self.settings.save(ignore_permissions=True)
        try:
            with patch.object(frappe, "enqueue", self._inline_enqueue()):
                with self._request(body):
                    self.assertEqual(api.payfast_itn(), "OK")
        finally:
            self.settings.enabled = 1
            self.settings.save(ignore_permissions=True)
        result = frappe.get_doc("PayFast Payment Log", log.name)
        self.assertEqual(result.status, "Manual Review")
        self.assertIn("disabled", result.review_reason.lower())
        self.assertFalse(result.processed)

    def test_endpoint_itn_while_disabled_does_not_override_complete(self):
        """A duplicate/retry ITN for an already-Complete payment received
        while disabled must not be clobbered into Manual Review."""
        log = self._make_log("PFM-END-DISABLED-2")
        frappe.db.set_value("PayFast Payment Log", log.name, "status", "Complete")
        body = _signed_body(_pairs(log.m_payment_id, pf_payment_id="PF-END-DISABLED-2"))
        self.settings.enabled = 0
        self.settings.save(ignore_permissions=True)
        try:
            with patch.object(frappe, "enqueue", self._inline_enqueue()):
                with self._request(body):
                    self.assertEqual(api.payfast_itn(), "OK")
        finally:
            self.settings.enabled = 1
            self.settings.save(ignore_permissions=True)
        result = frappe.get_doc("PayFast Payment Log", log.name)
        self.assertEqual(result.status, "Complete")

    def test_endpoint_unknown_mpayment_id(self):
        body = _signed_body(_pairs("PFM-DOES-NOT-EXIST"))
        titles = []
        real_log_error = frappe.log_error

        def _capture(*args, **kwargs):
            titles.append(kwargs.get("title") or (args[1] if len(args) > 1 else (args[0] if args else "")))
            return real_log_error(message=kwargs.get("message", "test"), title=kwargs.get("title", "test"))

        with patch.object(frappe, "enqueue", self._inline_enqueue()), \
                patch.object(frappe, "log_error", side_effect=_capture):
            with self._request(body):
                self.assertEqual(api.payfast_itn(), "OK")

        unknown = frappe.db.get_value(
            "PayFast Payment Log", {"m_payment_id": "PFM-DOES-NOT-EXIST"}, ["name", "status"], as_dict=True
        )
        self.assertIsNotNone(unknown)
        self.assertEqual(unknown.status, "Manual Review")
        self.assertTrue(any("unknown m_payment_id" in str(t) for t in titles))
