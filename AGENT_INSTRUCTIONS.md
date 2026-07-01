# Agent Instructions — PayFast Gateway Improvements

Handoff for an implementing agent. Read this entire document before changing code.

## Context

Repository: `/Users/andrestrauss/payfast`  
App: `payfast_gateway` (Frappe/ERPNext custom app)  
Python package root: `payfast_gateway/payfast_gateway/`

The core PayFast hosted-redirect + ITN integration is **already implemented** and tested. Your job is to close the gaps between the v1 implementation spec and the current codebase so the WhatsApp/LangGraph agent layer can integrate cleanly.

**Do not rewrite the app from scratch.** Extend what exists. Match existing naming, patterns, and test style.

### Scope agreement (code review + gap analysis)

The tasks below match the highest-impact gaps from an end-to-end review of the current codebase. **Agree and implement P0 + P1 first** — they unblock the WhatsApp/LangGraph agent layer. P2/P3 are valuable but not blocking for first agent integration.

**Intentionally deferred** (document only; do not implement unless explicitly asked):

- CI/CD pipeline (no `.github/workflows` today)
- ITN replay job for payloads received while `enabled=0`
- Full signature-order storage separate from JSON (only needed if production ITNs fail check #1)
- LangGraph agent code (lives in a separate repo — see § LangGraph)

**Recommended hardening** (P1.5 — see dedicated section): small fixes that reduce production footguns but are not agent-blockers.

### How to run this handoff

| Mode | When to use | Prompt |
|------|-------------|--------|
| **Single session** | One agent, one branch, P0→P1→P3 | Paste the “Full P0+P1” prompt below |
| **Split PRs** | Reviewable chunks, separate merges | Use `docs/instructions/P0.md` then `P1.md` (create from sections below) |
| **Ops-only** | No code; bench + nginx setup | Point implementer at root `README.md` + `docs/AGENT_DEV_GUIDE.md` |

**Full P0+P1 prompt** (paste to implementing agent):

```
Read /Users/andrestrauss/payfast/AGENT_INSTRUCTIONS.md and implement all P0 and P1 tasks in order, then P1.5 if time permits. Follow existing code conventions, extend tests under payfast_gateway/payfast_gateway/tests/, run `bench --site <site> run-tests --app payfast_gateway`, and update README (P3) for anything you ship. Do not rewrite the app. Do not commit unless I ask.
```

**P0-only prompt** (first PR):

```
Read AGENT_INSTRUCTIONS.md § P0 only. Implement the realtime event and /pf-return + /pf-cancel pages with tests. Do not start P1. Do not commit unless I ask.
```

**P1-only prompt** (second PR, after P0 merged):

```
Read AGENT_INSTRUCTIONS.md § P1 and P1.5. Implement auto-default URLs, agent API aliases, ITN raw-body validate, and P1.5 hardening. P0 is already merged. Do not commit unless I ask.
```

---

## Critical invariants (never break)

1. Payment is confirmed **only** after ITN passes all four checks **and** `payment_status == COMPLETE`.
2. `return_url` / WhatsApp messages are **never** proof of payment.
3. ITN endpoint **always** returns HTTP 200 after storing raw payload and enqueuing processing.
4. Raw ITN payload is stored **before** verification.
5. Never create two Payment Entries for one `pf_payment_id`.
6. `merchant_key` and `passphrase` must **never** appear in logs or error messages.
7. Preserve backward compatibility where noted (e.g. `reference_docname` alias, `m_payment_id` params).

---

## Task priority

| Priority | Task | Why |
|----------|------|-----|
| P0 | Emit `payfast_payment_confirmed` realtime event | WhatsApp layer depends on it |
| P0 | Add `/pf-return` and `/pf-cancel` pages | Spec + PayFast redirect UX |
| P1 | Auto-default URLs in settings / link creation | Removes manual misconfiguration |
| P1 | Align agent API contract (`get_payment_status`, regenerate/cancel) | LangGraph tools match spec |
| P1 | Fix ITN server-validate to POST raw body | PayFast validate may reject re-encoded dicts |
| P2 | Scheduled link expiry job | Cleanup stale `Awaiting Payment` logs |
| P2 | Sales Order advance Payment Entry | Spec §10; currently Manual Review only |
| P3 | Update README | Document new endpoints and agent integration |

Implement P0 first. Do not start P2 Sales Order until P0/P1 are done and tests pass.

---

## P0 — Realtime payment confirmation event

### Goal

When a verified payment is marked complete, emit the event the WhatsApp layer subscribes to.

### Files

- `payfast_gateway/payfast_gateway/services/itn.py`

### Change

Add a helper (e.g. `_publish_paid_event(log)`) and call it from `_mark_complete` **after** the log is saved successfully (Payment Entry exists, `status = Complete`, `processed = 1`).

Payload (match spec §9):

```python
frappe.publish_realtime("payfast_payment_confirmed", {
    "payment_log": log.name,
    "reference_doctype": log.reference_doctype,
    "reference_name": log.reference_docname,  # internal field name; see P1 alias note
    "customer_mobile": log.customer_mobile,
    "conversation_id": log.whatsapp_conversation_id,
})
```

Use `reference_docname` as the value for `reference_name` in the event payload (the log DocType uses `reference_docname`, not `reference_name`).

Do **not** emit on failed/cancelled ITNs or Manual Review.

### Tests

Add to `payfast_gateway/payfast_gateway/tests/test_itn.py`:

- Mock or capture `frappe.publish_realtime`.
- Assert it is called once with the expected payload when a COMPLETE ITN succeeds.
- Assert it is **not** called on signature failure or FAILED status.

Run: `bench --site <site> run-tests --app payfast_gateway`

---

## P0 — Return and cancel web pages

### Goal

Provide informational pages at `/pf-return` and `/pf-cancel`. They must **not** change payment state (no DB writes).

### Files to create

```
payfast_gateway/payfast_gateway/www/pf-return/index.html
payfast_gateway/payfast_gateway/www/pf-cancel/index.html
```

Optional minimal `index.py` for each if you need `context.no_cache = 1`; HTML-only is fine.

### Content requirements

- Simple, mobile-friendly message.
- `/pf-return`: “Payment submitted — we’ll confirm once PayFast notifies us.” (or similar)
- `/pf-cancel`: “Payment cancelled — you can request a new link from your agent.”
- No JavaScript that calls APIs or updates PayFast Payment Log.
- Match styling loosely with `www/pf/index.html` (card layout).

Frappe serves these automatically from `www/<route>/index.html` (see existing `/pf` pattern in `hooks.py` comment).

### Tests

Add a lightweight test in `test_payment_link.py` or a new `test_www_pages.py`:

- Import/get_context if py files exist, or assert template files exist and contain no form POST to PayFast.

---

## P1 — Auto-default URLs

### Goal

Reduce operator error: default `notify_url`, `return_url`, and `cancel_url` when blank.

### Files

- `payfast_gateway/payfast_gateway/doctype/payfast_settings/payfast_settings.py`
- `payfast_gateway/payfast_gateway/api.py` (`_build_redirect_payload`)

### Change

Add helpers in `payfast_settings.py`:

```python
from frappe.utils import get_url

def get_notify_url(settings=None):
    s = settings or get_settings()
    return s.notify_url or get_url("/api/method/payfast_gateway.payfast_gateway.api.payfast_itn")

def get_return_url(settings=None):
    s = settings or get_settings()
    return s.return_url or get_url("/pf-return")

def get_cancel_url(settings=None):
    s = settings or get_settings()
    return s.cancel_url or get_url("/pf-cancel")
```

Use these in `_build_redirect_payload` instead of raw `settings.return_url or ""` etc.

Keep explicit settings values when the operator has filled them in (settings override defaults).

### DocType

In `payfast_settings.json`, change `notify_url` **`reqd`: 0** (remove required) since a default now exists. Update field descriptions to mention auto-default behaviour.

### Tests

- `test_create_payment_link` with empty `notify_url` / `return_url` / `cancel_url` in settings still produces a valid redirect payload containing the defaulted URLs.

---

## P1 — Agent API contract alignment

### Goal

LangGraph tools in spec §13 expect certain request/response shapes. Extend the API without breaking existing callers.

### Files

- `payfast_gateway/payfast_gateway/api.py`

### 1. `get_payment_status`

**Keep** existing fields. **Add** spec-compatible aliases:

| Spec field | Current | Action |
|------------|---------|--------|
| `reference_name` | `reference_docname` | Include both in response (same value) |
| `booking_status` | read from reference doc `status` | **Also** derive from log status per spec: `paid`→`confirmed`, `failed`→`cancelled`, else `awaiting_payment` |
| `paid_at` | exists | Keep |
| `amount` | `amount` | Keep |
| `payment_status` | PayFast raw field on log | **Also** expose normalized lowercase log lifecycle: map internal `status` → `awaiting_payment` / `paid` / `failed` / `expired` / `manual_review` |

Suggested mapping from internal `log.status`:

| Internal `status` | Normalized `payment_status` | `booking_status` |
|-------------------|----------------------------|------------------|
| Awaiting Payment, Processing | `awaiting_payment` | `awaiting_payment` |
| Complete | `paid` | `confirmed` |
| Failed, Cancelled | `failed` | `cancelled` |
| Manual Review, ERP Sync Failed | `manual_review` | `awaiting_payment` |

Return **both** the normalized fields and legacy fields so existing tests keep passing.

### 2. `regenerate_payment_link`

Accept **`payment_log`** (spec) in addition to existing **`m_payment_id`**:

```python
@frappe.whitelist(methods=["POST"])
def regenerate_payment_link(m_payment_id=None, payment_log=None, amount=None):
    if payment_log and not m_payment_id:
        m_payment_id = frappe.db.get_value("PayFast Payment Log", payment_log, "m_payment_id")
    ...
```

### 3. `cancel_payment_request`

Same pattern: accept `payment_log` **or** `m_payment_id`.

### Tests

Extend `test_payment_link.py`:

- `get_payment_status` returns `reference_name` and spec-style `booking_status` when log is Complete.
- `regenerate_payment_link(payment_log=...)` works.
- `cancel_payment_request(payment_log=...)` works.

---

## P1 — ITN server-validate: POST raw body

### Problem

Spec §9 posts the **raw** `application/x-www-form-urlencoded` body to PayFast validate. Current code in `_server_validate` posts a parsed `dict`, which can reorder/re-encode fields and cause `INVALID`.

### Files

- `payfast_gateway/payfast_gateway/api.py` — pass raw body through enqueue
- `payfast_gateway/payfast_gateway/services/itn.py` — `_server_validate`, `process_itn`

### Change

1. In `payfast_itn`, when enqueuing `process_itn`, also pass `raw_body=body` (the original string from `req.get_data()` before JSON conversion).

2. Store raw body string on the log for retries:
   - Add field `raw_itn_body` (Long Text, read-only) to `PayFast Payment Log`, **or**
   - Reconstruct from ordered pairs when storing (prefer storing the exact raw string in `_store_raw_on_existing`).

3. Update `_server_validate(raw_body, validate_url)`:

```python
resp = requests.post(
    validate_url,
    data=raw_body,  # str, not dict
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    timeout=30,
)
```

4. Keep signature verification on parsed ordered fields as today.

5. Update `retry_erp_sync` to use stored raw body for re-validation if needed.

### Tests

- In `test_itn.py` or `test_itn_endpoint.py`, assert `_server_validate` receives the same byte string PayFast posted (mock `requests.post` and inspect `data=` argument).

---

## P1.5 — Production hardening (recommended, same PR as P1 or follow-up)

These items came from end-to-end review. Small diffs; high operational value.

### 1. Sandbox redirect URL fallback

**Problem:** `www/pf/index.py` uses `log.process_url or get_settings().live_process_url` — empty `process_url` sends sandbox payments to **live** PayFast.

**Fix:** Use environment-aware default from `get_credentials()["process_url"]`, not hard-coded live URL.

**File:** `payfast_gateway/payfast_gateway/www/pf/index.py`

**Test:** In `test_payment_link.py`, log with empty `process_url` in Sandbox settings → context uses sandbox process URL.

### 2. Link amount vs invoice outstanding

**Problem:** `create_payment_link` accepts any `amount > 0` with no check against Sales Invoice `outstanding_amount`.

**Fix:** When `reference_doctype == "Sales Invoice"`, reject if `amount > outstanding_amount + 0.01` (or allow partial pay explicitly — document behaviour in test name).

**File:** `payfast_gateway/payfast_gateway/api.py`

**Test:** SI with outstanding 100 → `amount=150` throws; `amount=50` succeeds (partial pay).

### 3. Regenerate guard for in-flight ITNs

**Problem:** `regenerate_payment_link` allows regeneration when log is `Processing` — can mint a second link while first ITN is in flight.

**Fix:** Also reject when `status == "Processing"` (same message pattern as Complete).

**File:** `payfast_gateway/payfast_gateway/api.py`

**Test:** Set log to `Processing` → regenerate throws.

### 4. Declare `requests` dependency

**Problem:** `itn.py` imports `requests` but `pyproject.toml` only lists `frappe`.

**Fix:** Add `requests` to `[project] dependencies` in `pyproject.toml`.

### 5. Update SI `payfast_status` on link lifecycle (optional)

**Problem:** Custom field `payfast_status` on Sales Invoice is only set to `Complete` on success.

**Fix (minimal):** Set `Awaiting Payment` when link is created (in `_create_log` or after insert in `create_payment_link`); set `Cancelled` when link is cancelled/expired. Use `db_set` + `allow_on_submit` pattern already in `_confirm_reference`.

**Files:** `api.py`, optionally `services/expiry.py` (P2)

---

## P2 — Scheduled link expiry

### Goal

Spec §12: cron job marks stale `Awaiting Payment` logs as expired.

### Files to create

- `payfast_gateway/payfast_gateway/services/expiry.py`

### Files to edit

- `payfast_gateway/hooks.py`

### Implementation

```python
def expire_stale_links():
    settings = get_settings()
    cutoff = add_to_date(now_datetime(), minutes=-cint(settings.link_expiry_minutes, 60))
    stale = frappe.get_all(
        "PayFast Payment Log",
        filters={"status": "Awaiting Payment", "creation": ["<", cutoff]},
        pluck="name",
    )
    for name in stale:
        frappe.db.set_value("PayFast Payment Log", name, "status", "Cancelled")
    if stale and not frappe.flags.in_test:
        frappe.db.commit()
```

**Note:** Current code uses `Cancelled` not `expired` for inactive links. Stay consistent with existing `/pf` page behaviour (`index.py` sets `Cancelled` on expiry). Optionally map `Cancelled` + past `expires_at` to normalized `expired` in `get_payment_status` only.

Add to `hooks.py` scheduler (same `*/10 * * * *` cron as ERP retry, or a separate entry):

```python
"*/10 * * * *": [
    "payfast_gateway.payfast_gateway.services.itn.retry_erp_sync",
    "payfast_gateway.payfast_gateway.services.expiry.expire_stale_links",
],
```

### Tests

- Create log with backdated `creation` (or mock `now_datetime`), run `expire_stale_links`, assert status becomes `Cancelled`.

---

## P2 — Sales Order advance Payment Entry (optional v1)

### Current behaviour

`create_payment_link` accepts Sales Order, but `_complete_payment` in `itn.py` sends SO payments to Manual Review.

### Goal (spec §10)

Use ERPNext's `get_payment_entry` for Sales Order → advance Payment Entry.

### Files

- `payfast_gateway/payfast_gateway/services/itn.py` — `_create_payment_entry`, `_complete_payment`

### Approach

```python
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

def _create_payment_entry(log):
    settings = get_settings()
    pe = get_payment_entry(log.reference_doctype, log.reference_docname)
    pe.mode_of_payment = settings.mode_of_payment
    if settings.clearing_account:
        pe.paid_to = settings.clearing_account
    pe.reference_no = log.pf_payment_id or log.m_payment_id
    pe.reference_date = now_datetime().date()
    pe.insert(ignore_permissions=True)
    pe.submit()
    return pe.name
```

Remove the Sales Invoice-only guard in `_complete_payment` **or** branch: SI → allocated PE via current logic; SO → `get_payment_entry` advance PE.

Keep submitted-reference checks (`docstatus == 1`).

### Tests

Extend `test_payment_entry.py` with a submitted Sales Order scenario (skip if ERPNext test fixtures unavailable).

---

## P3 — README updates

After code changes, update `README.md` with:

1. Agent API method paths (full dotted path).
2. `payfast_payment_confirmed` event payload and when it fires.
3. Default URL behaviour.
4. `/pf-return`, `/pf-cancel` routes.
5. Agent tool example using `payment_log` parameter.

Do **not** duplicate the full spec — link to this file for implementers.

---

## LangGraph agent layer (separate repo — document only)

Do **not** implement LangGraph in this repo unless asked. Provide this snippet in README for the WhatsApp agent repo:

```python
ERP = os.environ["ERPNEXT_URL"]
AUTH = {"Authorization": f"token {os.environ['ERP_API_KEY']}:{os.environ['ERP_API_SECRET']}"}

def create_payment_link_tool(...):
    r = requests.post(
        f"{ERP}/api/method/payfast_gateway.payfast_gateway.api.create_payment_link",
        headers=AUTH,
        json={...},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["message"]
```

ITN endpoint (for nginx config):

```
/api/method/payfast_gateway.payfast_gateway.api.payfast_itn
```

Agent guardrail: **no tool** may set payment/booking status to paid; only read status and create links.

---

## What NOT to change

- Do not flatten `payfast_gateway/payfast_gateway/` package structure.
- Do not remove rate limiting, XFF client-IP logic, ERP sync retry, or audit JSON — they are intentional improvements over the spec skeleton.
- Do not change signature algorithm unless PayFast official tester fails.
- Do not rename DocType fields (`reference_docname`, internal `status`) — add aliases in API responses only.
- Do not commit secrets or `.env` files.
- Do not create git commits unless explicitly requested.

---

## Verification checklist

Before marking work complete:

```
[ ] bench --site <site> migrate   (if DocType fields added)
[ ] bench --site <site> run-tests --app payfast_gateway   (all green)
[ ] create_payment_link → payment_url opens /pf → form fields present
[ ] notify_url defaults correctly when blank in settings
[ ] pf-return and pf-cancel load over HTTPS (no state changes)
[ ] Simulated COMPLETE ITN → one Payment Entry → publish_realtime fired
[ ] regenerate_payment_link(payment_log=...) and cancel_payment_request(payment_log=...) work
[ ] Server validate uses raw POST body (inspect mock or debug log)
[ ] README updated
[ ] (P1.5) Sandbox /pf redirect uses sandbox process URL when log.process_url empty
[ ] (P1.5) create_payment_link rejects amount > SI outstanding
[ ] (P1.5) regenerate blocked when status is Processing
```

---

## Split into separate instruction files?

**Recommendation:** Keep this file as the **single source of truth**. Optionally extract sections into `docs/instructions/P0.md`, `P1.md`, `P2.md` **only if** you want parallel agents or strict one-PR-per-priority review — not one file per task (too granular for ~26 files of app code).

| Approach | Pros | Cons |
|----------|------|------|
| One `AGENT_INSTRUCTIONS.md` (current) | No drift; one agent runs P0→P1 sequentially | Long doc |
| Split by priority (`P0.md`, `P1.md`, `P2.md`) | Clean PR boundaries; easier review | Must duplicate invariants + “do not break” in each file, or link back here |
| Split per task | — | Overkill; shared context repeated 8× |

If splitting: each child file must start with “Read `AGENT_INSTRUCTIONS.md` § Critical invariants first” and link to the master checklist.

---

## Reference — key file map

| Concern | File |
|---------|------|
| Agent API | `payfast_gateway/payfast_gateway/api.py` |
| ITN processing | `payfast_gateway/payfast_gateway/services/itn.py` |
| Signatures | `payfast_gateway/payfast_gateway/services/signature.py` |
| Settings helpers | `payfast_gateway/payfast_gateway/doctype/payfast_settings/payfast_settings.py` |
| Redirect page | `payfast_gateway/payfast_gateway/www/pf/index.py` |
| Scheduler | `payfast_gateway/hooks.py` |
| Tests | `payfast_gateway/payfast_gateway/tests/` |

---

## Suggested commit message (when user asks)

```
Close WhatsApp integration gaps: payment event, return/cancel pages, agent API aliases, and ITN validate raw body.
```
