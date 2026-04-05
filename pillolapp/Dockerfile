ARG BUILD_FROM
FROM $BUILD_FROM

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
