FROM ghcr.io/astral-sh/uv:0.11.30 AS uv
FROM python:3.11-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends skopeo ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 uploader && useradd -u 10001 -g uploader -m uploader
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock LICENSE README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev \
    && mkdir -p /data/input /data/processing /data/success /data/error \
    && chown -R uploader:uploader /data \
    && chmod -R a+rX /app/src \
    && chmod a+r /app/LICENSE /app/README.md
# Compressed docker-archive sources may require image-sized temporary files.
# Keep these on the writable data volume, not the read-only root or small tmpfs.
ENV PATH="/app/.venv/bin:$PATH" TMPDIR=/data/processing
USER 10001:10001
ENTRYPOINT ["nexus-auto-uploader"]
