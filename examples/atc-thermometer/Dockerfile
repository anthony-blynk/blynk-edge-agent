FROM debian:trixie-slim

# bleak isn't a Debian package - pip it, same --break-system-packages
# reasoning as the usb-barcode-scanner example's Dockerfile (isolated,
# single-purpose container image, not the shared host).
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --break-system-packages bleak paho-mqtt

WORKDIR /app
COPY atc_thermometer_bridge.py .

CMD ["python3", "-u", "atc_thermometer_bridge.py"]
