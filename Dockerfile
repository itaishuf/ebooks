FROM ghcr.io/astral-sh/uv:python3.13-bookworm AS deps

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv export --no-dev --format requirements-txt -o requirements.txt


FROM python:latest

ARG BW_VERSION=2025.11.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MOZ_HEADLESS=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        firefox-esr \
        unzip \
    && curl -fsSL -o /tmp/bw-linux.zip "https://github.com/bitwarden/clients/releases/download/cli-v${BW_VERSION}/bw-linux-${BW_VERSION}.zip" \
    && unzip -o /tmp/bw-linux.zip -d /tmp \
    && install -m 755 /tmp/bw /usr/local/bin/bw \
    && rm -rf /var/lib/apt/lists/* /tmp/bw-linux.zip /tmp/bw

COPY --from=deps /app/requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

CMD ["python", "service.py"]
