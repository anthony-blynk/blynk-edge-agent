FROM debian:trixie-slim

# python3-evdev via apt, not pip: it's a C extension wrapping Linux's
# /dev/input ioctls, and Debian's prebuilt package (confirmed working on
# this exact base OS - a Pi running Debian trixie) avoids needing a build
# toolchain in the image. paho-mqtt is plain Python, pip is fine for that -
# --break-system-packages since Debian trixie's system python3 is PEP 668
# locked by default; this is an isolated, single-purpose container image,
# not the shared host, so that's an acceptable, narrow exception here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-evdev \
    python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --break-system-packages paho-mqtt

WORKDIR /app
COPY barcode_bridge.py .

CMD ["python3", "-u", "barcode_bridge.py"]
