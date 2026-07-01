# PayFast × WhatsApp Agent — Developer Integration Guide

Guide for the **LangGraph / WhatsApp agent team** integrating with the PayFast Gateway app on ERPNext.

For ERPNext operators (bench install, PayFast Settings, nginx): see the root [README](../README.md).  
For implementers closing gateway gaps: see [AGENT_INSTRUCTIONS.md](../AGENT_INSTRUCTIONS.md).

---

## 1. What this integration does

```
Customer (WhatsApp)          Your Agent (LangGraph)              ERPNext (payfast_gateway)
       │                              │                                    │
       │  "Book + pay"                │                                    │
       ├─────────────────────────────►│  create Sales Invoice (ERP API)    │
       │                              ├───────────────────────────────────►│
       │                              │  create_payment_link               │
       │                              ├───────────────────────────────────►│
       │                              │◄──────── payment_url ──────────────┤
       │◄── "Pay here: {url}" ────────┤                                    │
       │                              │                                    │
       │  opens payment_url (/pf)     │                                    │
       ├──────────────────────────────────────────────────────────────────►│ PayFast hosted page
       │                              │                                    │
       │                              │         PayFast ITN (server-to-server)
       │                              │                                    ◄── payfast_itn
       │                              │                                    │ verify → Payment Entry
       │                              │◄── payfast_payment_confirmed * ────┤
       │◄── "Payment received…" ──────┤                                    │
```

\* **`payfast_payment_confirmed`** realtime event is **planned** (see §8). Until it ships, poll `get_payment_status`.

**The agent never marks a booking paid.** Only ERPNext does, after PayFast’s ITN passes four checks and `payment_status == COMPLETE`. Customer screenshots, “I paid” messages, and PayFast `return_url` redirects are **not** proof of payment.

---

## 2. Prerequisites

### 2.1 ERPNext side (ops / platform team)

Before your agent can call payment APIs:

| Requirement | Notes |
|-------------|--------|
| `payfast_gateway` installed | `bench install-app payfast_gateway && bench migrate` |
| PayFast Settings configured | Sandbox credentials first; `notify_url`, clearing account, mode of payment |
| Dedicated ERPNext user | e.g. `whatsapp-agent@company.com` |
| Role **PayFast Agent** | Assigned to that user (fixture ships with the app) |
| API key + secret | ERPNext → User → API Access → Generate Keys |
| HTTPS site URL | Payment links and ITN must be reachable from the public internet |
| Reference document submitted | Sales Invoice (or Sales Order) must be **submitted** before `create_payment_link` |

### 2.2 Agent service environment variables

```bash
ERPNEXT_URL=https://erp.yourcompany.co.za          # no trailing slash
ERP_API_KEY=xxxxxxxxxxxxxxxx
ERP_API_SECRET=yyyyyyyyyyyyyyyy
```

Store secrets in your secrets manager — never in the agent prompt or logs.

### 2.3 Permissions model

| Role | Can call payment API? | Can receive ITN? |
|------|----------------------|------------------|
| PayFast Agent | Yes (whitelisted methods only) | No |
| Guest | No | Yes (`payfast_itn` only) |
| System Manager | Yes | Yes |

Your agent user needs **PayFast Agent** only. It must **not** have broad write access to Payment Entry or arbitrary DocTypes.

---

## 3. Authentication

All agent-facing methods use Frappe token auth:

```http
Authorization: token {API_KEY}:{API_SECRET}
```

Example:

```bash
curl -s -X POST \
  "$ERPNEXT_URL/api/method/payfast_gateway.payfast_gateway.api.create_payment_link" \
  -H "Authorization: token $ERP_API_KEY:$ERP_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "reference_doctype": "Sales Invoice",
    "reference_name": "ACC-SINV-2026-00042",
    "amount": 1500.00,
    "currency": "ZAR",
    "customer": "CUST-00001",
    "whatsapp_number": "27821234567",
    "conversation_id": "wa-thread-abc123"
  }'
```

Successful responses wrap the payload in Frappe’s envelope:

```json
{
  "message": {
    "ok": true,
    "payment_url": "https://erp.yourcompany.co.za/pf?token=…",
    "payment_log": "PFLOG-00001",
    "m_payment_id": "PFM-00001",
    "payment_status": null,
    "expires_at": "2026-07-01 15:30:00"
  }
}
```

Errors return HTTP 4xx/5xx with `exc` / `message` — parse and surface appropriately; do **not** retry blindly on 403 (permission) or 417 (validation).

---

## 4. API reference (agent tools)

Base path for all methods:

```
{ERPNEXT_URL}/api/method/payfast_gateway.payfast_gateway.api.{method_name}
```

### 4.1 `create_payment_link` — **POST**

Creates (or reuses) a hosted PayFast payment link for a **submitted** Sales Invoice or Sales Order.

**Required parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `reference_doctype` | string | `"Sales Invoice"` (preferred v1) or `"Sales Order"` |
| `reference_name` | string | Document name, e.g. `ACC-SINV-2026-00042` |
| `amount` | number | Positive amount in ZAR |

**Recommended parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `customer` | string | Customer link name; validated against invoice if set |
| `whatsapp_number` | string | E.164-ish mobile; pre-fills PayFast `cell_number` |
| `conversation_id` | string | Your WhatsApp thread id — echoed back on payment confirmation event |

**Optional parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `currency` | string | Default `ZAR` (only ZAR supported in v1) |
| `item_name`, `item_description` | string | PayFast line item text |
| `email_address`, `name_first`, `name_last`, `cell_number` | string | Payer fields on PayFast form |
| `reference_docname` | string | Alias for `reference_name` (legacy) |

**Response fields**

| Field | Use in agent |
|-------|----------------|
| `payment_url` | Send to customer in WhatsApp |
| `payment_log` | Store in conversation state — primary key for status polling |
| `m_payment_id` | Merchant reference sent to PayFast |
| `expires_at` | Tell customer link expiry; default 60 min from settings |

**Link reuse:** If an active link already exists for the same reference + amount + `Awaiting Payment` status and is not expired, the same URL is returned (no duplicate PayFast session).

**Typical agent flow**

1. Create and **submit** Sales Invoice in ERPNext (separate ERP API call).
2. Call `create_payment_link` with invoice name, amount, customer, WhatsApp metadata.
3. Persist `payment_log`, `payment_url`, `expires_at` in conversation state.
4. Send WhatsApp template with `payment_url`.

---

### 4.2 `get_payment_status` — **GET**

Read-only status check. Use for polling until the realtime event (§8) is available.

**Parameters** (one required)

| Parameter | Description |
|-----------|-------------|
| `payment_log` | Doc name from `create_payment_link` — **preferred** |
| `m_payment_id` | Alternative lookup key |

```bash
curl -s -G \
  "$ERPNEXT_URL/api/method/payfast_gateway.payfast_gateway.api.get_payment_status" \
  -H "Authorization: token $ERP_API_KEY:$ERP_API_SECRET" \
  --data-urlencode "payment_log=PFLOG-00001"
```

**Response (current shape)**

```json
{
  "message": {
    "ok": true,
    "payment_log": "PFLOG-00001",
    "name": "PFLOG-00001",
    "m_payment_id": "PFM-00001",
    "status": "Complete",
    "payment_status": "COMPLETE",
    "amount": 1500.0,
    "currency": "ZAR",
    "reference_doctype": "Sales Invoice",
    "reference_docname": "ACC-SINV-2026-00042",
    "payment_entry": "ACC-PAY-2026-00008",
    "paid_at": "2026-07-01 14:22:00",
    "expires_at": "2026-07-01 15:30:00",
    "booking_status": "Paid"
  }
}
```

**How to interpret `status` (internal log lifecycle)**

| `status` | Meaning for agent | Customer message |
|----------|-------------------|------------------|
| `Awaiting Payment` | Link active, not paid | Send / resend `payment_url` |
| `Processing` | ITN being processed | “Checking payment…” |
| `Complete` | Paid + Payment Entry created | **Confirm booking** |
| `Failed` / `Cancelled` | Not completed | Offer new link (`regenerate_payment_link`) |
| `ERP Sync Failed` | PayFast paid; ERP retry in progress | “Payment received, confirming…” — poll, don’t re-pay |
| `Manual Review` | Human needed | **Do not** auto-confirm; escalate |

**Important:** Today `booking_status` reflects the **reference document’s** ERPNext `status` field (e.g. invoice `Paid`), not a dedicated booking enum. After gateway P1 work (see AGENT_INSTRUCTIONS), normalized `booking_status` values (`confirmed` / `awaiting_payment` / `cancelled`) will also be returned — design your state machine to prefer `status` on the payment log for payment decisions.

**Polling guidance**

- Poll every 15–30 s for up to `expires_at` while customer says they’re paying.
- Stop polling once `status == Complete` or terminal failure states.
- Do not poll faster than once per 10 s (unnecessary load).

---

### 4.3 `regenerate_payment_link` — **POST**

Issue a fresh link when the old one expired or payment failed/cancelled.

**Parameters (current)**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `m_payment_id` | Yes* | From original `create_payment_link` |
| `amount` | No | Override amount; defaults to original |

\* **Planned:** `payment_log` will also be accepted (see AGENT_INSTRUCTIONS).

```bash
curl -s -X POST \
  "$ERPNEXT_URL/api/method/payfast_gateway.payfast_gateway.api.regenerate_payment_link" \
  -H "Authorization: token $ERP_API_KEY:$ERP_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"m_payment_id": "PFM-00001"}'
```

Returns the same shape as `create_payment_link`. Cannot regenerate after `Complete` or while `ERP Sync Failed` (payment already verified at PayFast).

---

### 4.4 `cancel_payment_request` — **POST**

Cancel an outstanding payment request (agent or customer gave up).

**Parameters (current)**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `m_payment_id` | Yes* | Merchant payment id |
| `reason` | No | Stored on log for audit |

\* **Planned:** `payment_log` will also be accepted.

```bash
curl -s -X POST \
  "$ERPNEXT_URL/api/method/payfast_gateway.payfast_gateway.api.cancel_payment_request" \
  -H "Authorization: token $ERP_API_KEY:$ERP_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"m_payment_id": "PFM-00001", "reason": "Customer requested cancellation"}'
```

Does **not** refund money — only cancels the outstanding link. Refunds are manual in PayFast / ERPNext.

---

## 5. LangGraph tool implementations

Minimal Python tools your agent should expose. Adjust to your framework (ToolNode, `@tool`, etc.).

```python
import os
import requests

ERP = os.environ["ERPNEXT_URL"].rstrip("/")
AUTH = {"Authorization": f"token {os.environ['ERP_API_KEY']}:{os.environ['ERP_API_SECRET']}"}
TIMEOUT = 30


def _message(resp: requests.Response) -> dict:
    resp.raise_for_status()
    data = resp.json()
    if data.get("exc_type"):
        raise RuntimeError(data.get("message") or data.get("exc"))
    return data["message"]


def create_payment_link_tool(
    reference_doctype: str,
    reference_name: str,
    customer: str,
    amount: float,
    whatsapp_number: str,
    conversation_id: str,
    currency: str = "ZAR",
) -> dict:
    """Create a PayFast payment link. Returns payment_url to send to the customer."""
    return _message(requests.post(
        f"{ERP}/api/method/payfast_gateway.payfast_gateway.api.create_payment_link",
        headers=AUTH,
        json={
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "customer": customer,
            "amount": amount,
            "currency": currency,
            "whatsapp_number": whatsapp_number,
            "conversation_id": conversation_id,
        },
        timeout=TIMEOUT,
    ))


def get_payment_status_tool(payment_log: str = None, m_payment_id: str = None) -> dict:
    """Read payment status. Prefer payment_log from create_payment_link."""
    params = {}
    if payment_log:
        params["payment_log"] = payment_log
    elif m_payment_id:
        params["m_payment_id"] = m_payment_id
    else:
        raise ValueError("payment_log or m_payment_id required")
    return _message(requests.get(
        f"{ERP}/api/method/payfast_gateway.payfast_gateway.api.get_payment_status",
        headers=AUTH,
        params=params,
        timeout=TIMEOUT,
    ))


def regenerate_payment_link_tool(m_payment_id: str, amount: float = None) -> dict:
    body = {"m_payment_id": m_payment_id}
    if amount is not None:
        body["amount"] = amount
    return _message(requests.post(
        f"{ERP}/api/method/payfast_gateway.payfast_gateway.api.regenerate_payment_link",
        headers=AUTH,
        json=body,
        timeout=TIMEOUT,
    ))


def cancel_payment_request_tool(m_payment_id: str, reason: str = None) -> dict:
    body = {"m_payment_id": m_payment_id}
    if reason:
        body["reason"] = reason
    return _message(requests.post(
        f"{ERP}/api/method/payfast_gateway.payfast_gateway.api.cancel_payment_request",
        headers=AUTH,
        json=body,
        timeout=TIMEOUT,
    ))
```

---

## 6. Agent guardrails (system prompt / policy)

Encode these in the agent system prompt and **do not** expose tools that violate them.

### 6.1 Allowed tools

| Tool | Purpose |
|------|---------|
| `create_payment_link` | After invoice/booking doc exists and is submitted |
| `get_payment_status` | Poll or verify before sending confirmation |
| `regenerate_payment_link` | Expired / failed / cancelled payment |
| `cancel_payment_request` | Customer abandons payment |
| ERPNext read/write for **booking docs** | Create invoice, customer, items — normal ERP API |

### 6.2 Forbidden behaviour

- **Never** tell the customer “payment confirmed” based on: their message, a screenshot, PayFast return page, or `return_url` hit.
- **Never** expose a tool that sets `payment_status`, marks invoice paid, or submits Payment Entry.
- **Never** call ERPNext APIs to mutate **PayFast Payment Log** directly.
- **Never** log or echo `merchant_key`, `passphrase`, or full ITN payloads.
- **Do not** regenerate a link when `status == Complete` or `ERP Sync Failed` (payment already verified at PayFast).

### 6.3 Confirmation rule

Send the **booking confirmed** WhatsApp message only when:

```
get_payment_status → status == "Complete"
```

(or when you receive `payfast_payment_confirmed` for that `payment_log` — §8).

---

## 7. Conversation state machine

Recommended state stored per WhatsApp `conversation_id`:

```python
@dataclass
class PaymentContext:
    reference_doctype: str
    reference_name: str
    customer: str
    payment_log: str | None = None
    m_payment_id: str | None = None
    payment_url: str | None = None
    expires_at: str | None = None
    phase: Literal[
        "booking_created",
        "awaiting_payment",
        "confirming",      # ERP Sync Failed or polling
        "confirmed",
        "failed",
        "manual_review",
    ] = "booking_created"
```

**Transitions**

```
booking_created
    → create_payment_link → awaiting_payment

awaiting_payment
    → get_payment_status Complete → confirmed
    → get_payment_status Failed/Cancelled → failed → regenerate_payment_link → awaiting_payment
    → expires_at passed → failed → regenerate_payment_link
    → cancel_payment_request → failed (terminal unless new booking)

awaiting_payment + ERP Sync Failed on poll → confirming (keep polling)

confirming → Complete → confirmed

any + Manual Review → manual_review (escalate to human, no auto messages claiming paid)
```

---

## 8. Payment confirmation delivery

### 8.1 Target: realtime event (recommended)

After gateway P0 work, ERPNext will emit:

```
Event: payfast_payment_confirmed
```

**Payload:**

```json
{
  "payment_log": "PFLOG-00001",
  "reference_doctype": "Sales Invoice",
  "reference_name": "ACC-SINV-2026-00042",
  "customer_mobile": "27821234567",
  "conversation_id": "wa-thread-abc123"
}
```

**Your service should:**

1. Subscribe via Frappe Socket.IO / webhook bridge / message queue (implementation depends on your infra).
2. Match `conversation_id` to the active WhatsApp thread.
3. Send the confirmation template (§9).
4. Set conversation `phase = confirmed`.

If `conversation_id` was not passed at link creation, fall back to matching on `customer_mobile` + open `payment_log`.

### 8.2 Interim: polling (use today)

Until the event is deployed:

1. After sending `payment_url`, start a background poll loop on `payment_log`.
2. On `status == Complete`, send confirmation and stop.
3. Optionally combine with customer messages (“I’ve paid”) → single `get_payment_status` check, still **never** trust the message alone.

Example poll loop (pseudo):

```python
async def wait_for_payment(payment_log: str, expires_at: datetime):
    while datetime.utcnow() < expires_at:
        msg = get_payment_status_tool(payment_log=payment_log)
        if msg["status"] == "Complete":
            return "confirmed"
        if msg["status"] in ("Failed", "Cancelled", "Manual Review"):
            return msg["status"]
        await asyncio.sleep(20)
    return "expired"
```

---

## 9. WhatsApp message templates

Use your template provider’s variable syntax; placeholders shown as `{var}`.

| Situation | Template |
|-----------|----------|
| Link created | Your booking is created. Complete payment here: {payment_url}. I'll confirm automatically once payment reflects in our system. |
| Still waiting | I haven't received payment yet. You can complete it here: {payment_url} (expires {expires_at_local}). |
| Paid (`Complete`) | Payment received — your booking is confirmed. Reference: {reference_name}. |
| Failed / expired | That payment wasn't completed. Here's a new link: {payment_url} |
| ERP sync pending | Payment received from PayFast — finalising your booking now. You'll get confirmation shortly. |
| Manual review | Your payment needs a quick manual check. Our team will confirm shortly — no need to pay again. |

**Do not** send the paid template on PayFast redirect alone. The customer may hit `/pf-return` before ITN completes (typically seconds, sometimes longer).

---

## 10. End-to-end booking flow (happy path)

Step-by-step for agent developers testing against sandbox:

1. **Create customer** (if new) via ERPNext API.
2. **Create Sales Invoice** with line items; **submit** (`docstatus = 1`).
3. **`create_payment_link`**
   - `reference_doctype`: `Sales Invoice`
   - `reference_name`: submitted invoice name
   - `amount`: match invoice outstanding (usually `grand_total`)
   - `whatsapp_number`, `conversation_id`: from WhatsApp session
4. **Send WhatsApp** with `payment_url`.
5. Customer opens link → `/pf` auto-posts to PayFast sandbox → complete test payment.
6. PayFast sends ITN → ERPNext creates Payment Entry → log `status = Complete`.
7. Agent receives confirmation (poll or event) → send confirmed template.

**Sandbox test card:** use PayFast sandbox credentials in PayFast Settings (`environment = Sandbox`). See [PayFast sandbox docs](https://developers.payfast.co.za/docs).

---

## 11. Sales Invoice vs Sales Order

| Reference | `create_payment_link` | Automatic payment on ITN (v1) |
|-----------|----------------------|--------------------------------|
| Sales Invoice | Supported | **Yes** — Payment Entry allocated to invoice |
| Sales Order | Supported | **No** — log goes to Manual Review until P2 SO work ships |

**Agent team recommendation for v1:** always create and submit a **Sales Invoice** before requesting payment. Do not promise auto-confirmation on Sales Order-only flows until gateway P2 is deployed.

---

## 12. Error handling

| HTTP / error | Agent action |
|--------------|--------------|
| 403 Permission | Check user has PayFast Agent role and token is correct |
| “PayFast integration is disabled” | Ops must enable PayFast Settings |
| “Reference must be submitted” | Submit invoice before `create_payment_link` |
| “Amount must be greater than 0” | Fix amount in tool call |
| “Customer mismatch” | Align `customer` with invoice customer |
| “Cannot regenerate…” | Payment already verified — poll status instead |
| Network timeout | Retry with backoff (max 3); don’t create duplicate links blindly |
| `Manual Review` status | Escalate; don’t send paid template |
| `ERP Sync Failed` | Poll every 30–60 s; payment is valid at PayFast side |

Log `payment_log` and `m_payment_id` on errors for ops correlation — never log secrets.

---

## 13. What the customer sees (URLs)

| URL | Purpose | Agent concern |
|-----|---------|---------------|
| `{site}/pf?token=…` | Auto-redirect to PayFast | Send as `payment_url` |
| `{site}/pf-return` | PayFast success redirect | Informational only — **not** confirmation |
| `{site}/pf-cancel` | PayFast cancel redirect | Offer regenerate link |

Return/cancel pages are informational (planned/default routes — see AGENT_INSTRUCTIONS). They do not update payment state.

---

## 14. Sandbox checklist (agent QA)

```
[ ] ERP_API_KEY/SECRET works against staging ERPNext
[ ] create_payment_link returns HTTPS payment_url
[ ] WhatsApp message contains clickable link
[ ] Sandbox payment completes on PayFast
[ ] get_payment_status → status Complete within ~2 min
[ ] Agent sends confirmation only after Complete
[ ] regenerate_payment_link after cancel flow works
[ ] cancel_payment_request stops reuse of old link
[ ] Agent does NOT confirm on customer "I paid" without status Complete
[ ] conversation_id stored and recoverable for confirmation routing
```

---

## 15. Production cutover notes

- Staging and production use **separate** ERPNext sites or credentials.
- Production PayFast Settings: `environment = Live`, live merchant credentials, live passphrase.
- Update `ERPNEXT_URL` and API tokens in agent deployment.
- Smoke test one real small payment before enabling automated confirmations at scale.

---

## 16. Support escalation

Route to ops / finance when:

- `status == Manual Review`
- Customer paid but `status` stuck `Awaiting Payment` > 10 minutes (ITN / nginx issue)
- `ERP Sync Failed` for > 30 minutes
- Amount mismatch disputes

Ops should use **PayFast Payment Log** in ERPNext desk (filter by `payment_log` name) and follow the manual reconciliation runbook in the implementation spec.

---

## 17. Quick reference

| Item | Value |
|------|--------|
| App module | `payfast_gateway` |
| Create link | `POST …/api/method/payfast_gateway.payfast_gateway.api.create_payment_link` |
| Get status | `GET …/api/method/payfast_gateway.payfast_gateway.api.get_payment_status` |
| Regenerate | `POST …/api/method/payfast_gateway.payfast_gateway.api.regenerate_payment_link` |
| Cancel | `POST …/api/method/payfast_gateway.payfast_gateway.api.cancel_payment_request` |
| ITN (PayFast only) | `POST …/api/method/payfast_gateway.payfast_gateway.api.payfast_itn` |
| Auth header | `Authorization: token {key}:{secret}` |
| Paid signal | `get_payment_status.status == "Complete"` or event `payfast_payment_confirmed` |
| Currency | ZAR only (v1) |

---

## 18. Related documents

| Document | Audience |
|----------|----------|
| [README.md](../README.md) | Platform / DevOps |
| [AGENT_INSTRUCTIONS.md](../AGENT_INSTRUCTIONS.md) | Gateway implementers (P0–P2 tasks) |
| PayFast developer docs | Signature / sandbox behaviour |

Questions about ERPNext setup → platform team.  
Questions about tool behaviour or confirmation logic → this guide + `get_payment_status` field semantics in §4.2.
