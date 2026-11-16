# Kavita Bridge. Standard library only, so there is nothing to pip install.
#
# Build with the repository root as the context, so the GPL-3.0 licence files
# travel with the code:
#
#   docker build -f plugins/kavita-bridge/Dockerfile -t kavita-bridge .
#
# The repository publishes no image for this plugin; build it yourself. The
# base image is python:3.12-slim pinned by digest; update both together.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# A container binds every interface, so the bridge refuses to start until
# KAVITA_BRIDGE_API_KEY is a key of at least 16 characters.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KAVITA_BRIDGE_BIND_HOST=0.0.0.0

WORKDIR /app
COPY LICENSE COPYRIGHT /app/
COPY plugins/kavita-bridge/kavita_bridge /app/kavita_bridge

RUN useradd --system --uid 10001 --no-create-home bridge
USER 10001

EXPOSE 8103
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/v1/health' % os.environ.get('KAVITA_BRIDGE_PORT', '8103'), timeout=3)"]

CMD ["python", "-m", "kavita_bridge"]
