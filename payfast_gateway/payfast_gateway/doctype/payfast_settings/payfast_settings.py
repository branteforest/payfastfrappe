import frappe
from frappe.model.document import Document
from frappe.utils import cint


class PayFastSettings(Document):
    pass


def get_settings() -> "PayFastSettings":
    return frappe.get_single("PayFast Settings")


def is_sandbox() -> bool:
    return get_settings().environment == "Sandbox"


def is_enabled(settings=None) -> bool:
    """Master kill switch. Treat an unset value as enabled for backward compat;
    only an explicit 0 disables the integration."""
    s = settings or get_settings()
    val = s.get("enabled")
    if val is None:
        return True
    return bool(cint(val))


def is_debug_logging(settings=None) -> bool:
    s = settings or get_settings()
    return bool(cint(s.get("enable_debug_logging")))


def get_credentials():
    """Return the active-environment credentials.

    Never log merchant_key or passphrase anywhere downstream.
    """
    s = get_settings()
    if s.environment == "Sandbox":
        return {
            "merchant_id": s.sandbox_merchant_id,
            "merchant_key": s.sandbox_merchant_key,
            "passphrase": s.sandbox_passphrase,
            "process_url": s.sandbox_process_url,
            "validate_url": s.sandbox_validate_url,
        }
    return {
        "merchant_id": s.live_merchant_id,
        "merchant_key": s.live_merchant_key,
        "passphrase": s.live_passphrase,
        "process_url": s.live_process_url,
        "validate_url": s.live_validate_url,
    }


def get_allowed_source_hosts():
    raw = get_settings().allowed_source_hosts or ""
    return [h.strip() for h in raw.splitlines() if h.strip()]
