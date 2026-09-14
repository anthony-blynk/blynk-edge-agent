#!/usr/bin/env bash
set -e

OWNER="anthony-blynk"
REPO="blynk-edge-agent"
BRANCH="master"
RAW_BASE="https://raw.githubusercontent.com/$OWNER/$REPO/$BRANCH"
STATE_DIR=/opt/blynk

if ! command -v docker >/dev/null 2>&1; then
  echo ""
  echo ""
  echo "Installing Docker (takes a few minutes)..."
  echo ""
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"
  echo ""
  echo ""
  echo "Docker installed."
  echo ""
  echo ""
  echo "Log out and back in (group membership needs a new session), then re-run:"
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
  echo ""
  echo ""
  echo "Added."
  echo ""
  echo ""
  echo "Log out and back in (group membership needs a new session), then re-run:"
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
# mqtt-bridge/conf.d holds files the agent deliberately chowns to
# mosquitto's own fixed uid (1883), not this user - confirmed on real
# hardware that a blanket recursive chown here clobbers that ownership
# every time install.sh gets re-run (e.g. to pick up a later fix), and
# mosquitto then fails outright on its next restart ("Unable to open
# acl_file") since it can no longer read a file it doesn't own - a real
# observed crash loop, not a hypothetical. Excluded from the sweep.
sudo find "$STATE_DIR" -path "$STATE_DIR/mqtt-bridge/conf.d" -prune -o -exec chown "$USER":"$USER" {} \;

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
  echo "the Blynk app over Bluetooth - template ID is still required now)"
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
# Retroactively tightens permissions on a pre-existing file too (content
# untouched) - re-running install.sh is how an already-provisioned device
# would pick this fix up.
chmod 600 "$STATE_DIR/blynk.env"

CURRENT_AUTH_TOKEN=$(grep '^BLYNK_AUTH_TOKEN=' "$STATE_DIR/blynk.env" | cut -d= -f2-)
if [ -z "$CURRENT_AUTH_TOKEN" ]; then
  # BLE provisioning is about to run (no auth token stored) - check for a
  # known Raspberry Pi kernel regression that breaks BLE advertising
  # outright (raspberrypi/linux#7473: MGMT "Add Extended Advertising Data"
  # rejected with Invalid Parameters on 6.18.34/6.18.35, fixed by
  # 58d810354de1b, first shipped in 6.18.36). Confirmed on real hardware (a
  # Pi 5) that this makes the agent container loop "Failed to register
  # advertisement" forever without ever failing hard - this script would
  # otherwise report success while BLE provisioning is silently doomed, so
  # it needs catching before the stack even starts, not left to be found
  # via a confusing docker logs dig afterward.
  KERNEL_VER=$(uname -r)
  case "$KERNEL_VER" in
    6.18.3[45]+rpt-rpi-*)
      echo ""
      echo "*** WARNING ***: this kernel ($KERNEL_VER) has a known Raspberry Pi Bluetooth"
      echo "regression (raspberrypi/linux#7473) that breaks BLE advertising outright -"
      echo "BLE provisioning would fail in an endless loop, since no auth token was"
      echo "entered above."
      echo ""
      echo "Checking whether a fixed kernel is available via apt..."
      sudo apt-get update -qq
      if apt list --upgradable 2>/dev/null | grep -q '^linux-image'; then
        read -r -p "A kernel upgrade is available and should include the fix - install it and reboot now? [Y/n] " UPGRADE_KERNEL </dev/tty
        if [ -z "$UPGRADE_KERNEL" ] || [ "$UPGRADE_KERNEL" = "Y" ] || [ "$UPGRADE_KERNEL" = "y" ]; then
          # Plain `apt-get upgrade` refuses to install new packages as a
          # side effect, and Debian/Raspberry Pi OS ship each kernel
          # version as a separate new package (not an in-place upgrade of
          # the existing one) - confirmed on real hardware that this left
          # the actual linux-image-* package "kept back" while 122
          # unrelated packages upgraded fine, so the reboot below came
          # back on the exact same broken kernel. dist-upgrade is allowed
          # to pull in the new kernel package, which is the whole point.
          sudo apt-get dist-upgrade -y
          echo ""
          echo ""
          echo "Kernel upgraded. Reboot, then re-run this script to finish setup:"
          echo "  curl -fsSL $RAW_BASE/install.sh | bash"
          sudo reboot
          exit 0
        fi
      fi
      echo "No apt kernel upgrade available (or upgrade declined) - pull the fix"
      echo "directly instead:"
      echo "  sudo rpi-update"
      echo "  sudo reboot"
      echo "(or roll back to a known-good kernel - see the README Troubleshooting"
      echo "section for exact commands). Re-run this script afterwards:"
      echo "  curl -fsSL $RAW_BASE/install.sh | bash"
      exit 1
      ;;
  esac
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
  # Disabled BLE EATT (main.conf [GATT] Channels = 1) - see README Troubleshooting
fi

# Cellular USB modems (SIMCom SIM7070/7080/7600 and similar) present a raw
# "net" interface (cdc_ncm/cdc_ether/cdc_mbim/qmi_wwan/rndis_host) alongside
# their AT command ports - ble_provisioning.py's _connect_cellular never
# touches it, it only ever activates a NetworkManager "gsm" (PPP-over-AT)
# connection. Left alone, NetworkManager treats that raw net interface like
# any other newly-appeared Ethernet device: it auto-DHCPs it and activates
# it with a default route at the same metric as the real connection.
# Confirmed on real hardware (a Pi 5 with a SIM7080) that this silently
# black-holed roughly half of all outbound traffic - two default routes at
# equal metric competing for real traffic - despite Ethernet/WiFi being
# perfectly healthy on their own. Marking these interfaces NM_UNMANAGED
# stops NetworkManager from ever touching them. Mirrors the structure of
# NetworkManager's own shipped data/85-nm-unmanaged.rules (which has no
# rule for this case at all - confirmed by reading it upstream), just as a
# separate file per its own header's instructions ("place a file in
# /etc/udev/rules.d" rather than editing the shipped one).
UDEV_RULE=/etc/udev/rules.d/90-blynk-modem-net-unmanaged.rules
UDEV_RULE_CONTENT='SUBSYSTEM=="net", ACTION=="add|change", ENV{ID_NET_DRIVER}=="cdc_ncm|cdc_ether|cdc_mbim|qmi_wwan|rndis_host", ENV{NM_UNMANAGED}="1"'
if [ ! -f "$UDEV_RULE" ] || [ "$(cat "$UDEV_RULE" 2>/dev/null)" != "$UDEV_RULE_CONTENT" ]; then
  echo "$UDEV_RULE_CONTENT" | sudo tee "$UDEV_RULE" >/dev/null
  sudo udevadm control --reload-rules
  sudo udevadm trigger --subsystem-match=net
  # Added udev rule to stop NetworkManager auto-configuring cellular modems' raw net interfaces - see README Troubleshooting
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

echo ""
echo ""
echo "Blynk Edge Agent Installed successfully."
echo ""
echo "Check status with: docker compose -f $STATE_DIR/docker-compose.yml ps"
echo ""
echo "This only ever runs once - from here, updates go through Blynk OTA."
echo "When a new version is available, fetch the latest docker-compose.yml:"
echo "  $RAW_BASE/docker-compose.yml"
echo "and upload it through your Blynk console's OTA feature for this device."
echo "(If you've added your own service(s) to $STATE_DIR/docker-compose.yml, merge"
echo "the version/image changes into your copy rather than overwriting it wholesale.)"
