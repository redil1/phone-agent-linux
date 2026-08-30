app_name = "phoneagent_frappe"
app_title = "PhoneAgent Business Automation"
app_publisher = "PhoneAgent"
app_description = "CRM, customer service and ERP trust boundary for PhoneAgent"
app_email = "local@phoneagent.invalid"
app_license = "AGPLv3"
required_apps = ["erpnext", "crm", "helpdesk"]
require_type_annotated_api_methods = True

after_install = "phoneagent_frappe.setup.after_install"
after_migrate = "phoneagent_frappe.setup.after_migrate"

scheduler_events = {
    "hourly": ["phoneagent_frappe.setup.release_stale_campaign_claims"],
}

