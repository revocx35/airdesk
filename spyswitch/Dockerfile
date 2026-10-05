# Build context: the repository root (see docker-compose.yml)
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/sdrcommon ./sdrcommon
COPY spyswitch/spyswitch ./spyswitch

FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
WORKDIR /src
COPY conftest.py ./
COPY common ./common
COPY spyswitch ./spyswitch
RUN PYTHONPATH= python -m pytest -q -p no:warnings common/tests spyswitch/tests

FROM base AS app
RUN useradd --system --uid 10001 spyswitch && mkdir -p /data && chown spyswitch /data
USER spyswitch
ENV APP_DATA=/data
VOLUME /data
EXPOSE 8096
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('SPYSWITCH_HTTP_PORT','8096'), timeout=4)"
CMD ["python", "-m", "spyswitch"]
