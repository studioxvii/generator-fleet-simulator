# syntax=docker/dockerfile:1

# ── Stage 1: install deps + compile source ───────────────────────────────────
FROM python:3.11-alpine@sha256:0d55920083f1ce1e38ac292e2772f924b4f8bb4188d336c79bf66963039e6146 AS builder
WORKDIR /build

# Install the reviewed, hashed runtime dependency set first. The layer remains
# cached until the release lock changes.
COPY requirements.lock .
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock \
 && /venv/bin/python -m pip uninstall -y pip setuptools wheel

# Compile all .py → adjacent .pyc, then strip source and dev artifacts
COPY . .
RUN /venv/bin/python -m compileall -b -q . \
 && find . -name "*.py" -delete \
 && rm -f requirements.txt requirements.lock requirements-dev.txt requirements-dev.lock requirements-audit.txt requirements-audit.lock pyproject.toml setup.cfg setup.py pytest.ini

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.11-alpine@sha256:0d55920083f1ce1e38ac292e2772f924b4f8bb4188d336c79bf66963039e6146
WORKDIR /app

COPY --from=builder /venv /venv
COPY --from=builder /build /app

# Non-root user + writable data directory for state persistence
RUN apk add --no-cache --upgrade libuuid=2.42.3-r1 \
 && python -m pip uninstall -y pip setuptools wheel \
 && addgroup -S simulator \
 && adduser -S -D -u 1000 -G simulator -h /app -s /bin/false simulator \
 && mkdir -p /data \
 && chown -R simulator:simulator /data \
 && chmod -R a=rX,u+w /app

ENV PATH="/venv/bin:$PATH" \
    GENSIM_WEB_HOST=0.0.0.0 \
    GENSIM_WEB_PORT=5000 \
    GENSIM_MODBUS_HOST=0.0.0.0 \
    GENSIM_MODBUS_PORT=5020 \
    GENSIM_STATE_FILE=/data/generator_state.json \
    GENSIM_RUNBOOKS_FILE=/data/generator_runbooks.json

USER simulator

EXPOSE 5000 5020-5027

VOLUME ["/data"]

# Health check confirms the web server is accepting API requests. Readiness is
# exposed separately at /api/ready after the simulator is configured.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:'+os.environ.get('GENSIM_WEB_PORT','5000')+'/api/live')" || exit 1

CMD ["sh", "-c", "exec gunicorn --worker-class gthread --workers 1 --threads ${GENSIM_GUNICORN_THREADS:-100} --bind ${GENSIM_WEB_HOST:-0.0.0.0}:${GENSIM_WEB_PORT:-5000} --timeout ${GENSIM_GUNICORN_TIMEOUT:-120} wsgi:app"]
