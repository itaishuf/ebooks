FROM python:latest-bookworm

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
    && curl -fsSL -o /tmp/bw-linux.zip "https://vault.bitwarden.com/download/?app=cli&platform=linux" \
    && unzip -o /tmp/bw-linux.zip -d /tmp \
    && install -m 755 /tmp/bw /usr/local/bin/bw \
    && rm -rf /var/lib/apt/lists/* /tmp/bw-linux.zip /tmp/bw

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

CMD ["python", "service.py"]
