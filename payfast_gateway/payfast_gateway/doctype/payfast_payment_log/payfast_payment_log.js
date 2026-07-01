frappe.ui.form.on("PayFast Payment Log", {
    refresh: function (frm) {
        if (frm.doc.status === "Manual Review") {
            frm.dashboard.show_alert(
                __("Manual review required: {0}", [frm.doc.review_reason]),
                10
            );
        }
    },
});
