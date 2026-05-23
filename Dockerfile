FROM python:3.12-slim

WORKDIR /app

COPY relaystation.py /app/relaystation.py

RUN useradd --create-home --shell /usr/sbin/nologin relaystation \
    && mkdir -p /data \
    && chown -R relaystation:relaystation /data /app

USER relaystation

EXPOSE 8787

CMD ["python", "/app/relaystation.py", "server", "--bind", "0.0.0.0", "--port", "8787", "--db", "/data/relaystation.sqlite", "--quiet"]
