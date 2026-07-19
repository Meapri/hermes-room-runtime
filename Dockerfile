FROM python:3.11.13-slim@sha256:9bffe4353b925a1656688797ebc68f9c525e79b1d377a764d232182a519eeec4

ARG HERMES_AGENT_VERSION=0.18.2

RUN useradd --uid 1000 --create-home --shell /bin/bash user \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git ripgrep xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "hermes-agent==${HERMES_AGENT_VERSION}"

WORKDIR /home/user
RUN mkdir -p /home/user/workspace && chown -R user:user /home/user

USER user
ENV HOME=/home/user \
    HERMES_SKIP_UPDATES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The runtime overrides this with the same command explicitly. Keeping the image
# entrypoint-free avoids `hermes tail -f /dev/null` argument composition.
ENTRYPOINT []
CMD ["tail", "-f", "/dev/null"]
