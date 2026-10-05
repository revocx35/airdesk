# Build context: the repository root (see docker-compose.yml)
FROM python:3.12-slim AS vdl2
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake pkg-config git ca-certificates \
      libglib2.0-dev zlib1g-dev libxml2-dev libjansson-dev && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch v2.2.1 https://github.com/szpajder/libacars /src/libacars \
 && cmake -S /src/libacars -B /src/libacars/build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build /src/libacars/build -j"$(nproc)" && cmake --install /src/libacars/build
RUN git clone --depth 1 --branch v2.7.0 https://github.com/szpajder/dumpvdl2 /src/dumpvdl2 \
 && cmake -S /src/dumpvdl2 -B /src/dumpvdl2/build -DCMAKE_BUILD_TYPE=Release -DRTLSDR=FALSE -DSOAPYSDR=FALSE \
      -DMIRISDK=FALSE -DSDRPLAY=FALSE -DSDRPLAY3=FALSE -DZMQ=FALSE -DPROFILING=FALSE \
 && cmake --build /src/dumpvdl2/build -j"$(nproc)" && cmake --install /src/dumpvdl2/build

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libxml2 libjansson4 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=vdl2 /usr/local/lib/libacars-2.so* /usr/local/lib/
COPY --from=vdl2 /usr/local/bin/dumpvdl2 /usr/local/bin/dumpvdl2
RUN ldconfig && dumpvdl2 --version
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/sdrcommon ./sdrcommon
COPY airdesk/airdesk ./airdesk

FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
WORKDIR /src
COPY conftest.py ./
COPY common ./common
COPY airdesk ./airdesk
RUN PYTHONPATH= python -m pytest -q -p no:warnings common/tests airdesk/tests

FROM base AS app
RUN useradd --system --uid 10001 airdesk && mkdir -p /data && chown airdesk /data
USER airdesk
ENV APP_DATA=/data
VOLUME /data
EXPOSE 8097
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('AIRDESK_HTTP_PORT','8097'), timeout=4)"
CMD ["python", "-m", "airdesk"]
