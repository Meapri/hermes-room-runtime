# Minimal, hardened container image for running a Hermes agent in isolation.
# The agent code itself comes from the public `hermes-agent` PyPI package;
# model API keys are injected at runtime via -e (never baked into the image).
FROM python:3.11-slim

# Non-root user — the agent never runs as root inside the container.
RUN useradd --create-home --shell /bin/bash user

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir hermes-agent

WORKDIR /home/user
RUN mkdir -p /home/user/.hermes /home/user/workspace && chown -R user:user /home/user

USER user
ENV HOME=/home/user \
    HERMES_HOME=/home/user/.hermes \
    HERMES_YOLO_MODE=1 \
    HERMES_SKIP_UPDATES=1

# Started as a persistent container (see manager); commands run via `docker exec`.
ENTRYPOINT ["hermes"]
CMD ["--help"]
