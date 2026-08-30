from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, now_datetime

from phoneagent_frappe.setup import INTEGRATION_ROLE

MAX_ITEMS = 50
MAX_TEXT = 2_000


def _require_integration() -> None:
    if frappe.session.user == "Guest" or INTEGRATION_ROLE not in frappe.get_roles():
        frappe.throw(_("PhoneAgent integration role is required"), frappe.PermissionError)


def _text(value: Any, *, maximum: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _phone(value: Any) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if not 7 <= len(digits) <= 15:
        frappe.throw(_("A valid E.164 phone number is required"))
    return f"+{digits}"


def _limit(value: Any) -> int:
    return min(MAX_ITEMS, max(1, cint(value or 10)))


def _first_existing(doctype: str, filters: dict[str, Any], field: str = "name") -> Any:
    if not frappe.db.exists("DocType", doctype):
        return None
    return frappe.db.get_value(doctype, filters, field)


def _lead_by_phone(phone: str) -> str | None:
    for field in ("mobile_no", "phone"):
        name = _first_existing("CRM Lead", {field: phone})
        if name:
            return str(name)
    return None


def _customer_by_phone(phone: str) -> str | None:
    for field in ("mobile_no", "phone"):
        try:
            name = frappe.db.get_value("Customer", {field: phone}, "name")
        except Exception:
            name = None
        if name:
            return str(name)
    return None


def _deal_by_phone(phone: str) -> str | None:
    for field in ("mobile_no", "phone"):
        name = _first_existing("CRM Deal", {field: phone})
        if name:
            return str(name)
    return None


def _latest_consent(phone: str) -> dict[str, Any] | None:
    rows = frappe.get_all(
        "PhoneAgent Contact Consent",
        filters={"phone_e164": phone},
        fields=["name", "purpose", "status", "source", "captured_at", "expires_at", "do_not_call"],
        order_by="captured_at desc",
        limit=1,
    )
    return rows[0] if rows else None


def _is_do_not_call(phone: str) -> bool:
    consent = _latest_consent(phone)
    return bool(
        consent
        and (cint(consent.get("do_not_call")) or consent.get("status") in {"withdrawn", "declined"})
    )


def _default_link(doctype: str, *, category: str | None = None) -> str | None:
    if not frappe.db.exists("DocType", doctype):
        return None
    filters: dict[str, Any] = {}
    if category and frappe.get_meta(doctype).has_field("category"):
        filters["category"] = category
    rows = frappe.get_all(doctype, filters=filters, pluck="name", order_by="creation asc", limit=1)
    return str(rows[0]) if rows else None


def _ensure_lead(
    phone: str,
    *,
    name: str = "",
    email: str = "",
    company: str = "",
    notes: str = "",
) -> Any:
    lead_name = _lead_by_phone(phone)
    if lead_name:
        lead = frappe.get_doc("CRM Lead", lead_name)
    else:
        clean_name = _text(name, maximum=240) or f"Phone caller {phone[-4:]}"
        parts = clean_name.split(" ", 1)
        status = _default_link("CRM Lead Status")
        if not status:
            frappe.throw(_("Frappe CRM has no lead status configured"))
        lead = frappe.get_doc(
            {
                "doctype": "CRM Lead",
                "first_name": parts[0],
                "last_name": parts[1] if len(parts) > 1 else "",
                "lead_name": clean_name,
                "mobile_no": phone,
                "phone": phone,
                "status": status,
                "source": "PhoneAgent"
                if frappe.db.exists("CRM Lead Source", "PhoneAgent")
                else None,
            }
        )
    if name:
        clean_name = _text(name, maximum=240)
        parts = clean_name.split(" ", 1)
        lead.first_name = parts[0]
        lead.last_name = parts[1] if len(parts) > 1 else ""
        lead.lead_name = clean_name
    if email:
        lead.email = _text(email, maximum=320)
    if company:
        lead.organization = _text(company, maximum=240)
    if notes and lead.meta.has_field("notes"):
        lead.notes = _text(notes)
    lead.flags.ignore_permissions = True
    lead.save()
    return lead


def _ensure_customer(phone: str, lead: Any | None = None) -> Any:
    customer_name = _customer_by_phone(phone)
    if customer_name:
        return frappe.get_doc("Customer", customer_name)
    lead = lead or (_lead_by_phone(phone) and frappe.get_doc("CRM Lead", _lead_by_phone(phone)))
    display = _text(getattr(lead, "lead_name", ""), maximum=240) or f"Customer {phone[-4:]}"
    group = frappe.db.get_single_value("Selling Settings", "customer_group") or _default_link(
        "Customer Group"
    )
    territory = frappe.db.get_single_value("Selling Settings", "territory") or _default_link(
        "Territory"
    )
    customer = frappe.get_doc(
        {
            "doctype": "Customer",
            "customer_name": display,
            "customer_type": "Individual",
            "customer_group": group,
            "territory": territory,
            "mobile_no": phone,
        }
    )
    customer.insert(ignore_permissions=True)
    return customer


@frappe.whitelist(methods=["POST"])
def health() -> dict[str, Any]:
    _require_integration()
    required = ("erpnext", "crm", "helpdesk", "phoneagent_frappe")
    installed = set(frappe.get_installed_apps())
    return {
        "status": "ok",
        "site": frappe.local.site,
        "apps": sorted(installed),
        "required_ready": all(name in installed for name in required),
    }


@frappe.whitelist(methods=["POST"])
def get_customer_context(phone: str, max_items: int = 10, **_: Any) -> dict[str, Any]:
    _require_integration()
    number = _phone(phone)
    limit = _limit(max_items)
    lead = _lead_by_phone(number)
    deal = _deal_by_phone(number)
    customer = _customer_by_phone(number)
    context: dict[str, Any] = {
        "verified": True,
        "found": bool(lead or deal or customer),
        "lead": frappe.db.get_value(
            "CRM Lead", lead, ["name", "lead_name", "status", "organization", "email"], as_dict=True
        )
        if lead
        else None,
        "deal": frappe.db.get_value(
            "CRM Deal",
            deal,
            ["name", "status", "probability", "expected_deal_value", "next_step"],
            as_dict=True,
        )
        if deal
        else None,
        "customer": frappe.db.get_value(
            "Customer",
            customer,
            ["name", "customer_name", "customer_group", "territory", "disabled"],
            as_dict=True,
        )
        if customer
        else None,
        "consent": _latest_consent(number),
        "do_not_call": _is_do_not_call(number),
    }
    if customer:
        context["orders"] = frappe.get_all(
            "Sales Order",
            filters={"customer": customer},
            fields=[
                "name",
                "transaction_date",
                "status",
                "grand_total",
                "currency",
                "delivery_status",
            ],
            order_by="creation desc",
            limit=limit,
        )
        context["invoices"] = frappe.get_all(
            "Sales Invoice",
            filters={"customer": customer},
            fields=[
                "name",
                "posting_date",
                "status",
                "grand_total",
                "outstanding_amount",
                "currency",
            ],
            order_by="creation desc",
            limit=limit,
        )
        if frappe.db.exists("DocType", "Subscription"):
            context["subscriptions"] = frappe.get_all(
                "Subscription",
                filters={"party_type": "Customer", "party": customer},
                fields=[
                    "name",
                    "status",
                    "start_date",
                    "end_date",
                    "current_invoice_start",
                    "current_invoice_end",
                ],
                order_by="creation desc",
                limit=limit,
            )
    if frappe.db.exists("DocType", "HD Ticket"):
        ticket_filters: list[list[Any]] = (
            [["phoneagent_phone_e164", "=", number]]
            if frappe.get_meta("HD Ticket").has_field("phoneagent_phone_e164")
            else []
        )
        if not ticket_filters and customer:
            hd_customer = (
                frappe.db.get_value("HD Customer", {"customer_id": customer}, "name")
                if frappe.db.exists("DocType", "HD Customer")
                else None
            )
            if hd_customer:
                ticket_filters = [["customer", "=", hd_customer]]
        if ticket_filters:
            context["support_tickets"] = frappe.get_all(
                "HD Ticket",
                filters=ticket_filters,
                fields=["name", "subject", "status", "priority", "creation", "modified"],
                order_by="creation desc",
                limit=limit,
            )
    return context


@frappe.whitelist(methods=["POST"])
def upsert_lead(
    phone: str,
    name: str = "",
    email: str = "",
    company: str = "",
    notes: str = "",
    consent_status: str = "unknown",
    **_: Any,
) -> dict[str, Any]:
    _require_integration()
    number = _phone(phone)
    lead = _ensure_lead(number, name=name, email=email, company=company, notes=notes)
    if consent_status in {"consented", "declined", "do_not_call"}:
        _record_consent(
            number,
            status="consented" if consent_status == "consented" else "declined",
            source="live_phone_call",
            evidence=f"Caller status recorded during PhoneAgent call: {consent_status}",
            do_not_call=consent_status == "do_not_call",
            lead=lead.name,
        )
    frappe.db.commit()
    return {"verified": True, "created_or_updated": True, "lead_id": lead.name}


def _record_consent(
    phone: str,
    *,
    status: str,
    source: str,
    evidence: str,
    do_not_call: bool,
    lead: str | None = None,
    customer: str | None = None,
) -> Any:
    doc = frappe.get_doc(
        {
            "doctype": "PhoneAgent Contact Consent",
            "phone_e164": phone,
            "purpose": "sales",
            "status": status,
            "source": source,
            "evidence": _text(evidence),
            "captured_at": now_datetime(),
            "do_not_call": 1 if do_not_call else 0,
            "lead": lead,
            "customer": customer,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc


@frappe.whitelist(methods=["POST"])
def create_opportunity(
    phone: str,
    title: str,
    notes: str = "",
    estimated_value: float = 0,
    currency: str = "",
    probability: float = 0,
    **_: Any,
) -> dict[str, Any]:
    _require_integration()
    number = _phone(phone)
    if _is_do_not_call(number):
        frappe.throw(_("This caller is on the do-not-call list"))
    lead = _ensure_lead(number)
    status = _default_link("CRM Deal Status", category="Open")
    if not status:
        frappe.throw(_("Frappe CRM has no open deal status configured"))
    deal = frappe.get_doc(
        {
            "doctype": "CRM Deal",
            "lead": lead.name,
            "lead_name": lead.lead_name,
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "email": lead.email,
            "mobile_no": number,
            "phone": number,
            "organization_name": lead.organization,
            "status": status,
            "probability": min(100, max(0, flt(probability))),
            "expected_deal_value": max(0, flt(estimated_value)),
            "currency": _text(currency, maximum=3)
            or frappe.defaults.get_global_default("currency"),
            "next_step": _text(title, maximum=240),
        }
    )
    deal.insert(ignore_permissions=True)
    if notes:
        deal.add_comment("Comment", _text(notes))
    frappe.db.commit()
    return {"verified": True, "created": True, "opportunity_id": deal.name, "status": status}


@frappe.whitelist(methods=["POST"])
def schedule_follow_up(
    phone: str,
    at: str,
    description: str,
    channel: str = "phone",
    **_: Any,
) -> dict[str, Any]:
    _require_integration()
    number = _phone(phone)
    lead = _ensure_lead(number)
    scheduled = get_datetime(at)
    todo = frappe.get_doc(
        {
            "doctype": "ToDo",
            "allocated_to": frappe.session.user,
            "description": f"[{_text(channel, maximum=20)}] {_text(description)}",
            "date": scheduled.date(),
            "reference_type": "CRM Lead",
            "reference_name": lead.name,
            "status": "Open",
            "priority": "Medium",
        }
    )
    todo.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"verified": True, "scheduled": True, "follow_up_id": todo.name, "at": str(scheduled)}


@frappe.whitelist(methods=["POST"])
def search_catalog(query: str, max_items: int = 10, **_: Any) -> dict[str, Any]:
    _require_integration()
    term = _text(query, maximum=240)
    if len(term) < 2:
        frappe.throw(_("Catalog query is too short"))
    limit = _limit(max_items)
    names: list[str] = []
    for field in ("item_code", "item_name", "description"):
        rows = frappe.get_all(
            "Item",
            filters={"disabled": 0, field: ["like", f"%{term}%"]},
            pluck="name",
            limit=limit,
        )
        for name in rows:
            if name not in names:
                names.append(name)
            if len(names) >= limit:
                break
        if len(names) >= limit:
            break
    results = []
    price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
    for name in names:
        item = frappe.db.get_value(
            "Item",
            name,
            ["item_code", "item_name", "description", "stock_uom", "is_stock_item"],
            as_dict=True,
        )
        price = (
            frappe.db.get_value(
                "Item Price",
                {"item_code": name, "price_list": price_list, "selling": 1},
                ["price_list_rate", "currency", "valid_from", "valid_upto"],
                as_dict=True,
            )
            if price_list
            else None
        )
        results.append({"item": item, "price": price})
    return {"verified": True, "found": bool(results), "results": results}


def _validated_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        frappe.throw(_("One to twenty quotation items are required"))
    output = []
    for value in items:
        if not isinstance(value, dict):
            frappe.throw(_("Quotation item is invalid"))
        code = _text(value.get("item_code"), maximum=140)
        quantity = flt(value.get("quantity"))
        if not code or quantity <= 0 or not frappe.db.exists("Item", code):
            frappe.throw(_("Quotation item or quantity is invalid"))
        output.append({"item_code": code, "qty": quantity})
    return output


@frappe.whitelist(methods=["POST"])
def create_quotation_draft(phone: str, items: Any, notes: str = "", **_: Any) -> dict[str, Any]:
    _require_integration()
    number = _phone(phone)
    if _is_do_not_call(number):
        frappe.throw(_("This caller is on the do-not-call list"))
    lead = _ensure_lead(number)
    customer = _ensure_customer(number, lead)
    quotation = frappe.get_doc(
        {
            "doctype": "Quotation",
            "quotation_to": "Customer",
            "party_name": customer.name,
            "items": _validated_items(items),
            "terms": _text(notes),
        }
    )
    quotation.insert(ignore_permissions=True)
    frappe.db.commit()
    return {
        "verified": True,
        "created": True,
        "quotation_id": quotation.name,
        "docstatus": quotation.docstatus,
        "status": "draft_not_submitted",
        "grand_total": quotation.grand_total,
        "currency": quotation.currency,
    }


@frappe.whitelist(methods=["POST"])
def create_sales_order_draft(
    phone: str, quotation_id: str, notes: str = "", **_: Any
) -> dict[str, Any]:
    _require_integration()
    number = _phone(phone)
    quotation = frappe.get_doc("Quotation", _text(quotation_id, maximum=140))
    customer = _customer_by_phone(number)
    if not customer or quotation.quotation_to != "Customer" or quotation.party_name != customer:
        frappe.throw(_("Quotation does not belong to the current caller"), frappe.PermissionError)
    order = frappe.get_doc(
        {
            "doctype": "Sales Order",
            "customer": customer,
            "items": [
                {
                    "item_code": row.item_code,
                    "qty": row.qty,
                    "rate": row.rate,
                    "prevdoc_docname": quotation.name,
                }
                for row in quotation.items
            ],
            "remarks": _text(notes),
        }
    )
    order.insert(ignore_permissions=True)
    frappe.db.commit()
    return {
        "verified": True,
        "created": True,
        "sales_order_id": order.name,
        "docstatus": order.docstatus,
        "status": "draft_not_submitted",
        "grand_total": order.grand_total,
        "currency": order.currency,
    }


@frappe.whitelist(methods=["POST"])
def get_order_status(
    phone: str, order_id: str = "", max_items: int = 10, **_: Any
) -> dict[str, Any]:
    _require_integration()
    customer = _customer_by_phone(_phone(phone))
    if not customer:
        return {"verified": True, "found": False, "orders": []}
    filters: dict[str, Any] = {"customer": customer}
    if order_id:
        filters["name"] = _text(order_id, maximum=140)
    rows = frappe.get_all(
        "Sales Order",
        filters=filters,
        fields=[
            "name",
            "transaction_date",
            "status",
            "docstatus",
            "grand_total",
            "currency",
            "delivery_status",
            "per_delivered",
            "per_billed",
        ],
        order_by="creation desc",
        limit=_limit(max_items),
    )
    return {"verified": True, "found": bool(rows), "orders": rows}


@frappe.whitelist(methods=["POST"])
def get_invoice_status(
    phone: str, invoice_id: str = "", max_items: int = 10, **_: Any
) -> dict[str, Any]:
    _require_integration()
    customer = _customer_by_phone(_phone(phone))
    if not customer:
        return {"verified": True, "found": False, "invoices": []}
    filters: dict[str, Any] = {"customer": customer}
    if invoice_id:
        filters["name"] = _text(invoice_id, maximum=140)
    rows = frappe.get_all(
        "Sales Invoice",
        filters=filters,
        fields=[
            "name",
            "posting_date",
            "due_date",
            "status",
            "docstatus",
            "grand_total",
            "outstanding_amount",
            "currency",
        ],
        order_by="creation desc",
        limit=_limit(max_items),
    )
    return {"verified": True, "found": bool(rows), "invoices": rows}


def _ticket_customer(customer: str | None, *, create: bool = False) -> str | None:
    if not customer or not frappe.db.exists("DocType", "HD Customer"):
        return None
    existing = frappe.db.get_value("HD Customer", {"customer_id": customer}, "name")
    if existing:
        return str(existing)
    if not create:
        return None
    doc = frappe.get_doc(
        {"doctype": "HD Customer", "customer_name": customer, "customer_id": customer}
    )
    doc.insert(ignore_permissions=True)
    return doc.name


@frappe.whitelist(methods=["POST"])
def create_support_ticket(
    phone: str,
    subject: str,
    description: str,
    priority: str = "Medium",
    category: str = "",
    **_: Any,
) -> dict[str, Any]:
    _require_integration()
    number = _phone(phone)
    lead = _ensure_lead(number)
    customer = _customer_by_phone(number)
    status = _default_link("HD Ticket Status", category="Open")
    if not status:
        frappe.throw(_("Helpdesk has no open ticket status configured"))
    configured_priority = (
        priority
        if frappe.db.exists("HD Ticket Priority", priority)
        else _default_link("HD Ticket Priority")
    )
    ticket = frappe.get_doc(
        {
            "doctype": "HD Ticket",
            "subject": _text(subject, maximum=240),
            "description": _text(description),
            "status": status,
            "priority": configured_priority,
            "ticket_type": category
            if category and frappe.db.exists("HD Ticket Type", category)
            else None,
            "raised_by": lead.email or "",
            "phoneagent_phone_e164": number,
            "customer": _ticket_customer(customer, create=True),
        }
    )
    ticket.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"verified": True, "created": True, "ticket_id": ticket.name, "status": ticket.status}


def _ticket_for_phone(phone: str, ticket_id: str) -> Any:
    ticket = frappe.get_doc("HD Ticket", ticket_id)
    if getattr(ticket, "phoneagent_phone_e164", "") == phone:
        return ticket
    customer = _customer_by_phone(phone)
    hd_customer = _ticket_customer(customer) if customer else None
    if hd_customer and ticket.customer == hd_customer:
        return ticket
    lead = _lead_by_phone(phone)
    email = frappe.db.get_value("CRM Lead", lead, "email") if lead else None
    if email and ticket.raised_by == email:
        return ticket
    frappe.throw(_("Support ticket does not belong to the current caller"), frappe.PermissionError)


@frappe.whitelist(methods=["POST"])
def get_support_status(
    phone: str, ticket_id: str = "", max_items: int = 10, **_: Any
) -> dict[str, Any]:
    _require_integration()
    number = _phone(phone)
    if ticket_id:
        ticket = _ticket_for_phone(number, _text(ticket_id, maximum=140))
        return {
            "verified": True,
            "found": True,
            "tickets": [
                {
                    "name": ticket.name,
                    "subject": ticket.subject,
                    "status": ticket.status,
                    "priority": ticket.priority,
                    "modified": ticket.modified,
                }
            ],
        }
    customer = _customer_by_phone(number)
    hd_customer = _ticket_customer(customer) if customer else None
    lead = _lead_by_phone(number)
    email = frappe.db.get_value("CRM Lead", lead, "email") if lead else None
    if frappe.get_meta("HD Ticket").has_field("phoneagent_phone_e164"):
        filters: dict[str, Any] = {"phoneagent_phone_e164": number}
    else:
        filters = (
            {"customer": hd_customer}
            if hd_customer
            else {"raised_by": email}
            if email
            else {}
        )
    if not filters:
        return {"verified": True, "found": False, "tickets": []}
    rows = frappe.get_all(
        "HD Ticket",
        filters=filters,
        fields=["name", "subject", "status", "priority", "creation", "modified", "resolution_by"],
        order_by="creation desc",
        limit=_limit(max_items),
    )
    return {"verified": True, "found": bool(rows), "tickets": rows}


@frappe.whitelist(methods=["POST"])
def update_support_ticket(
    phone: str,
    ticket_id: str,
    comment: str,
    status: str = "",
    **_: Any,
) -> dict[str, Any]:
    _require_integration()
    ticket = _ticket_for_phone(_phone(phone), _text(ticket_id, maximum=140))
    ticket.add_comment("Comment", _text(comment))
    if status:
        if status == "Open":
            target = _default_link("HD Ticket Status", category="Open")
        elif status == "Resolved":
            target = _default_link("HD Ticket Status", category="Resolved")
        else:
            target = frappe.db.get_value("HD Ticket Status", {"label_agent": status}, "name")
        if not target:
            frappe.throw(_("Requested support status is unavailable"))
        ticket.status = target
        ticket.save(ignore_permissions=True)
    frappe.db.commit()
    return {"verified": True, "updated": True, "ticket_id": ticket.name, "status": ticket.status}


@frappe.whitelist(methods=["POST"])
def mark_do_not_call(phone: str, reason: str, **_: Any) -> dict[str, Any]:
    _require_integration()
    number = _phone(phone)
    lead = _lead_by_phone(number)
    customer = _customer_by_phone(number)
    consent = _record_consent(
        number,
        status="withdrawn",
        source="live_phone_call",
        evidence=_text(reason, maximum=240),
        do_not_call=True,
        lead=lead,
        customer=customer,
    )
    frappe.db.sql(
        """
        update `tabPhoneAgent Campaign Member`
           set consent_status='do_not_call', status='Skipped', last_outcome='do_not_call'
         where phone_e164=%s and status in ('Pending','Retry','In Progress')
        """,
        (number,),
    )
    frappe.db.commit()
    return {"verified": True, "do_not_call": True, "consent_id": consent.name}


@frappe.whitelist(methods=["POST"])
def record_call_outcome(
    phone: str,
    call_id: str,
    call_direction: str,
    task_id: str,
    disposition: str,
    summary: str,
    next_action: str = "",
    follow_up_at: str = "",
    channel: str = "gsm",
    duration_seconds: float = 0,
    structured_outcome: Any = None,
    campaign_member: str = "",
    **_: Any,
) -> dict[str, Any]:
    _require_integration()
    number = _phone(phone)
    identifier = _text(call_id, maximum=140)
    if not identifier:
        frappe.throw(_("Call ID is required"))
    existing = frappe.db.exists("PhoneAgent Call Log", identifier)
    doc = (
        frappe.get_doc("PhoneAgent Call Log", identifier)
        if existing
        else frappe.new_doc("PhoneAgent Call Log")
    )
    member_doc = (
        frappe.get_doc("PhoneAgent Campaign Member", campaign_member) if campaign_member else None
    )
    doc.update(
        {
            "call_id": identifier,
            "phone_e164": number,
            "direction": call_direction
            if call_direction in {"inbound", "outbound"}
            else "outbound",
            "channel": channel if channel in {"gsm", "whatsapp_phone", "whatsapp"} else "gsm",
            "task_id": _text(task_id, maximum=140),
            "started_at": doc.started_at or now_datetime(),
            "ended_at": now_datetime(),
            "duration_seconds": max(0, flt(duration_seconds)),
            "disposition": _text(disposition, maximum=140),
            "summary": _text(summary),
            "next_action": _text(next_action, maximum=240),
            "follow_up_at": get_datetime(follow_up_at) if follow_up_at else None,
            "lead": _lead_by_phone(number),
            "deal": _deal_by_phone(number),
            "customer": _customer_by_phone(number),
            "structured_outcome": json.dumps(
                structured_outcome or {}, ensure_ascii=False, default=str
            ),
            "campaign_member": campaign_member or None,
            "campaign": member_doc.campaign if member_doc else None,
        }
    )
    doc.save(ignore_permissions=True)
    if disposition == "do_not_call":
        mark_do_not_call(number, summary)
    if campaign_member:
        complete_campaign_member(campaign_member, identifier, disposition, summary)
    frappe.db.commit()
    return {"verified": True, "recorded": True, "call_log_id": doc.name}


def _campaign_window_open(campaign: Any, now: datetime) -> bool:
    try:
        local = now.astimezone(ZoneInfo(campaign.timezone))
    except Exception:
        return False
    start = campaign.window_start
    end = campaign.window_end
    current = local.timetz().replace(tzinfo=None)
    return bool(start and end and start <= current <= end)


@frappe.whitelist(methods=["POST"])
def next_campaign_contact(worker_id: str, claim_seconds: int = 180) -> dict[str, Any]:
    _require_integration()
    worker = _text(worker_id, maximum=140)
    if not worker:
        frappe.throw(_("Campaign worker ID is required"))
    now = now_datetime()
    campaigns = frappe.get_all(
        "PhoneAgent Campaign",
        filters={"status": "Active"},
        fields=[
            "name",
            "task_id",
            "channel",
            "timezone",
            "window_start",
            "window_end",
            "max_daily_calls",
            "max_attempts",
            "require_explicit_consent",
        ],
        order_by="creation asc",
        limit=100,
    )
    for row in campaigns:
        campaign = frappe._dict(row)
        if not _campaign_window_open(campaign, now):
            continue
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily = frappe.db.count(
            "PhoneAgent Call Log",
            {"campaign": campaign.name, "started_at": [">=", day_start]},
        )
        if daily >= cint(campaign.max_daily_calls):
            continue
        consent_clause = (
            "and consent_status='consented'"
            if cint(campaign.require_explicit_consent)
            else "and consent_status not in ('declined','do_not_call')"
        )
        member_rows = frappe.db.sql(
            f"""
            select name, phone_e164, display_name, email, company, language,
                   consent_status, attempts
              from `tabPhoneAgent Campaign Member`
             where campaign=%s
               and status in ('Pending','Retry')
               and attempts < %s
               and (next_attempt_at is null or next_attempt_at <= %s)
               {consent_clause}
             order by coalesce(next_attempt_at, creation), creation
             limit 1
             for update skip locked
            """,
            (campaign.name, cint(campaign.max_attempts), now),
            as_dict=True,
        )
        if not member_rows:
            continue
        member = frappe._dict(member_rows[0])
        if _is_do_not_call(member.phone_e164):
            frappe.db.set_value(
                "PhoneAgent Campaign Member",
                member.name,
                {"status": "Skipped", "consent_status": "do_not_call"},
                update_modified=False,
            )
            frappe.db.commit()
            continue
        frappe.db.set_value(
            "PhoneAgent Campaign Member",
            member.name,
            {
                "status": "In Progress",
                "attempts": cint(member.attempts) + 1,
                "claimed_by": worker,
                "claimed_until": add_to_date(now, seconds=min(900, max(60, cint(claim_seconds)))),
            },
            update_modified=True,
        )
        frappe.db.set_value(
            "PhoneAgent Campaign",
            campaign.name,
            "calls_started",
            cint(frappe.db.get_value("PhoneAgent Campaign", campaign.name, "calls_started")) + 1,
            update_modified=False,
        )
        frappe.db.commit()
        return {
            "verified": True,
            "available": True,
            "campaign_id": campaign.name,
            "member_id": member.name,
            "phone": member.phone_e164,
            "display_name": member.display_name,
            "language": member.language,
            "task_id": campaign.task_id,
            "channel": campaign.channel,
            "attempt": cint(member.attempts) + 1,
        }
    return {"verified": True, "available": False}


@frappe.whitelist(methods=["POST"])
def complete_campaign_member(
    member_id: str,
    call_id: str,
    disposition: str,
    summary: str = "",
) -> dict[str, Any]:
    _require_integration()
    member = frappe.get_doc("PhoneAgent Campaign Member", member_id)
    campaign = frappe.get_doc("PhoneAgent Campaign", member.campaign)
    outcome = _text(disposition, maximum=140)
    if outcome in {"no_answer", "failed"} and cint(member.attempts) < cint(campaign.max_attempts):
        member.status = "Retry"
        member.next_attempt_at = add_to_date(now_datetime(), minutes=cint(campaign.retry_minutes))
    elif outcome == "do_not_call":
        member.status = "Skipped"
        member.consent_status = "do_not_call"
    elif outcome == "failed":
        member.status = "Failed"
    else:
        member.status = "Completed"
    member.claimed_by = None
    member.claimed_until = None
    member.last_call_id = _text(call_id, maximum=140)
    member.last_outcome = outcome
    member.save(ignore_permissions=True)
    campaign.calls_completed = cint(campaign.calls_completed) + 1
    if outcome == "converted":
        campaign.converted_count = cint(campaign.converted_count) + 1
    campaign.save(ignore_permissions=True)
    frappe.db.commit()
    return {
        "verified": True,
        "completed": True,
        "member_status": member.status,
        "summary": _text(summary),
    }
