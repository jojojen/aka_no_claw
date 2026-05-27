FROM python:3.12-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      chromium \
      curl \
      procps \
      tar \
      tesseract-ocr \
      tesseract-ocr-eng \
      tesseract-ocr-jpn \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/mac_mini_realistic_docker_test.sh /usr/local/bin/mac_mini_realistic_docker_test.sh
RUN chmod +x /usr/local/bin/mac_mini_realistic_docker_test.sh

ENTRYPOINT ["/usr/local/bin/mac_mini_realistic_docker_test.sh"]
