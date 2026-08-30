FROM frappe/erpnext:v16.33.0

ARG CRM_REF=v1.82.0
ARG HELPDESK_REF=v1.30.0
ARG TELEPHONY_REF=develop

USER frappe
WORKDIR /home/frappe/frappe-bench

RUN bench get-app --branch "${TELEPHONY_REF}" --skip-assets \
      telephony https://github.com/frappe/telephony.git && \
    bench get-app --branch "${CRM_REF}" --skip-assets \
      crm https://github.com/frappe/crm.git && \
    bench get-app --branch "${HELPDESK_REF}" --skip-assets \
      helpdesk https://github.com/frappe/helpdesk.git

RUN bench set-config -gp socketio_port 9000 && \
    bench build --production && \
    find apps -type d -name .git -prune -exec rm -rf {} +

COPY --chown=frappe:frappe \
  integrations/business_suite/phoneagent_frappe \
  /home/frappe/frappe-bench/apps/phoneagent_frappe

RUN ./env/bin/pip install --no-cache-dir --editable apps/phoneagent_frappe

COPY --chown=frappe:frappe \
  integrations/business_suite/scripts/init-site.sh \
  /opt/phoneagent/init-site.sh
