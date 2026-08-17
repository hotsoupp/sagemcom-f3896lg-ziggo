# Build the dependencies in a throwaway stage so pip, its cache and the build
# tooling never reach the final image.
FROM python:3.14-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# Final image: just Python, the installed packages, and the two scripts.
FROM python:3.14-slim

# Fail fast, don't buffer logs, don't litter .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Inside a container the exporter must listen on all interfaces, keep it
    # private by publishing the port to 127.0.0.1 on the host (see README).
    MODEM_EXPORTER_BIND=0.0.0.0 \
    MODEM_EXPORTER_PORT=9105

# Copy the pre-built packages from the builder stage.
COPY --from=builder /install /usr/local

# Run as an unprivileged user, never root.
RUN useradd --system --no-create-home --uid 10001 exporter
WORKDIR /app
COPY modem.py exporter.py ./
USER exporter

EXPOSE 9105

# slim has no curl, so probe the endpoint with the interpreter we already have.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; port=os.environ.get('MODEM_EXPORTER_PORT','9105'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics', timeout=4).status==200 else 1)"]

ENTRYPOINT ["python", "exporter.py"]
