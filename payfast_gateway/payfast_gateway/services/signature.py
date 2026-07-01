import hashlib
from urllib.parse import quote_plus

# Field order for the PayFast hosted-redirect request, per the v1 spec.
# merchant_key IS transmitted to PayFast (over HTTPS) but never logged.
# passphrase is NEVER transmitted and never logged.
# The `testing` parameter is sent in test mode but EXCLUDED from the signature.
REDIRECT_FIELD_ORDER = [
    "merchant_id",
    "merchant_key",
    "return_url",
    "cancel_url",
    "notify_url",
    "name_first",
    "name_last",
    "email_address",
    "cell_number",
    "m_payment_id",
    "amount",
    "item_name",
    "item_description",
    "custom_int1",
    "custom_str1",
    "custom_int2",
    "custom_str2",
    "custom_int3",
    "custom_str3",
    "custom_int4",
    "custom_str4",
    "custom_int5",
    "custom_str5",
    "subscription_type",
    "billing_date",
    "recurring_amount",
    "frequency",
    "cycles",
    "payment_key",
]

# Fields excluded from signature computation even when transmitted.
SIGNATURE_EXCLUDED_FIELDS = {"signature", "testing"}


def _encode(value) -> str:
    """URL-encode a value to match PayFast's PHP ``urlencode()`` output.

    Python's ``quote_plus`` leaves ``~`` unescaped, but PHP ``urlencode``
    encodes it to ``%7E``. Post-process so both the redirect and ITN signature
    paths produce byte-identical strings to PayFast. Spaces render as ``+`` and
    hex is uppercase (both already produced by ``quote_plus``).
    """
    if value is None:
        return ""
    return quote_plus(str(value)).replace("~", "%7E")


def build_parameter_string(items, *, exclude=SIGNATURE_EXCLUDED_FIELDS):
    """Build the ordered `key=value&key=value` string used for signing.

    `items` is an ordered iterable of (name, value) pairs. Empty values are
    dropped. Fields in `exclude` are dropped. Field order is preserved exactly
    as provided (NOT alphabetical).
    """
    parts = []
    for name, value in items:
        if name in exclude:
            continue
        if value is None or str(value) == "":
            continue
        parts.append(f"{name}={_encode(value)}")
    return "&".join(parts)


def generate_signature(items, passphrase=""):
    """Compute the lowercase MD5 signature.

    The passphrase is appended as `&passphrase=<encoded>` only when non-empty.
    When empty it is omitted entirely (per spec §12).
    """
    param_str = build_parameter_string(items)
    if passphrase:
        param_str = f"{param_str}&passphrase={_encode(passphrase)}" if param_str else f"passphrase={_encode(passphrase)}"
    return hashlib.md5(param_str.encode("utf-8")).hexdigest().lower()


def verify_signature(items, passphrase, received_signature):
    """Constant-ish compare of a received signature against the computed one."""
    if not received_signature:
        return False
    expected = generate_signature(items, passphrase)
    if not expected:
        return False
    return _constant_time_eq(expected, str(received_signature).strip().lower())


def normalize_itn_fields(formdict):
    """Return ITN fields as an ordered list of (name, value) pairs.

    PayFast posts ITN parameters in a defined order. We preserve the received
    order from the form dict (Frappe preserves insertion order) and exclude the
    `signature` and `testing` fields from the signature string.
    """
    items = []
    for name, value in formdict.items():
        if name in SIGNATURE_EXCLUDED_FIELDS:
            continue
        items.append((name, value))
    return items


def _constant_time_eq(a, b):
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0
