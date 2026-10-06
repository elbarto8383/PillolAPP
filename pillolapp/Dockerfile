# Dal Supervisor 2026.04 BUILD_FROM non viene più fornito automaticamente: serve un default.
# Immagine base multi-arch (amd64/aarch64); le versioni vecchie del Supervisor possono ancora sovrascriverla.
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.22
FROM $BUILD_FROM

LABEL \
    io.hass.type="addon" \
    io.hass.name="PillolAPP" \
    io.hass.description="Gestione farmaci: promemoria Telegram + Alexa, scorte, astuccio settimanale" \
    io.hass.url="https://github.com/elbarto8383/PillolAPP"

ENV LANG C.UTF-8
ENV TZ Europe/Rome

RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-pillow \
    tesseract-ocr \
    tesseract-ocr-data-ita \
    jq \
    tzdata \
    curl \
    bash

RUN cp /usr/share/zoneinfo/Europe/Rome /etc/localtime && \
    echo "Europe/Rome" > /etc/timezone

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x run.sh

CMD ["/app/run.sh"]
