FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN addgroup --system --gid 10001 rommates \
    && adduser --system --uid 10001 --ingroup rommates --home /app rommates

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# World-readable so the image still works when compose overrides the runtime user with
# PUID/PGID (an arbitrary uid with no entry in /etc/passwd). /data and /emulation are
# bind mounts in practice, so their ownership comes from the host, not from here.
RUN mkdir -p /data /emulation \
    && chown -R rommates:rommates /app /data /emulation \
    && chmod -R a+rX /app

# Default for a bare `docker run`. compose.yaml overrides this with PUID/PGID so the
# container writes ROM files as the account that owns the Emulation directory.
USER rommates
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers"]
