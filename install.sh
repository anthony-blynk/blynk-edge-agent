#!/usr/bin/env bash
set -e

OWNER="anthony-blynk"
REPO="blynk-edge-agent"
BRANCH="master"
RAW_BASE="https://raw.githubusercontent.com/$OWNER/$REPO/$BRANCH"
STATE_DIR=/opt/blynk

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"
  echo "Docker installed. Log out and back in (group membership needs a new session), then re-run:"
  echo "  curl -fsSL $RAW_BASE/install.sh | bash"
  exit 0
fi

if ! docker info >/dev/null 2>&1; then
  # docker existing doesn't mean this user can actually talk to the daemon
  # - confirmed on both a Jetson and a freshly re-imaged CompuLab
  # IOT-GATE-iMX8, where a pre-installed Docker never added the invoking
  # user to the `docker` group at all (that only happens above, in the
  # branch where this script installs Docker itself). `docker compose
  # version` below doesn't catch this - it only checks the plugin's own
  # version, not daemon connectivity - so this needs its own check.
  echo "Can't talk to the Docker daemon as this user - adding to the docker group..."
  sudo usermod -aG docker "$USER"
  echo "Added. Log out and back in (group membership needs a new session), then re-run:"
  echo "  curl -fsSL $RAW_BASE/install.sh | bash"
  exit 0
fi

if ! docker compose version >/dev/null 2>&1; then
  # docker existing doesn't mean `docker compose` does - confirmed on a
  # CompuLab IOT-GATE-iMX8 pre-installed with Microsoft's Azure IoT Edge
  # "moby-engine" Docker build, which ships docker itself but not the
  # compose v2 plugin, and whose own apt repo doesn't carry
  # docker-compose-plugin either. Installing the standalone plugin binary
  # directly fixes this without touching the existing docker install.
  echo "docker compose not found - installing the compose plugin..."
  mkdir -p "$HOME/.docker/cli-plugins"
  curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
    -o "$HOME/.docker/cli-plugins/docker-compose"
  chmod +x "$HOME/.docker/cli-plugins/docker-compose"
  if ! docker compose version >/dev/null 2>&1; then
    echo "Failed to install docker compose automatically - install it manually and re-run this script."
    exit 1
  fi
fi

sudo mkdir -p "$STATE_DIR/backups" "$STATE_DIR/mqtt-bridge/conf.d"
sudo chown -R "$USER":"$USER" "$STATE_DIR"

if [ ! -f "$STATE_DIR/docker-compose.yml" ]; then
  curl -fsSL "$RAW_BASE/docker-compose.yml" -o "$STATE_DIR/docker-compose.yml"
  echo "Installed docker-compose.yml to $STATE_DIR"
else
  echo "$STATE_DIR/docker-compose.yml already exists, leaving it alone"
fi

if [ ! -f "$STATE_DIR/blynk.env" ]; then
  # Piping this script through `curl | bash` connects stdin to the pipe, not
  # the keyboard - reading from /dev/tty explicitly is what makes these
  # prompts actually work rather than silently fail or hang.
  echo "Enter this device's Blynk credentials:"
  echo "(leave server/auth token blank to provision this device later via"
  echo "the Blynk app over Bluetooth - template ID is still required now,"
  echo "it identifies the product itself rather than something the app hands over)"
  read -r -p "BLYNK_SERVER (e.g. lon1.blynk.cloud), or leave blank: " BLYNK_SERVER </dev/tty
  read -r -p "BLYNK_TEMPLATE_ID: " BLYNK_TEMPLATE_ID </dev/tty
  read -r -s -p "BLYNK_AUTH_TOKEN, or leave blank: " BLYNK_AUTH_TOKEN </dev/tty
  echo
  # Only relevant for Blynk Enterprise clients running their own server and
  # a branded mobile app - replaces "Blynk" in the BLE-advertised device
  # name and the provisioning "vendor" field. Leave blank for the normal
  # Blynk Cloud/app.
  read -r -p "BLYNK_VENDOR_PREFIX (white-label branding, leave blank for 'Blynk'): " BLYNK_VENDOR_PREFIX </dev/tty
  cat > "$STATE_DIR/blynk.env" <<EOF
BLYNK_SERVER=$BLYNK_SERVER
BLYNK_TEMPLATE_ID=$BLYNK_TEMPLATE_ID
BLYNK_AUTH_TOKEN=$BLYNK_AUTH_TOKEN
BLYNK_VENDOR_PREFIX=${BLYNK_VENDOR_PREFIX:-Blynk}
EOF
  echo "Wrote $STATE_DIR/blynk.env"
  if [ -z "$BLYNK_AUTH_TOKEN" ]; then
    echo "No auth token entered - the agent will start in BLE provisioning mode"
    echo "on first boot. Open the Blynk app and add this device to finish setup."
  fi
else
  echo "$STATE_DIR/blynk.env already exists, leaving it alone"
fi

# Confirmed on a CompuLab IOT-GATE-iMX8 running an older BlueZ: EATT
# (Enhanced ATT) being active means bluez_peripheral's lack of stable GATT
# attribute handles across container restarts can trigger Android's Robust
# Caching feature into an unexpected "Pair with device?" prompt when a
# phone reconnects to a device whose handle layout has since changed.
# Channels = 1 disables EATT entirely, stopping that specific symptom.
# BlueZ itself disabled EATT by default upstream from March 2023
# (bluez/bluez@ab3ff0d2, "main: Disable EATT by default") - so on any
# device already running a BlueZ from after that change, this setting is
# already the default and setting it explicitly here is a no-op. It's kept
# as an explicit, version-independent guarantee for anything running an
# older BlueZ that still defaults the other way, not because every device
# needs it. Note this does NOT prevent every stale-handle symptom - a
# Raspberry Pi 5 with EATT already off by default still hit a *different*
# manifestation of the same underlying handle-instability issue (an
# outright ATT "Invalid Handle" error via Android's plain, EATT-independent
# GATT cache) - see the README Troubleshooting entry for that case, whose
# actual fix is clearing the phone's cached pairing for the device, not
# anything in this file.
BLUEZ_CONF=/etc/bluetooth/main.conf
if [ -f "$BLUEZ_CONF" ] && ! grep -q "^Channels = 1" "$BLUEZ_CONF"; then
  if grep -q "^Channels" "$BLUEZ_CONF"; then
    sudo sed -i "s/^Channels.*/Channels = 1/" "$BLUEZ_CONF"
  elif grep -q "^\[GATT\]" "$BLUEZ_CONF"; then
    sudo sed -i "/^\[GATT\]/a Channels = 1" "$BLUEZ_CONF"
  else
    printf '\n[GATT]\nChannels = 1\n' | sudo tee -a "$BLUEZ_CONF" >/dev/null
  fi
  sudo systemctl restart bluetooth
  echo "Disabled BLE EATT (main.conf [GATT] Channels = 1) - avoids a known BlueZ issue where repeated BLE provisioning can silently break (see README Troubleshooting)"
fi

echo "Pulling images..."
if ! docker compose -f "$STATE_DIR/docker-compose.yml" pull; then
  echo "Pull failed, cloning repo to build locally instead..."
  REPO_DIR="$HOME/$REPO"
  if [ -d "$REPO_DIR/.git" ]; then
    git -C "$REPO_DIR" pull
  else
    if ! command -v git >/dev/null 2>&1; then
      sudo apt-get update && sudo apt-get install -y git
    fi
    git clone "https://github.com/$OWNER/$REPO.git" "$REPO_DIR"
  fi
  (cd "$REPO_DIR" && docker compose build)
fi

# Confirmed on a RAK LoRaWAN gateway already running its own mosquitto on
# 1883 for its packet-forwarder/cloud bridge - `docker compose up` would
# otherwise just fail deep inside with an opaque "address already in use"
# error, giving no hint that another host port is the actual fix (this same
# fix is in the README's Troubleshooting section, but better to catch it here
# than send someone there after a confusing failure). Skips quietly if `ss`
# isn't available rather than blocking the install over a missing diagnostic
# tool - not worth failing installation over.
HOST_MQTT_PORT=$(grep -oE '127\.0\.0\.1:[0-9]+:1883' "$STATE_DIR/docker-compose.yml" | head -1 | cut -d: -f2)
if [ -n "$HOST_MQTT_PORT" ] && command -v ss >/dev/null 2>&1 && ss -tln 2>/dev/null | grep -q ":$HOST_MQTT_PORT "; then
  echo "Port $HOST_MQTT_PORT is already in use by something else on this device"
  echo "(common on gateways already running their own MQTT broker) - mqtt-bridge"
  echo "needs its own free host port instead (this doesn't affect the actual"
  echo "Blynk bridge, which talks to mqtt-bridge over Docker's internal network"
  echo "regardless of this host-side mapping)."
  read -r -p "Host port for mqtt-bridge, or press Enter to use 18830: " NEW_MQTT_PORT </dev/tty
  NEW_MQTT_PORT=${NEW_MQTT_PORT:-18830}
  sed -i "s/127.0.0.1:$HOST_MQTT_PORT:1883/127.0.0.1:$NEW_MQTT_PORT:1883/" "$STATE_DIR/docker-compose.yml"
  echo "Remapped mqtt-bridge's host port to $NEW_MQTT_PORT in $STATE_DIR/docker-compose.yml"
fi

echo "Starting stack..."
docker compose -f "$STATE_DIR/docker-compose.yml" up -d

echo "Done. Check status with: docker compose -f $STATE_DIR/docker-compose.yml ps"
echo
echo "This only ever runs once - from here, updates go through Blynk OTA."
echo "When a new version is available, fetch the latest docker-compose.yml:"
echo "  $RAW_BASE/docker-compose.yml"
echo "and upload it through your Blynk console's OTA feature for this device."
echo "(If you've added your own service(s) to $STATE_DIR/docker-compose.yml, merge"
echo "the version/image changes into your copy rather than overwriting it wholesale.)"
