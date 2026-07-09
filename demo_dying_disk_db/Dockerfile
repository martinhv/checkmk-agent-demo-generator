# Conf #12 demo: fake database host for the "Explain with AI" story (Theme 2)
FROM python:3.12-slim

WORKDIR /opt/db-demo
COPY serve.py /opt/db-demo/serve.py

# agent (Checkmk) + admin/toggle endpoint
EXPOSE 6556 8080

ENV AGENT_PORT=6556 \
    HTTP_PORT=8080 \
    START_STATE=healthy

# Unbuffered so [boot]/[ctl] logs show up live in `docker logs`
ENTRYPOINT ["python3", "-u", "/opt/db-demo/serve.py"]
