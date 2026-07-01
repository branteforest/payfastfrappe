# PayFast Gateway — Comprehensive Review & Improvement Plan

**Date:** July 2026  
**Repository:** `/Users/andrestrauss/payfast`  
**Scope:** End-to-end review (no code changes), gap analysis, and consolidated handoff for implementing agents  
**Related docs:** [README](../README.md) · [AGENT_INSTRUCTIONS.md](../AGENT_INSTRUCTIONS.md) · [AGENT_DEV_GUIDE.md](./AGENT_DEV_GUIDE.md)

---

## 1. Executive summary

**PayFast Gateway** is a focused Frappe/ERPNext custom app (~28 files) that integrates South Africa’s PayFast hosted checkout and ITN (Instant Transaction Notification) webhooks with ERPNext accounting. The v1 booking primitive is **Sales Invoice first**: a verified ITN creates a submitted Payment Entry allocated directly against the invoice.

**Verdict:** The core payment path is **production-grade in design** — redirect signing, ITN ingestion, four mandatory verification checks, idempotent Payment Entry creation, ERP sync retries, and audit trails are thoughtfully implemented and well tested in isolation (~39 test cases). The app is suitable for staging and cautious production use **once ops configuration is correct**.

**Primary gaps** are not in low-level PayFast crypto but in **agent integration, customer-facing redirect UX, API contract alignment, and operational hardening**. Until P0/P1 improvements ship (documented in [AGENT_INSTRUCTIONS.md](../AGENT_INSTRUCTIONS.md)), the WhatsApp/LangGraph agent layer must **poll** `get_payment_status` and cannot rely on realtime payment confirmation or shipped return/cancel pages.

**Recommendation:** Implement **P0 + P1 + P1.5** before agent go-live. Defer P2 (expiry cron, Sales Order PE) unless product requires them immediately. Add CI and ITN replay as a later ops investment.

---

## 2. Application overview

| Aspect | Detail |
|--------|--------|
| **Type** | Frappe / ERPNext custom app (`bench get-app` + `install-app payfast_gateway`) |
| **Language** | Python ≥ 3.10 |
| **Packaging** | Flit (`pyproject.toml`); declares `frappe` only |
| **Database** | MariaDB via Frappe DocTypes (InnoDB) |
| **Frontend** | Minimal Jinja redirect page (`/pf`); thin Frappe Desk JS |
| **External deps** | PayFast hosted checkout, ITN webhook, server validate API; `requests` (undeclared) |
| **Deployment** | Existing Frappe bench + nginx reverse proxy; no Docker/CI in repo |

### Directory map

```
payfast/
├── README.md                          # Ops: invariants, nginx, install
├── AGENT_INSTRUCTIONS.md              # Implementing-agent handoff (P0–P3)
├── docs/
│   ├── AGENT_DEV_GUIDE.md             # LangGraph/WhatsApp integration guide
│   └── COMPREHENSIVE_REPORT.md        # This document
└── payfast_gateway/
    ├── hooks.py                       # Fixtures, scheduler (ERP retry every 10 min)
    └── payfast_gateway/
        ├── api.py                     # Agent API + guest ITN endpoint
        ├── services/
        │   ├── itn.py                 # ITN processing, PE creation, retries
        │   └── signature.py           # MD5 signing (PayFast spec)
        ├── doctype/
        │   ├── payfast_settings/      # Singleton config
        │   └── payfast_payment_log/   # Payment audit + state machine
        ├── www/pf/                    # Guest redirect to PayFast
        ├── fixtures/                  # Role, Mode of Payment, SI custom fields
        └── tests/                     # 5 test modules + 2 doctype tests
```

### Key DocTypes

| DocType | Role |
|---------|------|
| **PayFast Settings** (singleton) | Kill switch, sandbox/live credentials, URLs, clearing account, ITN allowlist |
| **PayFast Payment Log** | Full lifecycle: link creation → ITN → verification → Payment Entry |
| **Sales Invoice** (extended) | Custom fields `payfast_status`, `payfast_payment_log` via fixtures |

### API surface

| Method | Auth | Purpose |
|--------|------|---------|
| `create_payment_link` | PayFast Agent | Create/reuse signed payment link |
| `get_payment_status` | PayFast Agent | Poll payment + reference status |
| `regenerate_payment_link` | PayFast Agent | Cancel old link, mint new one |
| `cancel_payment_request` | PayFast Agent | Cancel outstanding link |
| `payfast_itn` | **Guest** | Receive PayFast ITN (always HTTP 200) |

Website route: `/pf?token=<redirect_token>` — auto-POST to PayFast.

---

## 3. End-to-end payment flow

```
Customer (WhatsApp)     Agent (LangGraph)              ERPNext (payfast_gateway)
       │                        │                                    │
       │  "Book + pay"            │                                    │
       ├───────────────────────►│  create Sales Invoice (ERP API)    │
       │                        ├───────────────────────────────────►│
       │                        │  create_payment_link               │
       │                        ├───────────────────────────────────►│
       │                        │◄──────── payment_url ──────────────┤
       │◄── "Pay here: {url}" ───┤                                    │
       │                        │                                    │
       │  opens /pf?token=…     │                                    │
       ├────────────────────────────────────────────────────────────►│ auto-POST → PayFast
       │                        │                                    │
       │                        │         PayFast ITN (server-to-server)
       │                        │                                    ◄── payfast_itn
       │                        │                                    │ store raw → enqueue
       │                        │                                    │ 4 checks → Payment Entry
       │                        │◄── payfast_payment_confirmed * ───┤  (* PLANNED — P0)
       │◄── "Payment received" ─┤                                    │
```

### Critical invariants (must never break)

1. An order is marked paid **only** after ITN passes **all four checks** AND `payment_status == COMPLETE`.
2. `return_url`, WhatsApp messages, and customer screenshots are **never** proof of payment.
3. The ITN endpoint **always** returns HTTP 200 after storing raw payload and enqueuing processing.
4. Raw ITN payload is stored **before** any verification.
5. **Never** two Payment Entries for one `pf_payment_id`.
6. `passphrase` is never transmitted or logged; `merchant_key` is never logged.
7. Duplicate/conflicting ITNs return 200 and do not double-process.

### Four mandatory ITN checks

| # | Check | Implementation |
|---|-------|----------------|
| 1 | MD5 signature | `services/signature.py` — field order preserved from POST |
| 2 | Source IP | DNS-resolved PayFast notify hosts + operator allowlist |
| 3 | Amount match | ±0.01 tolerance; gross/fee/net consistency |
| 4 | Server validate | POST to PayFast validate URL → must return `VALID` |

### Payment Log status machine

| Status | Meaning |
|--------|---------|
| `Awaiting Payment` | Link active; no verified ITN yet |
| `Processing` | ITN claimed; checks running |
| `Complete` | Verified + Payment Entry created |
| `Failed` / `Cancelled` | PayFast terminal status or link retired |
| `Manual Review` | Check failed or unsupported reference |
| `ERP Sync Failed` | Verified but PE creation failed; scheduler retries |

---

## 4. What works well

### Architecture

- Clean separation: `api.py` (HTTP), `itn.py` (business logic), `signature.py` (crypto).
- Row-lock pattern releases DB lock **before** outbound PayFast validate — avoids holding locks across network I/O.
- Idempotency: `processed` flag, `pf_payment_id` duplicate guard, Payment Entry lookup by `reference_no`.
- ERP sync failure path retains raw payload, increments `retry_count`, retries via scheduler (`MAX_ERP_RETRIES = 10`), then escalates to Manual Review.

### Security

- PayFast Agent role gating on all agent APIs; ITN is guest-only by design.
- Client IP from left-most public `X-Forwarded-For` (with README warning about proxy overwrite).
- Rate limiting on ITN (`120/min` per IP) with nginx defense-in-depth documented.
- Constant-time signature comparison; secrets in Frappe Password fields.
- Processed logs cannot be deleted when linked to a Payment Entry.

### Testing

~39 tests across signature encoding, link lifecycle, all four ITN checks, full guest endpoint path, real Payment Entry creation, duplicate ITN handling, ERP retry scheduler, and role gating. Tests use `FrappeTestCase` and require a live bench with ERPNext.

### Documentation

- README covers invariants, nginx config, install steps.
- `AGENT_DEV_GUIDE.md` gives LangGraph team integration patterns.
- `AGENT_INSTRUCTIONS.md` provides actionable implementer handoff.

---

## 5. Issues & risks

Issues are grouped by severity. Items marked **→ Fix** have a task in [AGENT_INSTRUCTIONS.md](../AGENT_INSTRUCTIONS.md).

### 5.1 Critical — agent / production blockers

| ID | Issue | Impact | Fix |
|----|-------|--------|-----|
| C1 | **No `payfast_payment_confirmed` realtime event** | Agent must poll indefinitely; poor UX, race conditions | **P0** |
| C2 | **Missing `/pf-return` and `/pf-cancel` pages** | PayFast redirect URLs must be hand-built; poor customer UX | **P0** |
| C3 | **Proxy misconfiguration enables IP spoofing** | If nginx appends (not overwrites) `X-Forwarded-For`, check #2 can be bypassed; checks #1/#4 may still block | Ops: README nginx config; document in runbook |
| C4 | **Kill switch leaves ITNs unprocessed** | `enabled=0` stores ITN but skips processing; no replay on re-enable | Deferred: ITN replay job |

### 5.2 High — correctness / money risk

| ID | Issue | Impact | Fix |
|----|-------|--------|-----|
| H1 | **Sales Order accepted but not auto-fulfilled** | Verified SO payments → Manual Review, not Payment Entry | **P2** (optional) or remove SO from API until supported |
| H2 | **No amount vs outstanding validation** | Overpayment can fail PE submit or create accounting errors | **P1.5** |
| H3 | **Multiple concurrent links per invoice** | Reuse only matches same amount; different amounts → multiple active links | Document agent behaviour; optional guard |
| H4 | **Sandbox redirect falls back to live URL** | Empty `process_url` on log sends sandbox traffic to live PayFast | **P1.5** |
| H5 | **ITN validate POSTs parsed dict, not raw body** | Field reorder/re-encode may cause check #4 to fail in production | **P1** |
| H6 | **Signature verify uses JSON round-trip order** | `json.loads` dict order usually preserved (Py 3.7+); reordering could break check #1 | Monitor; store raw body (P1 helps validate path) |

### 5.3 Medium — reliability / ops

| ID | Issue | Impact | Fix |
|----|-------|--------|-----|
| M1 | **`Processing` state can stall UX** | Worker crash mid-job; no timeout job | Acceptable; re-entry works via `processed=0` |
| M2 | **No scheduled link expiry** | Stale `Awaiting Payment` logs until visited or reused | **P2** |
| M3 | **ERP retry ~100 min max** | 10 retries × 10 min; then Manual Review | Document ops runbook |
| M4 | **Rate limit silently disabled** | If `frappe.rate_limiter` import fails, decorator is no-op | Log warning on fallback |
| M5 | **DNS cache per-worker** | 5 min TTL, not shared across gunicorn workers | Acceptable |
| M6 | **No CI pipeline** | Tests not run on push/PR | Deferred |

### 5.4 Medium — API / integration contract

| ID | Issue | Impact | Fix |
|----|-------|--------|-----|
| A1 | **`booking_status` misleading** | Returns ERPNext doc `status`, not normalized booking enum | **P1** |
| A2 | **`payfast_status` on SI rarely updated** | Only set to `Complete` on success | **P1.5** (optional) |
| A3 | **WhatsApp fields are metadata only** | Stored but not emitted in events yet | **P0** event payload |
| A4 | **Regenerate allowed during `Processing`** | Second link while first ITN in flight | **P1.5** |
| A5 | **`regenerate`/`cancel` lack `payment_log` param** | Spec expects it; only `m_payment_id` today | **P1** |
| A6 | **Manual URL configuration** | Empty notify/return/cancel URLs break or confuse setup | **P1** auto-defaults |

### 5.5 Low — packaging / maintainability

| ID | Issue | Impact | Fix |
|----|-------|--------|-----|
| L1 | **`requests` not in `pyproject.toml`** | Relies on transitive Frappe dep | **P1.5** |
| L2 | **ERPNext not declared as dependency** | Implicit; install fails without ERPNext | Document in README |
| L3 | **Empty `patches.txt`** | Schema via DocType JSON sync only | Acceptable for v1 |
| L4 | **Minimal Desk UX** | No SI form button, stub settings JS | Future enhancement |
| L5 | **No settings validation** | `notify_url` not verified to match ITN endpoint | **P1** defaults reduce risk |

---

## 6. Test coverage assessment

| Area | Status |
|------|--------|
| Signature encoding (`~`, spaces, field order, passphrase) | ✅ Covered |
| Link create / reuse / expiry / cancel / regenerate | ✅ Covered |
| All 4 ITN checks + FAILED/CANCELLED/PENDING/COMPLETE | ✅ Covered |
| Full guest ITN endpoint (mock request + inline enqueue) | ✅ Covered |
| Real Payment Entry creation + SI allocation | ✅ Covered |
| Duplicate ITN → single PE | ✅ Covered |
| ERP sync failure + scheduler retry | ✅ Covered |
| Role gating (PayFast Agent) | ✅ Covered |
| IP-based source validation (DNS) | ✅ Covered (skips if no DNS) |
| Real PayFast HTTP (live validate/ITN) | ❌ Mocked only |
| `/pf-return`, `/pf-cancel` | ❌ Not implemented |
| Realtime event | ❌ Not implemented |
| Sales Order payment path | ❌ Not covered |
| Overpayment / partial pay guards | ❌ Not covered |
| Concurrent ITN race (beyond duplicate retry) | ⚠️ Partial |

**Run tests:** `bench --site <site> run-tests --app payfast_gateway`

---

## 7. Improvement plan (consolidated)

All implementation detail lives in [AGENT_INSTRUCTIONS.md](../AGENT_INSTRUCTIONS.md). This section is the agreed priority summary.

### P0 — Agent blockers (implement first)

| Task | Files | Acceptance |
|------|-------|------------|
| Emit `payfast_payment_confirmed` via `frappe.publish_realtime` from `_mark_complete` | `services/itn.py` | Fires once on verified Complete; not on failure/review |
| Add `/pf-return` and `/pf-cancel` informational pages | `www/pf-return/`, `www/pf-cancel/` | Mobile-friendly; **no DB writes**; no API calls |
| Tests for both | `tests/test_itn.py`, `tests/test_www_pages.py` or `test_payment_link.py` | Mock `publish_realtime`; assert pages exist |

### P1 — Integration & correctness

| Task | Files | Acceptance |
|------|-------|------------|
| Auto-default `notify_url`, `return_url`, `cancel_url` | `payfast_settings.py`, `api.py` | Blank settings → sensible defaults from `get_url()` |
| Agent API aliases | `api.py` | `reference_name`, normalized `booking_status`/`payment_status`; `payment_log` param on regenerate/cancel |
| ITN validate POST raw body | `api.py`, `itn.py` | Pass original urlencoded string to PayFast validate; store for retries |
| Tests | `tests/test_payment_link.py`, `tests/test_itn*.py` | All existing tests still pass |

### P1.5 — Production hardening (recommended with P1)

| Task | Files |
|------|-------|
| Fix sandbox `/pf` process URL fallback | `www/pf/index.py` |
| Reject link amount > SI outstanding | `api.py` |
| Block regenerate when status is `Processing` | `api.py` |
| Declare `requests` in `pyproject.toml` | `pyproject.toml` |
| Update SI `payfast_status` on link create/cancel (optional) | `api.py` |

### P2 — Nice to have (defer until P0/P1 done)

| Task | Files |
|------|-------|
| Scheduled stale link expiry cron | `services/expiry.py`, `hooks.py` |
| Sales Order advance Payment Entry via `get_payment_entry` | `services/itn.py` |

### P3 — Documentation

Update [README](../README.md): agent API paths, event payload, default URLs, new www routes, example tool snippet.

### Intentionally deferred

- CI/CD (GitHub Actions or similar)
- ITN replay after kill-switch disable
- Separate ordered-field storage for signature (only if production ITNs fail check #1)
- LangGraph agent implementation (separate repo)

---

## 8. Production deployment checklist

### ERPNext / bench

- [ ] `bench get-app` + `install-app payfast_gateway` + `migrate`
- [ ] PayFast Settings: sandbox credentials first; clearing account; mode of payment; currency ZAR
- [ ] Dedicated user with **PayFast Agent** role + API key/secret for agent
- [ ] Frappe scheduler running (ERP retry every 10 min)
- [ ] HTTPS site URL reachable from public internet (PayFast ITN inbound)

### nginx (required)

```nginx
# Overwrite XFF — do NOT append
proxy_set_header X-Forwarded-For $remote_addr;

# Rate limit ITN endpoint
limit_req_zone $binary_remote_addr zone=payfast_itn:10m rate=10r/s;
location = /api/method/payfast_gateway.payfast_gateway.api.payfast_itn {
    limit_req zone=payfast_itn burst=20 nodelay;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_pass http://frappe-bench-frappe;
}
```

### Monitoring / ops

- [ ] Alert on Frappe Error Log titles: `PayFast ITN manual review`, `ERP sync failed`, `unknown m_payment_id`
- [ ] Runbook for Manual Review queue in PayFast Payment Log list
- [ ] Staging sandbox end-to-end test with real PayFast ITN before live credentials

### Agent integration (until P0 ships)

- [ ] Poll `get_payment_status` with exponential backoff — **do not** trust return URL or customer messages
- [ ] Never mutate PayFast Payment Log or set booking paid from agent code
- [ ] Store `ERPNEXT_URL`, `ERP_API_KEY`, `ERP_API_SECRET` in secrets manager

---

## 9. Handoff to implementing agent

### Single source of truth

**[AGENT_INSTRUCTIONS.md](../AGENT_INSTRUCTIONS.md)** — task specs, code sketches, tests, invariants, verification checklist.

**Do not split per task.** Optionally split by priority (`P0.md`, `P1.md`) only if using separate PRs; keep invariants duplicated or linked in each.

### Recommended prompt (full P0 + P1)

```
Read /Users/andrestrauss/payfast/AGENT_INSTRUCTIONS.md and implement all P0 and P1 tasks in order, then P1.5 if time permits. Follow existing code conventions, extend tests under payfast_gateway/payfast_gateway/tests/, run bench --site <site> run-tests --app payfast_gateway, and update README (P3) for anything you ship. Do not rewrite the app. Do not commit unless I ask.
```

### What the implementing agent must NOT change

- Package structure under `payfast_gateway/payfast_gateway/`
- Rate limiting, XFF client-IP logic, ERP sync retry, audit JSON array
- MD5 signature algorithm (PayFast-mandated)
- DocType field names (`reference_docname`, internal `status`) — API aliases only
- Critical invariants listed in §3 above

### Verification before merge

```
[ ] bench migrate (if DocType fields added)
[ ] bench run-tests --app payfast_gateway — all green
[ ] create_payment_link → /pf → form fields present
[ ] notify_url defaults when blank
[ ] /pf-return and /pf-cancel load (no state changes)
[ ] Simulated COMPLETE ITN → one PE → publish_realtime fired
[ ] regenerate/cancel with payment_log= works
[ ] Server validate uses raw POST body
[ ] P1.5: sandbox URL fallback, outstanding guard, Processing regenerate block
[ ] README updated
```

---

## 10. Document index

| Document | Audience | Purpose |
|----------|----------|---------|
| **This report** | Product, tech lead, reviewers | Full picture: review + risks + plan |
| [README.md](../README.md) | ERPNext operators | Install, invariants, nginx |
| [AGENT_INSTRUCTIONS.md](../AGENT_INSTRUCTIONS.md) | Implementing agent (Cursor/CI) | Actionable tasks P0–P3 |
| [AGENT_DEV_GUIDE.md](./AGENT_DEV_GUIDE.md) | LangGraph / WhatsApp team | API auth, tools, polling, guardrails |

---

## 11. Conclusion

PayFast Gateway delivers a **solid v1 core** for Sales Invoice payments through PayFast. The payment invariants are correct, the ITN pipeline is defensively designed, and test coverage is strong for a small codebase.

**Before WhatsApp agent go-live:** ship P0 (realtime event + return/cancel pages) and P1 (URL defaults, API contract, raw validate body). Add P1.5 hardening in the same pass. Treat nginx XFF configuration as a **security requirement**, not optional.

After P0/P1, the agent can subscribe to `payfast_payment_confirmed` instead of polling, customers get clear post-checkout pages, and operators face fewer misconfiguration failures — closing the gap between a working payment backend and a production-ready agent integration.
