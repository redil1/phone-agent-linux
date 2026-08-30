import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class PhoneAgentCampaign(Document):
    def validate(self) -> None:
        if self.window_start and self.window_end and self.window_start >= self.window_end:
            frappe.throw(_("Calling window end must be after its start"))
        if self.max_daily_calls <= 0 or self.max_attempts <= 0 or self.retry_minutes <= 0:
            frappe.throw(_("Campaign limits must be positive"))
        if self.status == "Active" and not self.consent_basis:
            frappe.throw(_("A reviewed lawful contact basis is required before activation"))
        if self.has_value_changed("status") and self.status == "Active":
            self.activated_by = frappe.session.user
            self.activated_at = now_datetime()
