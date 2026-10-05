# Mock SAP Gateway for the local sandbox. Standard library only.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

RUN groupadd --system --gid 10002 mocksap \
 && useradd --system --uid 10002 --gid 10002 --no-create-home --shell /usr/sbin/nologin mocksap \
 && mkdir -p /data && chown 10002:10002 /data

COPY server.py /srv/server.py

USER 10002:10002
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/_mock/healthz', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["python", "/srv/server.py"]
