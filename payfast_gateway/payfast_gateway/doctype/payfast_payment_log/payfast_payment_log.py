import frappe
from frappe.model.document import Document
from frappe.model.naming import make_autoname


class PayFastPaymentLog(Document):
    def before_insert(self):
        if not self.redirect_token:
            self.redirect_token = frappe.generate_hash(length=32)
        if not self.m_payment_id:
            self.m_payment_id = make_autoname("PFM-.#####")

    def on_trash(self):
        if self.processed and self.payment_entry:
            frappe.throw(
                f"Cannot delete processed PayFast Payment Log linked to {self.payment_entry}."
            )
