import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import make_autoname


class PayFastPaymentLog(Document):
    def before_insert(self):
        if not self.redirect_token:
            self.redirect_token = frappe.generate_hash(length=32)
        if not self.m_payment_id:
            self.m_payment_id = make_autoname("PFM-.#####")

    def validate(self):
        self._guard_complete_requires_payment_entry()

    def _guard_complete_requires_payment_entry(self):
        """Core invariant: a payment is only ever "Complete" once the ITN
        pipeline has actually allocated a Payment Entry. This blocks a manual
        edit (Desk form, API, or script) from marking a payment Complete
        without one — the pipeline itself always sets both together.
        """
        if self.status == "Complete" and not self.payment_entry:
            frappe.throw(
                _(
                    "PayFast Payment Log cannot be marked Complete without a linked "
                    "Payment Entry. This status is set automatically once an ITN "
                    "passes all four verification checks."
                )
            )

    def on_trash(self):
        if self.processed and self.payment_entry:
            frappe.throw(
                f"Cannot delete processed PayFast Payment Log linked to {self.payment_entry}."
            )
        if self.status in ("Manual Review", "ERP Sync Failed"):
            frappe.throw(
                _(
                    "Cannot delete a PayFast Payment Log in {0} status — it may represent "
                    "money already received from PayFast that has not yet been reconciled. "
                    "Resolve it (or cancel it explicitly) before deleting."
                ).format(self.status)
            )
