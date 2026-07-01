import json

import frappe

from payfast_gateway.payfast_gateway.doctype.payfast_settings.payfast_settings import (
    get_credentials,
    is_enabled,
)
from payfast_gateway.payfast_gateway.services.itn import _update_reference_payfast_status

# Never website-cache this route: Frappe caches by path (query string excluded),
# so a single cached error render would be served to every subsequent customer
# regardless of their token.
no_cache = 1


def get_context(context):
    context.no_cache = 1
    token = frappe.form_dict.get("token")
    if not token:
        context.error = "Missing redirect token."
        context.show_form = False
        return context

    try:
        log = frappe.get_doc("PayFast Payment Log", {"redirect_token": token})
    except frappe.DoesNotExistError:
        context.error = "Invalid or expired payment link."
        context.show_form = False
        return context

    if not is_enabled():
        # Master kill switch: never send a customer on to PayFast while
        # disabled, even for a link minted before the switch was flipped off.
        context.error = "Online payments are temporarily unavailable. Please try again later."
        context.show_form = False
        return context

    if log.status in ("Complete", "Cancelled", "Failed"):
        context.error = f"This payment link is no longer active ({log.status})."
        context.show_form = False
        return context

    if log.expires_at and log.expires_at < frappe.utils.now_datetime():
        if log.status == "Awaiting Payment":
            frappe.db.set_value("PayFast Payment Log", log.name, "status", "Cancelled")
            _update_reference_payfast_status(
                log.reference_doctype, log.reference_docname, "Cancelled"
            )
            frappe.db.commit()
        context.error = "This payment link has expired."
        context.show_form = False
        return context

    payload = json.loads(log.request_payload_json or "{}")
    fields = []
    for name, value in payload.items():
        if name in ("signature",):
            continue
        fields.append((name, value))
    fields.append(("signature", log.signature))

    context.fields = fields
    context.process_url = log.process_url or get_credentials()["process_url"]
    context.amount = log.amount
    context.m_payment_id = log.m_payment_id
    context.show_form = True
    context.no_cache = 1
    return context
