# Hosted deployment: one Google credential, many connecting AI tools.
FROM python:3.13-slim AS build
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install ".[http]"

FROM python:3.13-slim
# Unbuffered, or startup lines sit in a buffer and the container looks silent.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # The container filesystem is ephemeral and unreachable; summaries must stand alone.
    SEO_MCP_EXPORTS=0 \
    # Cache and profile state go somewhere writable by a non-root user.
    SEO_MCP_HOME=/tmp/seo-mcp \
    PORT=8080
COPY --from=build /install /usr/local
# Never run as root, and never bake a credential into the image: the service-account
# key arrives at runtime via SEO_MCP_SERVICE_ACCOUNT_JSON from Secret Manager.
RUN useradd --uid 10001 --create-home app
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz',timeout=3)"
CMD ["sh", "-c", "seo-analytics-mcp serve-http --host 0.0.0.0 --port ${PORT}"]
