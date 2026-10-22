# NOMAD v2 engine — the pipeline state machine + cockpit + gate poller, as a managed service.
#
# Runtime note: in-container the specialist runtime defaults to LiteLLM (NOMAD_RUNTIME=litellm),
# which routes Claude (deep/balanced) through the gateway → the native claude-bridge → the same
# Opus/Sonnet login. The native Agent-SDK runtime (2D) runs on the host/dev; running the SDK
# IN-container (mount ~/.claude + the claude CLI) is a documented follow-up — the runtime falls
# back to LiteLLM automatically, so the container needs neither the SDK nor the CLI.
FROM python:3.11-slim

WORKDIR /app
# ⚠ mcp is PINNED <2: 2.x renames FastMCP → MCPServer, so an unpinned rebuild breaks every
# mcp_*.py import and the engine won't boot. Unpin only together with the v1→v2 API migration.
RUN pip install --no-cache-dir "mcp>=1.0,<2" "requests>=2.31" "psycopg2-binary>=2.9" \
    "fastapi>=0.110" "uvicorn[standard]>=0.29" "websockets>=13" \
    "pytest>=8"   # server.py needs fastapi+psycopg2, which the host does not have, so its tests run
                  # HERE. Name the files, because bare collection also imports test_2a/test_3c/
                  # test_engine, which are standalone scripts that sys.exit at import:
                  #   docker compose exec nomad-v2-engine sh -c \
                  #     'python -m pytest test_chat_context.py test_engine_states.py -q'
                  # Installing pytest ad hoc into a running container works until the next recreate,
                  # at which point the tests quietly stop being runnable, which is how a suite dies.

COPY *.py ./
# Not run from in here. It ships so test_auth.py can check it: the suite runs inside this
# image, and a test that reads a file the image does not have is a skip that never fires.
COPY publish.sh ./
COPY skills ./skills
COPY static ./static
# The creation module every entry point shares (#1372). It sits outside this context, at the repo
# root; compose passes it as an additional context. owui_newproject/owui_setup re-export it.
COPY --from=nomad_projects *.py ./lib/nomad_projects/

# Bind 0.0.0.0 INSIDE the container; the compose port-publish restricts it to 127.0.0.1 on the
# host. On the docker network it's reachable as nomad-v2-engine:8099 (no LAN exposure).
ENV NOMAD_V2_ENGINE_HOST=0.0.0.0 \
    NOMAD_RUNTIME=litellm
# The test suite runs in this image as root against a bind-mounted worktree. Bytecode and the
# pytest cache would land in that worktree owned by root, and `git worktree remove` then fails
# without sudo (#1008). Neither is worth having in a container that is recreated, not upgraded.
# server.py and mcp_image.py also makedirs the image dir at import; its default sits next to the
# source, i.e. in the worktree, so point it where compose already mounts it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTEST_ADDOPTS="-p no:cacheprovider" \
    NOMAD_IMAGE_DIR=/app/generated_images \
    PYTHONPATH=/app/lib
EXPOSE 8099
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8099"]
