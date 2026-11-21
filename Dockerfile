# Same base image the scorer labeled with.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# sqlglot source at the pinned commit: the selector reads it to apply patches and parse code.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import urllib.request,tarfile,io; \
d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/$SQLGLOT_SHA.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(d)).extractall('/app', filter='data')" \
 && mv /app/sqlglot-$SQLGLOT_SHA /app/sqlglot && chmod -R a+rX /app
COPY selector.py /app/selector.py
COPY maps /app/maps
RUN chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-I", "/app/selector.py"]
