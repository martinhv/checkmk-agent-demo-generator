# Conf #12 demo: fake "black-box dependency" host (Check_MK agent + HTTP endpoint)
FROM python:3.12-slim

WORKDIR /opt/payment-api
COPY serve.py /opt/payment-api/serve.py

# agent (Checkmk) + http (check_http)
EXPOSE 6556 8080

ENV AGENT_PORT=6556 \
    HTTP_PORT=8080 \
    START_BROKEN=1 \
    BROKEN_DELAY_MS=1500

# Unbuffered so [boot]/[ctl] logs show up live in `docker logs`
ENTRYPOINT ["python3", "-u", "/opt/payment-api/serve.py"]
