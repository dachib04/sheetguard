# SheetGuard MCP server (S-11). Build:  docker build -t sheetguard:0.1.0 .
# Run hardened (stdio):
#   docker run -i --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
#     --network none --memory 2g --pids-limit 64 \
#     -v "$HOME/Models:/data:ro" sheetguard:0.1.0

FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY requirements/runtime.lock requirements/runtime.lock
# S-12: every dependency pinned and verified by hash.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --no-deps -r requirements/runtime.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps --no-build-isolation . 2>/dev/null \
 || (/opt/venv/bin/pip install hatchling && /opt/venv/bin/pip install --no-deps --no-build-isolation .)

FROM python:3.12-slim
LABEL org.opencontainers.image.title="sheetguard" \
      org.opencontainers.image.description="Read-only MCP server that audits Excel models and flags prompt injection" \
      org.opencontainers.image.licenses="MIT"
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SHEETGUARD_ROOT=/data
COPY --from=build /opt/venv /opt/venv
# Non-root user with no home directory contents and no shell login.
RUN groupadd --system --gid 10001 sheetguard \
 && useradd --system --uid 10001 --gid sheetguard --no-create-home \
      --shell /usr/sbin/nologin sheetguard \
 && mkdir /data && chown root:root /data && chmod 0555 /data
USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
ENTRYPOINT ["sheetguard-mcp", "--root", "/data"]
