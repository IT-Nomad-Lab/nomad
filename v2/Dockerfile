# NOMAD v2 engine — the pipeline state machine + cockpit + gate poller, as a managed service.
#
# Runtime note: in-container the specialist runtime defaults to LiteLLM (NOMAD_RUNTIME=litellm),
# which routes Claude (deep/balanced) through the gateway → the native claude-bridge → the same
# Opus/Sonnet login. The native Agent-SDK runtime (2D) runs on the host/dev; running the SDK
# IN-container (mount ~/.claude + the claude CLI) is a documented follow-up — the runtime falls
# back to LiteLLM automatically, so the container needs neither the SDK nor the CLI.
FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir "mcp>=1.0" "requests>=2.31" "psycopg2-binary>=2.9" \
    "fastapi>=0.110" "uvicorn[standard]>=0.29" "websockets>=13"

COPY *.py ./
COPY skills ./skills
COPY static ./static

# Bind 0.0.0.0 INSIDE the container; the compose port-publish restricts it to 127.0.0.1 on the
# host. On the docker network it's reachable as nomad-v2-engine:8099 (no LAN exposure).
ENV NOMAD_V2_ENGINE_HOST=0.0.0.0 \
    NOMAD_RUNTIME=litellm
EXPOSE 8099
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8099"]
