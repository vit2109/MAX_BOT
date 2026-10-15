FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache

COPY *.py ./
RUN python ca_bundle.py /opt/max-ca-bundle.pem
ENV SSL_CERT_FILE=/opt/max-ca-bundle.pem
COPY web/ ./web/
COPY config.ini channels.txt recipients.txt keywords.json keywords.txt ./
RUN mkdir -p /app/data /app/output

EXPOSE 8000
CMD ["python", "main.py", "--host", "0.0.0.0", "--port", "8000"]
