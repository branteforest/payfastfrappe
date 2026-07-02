# PayFast Gateway — LangGraph Agent Integration Guide

How to wire the WhatsApp booking agent to take payments through ERPNext +
PayFast. The agent's job is conversation only: it requests links, sends them,
and reads status. It never verifies payments, never holds PayFast credentials,
and never decides that money arrived — ERPNext does, via PayFast's ITN.

---

## 1. Credentials & auth

The ERPNext team gives you an `api_key` + `api_secret` for a dedicated user
holding the **PayFast Agent** role (see the site setup guide). Every call:

```
Authorization: token <api_key>:<api_secret>
Content-Type: application/json
```

Store the pair in the agent's secret store (env var / vault). The agent has
no PayFast merchant credentials and no passphrase — by design.

Base URL: `https://lambchamps.jh.frappe.cloud/api/method/payfast_gateway.payfast_gateway.api.`

## 2. The four tools to register

### 2.1 `create_payment_link` — POST
Call when a booking is confirmed and its Sales Invoice is **submitted**.

Request body:
```json
{
  "reference_doctype": "Sales Invoice",
  "reference_name": "ACC-SINV-2026-00042",
  "amount": 1250.00,
  "currency": "ZAR",
  "customer": "CUST-0001",
  "item_name": "Booking 12 Jul",
  "email_address": "guest@example.com",
  "name_first": "Thandi",
  "whatsapp_number": "+27821234567",
  "conversation_id": "wa_conv_abc123"
}
```
`reference_doctype`, `reference_name`, `amount` are required; the rest are
optional but `whatsapp_number` + `conversation_id` should always be sent (they
are stored for reconciliation and echoed in the paid event).

Response (`message` key in Frappe's envelope):
```json
{
  "ok": true,
  "payment_url": "https://lambchamps.jh.frappe.cloud/pf?token=…",
  "payment_log": "PFLOG-00042",
  "m_payment_id": "PFM-00042",
  "payment_status": "Awaiting Payment",
  "expires_at": "2026-07-02 09:05:00"
}
```
Send `payment_url` to the customer as a plain tappable URL. Links expire
(default 60 min). Calling again for the same invoice+amount while a link is
still active returns the SAME link (safe to retry).

Errors to handle: integration disabled; invoice not submitted; amount exceeds
outstanding; non-ZAR currency. Surface a friendly message and alert ops.

### 2.2 `get_payment_status` — GET
Query params: `payment_log=PFLOG-00042` or `m_payment_id=PFM-00042`.

Key response fields:
```json
{
  "ok": true,
  "payment_status": "paid",
  "booking_status": "confirmed",
  "amount": 1250.0,
  "currency": "ZAR",
  "pf_payment_id": "1234567",
  "reference_name": "ACC-SINV-2026-00042",
  "paid_at": "2026-07-02 08:41:03",
  "expires_at": "2026-07-02 09:05:00"
}
```

`payment_status` values (lowercase, normalized for the agent):
| Value | Meaning | Agent action |
|---|---|---|
| `awaiting_payment` | link live, not paid yet | wait / gentle reminder |
| `paid` | verified by all 4 PayFast checks; Payment Entry booked | send confirmation |
| `expired` | link TTL passed | offer `regenerate_payment_link` |
| `failed` | payment failed or was cancelled | offer a fresh link |
| `manual_review` | verification anomaly; humans notified | tell customer payment is being reviewed; do NOT confirm the booking |

`booking_status` is the same signal collapsed to
`awaiting_payment / confirmed / cancelled`.

### 2.3 `regenerate_payment_link` — POST
Body: `{"payment_log": "PFLOG-00042"}` (or `m_payment_id`), optional new
`"amount"`. Cancels the old link and returns a fresh
`create_payment_link`-shaped response. Refuses if already paid.
Use when: link expired, customer asks again later, or amount changed.

### 2.4 `cancel_payment_request` — POST
Body: `{"payment_log": "PFLOG-00042", "reason": "customer cancelled booking"}`.
Refuses if already paid. Use when the customer abandons the booking.

## 3. Conversation flow (recommended graph)

```
booking confirmed & invoice submitted
        │
        ▼
create_payment_link ──► send payment_url on WhatsApp
        │
        ▼
poll get_payment_status every 20–30s while conversation active
(also poll on any inbound customer message, and once at expires_at)
        │
   ┌────┴──────────┬───────────────┬────────────────┐
   ▼               ▼               ▼                ▼
 "paid"        "expired"       "failed"      "manual_review"
confirm the   offer new link  offer new     "we're checking your
booking; send (regenerate)    link          payment, an agent will
receipt msg                                 confirm shortly" — never
                                            confirm the booking
```

Push instead of poll (optional): on verified payment ERPNext publishes a
realtime event `payfast_payment_confirmed` (Frappe socketio, payload includes
`payment_log`, `reference_name`, `customer_mobile`, `conversation_id`). If
your middleware can hold a socketio connection to the site, subscribe and use
polling only as fallback. Otherwise polling is fine — it is cheap and the
status endpoint is authoritative.

## 4. Hard rules for the agent (prompt-level guardrails)

Put these in the agent's system prompt / tool policy:

1. **Only `get_payment_status` == "paid" means paid.** A customer saying "I
   paid", sending a screenshot, or returning from the payment page is NEVER
   proof. If they claim payment but status isn't `paid`, say the payment is
   still being confirmed and re-check shortly.
2. Never promise a confirmed booking before `paid` / `confirmed`.
3. Never quote or request card details in chat — payment happens only on the
   PayFast page behind the link.
4. `manual_review` is a terminal state for the agent: hand off to a human,
   don't retry links against the same payment.
5. Amounts are ZAR with 2 decimals; the amount must match what the invoice
   says is owed (full or agreed partial).
6. Don't spam links: reuse the active one (the API already returns the same
   link while it's valid); regenerate only on expiry/failure or explicit
   customer request.

## 5. Failure handling

| Situation | What the agent does |
|---|---|
| API 4xx with message | relay a friendly version, alert ops channel |
| API 5xx / timeout | retry with backoff (calls are safe to retry) |
| Customer paid but status stays `awaiting_payment` >5 min | tell customer confirmation is pending; ops checks the PayFast Payment Log — do not confirm |
| Link expired mid-conversation | `regenerate_payment_link`, send new URL |
| Booking cancelled before payment | `cancel_payment_request` with a reason |

## 6. Quick test from the agent side

```bash
# 1. create
curl -s -X POST "$BASE/create_payment_link" -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"reference_doctype":"Sales Invoice","reference_name":"<SINV>","amount":10.00,"currency":"ZAR","whatsapp_number":"+27820000000","conversation_id":"test_conv_1"}'
# 2. open payment_url, pay on the sandbox page
# 3. status
curl -s "$BASE/get_payment_status?payment_log=<PFLOG>" -H "$AUTH"
# expect payment_status "paid" and booking_status "confirmed"
```
