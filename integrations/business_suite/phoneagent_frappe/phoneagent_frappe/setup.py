from __future__ import annotations

import secrets
from typing import Any

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import now_datetime

INTEGRATION_ROLE = "PhoneAgent Integration"
DEFAULT_USER = "phoneagent-integration@local.invalid"


def _ensure_role() -> None:
    if not frappe.db.exists("Role", INTEGRATION_ROLE):
        frappe.get_doc({"doctype": "Role", "role_name": INTEGRATION_ROLE}).insert(
            ignore_permissions=True
        )


def _ensure_link_value(doctype: str, name: str, values: dict[str, Any]) -> None:
    if frappe.db.exists("DocType", doctype) and not frappe.db.exists(doctype, name):
        frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)


def after_install() -> None:
    _ensure_role()
    _ensure_link_value(
        "CRM Lead Source",
        "PhoneAgent",
        {"source_name": "PhoneAgent"},
    )
    if frappe.db.exists("DocType", "HD Ticket"):
        create_custom_fields(
            {
                "HD Ticket": [
                    {
                        "fieldname": "phoneagent_phone_e164",
                        "label": "PhoneAgent Caller Phone",
                        "fieldtype": "Data",
                        "options": "Phone",
                        "read_only": 1,
                        "in_standard_filter": 1,
                        "insert_after": "raised_by",
                    }
                ]
            },
            update=True,
        )
    frappe.db.commit()


def after_migrate() -> None:
    after_install()


def provision_integration(
    api_key: str = "",
    api_secret: str = "",
    user_email: str = DEFAULT_USER,
) -> dict[str, str]:
    """Create the least-privilege API principal used by PhoneAgent.

    This is invoked locally through ``bench execute`` by the installer.  The
    generated secret is never returned through a public HTTP endpoint.
    """

    _ensure_role()
    key = api_key.strip() or secrets.token_hex(15)
    secret = api_secret.strip() or secrets.token_urlsafe(36)
    if not frappe.db.exists("User", user_email):
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": user_email,
                "first_name": "PhoneAgent",
                "last_name": "Integration",
                "enabled": 1,
                "send_welcome_email": 0,
                "user_type": "System User",
            }
        )
        user.insert(ignore_permissions=True)
    else:
        user = frappe.get_doc("User", user_email)
        user.enabled = 1
    user.api_key = key
    user.api_secret = secret
    user.save(ignore_permissions=True)
    roles = [INTEGRATION_ROLE]
    if frappe.db.exists("Role", "Agent"):
        roles.append("Agent")
    user.add_roles(*roles)
    frappe.db.commit()
    return {"user": user_email, "api_key": key, "api_secret": secret}


def release_stale_campaign_claims() -> None:
    if not frappe.db.exists("DocType", "PhoneAgent Campaign Member"):
        return
    frappe.db.sql(
        """
        update `tabPhoneAgent Campaign Member`
           set status='Pending', claimed_until=null, claimed_by=null
         where status='In Progress'
           and claimed_until is not null
           and claimed_until < %s
        """,
        (now_datetime(),),
    )
    frappe.db.commit()
