import ipaddress
import json
from urllib.parse import parse_qsl

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, now_datetime

from payfast_gateway.payfast_gateway.doctype.payfast_settings.payfast_settings import (
    get_cancel_url,
    get_credentials,
    get_notify_url,
    get_return_url,
    get_settings,
    is_enabled,
)
from payfast_gateway.payfast_gateway.services.itn import _update_reference_payfast_status
from payfast_gateway.payfast_gateway.services.signature import (
    REDIRECT_FIELD_ORDER,
    generate_signature,
)

try:
    from frappe.rate_limiter import rate_limit
except Exception:  # noqa: BLE001 - keep the module importable if the path differs
    def rate_limit(*_args, **_kwargs):
        def _decorator(fn):
            return fn
        return _decorator

PAYFAST_AGENT_ROLE = "PayFast Agent"
ALLOWED_REFERENCE_DOCTYPES = ("Sales Invoice", "Sales Order")

# Generous per-IP limit: genuine PayFast ITN traffic never approaches this, but
# it sheds abusive floods. Pair with an nginx limit_req zone (see README).
ITN_RATE_LIMIT = 120
ITN_RATE_WINDOW = 60

# Internal log statuses that must not be regenerated while ITN may be in flight.
NON_REGENERABLE_STATUSES = ("Complete", "ERP Sync Failed", "Processing")


def _require_agent():
    if frappe.session.user == "Administrator":
        return
    if PAYFAST_AGENT_ROLE not in frappe.get_roles():
        frappe.throw(_("Not permitted. Role {0} is required.").format(PAYFAST_AGENT_ROLE), frappe.PermissionError)


def _get_reference_doc(doctype, docname):
    return frappe.get_doc(doctype, docname)


def _validate_amount(amount):
    amount = flt(amount)
    if amount <= 0:
        frappe.throw(_("Amount must be greater than 0."))
    return amount


def _validate_amount_against_reference(reference_doctype, ref, amount):
    if reference_doctype == "Sales Invoice":
        outstanding = flt(getattr(ref, "outstanding_amount", 0))
        if amount > outstanding + 0.01:
            frappe.throw(
                _("Amount {0} cannot exceed outstanding amount {1}.").format(amount, outstanding)
            )


def _sync_reference_payfast_status(reference_doctype, reference_docname, payfast_status, payment_log=None):
    _update_reference_payfast_status(
        reference_doctype, reference_docname, payfast_status, payment_log=payment_log
    )


def _normalize_payment_status(log_status, expires_at=None):
    """Map internal log status to agent-facing lowercase lifecycle."""
    if log_status == "Complete":
        return "paid"
    if log_status in ("Failed", "Cancelled"):
        if log_status == "Cancelled" and expires_at and expires_at < now_datetime():
            return "expired"
        return "failed"
    if log_status in ("Manual Review", "ERP Sync Failed"):
        return "manual_review"
    return "awaiting_payment"


def _normalize_booking_status(log_status, expires_at=None):
    normalized = _normalize_payment_status(log_status, expires_at)
    if normalized == "paid":
        return "confirmed"
    if normalized in ("failed", "expired"):
        return "cancelled"
    return "awaiting_payment"


def _resolve_m_payment_id(m_payment_id=None, payment_log=None):
    if payment_log and not m_payment_id:
        m_payment_id = frappe.db.get_value("PayFast Payment Log", payment_log, "m_payment_id")
    if not m_payment_id:
        frappe.throw(_("m_payment_id or payment_log is required."))
    return m_payment_id


def _build_redirect_payload(log, creds, customer_doc=None):
    """Build the ordered PayFast redirect fields + signature.

    merchant_key IS transmitted (to PayFast) but never logged elsewhere.
    passphrase is never transmitted and never logged (only used in signature).
    """
    settings = get_settings()
    item_name = log.item_name or ""
    if item_name and settings.item_name_prefix:
        item_name = f"{settings.item_name_prefix}{item_name}"
    fields = {
        "merchant_id": creds["merchant_id"],
        "merchant_key": creds["merchant_key"],
        "return_url": get_return_url(settings),
        "cancel_url": get_cancel_url(settings),
        "notify_url": get_notify_url(settings),
        "name_first": log.name_first or "",
        "name_last": log.name_last or "",
        "email_address": log.email_address or "",
        "cell_number": log.cell_number or "",
        "m_payment_id": log.m_payment_id,
        "amount": f"{log.amount:.2f}",
        "item_name": item_name,
        "item_description": log.item_description or "",
        "custom_str1": log.reference_doctype + "|" + log.reference_docname,
    }
    ordered = [(name, fields.get(name, "")) for name in REDIRECT_FIELD_ORDER if name in fields]
    if settings.environment == "Sandbox":
        ordered.append(("testing", "true"))
    signature = generate_signature(ordered, creds.get("passphrase") or "")
    payload_for_form = {name: value for name, value in ordered}
    payload_for_form["signature"] = signature
    return ordered, payload_for_form, signature


def _create_log(reference_doctype, reference_docname, amount, currency, customer=None,
                item_name=None, item_description=None, email_address=None,
                name_first=None, name_last=None, cell_number=None,
                customer_mobile=None, whatsapp_conversation_id=None):
    creds = get_credentials()
    settings = get_settings()
    expires_at = add_to_date(now_datetime(), minutes=cint(settings.link_expiry_minutes, 60))
    log = frappe.get_doc({
        "doctype": "PayFast Payment Log",
        "reference_doctype": reference_doctype,
        "reference_docname": reference_docname,
        "customer": customer,
        "amount": amount,
        "currency": currency or settings.currency or "ZAR",
        "item_name": item_name,
        "item_description": item_description,
        "email_address": email_address,
        "name_first": name_first,
        "name_last": name_last,
        "cell_number": cell_number,
        "customer_mobile": customer_mobile,
        "whatsapp_conversation_id": whatsapp_conversation_id,
        "status": "Awaiting Payment",
        "process_url": creds["process_url"],
        "expires_at": expires_at,
        "created_by_link": frappe.session.user,
    })
    log.insert(ignore_permissions=True)
    return log


@frappe.whitelist(methods=["POST"])
def create_payment_link(reference_doctype="Sales Invoice", reference_name=None,
                        reference_docname=None, amount=None, currency="ZAR",
                        customer=None, item_name=None, item_description=None,
                        email_address=None, name_first=None, name_last=None,
                        cell_number=None, whatsapp_number=None, conversation_id=None):
    """Create (or reuse) a PayFast payment link for a submitted reference document."""
    _require_agent()
    settings = get_settings()
    if not is_enabled(settings):
        frappe.throw(_("PayFast integration is disabled."))

    # `reference_name` is the spec §7 field; `reference_docname` is accepted as
    # the internal alias for backward compatibility.
    reference_name = reference_name or reference_docname
    if not reference_name:
        frappe.throw(_("reference_name is required."))
    if reference_doctype not in ALLOWED_REFERENCE_DOCTYPES:
        frappe.throw(_("reference_doctype {0} is not supported.").format(reference_doctype))
    amount = _validate_amount(amount)

    if currency and currency != (settings.currency or "ZAR"):
        frappe.throw(_("Only {0} is supported.").format(settings.currency or "ZAR"))

    ref = _get_reference_doc(reference_doctype, reference_name)
    if cint(getattr(ref, "docstatus", 0)) != 1:
        frappe.throw(
            _("Reference {0} {1} must be submitted before requesting payment.").format(
                reference_doctype, reference_name
            )
        )
    if customer and hasattr(ref, "customer") and ref.customer and ref.customer != customer:
        frappe.throw(_("Customer mismatch with reference document."))

    _validate_amount_against_reference(reference_doctype, ref, amount)

    existing = frappe.db.get_value(
        "PayFast Payment Log",
        {
            "reference_doctype": reference_doctype,
            "reference_docname": reference_name,
            "amount": amount,
            "status": "Awaiting Payment",
        },
        ["name", "redirect_token", "expires_at"],
        as_dict=True,
    )
    reuse = False
    if existing:
        expired = existing.expires_at and existing.expires_at < now_datetime()
        if expired:
            # Don't hand back an already-expired link; retire it and mint a new one.
            frappe.db.set_value("PayFast Payment Log", existing.name, "status", "Cancelled")
            _sync_reference_payfast_status(reference_doctype, reference_name, "Cancelled")
        else:
            reuse = True

    if reuse:
        log_name = existing.name
        token = existing.redirect_token
        expires_at = existing.expires_at
        vals = frappe.db.get_value(
            "PayFast Payment Log", existing.name, ["m_payment_id", "payment_status"], as_dict=True
        )
        m_payment_id = vals.m_payment_id
        payment_status = vals.payment_status
    else:
        log = _create_log(
            reference_doctype, reference_name, amount, currency,
            customer=customer or getattr(ref, "customer", None),
            item_name=item_name, item_description=item_description,
            email_address=email_address, name_first=name_first, name_last=name_last,
            cell_number=cell_number, customer_mobile=whatsapp_number,
            whatsapp_conversation_id=conversation_id,
        )
        creds = get_credentials()
        ordered, payload_form, signature = _build_redirect_payload(log, creds)
        log.request_payload_json = json.dumps(payload_form, ensure_ascii=False)
        log.signature = signature
        log.save(ignore_permissions=True)
        _sync_reference_payfast_status(
            reference_doctype, reference_name, "Awaiting Payment", payment_log=log.name
        )
        log_name = log.name
        token = log.redirect_token
        expires_at = log.expires_at
        m_payment_id = log.m_payment_id
        payment_status = log.payment_status

    base_url = frappe.utils.get_url()
    payment_url = f"{base_url}/pf?token={token}"
    return {
        "ok": True,
        "payment_url": payment_url,
        "payment_log": log_name,
        "m_payment_id": m_payment_id,
        "payment_status": payment_status,
        "expires_at": expires_at,
    }


@frappe.whitelist(methods=["GET"])
def get_payment_status(m_payment_id=None, payment_log=None):
    _require_agent()
    if not m_payment_id and not payment_log:
        frappe.throw(_("m_payment_id or payment_log is required."))
    filters = {"name": payment_log} if payment_log else {"m_payment_id": m_payment_id}
    row = frappe.db.get_value(
        "PayFast Payment Log", filters,
        ["name", "m_payment_id", "status", "payment_status", "amount", "currency",
         "pf_payment_id", "payment_entry", "reference_doctype", "reference_docname",
         "expires_at", "paid_at"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("No payment request found."))

    reference_doc_status = None
    if row.reference_doctype and row.reference_docname and frappe.db.exists(
        row.reference_doctype, row.reference_docname
    ):
        reference_doc_status = frappe.db.get_value(
            row.reference_doctype, row.reference_docname, "status"
        )

    normalized_payment_status = _normalize_payment_status(row.status, row.expires_at)
    normalized_booking_status = _normalize_booking_status(row.status, row.expires_at)

    row["ok"] = True
    row["payment_log"] = row["name"]
    row["reference_name"] = row.reference_docname
    row["booking_status"] = normalized_booking_status
    row["reference_doc_status"] = reference_doc_status
    row["normalized_payment_status"] = normalized_payment_status
    row["payfast_payment_status"] = row.payment_status
    row["payment_status"] = normalized_payment_status
    return row


@frappe.whitelist(methods=["POST"])
def regenerate_payment_link(m_payment_id=None, payment_log=None, amount=None):
    _require_agent()
    m_payment_id = _resolve_m_payment_id(m_payment_id, payment_log)
    existing_name = frappe.db.get_value("PayFast Payment Log", {"m_payment_id": m_payment_id})
    if not existing_name:
        frappe.throw(_("No payment request found for {0}").format(m_payment_id))
    existing = frappe.get_doc("PayFast Payment Log", existing_name)
    if existing.status in NON_REGENERABLE_STATUSES:
        frappe.throw(_("Cannot regenerate a payment that has already been verified or is processing."))
    # Cancel the old awaiting attempt.
    if existing.status == "Awaiting Payment":
        existing.status = "Cancelled"
        existing.save(ignore_permissions=True)
        _sync_reference_payfast_status(
            existing.reference_doctype, existing.reference_docname, "Cancelled"
        )
    amount = _validate_amount(amount or existing.amount)
    return create_payment_link(
        reference_doctype=existing.reference_doctype,
        reference_name=existing.reference_docname,
        amount=amount,
        currency=existing.currency,
        customer=existing.customer,
        item_name=existing.item_name,
        item_description=existing.item_description,
        email_address=existing.email_address,
        name_first=existing.name_first,
        name_last=existing.name_last,
        cell_number=existing.cell_number,
        whatsapp_number=existing.customer_mobile,
        conversation_id=existing.whatsapp_conversation_id,
    )


@frappe.whitelist(methods=["POST"])
def cancel_payment_request(m_payment_id=None, payment_log=None, reason=None):
    _require_agent()
    m_payment_id = _resolve_m_payment_id(m_payment_id, payment_log)
    name = frappe.db.get_value("PayFast Payment Log", {"m_payment_id": m_payment_id})
    if not name:
        frappe.throw(_("No payment request found for {0}").format(m_payment_id))
    log = frappe.get_doc("PayFast Payment Log", name)
    if log.status == "Complete":
        frappe.throw(_("Cannot cancel a completed payment."))
    log.status = "Cancelled"
    if reason:
        log.review_reason = reason
        log.error_log = (log.error_log or "") + f"\n[{now_datetime()}] cancelled: {reason}"
    log.save(ignore_permissions=True)
    _sync_reference_payfast_status(log.reference_doctype, log.reference_docname, "Cancelled")
    return {"ok": True, "m_payment_id": m_payment_id, "payment_log": name, "status": "Cancelled"}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=ITN_RATE_LIMIT, seconds=ITN_RATE_WINDOW)
def payfast_itn():
    """Guest ITN receiver.

    ALWAYS returns HTTP 200 ("OK") after best-effort raw-payload storage and
    enqueuing processing — even on unknown m_payment_id, duplicate/retry ITNs,
    or unexpected errors. Raw payload is stored BEFORE any verification.
    """
    try:
        ordered, raw, body = _parse_raw_itn()
        client_ip = _get_client_ip()
        source_meta = _source_meta(client_ip)
        raw_json = json.dumps(raw, ensure_ascii=False)

        pf_payment_id = raw.get("pf_payment_id") or ""
        m_payment_id = raw.get("m_payment_id") or ""

        log_name = None
        if m_payment_id:
            log_name = frappe.db.get_value("PayFast Payment Log", {"m_payment_id": m_payment_id})

        if log_name:
            _store_raw_on_existing(log_name, raw, raw_json, body, client_ip, source_meta, pf_payment_id)
        else:
            _store_unknown_itn(m_payment_id, raw, raw_json, body, client_ip, source_meta, pf_payment_id)
            # Unknown reference: audited only, never processed into a Payment Entry.
            return "OK"

        # Respect the master kill switch: raw is stored + 200, but skip processing.
        if not is_enabled():
            frappe.log_error(
                title="PayFast ITN received while disabled",
                message=f"log={log_name} m_payment_id={m_payment_id}",
            )
            return "OK"

        frappe.enqueue(
            "payfast_gateway.payfast_gateway.services.itn.process_itn",
            queue="long",
            timeout=600,
            log_name=log_name,
            raw_payload_json=raw_json,
            raw_body=body,
            source_host=client_ip,
        )
    except Exception:  # noqa: BLE001 - the ITN endpoint must always ack with 200
        frappe.log_error(title="PayFast ITN handler error", message=frappe.get_traceback())
    return "OK"


def _parse_raw_itn():
    """Parse the raw ITN body preserving field order (spec §9 step 2).

    PayFast signs the parameter string in the exact order posted, so we parse
    the raw request body with ``parse_qsl`` rather than relying on ``form_dict``.
    """
    ordered = []
    req = getattr(frappe, "request", None)
    body = ""
    if req is not None:
        try:
            data = req.get_data()
            if isinstance(data, bytes):
                body = data.decode("utf-8", errors="replace")
            elif isinstance(data, str):
                body = data
        except Exception:  # noqa: BLE001
            body = ""
    if body:
        ordered = parse_qsl(body, keep_blank_values=True)
    else:
        for key, value in frappe.form_dict.items():
            ordered.append((key, value))
    ordered = [(k, v) for (k, v) in ordered if k != "cmd"]
    raw = {}
    for k, v in ordered:
        if k not in raw:
            raw[k] = v
    return ordered, raw, body


def _is_public_ip(value):
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    )


def _get_client_ip():
    """Determine the real client IP.

    Behind a proxy, honour the left-most public entry of X-Forwarded-For;
    otherwise use REMOTE_ADDR. The proxy MUST overwrite (not append)
    client-supplied XFF headers for this to be trustworthy (see README).
    """
    req = getattr(frappe, "request", None)
    if req is None:
        return ""
    headers = getattr(req, "headers", None)
    environ = getattr(req, "environ", None) or {}
    xff = headers.get("X-Forwarded-For", "") if headers else ""
    remote = environ.get("REMOTE_ADDR", "") or ""
    if xff:
        for part in (p.strip() for p in xff.split(",")):
            if part and _is_public_ip(part):
                return part
    return remote


def _source_meta(client_ip):
    req = getattr(frappe, "request", None)
    if req is None:
        return {"client_ip": client_ip}
    headers = getattr(req, "headers", None)
    environ = getattr(req, "environ", None) or {}
    return {
        "client_ip": client_ip,
        "remote_addr": environ.get("REMOTE_ADDR", ""),
        "x_forwarded_for": (headers.get("X-Forwarded-For", "") if headers else ""),
        "host": (headers.get("Host", "") if headers else ""),
    }


def _append_audit(audit_json, raw, source_meta):
    audit = []
    if audit_json:
        try:
            loaded = json.loads(audit_json)
            if isinstance(loaded, list):
                audit = loaded
            elif loaded:
                audit = [loaded]
        except (ValueError, TypeError):
            audit = []
    audit.append({
        "received_at": now_datetime().isoformat(),
        "source": source_meta,
        "payload": raw,
    })
    return audit


def _store_raw_on_existing(log_name, raw, raw_json, raw_body, client_ip, source_meta, pf_payment_id):
    """Store the raw ITN on an existing log without corrupting JSON parsing.

    The latest raw body is kept as a single valid JSON object in
    ``raw_payload_json`` (what ``process_itn`` loads), and every raw body is
    appended to the ``raw_payload_audit_json`` array for a lossless audit trail.
    Uses direct DB writes so raw storage survives even if doc validation would
    otherwise fail.
    """
    audit_json = frappe.db.get_value("PayFast Payment Log", log_name, "raw_payload_audit_json")
    audit = _append_audit(audit_json, raw, source_meta)
    updates = {
        "raw_payload_json": raw_json,
        "raw_itn_body": raw_body or "",
        "raw_payload_audit_json": json.dumps(audit, ensure_ascii=False),
        "source_host": client_ip or (source_meta.get("host") or "unknown"),
    }
    if raw.get("payment_status"):
        updates["payment_status"] = raw.get("payment_status")
    # Only set pf_payment_id when not already set, so process_itn can still
    # detect a conflicting pf_payment_id on a later ITN.
    if pf_payment_id and not frappe.db.get_value("PayFast Payment Log", log_name, "pf_payment_id"):
        updates["pf_payment_id"] = pf_payment_id
    frappe.db.set_value("PayFast Payment Log", log_name, updates, update_modified=True)
    if not frappe.flags.in_test:
        frappe.db.commit()


def _store_unknown_itn(m_payment_id, raw, raw_json, raw_body, client_ip, source_meta, pf_payment_id):
    """Store an ITN with no matching m_payment_id for manual review, guarding
    against unique-key collisions, and raise an admin alert."""
    audit = _append_audit(None, raw, source_meta)
    generated = m_payment_id or f"UNKNOWN-{frappe.generate_hash(length=10)}"
    doc = {
        "doctype": "PayFast Payment Log",
        "m_payment_id": generated,
        "status": "Manual Review",
        "reference_doctype": "Sales Invoice",
        "reference_docname": "",
        "amount": flt(raw.get("amount_gross") or 0),
        "currency": "ZAR",
        "raw_payload_json": raw_json,
        "raw_itn_body": raw_body or "",
        "raw_payload_audit_json": json.dumps(audit, ensure_ascii=False),
        "source_host": client_ip or (source_meta.get("host") or "unknown"),
        "pf_payment_id": pf_payment_id,
        "payment_status": raw.get("payment_status"),
        "review_reason": "ITN received with no matching m_payment_id.",
    }
    name = None
    try:
        frappe.db.savepoint("pf_unknown_itn")
        log = frappe.get_doc(doc)
        log.insert(ignore_permissions=True)
        name = log.name
    except Exception:  # noqa: BLE001 - unique-key collision on a concurrent insert
        frappe.db.rollback(save_point="pf_unknown_itn")
        existing = frappe.db.get_value("PayFast Payment Log", {"m_payment_id": generated})
        if existing:
            _store_raw_on_existing(existing, raw, raw_json, raw_body, client_ip, source_meta, pf_payment_id)
            name = existing
        else:
            raise
    if not frappe.flags.in_test:
        frappe.db.commit()
    frappe.log_error(
        title="PayFast ITN: unknown m_payment_id",
        message=f"m_payment_id={m_payment_id!r} pf_payment_id={pf_payment_id!r} stored as {name}",
    )
    return name
