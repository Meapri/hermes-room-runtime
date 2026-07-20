FROM python:3.11.13-slim@sha256:9bffe4353b925a1656688797ebc68f9c525e79b1d377a764d232182a519eeec4

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tinyproxy \
    && rm -rf /var/lib/apt/lists/*

USER tinyproxy
ENTRYPOINT ["/usr/bin/tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
