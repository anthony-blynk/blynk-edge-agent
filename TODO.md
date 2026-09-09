# TODO

Ideas worth revisiting, not yet planned or scheduled.

## Blynk HTTP file-upload proxy in agent.py

Give `agent.py` a generic, app-agnostic local topic (e.g. `local/blynk/upload`) that proxies to Blynk's HTTP-only Device API - starting with [file upload](https://docs.blynk.io/en/blynk.cloud/device-https-api/upload-a-file) (`POST /external/api/upload?token=...`, multipart field `upfile`, 5MB/file, 10 files held per device). A local app publishes raw bytes to that topic; the agent (which already holds the token, to render the mqtt-bridge config) makes the authenticated HTTP call and publishes the resulting URL back on a reply topic (e.g. `local/blynk/upload/result`).

Why: MQTT's device API has no file-upload capability, only the HTTPS API does - which needs the raw token. The project's whole security model is that only mqtt-bridge (and now, by extension, the agent) ever holds the token; local apps never do. Came up while discussing a camera-detection demo that wanted to push captured frames to Blynk (see `examples/camera-detector/`).

Keep the proxy itself ignorant of what's being uploaded or why - same generic-infrastructure principle as the existing OTA/ping/reboot/redirect handling, or how mqtt-bridge bridges `ds/#` traffic without caring what a datastream means. Don't bake in app-specific assumptions (e.g. camera-frame-specific topics/logic) the way an earlier version of this idea wrongly did.

## OTA rollback: post-start health check, not just apply-failure

`ComposeManager`/`run_apply_only` in `agent.py` already roll back to the last backed-up `docker-compose.yml` when `docker compose up -d` itself fails (non-zero exit - bad image ref, compose syntax error, etc.) - this part is done, not a gap. What's missing is the case where `up -d` succeeds but the new container then crash-loops or never becomes healthy afterward - nothing currently watches for that, so a device can end up "successfully applied" an OTA that's actually broken. Every comparable fleet-management system that came up while researching this (balena's post-boot health-check rollback on top of its unbootable-detection; Home Assistant OS's RAUC boot-attempt-counted fallback; AWS Greengrass's `FailureHandlingPolicy: ROLLBACK`, which explicitly covers a component that fails to report healthy, not just one that fails to start) treats this as a baseline expectation.

Feasible pattern: after `_run_docker_compose()` reports success, watch the affected container(s) for N seconds (e.g. poll `docker inspect`'s restart count / running state) - if it crash-loops or exits repeatedly in that window, treat it the same as an apply failure and roll back via the existing backup-restore path in `run_apply_only`. Extends already-existing OTA-handling logic in `agent.py`, doesn't need new infrastructure.

## Show running services/containers on the dashboard

A way to see what's actually running on a device from Blynk - both host `systemctl` services and Docker containers (`docker ps`) - came up while discussing per-device monitoring for a RAK LoRaWAN gateway (wanting to confirm its `basicstation` service and pre-existing `mosquitto` broker were healthy) and, separately, container health in general.

Deliberately out of scope for the core agent's own health metrics: `mqtt-bridge`/`agent` container health isn't worth a separate datastream, since a broken agent or mqtt-bridge already shows up as the device going offline - that's the direct, existing signal. User-added containers are harder - there's no generic way to know which ones matter or what "healthy" means for someone else's app, the same problem already noted for product-specific service monitoring (see the `agent/agent.py`'s _run_terminal_command-based remote-terminal path already covers ad-hoc "what's running" checks for now, e.g. `systemctl list-units --type=service --state=running` or `docker ps`, one-off via the Terminal widget). A first-class dashboard feature for this would need to solve "what's worth showing and how" generically, not just dump raw process/container lists.

## Offline datastream buffering with timestamped replay

While the mqtt-bridge's cloud connection is down, local apps' `ds/#` publishes still land on the local broker fine (that part of the design already tolerates a cloud outage) but never reach Blynk, since mosquitto's own bridge can only forward what actually gets through to the remote side. Buffer them locally and replay them with their real timestamps once reconnected, instead of just losing that window's data.

Came up discussing commercial fleet-management gaps against this project - ChatGPT and others list "offline operation"/"store and forward" as a baseline expectation for this kind of agent.

Blynk's own API supports this precisely: `ds/<DatastreamName>/timestamped_batch`, payload a JSON array of `[timestamp_ms, value]` pairs (timestamps in ms, can't be older than 1 month - see [the docs](https://docs.blynk.io/en/blynk.cloud-mqtt-api/device-mqtt-api/datastreams#send-timestamped-batch-to-blynk)). Since that topic is still under `ds/`, it's already covered by the existing bridge's `topic ds/# out 1` - no mosquitto/ACL/bridge config changes needed, and the agent never needs the Blynk token itself, matching the project's existing "mqtt-bridge is the only thing that holds it" boundary.

Feasible pattern: the agent already subscribes locally to `ds/#` (its own ACL user block already grants `readwrite ds/#`) and already watches the bridge's connection state via `$SYS/broker/connection/.../state` (used today for the BLE re-provisioning-after-sustained-disconnection feature). While that state is disconnected, buffer incoming `ds/<name>` publishes to disk, grouped per datastream name, with a wall-clock timestamp; on reconnect, flush each datastream's backlog as one `ds/<name>/timestamped_batch` publish to the local broker, which the existing bridge relays up like any other `ds/#` message.

Not hard in the plumbing, but the edges need care: exactly when to start/stop buffering (to avoid gaps or a few duplicate points right at the reconnect boundary, since mosquitto's own bridge might also redeliver whatever little it had in flight at disconnect - `cleansession true` is set on the bridge's remote connection, so that's likely small/nonexistent, but not confirmed), a disk-growth cap/rotation policy for a very long outage, and dropping/capping anything that would violate the 1-month timestamp limit.

## Diagnostic/support-bundle upload command

A single MQTT-triggered command (e.g. a new downlink topic) that gathers `docker logs`, `docker ps`, and `systemctl status` for the stack's units, then uploads the result somewhere reachable for support purposes - instead of walking a user through the terminal manually every time. Directly depends on the **Blynk HTTP file-upload proxy** idea above (same upload path, generic infrastructure). Precedent: Azure IoT Edge's `UploadSupportBundle` direct method and Memfault's auto-collected coredumps/custom data recordings both solve the same "get diagnostics off a misbehaving device without an interactive session" problem.
