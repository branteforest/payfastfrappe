# PayFast Gateway

Custom Frappe/ERPNext app implementing the PayFast hosted-redirect + ITN (Instant
Transaction Notification) integration per the v1 developer spec.

## Booking primitive

v1 uses **Sales Invoice first**: a Payment Entry (Receive) is allocated directly
against the Sales Invoice when an ITN passes all four mandatory checks and
`payment_status == COMPLETE`.

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

## ITN source validation

The ITN receiver determines the real client IP (left-most public entry of
`X-Forwarded-For` when behind a proxy, else `REMOTE_ADDR`) and checks it against
the IPs resolved from the PayFast notify hosts (`www`, `w1w`, `w2w`,
`sandbox`.payfast.co.za) plus any operator-configured hosts/IPs in
**PayFast Settings → Allowed ITN Source Hosts**. DNS results are cached briefly.

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

## Documentation

| Document | Audience |
|----------|----------|
| [docs/AGENT_DEV_GUIDE.md](docs/AGENT_DEV_GUIDE.md) | **WhatsApp / LangGraph agent team** — API tools, flows, guardrails, message templates |
| [AGENT_INSTRUCTIONS.md](AGENT_INSTRUCTIONS.md) | Gateway implementers — P0–P2 code improvements |

## Install

```bash
bench get-app /Users/andrestrauss/payfast
bench --site <site> install-app payfast_gateway
bench --site <site> migrate
```
