FROM python:3.11.13-slim@sha256:9bffe4353b925a1656688797ebc68f9c525e79b1d377a764d232182a519eeec4

RUN useradd --uid 1000 --create-home user
COPY fake_hermes.py /usr/local/bin/hermes
RUN chmod 0555 /usr/local/bin/hermes
USER user
ENTRYPOINT []
CMD ["tail", "-f", "/dev/null"]
