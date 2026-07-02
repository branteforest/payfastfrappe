import json

import frappe


def execute():
    """Rebuild the stored redirect payload + signature for links that are still
    awaiting payment.

    Payloads minted before the blank-field fix transmitted empty fields (and a
    `testing` flag in sandbox) that were excluded from the signature, so
    PayFast rejected them with "Generated signature does not match submitted
    signature". Regenerating in place keeps existing payment URLs (same
    redirect token) working for customers who already received a link.
    """
    from payfast_gateway.payfast_gateway.api import _build_redirect_payload
    from payfast_gateway.payfast_gateway.doctype.payfast_settings.payfast_settings import (
        get_credentials,
    )

    names = frappe.get_all(
        "PayFast Payment Log", filters={"status": "Awaiting Payment"}, pluck="name"
    )
    if not names:
        return

    creds = get_credentials()
    if not creds.get("merchant_id"):
        # No active-environment credentials configured; nothing sane to sign.
        return

    for name in names:
        log = frappe.get_doc("PayFast Payment Log", name)
        _ordered, payload_form, signature = _build_redirect_payload(log, creds)
        frappe.db.set_value(
            "PayFast Payment Log",
            name,
            {
                "request_payload_json": json.dumps(payload_form, ensure_ascii=False),
                "signature": signature,
            },
            update_modified=False,
        )
