FROM ghcr.io/astral-sh/uv:python3.13-bookworm AS deps

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv export --no-dev --format requirements-txt -o requirements.txt


FROM python:latest AS calibre-build

ARG CALIBRE_VERSION=9.6.0

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && case "$(uname -m)" in \
         x86_64)  CALIBRE_ARCH="x86_64" ;; \
         aarch64) CALIBRE_ARCH="arm64" ;; \
         *)       echo "Unsupported arch: $(uname -m)"; exit 1 ;; \
       esac \
    && curl -fsSL \
         "https://download.calibre-ebook.com/${CALIBRE_VERSION}/calibre-${CALIBRE_VERSION}-${CALIBRE_ARCH}.txz" \
         -o /tmp/calibre.txz \
    && mkdir -p /opt/calibre \
    && tar xJf /tmp/calibre.txz -C /opt/calibre \
    && rm /tmp/calibre.txz \
    && rm -f /opt/calibre/lib/libQt6Designer* \
             /opt/calibre/lib/libQt6Multimedia* \
             /opt/calibre/lib/libQt6SpatialAudio.so.* \
             /opt/calibre/lib/libQt6NetworkAuth.so.* \
             /opt/calibre/lib/libQt6Concurrent.so.* \
             /opt/calibre/lib/libQt6OpenGLWidgets.so.* \
             /opt/calibre/lib/libQt6QuickWidgets.so.* \
             /opt/calibre/lib/libavcodec.so.* \
             /opt/calibre/lib/libavfilter.so.* \
             /opt/calibre/lib/libavformat.so.* \
             /opt/calibre/lib/libavutil.so.* \
             /opt/calibre/lib/libavdevice.so.* \
             /opt/calibre/lib/libpostproc.so.* \
             /opt/calibre/lib/libswresample.so.* \
             /opt/calibre/lib/libswscale.so.* \
             /opt/calibre/lib/libspeex.so.* \
             /opt/calibre/lib/libFLAC.so.* \
             /opt/calibre/lib/libopus.so.* \
             /opt/calibre/lib/libvorbis*.so.* \
             /opt/calibre/lib/libasyncns.so.* \
             /opt/calibre/lib/libspeechd.so.* \
             /opt/calibre/lib/libespeak-ng.so.* \
             /opt/calibre/lib/libonnxruntime.so.* \
             /opt/calibre/lib/libgio-2.0.so.* \
             /opt/calibre/lib/libzstd.so.* \
             /opt/calibre/lib/libhunspell-1.7.so.* \
             /opt/calibre/lib/libbrotlienc.so.* \
             /opt/calibre/lib/libbrotlicommon.so.* \
             /opt/calibre/lib/libbrotlidec.so.* \
             /opt/calibre/lib/libstemmer.so.* \
             /opt/calibre/lib/libmtp.so.* \
             /opt/calibre/lib/libncursesw.so.* \
             /opt/calibre/lib/libchm.so.* \
             /opt/calibre/lib/libgcrypt.so.* \
             /opt/calibre/lib/libgpg-error.so.* \
             /opt/calibre/lib/libicuio.so.* \
             /opt/calibre/lib/libreadline.so.* \
             /opt/calibre/lib/libusb-1.0.so.* \
    && rm -rf /opt/calibre/lib/qt6/plugins/platformthemes \
              /opt/calibre/lib/qt6/plugins/multimedia \
              /opt/calibre/lib/qt6/plugins/designer \
              /opt/calibre/lib/qt6/plugins/qmltooling \
    && rm -f /opt/calibre/calibre \
             /opt/calibre/calibre-server \
             /opt/calibre/calibre-smtp \
             /opt/calibre/calibre-debug \
             /opt/calibre/calibre-customize \
             /opt/calibre/calibredb \
             /opt/calibre/ebook-viewer \
             /opt/calibre/ebook-edit \
             /opt/calibre/ebook-polish \
             /opt/calibre/ebook-device \
             /opt/calibre/fetch-ebook-metadata \
             /opt/calibre/lrf2lrs \
             /opt/calibre/lrs2lrf \
             /opt/calibre/markdown-calibre \
             /opt/calibre/web2disk \
    && rm -rf /opt/calibre/lib/calibre/gui2 \
              /opt/calibre/lib/calibre/devices \
              /opt/calibre/lib/calibre/library \
              /opt/calibre/lib/calibre/db \
              /opt/calibre/lib/calibre/srv \
              /opt/calibre/lib/calibre/spell \
              /opt/calibre/lib/calibre/live \
    && rm -rf /opt/calibre/resources/images \
              /opt/calibre/resources/icons \
              /opt/calibre/resources/icons.rcc \
              /opt/calibre/resources/content-server \
              /opt/calibre/resources/editor* \
              /opt/calibre/resources/viewer \
              /opt/calibre/resources/viewer.js \
              /opt/calibre/resources/viewer.html \
              /opt/calibre/resources/recipes \
              /opt/calibre/resources/dictionaries \
              /opt/calibre/resources/hyphenation \
              /opt/calibre/resources/catalog \
              /opt/calibre/resources/calibre-mimetypes.xml \
              /opt/calibre/resources/changelog.json \
              /opt/calibre/resources/user-agent-data.json \
              /opt/calibre/resources/builtin_recipes.zip \
              /opt/calibre/resources/builtin_recipes.xml


FROM python:latest

ARG BW_VERSION=2025.11.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MOZ_HEADLESS=1 \
    PATH="/opt/calibre:${PATH}"

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        firefox-esr \
        unzip \
        libnss3 \
        libfontconfig1 \
        libgl1 \
        libegl1 \
        libdbus-1-3 \
        libxcomposite1 \
        libxrandr2 \
        libxkbcommon0 \
        libxi6 \
        libxtst6 \
        libopengl0 \
    && curl -fsSL -o /tmp/bw-linux.zip "https://github.com/bitwarden/clients/releases/download/cli-v${BW_VERSION}/bw-linux-${BW_VERSION}.zip" \
    && unzip -o /tmp/bw-linux.zip -d /tmp \
    && install -m 755 /tmp/bw /usr/local/bin/bw \
    && rm -rf /var/lib/apt/lists/* /tmp/bw-linux.zip /tmp/bw

COPY --from=calibre-build /opt/calibre /opt/calibre
COPY --from=deps /app/requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

CMD ["python", "service.py"]
