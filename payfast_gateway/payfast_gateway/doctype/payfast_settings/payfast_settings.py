import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_url


class PayFastSettings(Document):
    def validate(self):
        self._validate_active_credentials()

    def _validate_active_credentials(self):
        """Require merchant credentials for the active environment while the
        integration is enabled -- otherwise create_payment_link() silently
        builds a redirect with a blank merchant_id/key that only fails
        opaquely on PayFast's hosted page.
        """
        if not is_enabled(self):
            return
        if self.environment == "Sandbox":
            required = [
                ("sandbox_merchant_id", _("Sandbox Merchant ID")),
                ("sandbox_merchant_key", _("Sandbox Merchant Key")),
            ]
        else:
            required = [
                ("live_merchant_id", _("Live Merchant ID")),
                ("live_merchant_key", _("Live Merchant Key")),
            ]
        missing = [label for fieldname, label in required if not self.get(fieldname)]
        if missing:
            frappe.throw(
                _(
                    "Missing required PayFast credential(s) for the {0} environment: {1}. "
                    "Fill them in, or turn off \"Enabled\" until you do."
                ).format(self.environment, ", ".join(missing))
            )


def get_settings() -> "PayFastSettings":
    return frappe.get_single("PayFast Settings")


def is_sandbox() -> bool:
    return get_settings().environment == "Sandbox"


def is_enabled(settings=None) -> bool:
    """Master kill switch. Off by default: the integration only runs after an
    operator explicitly enables it (which also validates credentials)."""
    s = settings or get_settings()
    return bool(cint(s.get("enabled")))


def is_debug_logging(settings=None) -> bool:
    s = settings or get_settings()
    return bool(cint(s.get("enable_debug_logging")))


def _read_password(settings, fieldname):
    """Password fields are masked ('********') on the loaded doc; the real
    value lives in __Auth and must be read via get_password()."""
    try:
        return settings.get_password(fieldname, raise_exception=False) or ""
    except Exception:  # noqa: BLE001 - unset/legacy values must not break callers
        return settings.get(fieldname) or ""


def get_credentials():
    """Return the active-environment credentials.

    Never log merchant_key or passphrase anywhere downstream.
    """
    s = get_settings()
    if s.environment == "Sandbox":
        return {
            "merchant_id": s.sandbox_merchant_id,
            "merchant_key": _read_password(s, "sandbox_merchant_key"),
            "passphrase": _read_password(s, "sandbox_passphrase"),
            "process_url": s.sandbox_process_url,
            "validate_url": s.sandbox_validate_url,
        }
    return {
        "merchant_id": s.live_merchant_id,
        "merchant_key": _read_password(s, "live_merchant_key"),
        "passphrase": _read_password(s, "live_passphrase"),
        "process_url": s.live_process_url,
        "validate_url": s.live_validate_url,
    }


def get_allowed_source_hosts():
    raw = get_settings().allowed_source_hosts or ""
    return [h.strip() for h in raw.splitlines() if h.strip()]


def get_notify_url(settings=None):
    s = settings or get_settings()
    return s.notify_url or get_url(
        "/api/method/payfast_gateway.payfast_gateway.api.payfast_itn"
    )


def get_return_url(settings=None):
    s = settings or get_settings()
    return s.return_url or get_url("/pf-return")


def get_cancel_url(settings=None):
    s = settings or get_settings()
    return s.cancel_url or get_url("/pf-cancel")
