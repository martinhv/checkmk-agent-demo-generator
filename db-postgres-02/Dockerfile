# Meridian Retail demo host: db-postgres-02 (PostgreSQL read replica)
# Incident: BI connection leak -> connection-pool exhaustion (Connections CRIT)
FROM python:3.12-slim

WORKDIR /opt/db-replica-demo
COPY serve.py /opt/db-replica-demo/serve.py

EXPOSE 6556 8080

ENV CMK_HOSTNAME=db-postgres-02 \
    AGENT_PORT=6556 \
    HTTP_PORT=8080 \
    START_STATE=healthy

# Unbuffered so [boot]/[ctl]/[state] logs show up live in `docker logs`
ENTRYPOINT ["python3", "-u", "/opt/db-replica-demo/serve.py"]
