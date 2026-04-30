FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      coreutils \
      findutils \
      gawk \
      procps \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/rpi5_docker_smoke.sh /usr/local/bin/rpi5_docker_smoke.sh
RUN chmod +x /usr/local/bin/rpi5_docker_smoke.sh

ENTRYPOINT ["/usr/local/bin/rpi5_docker_smoke.sh"]
