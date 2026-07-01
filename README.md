# PayFast Gateway

Custom Frappe/ERPNext app implementing the PayFast hosted-redirect + ITN (Instant
Transaction Notification) integration per the v1 developer spec.

## Booking primitive

v1 uses **Sales Invoice first**: a Payment Entry (Receive) is allocated directly
against the Sales Invoice when an ITN passes all four mandatory checks and
`payment_status == COMPLETE`. **Sales Order** payments create an advance Payment
Entry via ERPNext's `get_payment_entry`.

## Critical invariants

- An order is marked paid ONLY after the ITN passes all four checks AND
  `payment_status == COMPLETE`. `return_url`, WhatsApp messages and screenshots
  are never proof of payment.
- The `passphrase` is never transmitted and never logged. `merchant_key` is
  never logged.
- The ITN endpoint always returns HTTP 200 after storing the raw payload and
  enqueuing processing.
- The raw ITN payload is stored before any verification.
- Never two Payment Entries for one `pf_payment_id`.
- Duplicate / conflicting ITNs return 200 and do not double-process.

## Agent API

All methods require the **PayFast Agent** role (token auth). Full dotted paths:

| Method | HTTP | Path |
|--------|------|------|
| `create_payment_link` | POST | `payfast_gateway.payfast_gateway.api.create_payment_link` |
| `get_payment_status` | GET | `payfast_gateway.payfast_gateway.api.get_payment_status` |
| `regenerate_payment_link` | POST | `payfast_gateway.payfast_gateway.api.regenerate_payment_link` |
| `cancel_payment_request` | POST | `payfast_gateway.payfast_gateway.api.cancel_payment_request` |
| `payfast_itn` | POST (guest) | `payfast_gateway.payfast_gateway.api.payfast_itn` |

`regenerate_payment_link` and `cancel_payment_request` accept **`payment_log`**
or **`m_payment_id`**. `get_payment_status` returns normalized `payment_status`
and `booking_status` for agents, plus legacy `reference_doc_status` and
`payfast_payment_status` (raw PayFast ITN field).

### Example (agent tool)

```python
import os
import requests

ERP = os.environ["ERPNEXT_URL"].rstrip("/")
AUTH = {"Authorization": f"token {os.environ['ERP_API_KEY']}:{os.environ['ERP_API_SECRET']}"}

def create_payment_link(reference_name, amount):
    r = requests.post(
        f"{ERP}/api/method/payfast_gateway.payfast_gateway.api.create_payment_link",
        headers=AUTH,
        json={
            "reference_doctype": "Sales Invoice",
            "reference_name": reference_name,
            "amount": amount,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["message"]

def get_payment_status(payment_log):
    r = requests.get(
        f"{ERP}/api/method/payfast_gateway.payfast_gateway.api.get_payment_status",
        headers=AUTH,
        params={"payment_log": payment_log},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["message"]
```

## Realtime event: `payfast_payment_confirmed`

Fired via `frappe.publish_realtime` when a verified ITN completes (Payment Entry
created, log `status = Complete`). Payload:

```json
{
  "payment_log": "PFLOG-00001",
  "reference_doctype": "Sales Invoice",
  "reference_name": "SINV-00001",
  "customer_mobile": "+27...",
  "conversation_id": "whatsapp-thread-id"
}
```

Not emitted on failed ITNs, Manual Review, or cancelled links.

## Website routes

| Route | Purpose |
|-------|---------|
| `/pf?token=…` | Auto-redirect to PayFast hosted checkout |
| `/pf-return` | Customer return page (informational only — not proof of payment) |
| `/pf-cancel` | Customer cancel page (informational only) |

## Default URLs

When **PayFast Settings** fields are blank, the app auto-defaults:

- `notify_url` → `{site}/api/method/payfast_gateway.payfast_gateway.api.payfast_itn`
- `return_url` → `{site}/pf-return`
- `cancel_url` → `{site}/pf-cancel`

Explicit settings values always override defaults.

## ITN source validation

The ITN receiver determines the real client IP (left-most public entry of
`X-Forwarded-For` when behind a proxy, else `REMOTE_ADDR`) and checks it against
the IPs resolved from the PayFast notify hosts (`www`, `w1w`, `w2w`,
`sandbox`.payfast.co.za) plus any operator-configured hosts/IPs in
**PayFast Settings → Allowed ITN Source Hosts**. DNS results are cached briefly.

Server validate posts the **original raw** form-encoded ITN body to PayFast
(not a re-serialized dict).

> Your reverse proxy MUST overwrite (not append) client-supplied
> `X-Forwarded-For` so the left-most entry is trustworthy. Example nginx:
> `proxy_set_header X-Forwarded-For $remote_addr;`

## Rate limiting

`payfast_itn` is decorated with Frappe's `@rate_limit` (per-IP, generous — well
above genuine PayFast volume). Add a defense-in-depth nginx limit in front:

```nginx
# http { } block
limit_req_zone $binary_remote_addr zone=payfast_itn:10m rate=10r/s;

# server { } block, scoped to the ITN method
location = /api/method/payfast_gateway.payfast_gateway.api.payfast_itn {
    limit_req zone=payfast_itn burst=20 nodelay;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_pass http://frappe-bench-frappe;
}
```

## ERP sync retries

If all four checks pass and `payment_status == COMPLETE` but the ERP Payment
Entry cannot be created (e.g. transient accounting error), the log is set to
`ERP Sync Failed` (raw payload retained, `retry_count` incremented) rather than
`Manual Review`. A scheduler job (`retry_erp_sync`, every 10 min) retries until
success or `MAX_ERP_RETRIES`, after which it escalates to `Manual Review`.

## Scheduled link expiry

`expire_stale_links` runs every 10 minutes and cancels `Awaiting Payment` logs
older than the configured link expiry window.

## Documentation

| Document | Audience |
|----------|----------|
| [docs/AGENT_DEV_GUIDE.md](docs/AGENT_DEV_GUIDE.md) | **WhatsApp / LangGraph agent team** — API tools, flows, guardrails |
| [docs/COMPREHENSIVE_REPORT.md](docs/COMPREHENSIVE_REPORT.md) | Review + improvement plan |
| [AGENT_INSTRUCTIONS.md](AGENT_INSTRUCTIONS.md) | Gateway implementers — task handoff |

## Install

```bash
bench get-app /Users/andrestrauss/payfast
bench --site <site> install-app payfast_gateway
bench --site <site> migrate
bench --site <site> run-tests --app payfast_gateway
```
