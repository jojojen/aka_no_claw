FROM python:3.12-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      procps \
      tar \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/mac_mini_docker_smoke.sh /usr/local/bin/mac_mini_docker_smoke.sh
RUN chmod +x /usr/local/bin/mac_mini_docker_smoke.sh

ENTRYPOINT ["/usr/local/bin/mac_mini_docker_smoke.sh"]
