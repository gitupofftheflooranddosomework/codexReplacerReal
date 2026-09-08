FROM node:24-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       bash build-essential ca-certificates curl gh git jq openssh-client \
       procps python3 python3-pip python3-venv ripgrep rsync unzip zip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
USER node
CMD ["sleep", "infinity"]
