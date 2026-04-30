FROM python:3.12-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      procps \
      tar \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/rpi5_realistic_docker_test.sh /usr/local/bin/rpi5_realistic_docker_test.sh
RUN chmod +x /usr/local/bin/rpi5_realistic_docker_test.sh

ENTRYPOINT ["/usr/local/bin/rpi5_realistic_docker_test.sh"]
