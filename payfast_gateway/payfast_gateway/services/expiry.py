import frappe
from frappe.utils import add_to_date, cint, now_datetime

from payfast_gateway.payfast_gateway.doctype.payfast_settings.payfast_settings import get_settings
from payfast_gateway.payfast_gateway.services.itn import _update_reference_payfast_status


def expire_stale_links():
    """Mark stale awaiting-payment links as cancelled (spec §12)."""
    settings = get_settings()
    cutoff = add_to_date(now_datetime(), minutes=-cint(settings.link_expiry_minutes, 60))
    stale = frappe.get_all(
        "PayFast Payment Log",
        filters={"status": "Awaiting Payment", "creation": ["<", cutoff]},
        pluck="name",
    )
    for name in stale:
        frappe.db.set_value("PayFast Payment Log", name, "status", "Cancelled")
        log = frappe.get_doc("PayFast Payment Log", name)
        _update_reference_payfast_status(
            log.reference_doctype, log.reference_docname, "Cancelled"
        )
    if stale and not frappe.flags.in_test:
        frappe.db.commit()
