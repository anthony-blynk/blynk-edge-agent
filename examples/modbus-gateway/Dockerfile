FROM debian:trixie-slim

# pymodbus, pyserial and paho-mqtt are all plain Python - no apt packages or
# build toolchain needed. --break-system-packages: Debian trixie's system
# python3 is PEP 668 locked by default; this is an isolated, single-purpose
# container image, not the shared host, so that's an acceptable, narrow
# exception here (same reasoning as this project's other examples).
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --break-system-packages "pymodbus>=3,<4" pyserial "paho-mqtt>=2,<3"

WORKDIR /app
COPY modbus_gateway ./modbus_gateway

CMD ["python3", "-u", "-m", "modbus_gateway"]
