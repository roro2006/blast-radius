# Same base image the scorer labeled with.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# Runtime test verification (verify.py) is off by default: on the practice board it cost
# ~2.5 s CPU and saved ~0.01 s. Build with --build-arg VERIFY=1 to turn it on; that installs
# sqlglot's test dependencies, precompiles bytecode and records the baseline (~3 min).
ARG VERIFY=0
ENV VERIFY=$VERIFY
COPY requirements.lock /app/requirements.lock
RUN if [ "$VERIFY" = 1 ]; then pip install --no-cache-dir -r /app/requirements.lock; fi
# sqlglot source at the pinned commit: the selector reads it to apply patches and parse code.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import urllib.request,tarfile,io; \
d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/$SQLGLOT_SHA.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(d)).extractall('/app', filter='data')" \
 && mv /app/sqlglot-$SQLGLOT_SHA /app/sqlglot && chmod -R a+rX /app
COPY selector.py verify.py /app/
COPY maps /app/maps
# Verification only: bytecode for the symlinked mirror the verifier builds at /tmp/w (cached
# under /app/pyc), and tests that fail on the unpatched tree in any of several environments
# (default, other hash seed, UTC+14 time zone, reversed order), which it never drops.
ENV PYTHONPYCACHEPREFIX=/app/pyc SKIP_INTEGRATION=0
RUN if [ "$VERIFY" = 1 ]; then \
    python -c "import sys; sys.path.insert(0, '/app'); import verify; verify.build_farm('/app/sqlglot')" \
 && python -m compileall -q /tmp/w/sqlglot /tmp/w/tests \
 && (cd /tmp/w && python -m pytest -q -p no:cacheprovider -p no:xdist --collect-only tests > /dev/null) \
 && rm -rf /tmp/w \
 && python /app/verify.py baseline /app/sqlglot /app/baseline_fail.json \
 && rm -rf /tmp/w; fi \
 && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
# Not -I: the verifier and its spawned test workers need PYTHONPYCACHEPREFIX.
CMD ["python", "-s", "/app/selector.py"]
