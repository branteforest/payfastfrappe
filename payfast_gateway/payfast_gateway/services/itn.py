import json
import socket
import time

import frappe
import requests
from frappe import _
from frappe.utils import cint, flt, now_datetime

from payfast_gateway.payfast_gateway.doctype.payfast_settings.payfast_settings import (
    get_allowed_source_hosts,
    get_credentials,
    get_settings,
    is_debug_logging,
)
from payfast_gateway.payfast_gateway.services.signature import (
    normalize_itn_fields,
    verify_signature,
)

# Tolerance for amount comparison (spec: within 0.01).
AMOUNT_TOLERANCE = 0.01

# Standard published PayFast notify hosts. ITNs always originate from one of
# these; they are resolved to IPs and merged with any operator-configured hosts.
DEFAULT_NOTIFY_HOSTS = (
    "www.payfast.co.za",
    "w1w.payfast.co.za",
    "w2w.payfast.co.za",
    "sandbox.payfast.co.za",
)

# Max attempts to sync a verified-COMPLETE payment into ERP before giving up
# and escalating to manual review. The raw payload is never discarded.
MAX_ERP_RETRIES = 10

_DNS_CACHE = {}
_DNS_TTL = 300  # seconds


def process_itn(log_name, raw_payload_json=None, source_host=None):
    """Background ITN processor enforcing the mandatory 4 checks.

    Idempotent: claims the log under a short row lock, performs the network
    validate WITHOUT holding the lock, then re-locks to finalize. Guards against
    duplicate Payment Entry creation via reference_no lookup.
    """
    if not log_name:
        frappe.log_error(title="PayFast ITN: missing log_name", message="No log_name supplied")
        return

    try:
        log = frappe.get_doc("PayFast Payment Log", log_name)
    except frappe.DoesNotExistError:
        frappe.log_error(title="PayFast ITN: log missing", message=f"log_name={log_name}")
        return

    # Phase 1: claim under a short lock. The lock is released before any
    # outbound HTTPS validate call so it is never held across network I/O.
    if not _claim_for_processing(log_name):
        return
    log.reload()

    if not raw_payload_json:
        raw_payload_json = log.raw_payload_json

    try:
        payload = json.loads(raw_payload_json or "{}")
    except (ValueError, TypeError):
        _move_to_review(log, "raw_payload_json could not be parsed.")
        return
    if not isinstance(payload, dict):
        _move_to_review(log, "raw_payload_json is not a JSON object.")
        return

    # Detect conflicting/duplicate ITN.
    if log.pf_payment_id and payload.get("pf_payment_id") and log.pf_payment_id != payload.get("pf_payment_id"):
        _move_to_review(
            log,
            f"Conflicting pf_payment_id: stored={log.pf_payment_id} received={payload.get('pf_payment_id')}",
        )
        return

    creds = get_credentials()
    settings = get_settings()

    # Check 1: signature.
    sig_items = normalize_itn_fields(payload)
    received_sig = payload.get("signature") or ""
    signature_valid = verify_signature(sig_items, creds.get("passphrase") or "", received_sig)

    # Check 2: source host/IP (real DNS-backed membership check).
    source_valid = _source_valid(source_host or log.source_host, get_allowed_source_hosts())

    # Check 3: amount match.
    expected_amount = flt(log.amount)
    amount_gross = flt(payload.get("amount_gross") or 0)
    amount_fee = flt(payload.get("amount_fee") or 0)
    amount_net = flt(payload.get("amount_net") or 0)
    amount_valid = (
        abs(amount_gross - expected_amount) <= AMOUNT_TOLERANCE
        and abs((amount_gross - amount_fee) - amount_net) <= AMOUNT_TOLERANCE
    )

    # Check 4: server-to-server validate POST returns VALID (network I/O, no lock held).
    server_valid, validate_response = _server_validate(payload, creds)

    _debug(
        settings,
        f"ITN {log_name} checks sig={signature_valid} source={source_valid} "
        f"amount={amount_valid} server={server_valid} status={payload.get('payment_status')}",
    )

    # Phase 2: finalize under a fresh lock.
    frappe.db.sql("SELECT name FROM `tabPayFast Payment Log` WHERE name = %s FOR UPDATE", (log_name,))
    log.reload()
    if log.processed:
        return

    log.signature_valid = 1 if signature_valid else 0
    log.source_valid = 1 if source_valid else 0
    log.amount_valid = 1 if amount_valid else 0
    log.server_valid = 1 if server_valid else 0
    log.amount_gross = amount_gross
    log.amount_fee = amount_fee
    log.amount_net = amount_net
    log.validate_response = json.dumps(validate_response, ensure_ascii=False) if validate_response else ""
    log.payment_status = payload.get("payment_status") or log.payment_status
    if payload.get("pf_payment_id"):
        log.pf_payment_id = payload.get("pf_payment_id")
    if payload.get("payment_gateway"):
        log.payment_gateway = payload.get("payment_gateway")
    log.save(ignore_permissions=True)

    all_checks_ok = signature_valid and source_valid and amount_valid and server_valid

    if not all_checks_ok:
        reasons = []
        if not signature_valid:
            reasons.append("signature mismatch")
        if not source_valid:
            reasons.append("source host/IP not allowed")
        if not amount_valid:
            reasons.append("amount mismatch")
        if not server_valid:
            reasons.append("server validate did not return VALID")
        _move_to_review(log, "; ".join(reasons))
        return

    payment_status = (payload.get("payment_status") or "").upper()
    if payment_status == "COMPLETE":
        _complete_payment(log, payload)
    elif payment_status in ("FAILED", "CANCELLED"):
        log.status = payment_status.title()
        log.processed = 1
        log.save(ignore_permissions=True)
    elif payment_status == "PENDING":
        log.status = "Awaiting Payment"
        log.save(ignore_permissions=True)
    else:
        _move_to_review(log, f"unhandled payment_status={payment_status}")


def _claim_for_processing(log_name):
    """Row-lock the log to claim it for processing. Returns False when the log
    is already processed. Marks the log 'Processing' and releases the lock
    (commit) so the subsequent network validate does not hold a DB lock.
    """
    frappe.db.sql("SELECT name FROM `tabPayFast Payment Log` WHERE name = %s FOR UPDATE", (log_name,))
    if frappe.db.get_value("PayFast Payment Log", log_name, "processed"):
        return False
    frappe.db.set_value(
        "PayFast Payment Log", log_name, "status", "Processing", update_modified=False
    )
    # Release the claim lock before outbound network I/O. In tests the shared
    # transaction is rolled back at teardown, so committing is skipped there.
    if not frappe.flags.in_test:
        frappe.db.commit()
    return True


def _debug(settings, message):
    if is_debug_logging(settings):
        frappe.logger("payfast_gateway").info(message)


def _resolve_host_ips(host):
    host = (host or "").strip().lower()
    if not host:
        return set()
    now = time.time()
    cached = _DNS_CACHE.get(host)
    if cached and cached[0] > now:
        return cached[1]
    ips = set()
    try:
        for info in socket.getaddrinfo(host, None):
            ip = info[4][0]
            if ip:
                ips.add(ip.lower())
    except (socket.gaierror, OSError):
        ips = set()
    _DNS_CACHE[host] = (now + _DNS_TTL, ips)
    return ips


def _is_ip(value):
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, value)
            return True
        except (OSError, ValueError):
            continue
    return False


def get_allowed_source_ips(allowed_hosts):
    """Union of literal IPs among the allowed entries, the resolved IPs of
    hostname entries, and the resolved IPs of the default PayFast notify hosts.
    """
    hosts = list(allowed_hosts or []) + list(DEFAULT_NOTIFY_HOSTS)
    ips = set()
    for h in hosts:
        h = (h or "").strip()
        if not h:
            continue
        if _is_ip(h):
            ips.add(h.lower())
        else:
            ips |= _resolve_host_ips(h)
    return ips


def _source_valid(source_host, allowed_hosts):
    """Real source validation.

    In production ``source_host`` is the determined client IP; it passes only
    when that IP belongs to a resolved allowed/PayFast notify host (or is a
    literal allowed IP). Hostname candidates are accepted only when they exactly
    match a configured/default host name (this branch never triggers for
    untrusted traffic because the receiver only stores IP-formatted candidates).
    """
    if not source_host:
        return False
    candidates = [c.strip().lower() for c in str(source_host).split(",") if c.strip()]
    if not candidates:
        return False
    allowed_names = {h.strip().lower() for h in (allowed_hosts or []) if h and h.strip()}
    allowed_names |= {h.lower() for h in DEFAULT_NOTIFY_HOSTS}
    allowed_ips = get_allowed_source_ips(allowed_hosts)
    for c in candidates:
        if _is_ip(c):
            if c in allowed_ips:
                return True
        elif c in allowed_names:
            return True
    return False


def _server_validate(payload, creds):
    """Server-to-server POST to PayFast validate URL. Must return 'VALID'."""
    validate_url = creds.get("validate_url")
    if not validate_url:
        return False, {"error": "no validate_url configured"}
    try:
        resp = requests.post(validate_url, data=payload, timeout=30)
    except Exception as exc:  # noqa: BLE001 - network errors must be caught
        return False, {"error": f"validate request failed: {exc}"}
    text = (resp.text or "").strip().upper()
    try:
        parsed = json.loads(resp.text)
        text = (parsed.get("status") or resp.text or "").strip().upper()
    except (ValueError, TypeError):
        pass
    return text == "VALID", {"http_status": resp.status_code, "body": resp.text}


def _complete_payment(log, payload):
    # v1 accounting path: only a submitted Sales Invoice yields an automatic,
    # fully-allocated Payment Entry. Anything else is escalated rather than
    # producing a submitted-but-unallocated (or unconfirmed) Payment Entry.
    if log.reference_doctype != "Sales Invoice":
        _move_to_review(
            log,
            f"reference_doctype '{log.reference_doctype}' is not supported for automatic "
            "Payment Entry in v1 (Sales Invoice only).",
        )
        return
    if not log.reference_docname:
        _move_to_review(log, "verified COMPLETE but no reference document to allocate against.")
        return

    ref_docstatus = frappe.db.get_value(log.reference_doctype, log.reference_docname, "docstatus")
    if ref_docstatus != 1:
        _move_to_review(
            log,
            f"reference {log.reference_doctype} {log.reference_docname} is not submitted "
            f"(docstatus={ref_docstatus}); refusing to create an unallocated Payment Entry.",
        )
        return

    # Guard against duplicate Payment Entry creation.
    existing_pe = None
    if log.pf_payment_id:
        existing_pe = frappe.db.get_value(
            "Payment Entry", {"reference_no": log.pf_payment_id, "docstatus": ["<", 2]}
        )

    try:
        pe_name = existing_pe or _create_payment_entry(log)
        _mark_complete(log, pe_name)
    except Exception as exc:  # noqa: BLE001 - verified payment: retry, do not lose it
        _flag_erp_sync_failed(log, exc)
        return

    # Reference confirmation is best-effort; the Payment Entry allocation is the
    # source of truth. A failure here must not undo a completed payment.
    try:
        _confirm_reference(log)
    except Exception as exc:  # noqa: BLE001
        frappe.log_error(
            title=f"PayFast ITN: reference confirm failed {log.name}", message=str(exc)
        )


def _mark_complete(log, pe_name):
    log.payment_entry = pe_name
    log.status = "Complete"
    log.processed = 1
    if not log.paid_at:
        log.paid_at = now_datetime()
    log.save(ignore_permissions=True)


def _flag_erp_sync_failed(log, exc):
    """Verified-COMPLETE payment whose ERP update failed. Keep the raw payload,
    increment retry_count, and queue for scheduler retry instead of routing to
    manual review (until attempts are exhausted).
    """
    log.retry_count = cint(log.retry_count) + 1
    reason = f"payment verified COMPLETE but ERP update failed (attempt {log.retry_count}): {exc}"
    log.error_log = (log.error_log or "") + f"\n[{now_datetime()}] {reason}"
    if log.retry_count >= MAX_ERP_RETRIES:
        log.status = "Manual Review"
        log.review_reason = f"ERP sync failed after {log.retry_count} attempts: {exc}"
    else:
        log.status = "ERP Sync Failed"
    log.save(ignore_permissions=True)
    frappe.log_error(title=f"PayFast ITN ERP sync failed: {log.name}", message=reason)


def retry_erp_sync():
    """Scheduler job: retry ERP sync for verified payments whose Payment Entry
    creation previously failed. Never discards the stored raw payload.
    """
    names = frappe.get_all(
        "PayFast Payment Log",
        filters={"status": "ERP Sync Failed", "processed": 0},
        pluck="name",
    )
    for name in names:
        try:
            log = frappe.get_doc("PayFast Payment Log", name)
            payload = json.loads(log.raw_payload_json or "{}")
            _complete_payment(log, payload if isinstance(payload, dict) else {})
            if not frappe.flags.in_test:
                frappe.db.commit()
        except Exception as exc:  # noqa: BLE001
            if not frappe.flags.in_test:
                frappe.db.rollback()
            frappe.log_error(title=f"PayFast ITN retry failed: {name}", message=str(exc))


def _create_payment_entry(log):
    settings = get_settings()
    reference = frappe.get_doc(log.reference_doctype, log.reference_docname)

    party = log.customer or getattr(reference, "customer", None)
    party_type = "Customer"
    if not party:
        frappe.throw(_("Cannot create Payment Entry without a customer."))

    pe = frappe.new_doc("Payment Entry")
    pe.payment_entry_type = "Receive"
    pe.party_type = party_type
    pe.party = party
    pe.company = getattr(reference, "company", None) or frappe.defaults.get_user_default("Company")
    pe.paid_amount = flt(log.amount)
    pe.received_amount = flt(log.amount)
    pe.mode_of_payment = settings.mode_of_payment
    pe.paid_to = settings.clearing_account
    pe.reference_no = log.pf_payment_id or log.m_payment_id
    pe.reference_date = now_datetime().date()
    pe.remark = f"PayFast payment {log.pf_payment_id or log.m_payment_id}"

    # Allocate against the Sales Invoice (guaranteed submitted by _complete_payment).
    if log.reference_doctype == "Sales Invoice" and reference.docstatus == 1:
        pe.append(
            "references",
            {
                "reference_doctype": "Sales Invoice",
                "reference_name": log.reference_docname,
                "total_amount": flt(reference.grand_total or reference.outstanding_amount),
                "outstanding_amount": flt(reference.outstanding_amount),
                "allocated_amount": flt(log.amount),
            },
        )

    pe.insert(ignore_permissions=True)
    pe.submit()
    return pe.name


def _confirm_reference(log):
    if log.reference_doctype != "Sales Invoice":
        return
    si = frappe.get_doc("Sales Invoice", log.reference_docname)
    si.db_set("status", "Paid" if flt(si.outstanding_amount) <= 0.01 else "Partly Paid")
    if si.meta.has_field("payfast_status"):
        si.db_set("payfast_status", "Complete")
    if si.meta.has_field("payfast_payment_log"):
        si.db_set("payfast_payment_log", log.name)
    si.notify_update()


def _move_to_review(log, reason):
    log.status = "Manual Review"
    log.review_reason = reason
    log.error_log = (log.error_log or "") + f"\n[{now_datetime()}] {reason}"
    log.save(ignore_permissions=True)
    frappe.log_error(
        title=f"PayFast ITN manual review: {log.name}",
        message=reason,
    )
