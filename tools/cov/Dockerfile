# Builds the selection maps (run locally, output committed under maps/).
# Same base image the scorer labeled with, so Python/SQLite match.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /tmp/req.lock
RUN pip install --no-cache-dir -r /tmp/req.lock coverage==7.10.7
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import urllib.request,tarfile,io,sys; \
d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/$SQLGLOT_SHA.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(d)).extractall('/', filter='data')" \
 && mv /sqlglot-$SQLGLOT_SHA /sqlglot
ENV PYTHONPATH=/sqlglot SKIP_INTEGRATION=0 COVERAGE_CORE=ctrace PYTHONDONTWRITEBYTECODE=1
COPY ctxplugin.py dump.py run.sh /cov/
WORKDIR /sqlglot
CMD ["bash", "/cov/run.sh"]
