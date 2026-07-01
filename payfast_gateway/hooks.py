app_name = "payfast_gateway"
app_title = "PayFast Gateway"
app_publisher = "PayFast Gateway"
app_description = "PayFast hosted-redirect + ITN integration for Frappe/ERPNext."
app_icon = "octicon octicon-credit-card"
app_color = "grey"
app_email = "dev@payfast.local"
app_license = "MIT"

fixtures = [
    {"doctype": "Role", "filters": [["name", "in", ["PayFast Agent"]]]},
    {"doctype": "Custom Field", "filters": [["dt", "in", ["Sales Invoice"]]]},
    {"doctype": "Mode of Payment", "filters": [["name", "in", ["PayFast"]]]},
]

# The /pf redirect page is served automatically from www/pf/index.{py,html};
# no website_route_rules entry is required.

scheduler_events = {
    "cron": {
        # Retry ERP sync for payments verified COMPLETE whose Payment Entry
        # creation previously failed (status "ERP Sync Failed").
        "*/10 * * * *": [
            "payfast_gateway.payfast_gateway.services.itn.retry_erp_sync",
        ],
    },
}
