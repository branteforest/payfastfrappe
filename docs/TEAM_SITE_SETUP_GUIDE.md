# PayFast Gateway — Site Setup Guide (for the ERPNext team)

Definitive checklist for configuring PayFast payments on the Frappe/ERPNext site
(`lambchamps.jh.frappe.cloud`). The app (`payfast_gateway` v0.1.0) is already
deployed and installed. Work through this top to bottom.

---

## 1. Get PayFast credentials

**Sandbox (start here):**
- Public shared sandbox: Merchant ID `10000100`, Merchant Key `46f0cd694581a`
  (no passphrase), or
- Your own sandbox account at https://sandbox.payfast.co.za — preferred, so you
  control the passphrase and can see transactions in the sandbox dashboard.

**Live (later):** Merchant ID + Merchant Key from the PayFast merchant
dashboard (https://www.payfast.co.za), and set a **passphrase** in
PayFast Settings > Security there. For live, a passphrase is strongly
recommended and must match exactly what you enter in ERPNext.

## 2. Configure PayFast Settings

Open: `https://lambchamps.jh.frappe.cloud/app/payfast-settings`
(or ⌘+G and search "PayFast Settings").

| Field | Value | Notes |
|---|---|---|
| Environment | `Sandbox` | Switch to `Live` only after the go-live checklist (§7) |
| Sandbox Merchant ID / Key | from §1 | |
| Sandbox Passphrase | only if set in your sandbox account | Leave empty otherwise — an empty passphrase must stay empty on BOTH sides |
| Clearing Account | e.g. a "PayFast Clearing" bank-type account | Payment Entries post into this account (`paid_to`). Create one under Chart of Accounts if needed |
| Mode of Payment | `PayFast` | Created automatically by the app |
| Link Expiry Minutes | default 60 | TTL of payment links |
| Allowed Source Hosts | leave empty | Only for overrides; PayFast's official notify hosts are built in |
| Notify / Return / Cancel URL | leave empty | Sensible defaults are derived from the site URL |
| Enable Debug Logging | optional | Verbose non-secret logs |
| **Enabled** | tick **last** | Refuses to save enabled without credentials, by design |

Save. If saving with Enabled fails, fill in the missing credentials it names.

**Security rules (non-negotiable):**
- The passphrase never leaves this form. Never paste it into chat tools,
  scripts, or the agent's config.
- Nobody ever marks a payment "paid" by hand. The system blocks marking a
  Payment Log `Complete` without a Payment Entry; payments only complete via
  verified PayFast ITNs (4 security checks).

## 3. Create the agent API user

The LangGraph agent needs its own locked-down user:

1. **Users** > New: e.g. `payments-agent@yourdomain.com`, User Type "System User".
2. Roles: give it **PayFast Agent** only (plus nothing else beyond defaults).
3. On the user form > API Access > **Generate Keys**. Record the
   `api_key` + `api_secret` — this pair goes to the agent team (§ Agent guide).
4. Do NOT give this user System Manager, Accounts, or desk access beyond
   what's default. It cannot read PayFast Settings and never sees the passphrase.

## 4. Business prerequisites per payment

For a payment link to be issued against a booking:
- A **Customer** exists.
- A **Sales Invoice** for the booking exists and is **submitted** (docstatus 1).
  Draft invoices are rejected by design.
- Currency is **ZAR**. Amount requested must be > 0 and ≤ the invoice
  outstanding (partial payments are allowed and book at the paid amount).

## 5. Test the full sandbox flow (acceptance test)

1. Create + submit a test Sales Invoice (e.g. R10).
2. Create a link (from a terminal, replacing key:secret):

```bash
curl -s -X POST \
  "https://lambchamps.jh.frappe.cloud/api/method/payfast_gateway.payfast_gateway.api.create_payment_link" \
  -H "Authorization: token <api_key>:<api_secret>" \
  -H "Content-Type: application/json" \
  -d '{"reference_doctype":"Sales Invoice","reference_name":"<SINV-NAME>","amount":10.00,"currency":"ZAR"}'
```

3. Open the returned `payment_url` in a browser — you should land on the
   PayFast sandbox payment page (auto-redirect from `/pf`).
4. Pay with the sandbox buyer (sandbox site shows test buyer credentials).
5. Verify, in this order:
   - **PayFast Payment Log** list: the log turns `Complete`, all four check
     boxes (signature/source/amount/server) ticked, `payment_entry` linked.
   - **Payment Entry**: submitted, `reference_no` = the PayFast payment ID,
     posted to the clearing account.
   - **Sales Invoice**: outstanding 0 / status Paid (or Partly Paid for a
     partial amount).
6. If the log lands in **Manual Review** instead: open it, read
   `review_reason`, check the Error Log. Common causes: passphrase mismatch
   (sandbox account has one but Settings doesn't, or vice versa), or the
   sandbox not sending ITNs (use your own sandbox account, which does).

## 6. Day-to-day operations

- **Monitoring:** PayFast Payment Log list view. Filter `status = Manual Review`
  — these are payments needing a human. System Managers also get an email
  alert for each. `ERP Sync Failed` rows are retried automatically every
  10 minutes (up to 10 attempts, then escalated to Manual Review).
- **Reconciliation:** find any payment by invoice name, customer mobile, or
  PayFast payment ID (`pf_payment_id`) in the log list. The raw ITN payload is
  stored on every log.
- **Customer didn't pay / link expired:** the agent regenerates the link; no
  admin action needed.
- **Emergency stop:** untick **Enabled** in PayFast Settings. New links are
  refused; incoming ITNs are still stored (and routed to Manual Review) but
  never processed while disabled.
- **Never** delete Manual Review / ERP Sync Failed logs — deletion is blocked
  because they may represent money already received.

## 7. Go-live checklist

1. Sandbox acceptance test (§5) passes end-to-end.
2. Live PayFast account verified; **passphrase set** in the PayFast dashboard.
3. In PayFast Settings: enter Live Merchant ID/Key/Passphrase, switch
   Environment to `Live`, Save.
4. Make one small real payment (e.g. R5 invoice) and verify §5 checks.
5. Confirm the clearing account reconciles against the PayFast settlement.
