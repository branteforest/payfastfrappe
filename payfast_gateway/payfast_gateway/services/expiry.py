import frappe
from frappe.utils import add_to_date, cint, now_datetime

from payfast_gateway.payfast_gateway.doctype.payfast_settings.payfast_settings import get_settings


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
        _sync_reference_payfast_status(log, "Cancelled")
    if stale and not frappe.flags.in_test:
        frappe.db.commit()


def _sync_reference_payfast_status(log, payfast_status):
    if log.reference_doctype != "Sales Invoice" or not log.reference_docname:
        return
    if not frappe.db.exists(log.reference_doctype, log.reference_docname):
        return
    meta = frappe.get_meta(log.reference_doctype)
    if meta.has_field("payfast_status"):
        frappe.db.set_value(
            log.reference_doctype, log.reference_docname, "payfast_status", payfast_status
        )
