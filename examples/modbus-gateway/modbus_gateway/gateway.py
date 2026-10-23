"""The gateway logic: running config, poll scheduling, downlink writes,
ModbusStatus and probes. No pymodbus/paho/blynk_provisioning here - the bus
and the MQTT client are injected, so all of this runs under test with
fakes.

Threads: run_forever() runs one poll thread per RS485 port (that port's
polls and probes, so a probe can never overlap a poll on its port, and a
dead device timing out on one port doesn't slow the others);
handle_message() runs on paho's network thread (config applies, downlink
writes, probe enqueueing). Bus I/O is serialised by each bus's own lock;
self._lock only guards gateway state and is never held across bus I/O."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from . import codec
from .config import (EMPTY_CONFIG, SCAN_ANY, SCAN_ANY_TABLES, BusSettings, Config, ConfigError, Point,
                     Probe, parse_config, parse_probe)
from .errors import BusError, ModbusExceptionReply, ModbusTimeout

logger = logging.getLogger(__name__)

CONFIG_DS = "ModbusConfig"
STATUS_DS = "ModbusStatus"
PROBE_DS = "ModbusProbe"
PROBE_RESULT_DS = "ModbusProbeResult"
TOPIC_DOWNLINK_PREFIX = "downlink/ds/"
MAX_STRING_DS_LENGTH = 1024
STATUS_REFRESH_SECONDS = 300
# Upper bound on one idle wait, so a clock jump or a missed wake can't
# stall polling for long
MAX_IDLE_SECONDS = 5
# Per-request timeout while scanning (capped by the bus's own timeout).
# Devices on the CompuLab answered in 30-50ms at 9600 baud.
SCAN_TIMEOUT_MS = 200
# Events (docs/config-format.md "Events"): a slave must fail this long
# before modbus_error, so one dropped reply stays quiet and every point of
# the slave has been tried by the time the description lists them
EVENT_ERROR = "modbus_error"
EVENT_OK = "modbus_ok"
EVENT_GRACE_SECONDS = 10
EVENT_TEXT_MAX = 300


@dataclasses.dataclass
class _SlaveAlert:
    alerted: bool = False               # modbus_error sent, modbus_ok not yet
    answered_since_alert: bool = False


def _event_text(text: str) -> str:
    return text if len(text) <= EVENT_TEXT_MAX else text[:EVENT_TEXT_MAX - 3] + "..."


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


SlaveKey = Tuple[int, int]   # (port index, slave id) - slave ids are per port


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


class Gateway:
    def __init__(self, buses, state_path: Optional[os.PathLike] = None,
                 clock=time.monotonic, wall_clock=time.time):
        """buses: one Bus per RS485 port, in port order (a single Bus is
        taken as a one-port gateway)."""
        self.buses: List = list(buses) if isinstance(buses, (list, tuple)) else [buses]
        if not self.buses:
            raise ValueError("a gateway needs at least one port")
        self.mqtt = None
        self.state_path = Path(state_path) if state_path else None
        self._clock = clock
        self._wall_clock = wall_clock
        self._started = clock()
        self._lock = threading.RLock()
        self._wakes = [threading.Event() for _ in self.buses]   # per port thread

        self.config: Config = EMPTY_CONFIG
        self.cfg_status = "ok"
        self._bad: Dict[str, str] = {}
        self._bad_since: Dict[str, float] = {}  # when each failing point started failing
        self._next_due: Dict[str, float] = {}
        self._status_bad_names: Optional[frozenset] = None  # as last published
        self._last_status: Optional[float] = None
        self._probe: Optional[Probe] = None
        self._probe_running = False
        self._subscribed: Set[str] = set()   # exact downlink topics, see _sync_subscriptions
        # Last value a point is known to hold on the device (written OK, or
        # read for rw points) - what a failed write snaps the datastream back to
        self._confirmed: Dict[str, str] = {}
        # The write-confirmed subset of _confirmed - the part that survives a
        # restart (rw reads are re-read by the first poll anyway)
        self._written: Dict[str, str] = {}
        self._slaves: Dict[SlaveKey, _SlaveAlert] = {}  # per-slave event state
        self._saved_runtime: Optional[dict] = None
        # Every port's poll thread and paho's thread can save it
        self._runtime_lock = threading.Lock()

        self._load_state()
        self._load_runtime()
        self._configure_buses()
        self._reset_schedule()

    @property
    def bus(self):
        """The first port's bus."""
        return self.buses[0]

    # --- wiring ----------------------------------------------------------

    def attach_mqtt(self, client) -> None:
        self.mqtt = client

    def on_connected(self) -> None:
        """Call after every (re)connect. No persistent session: the
        subscription is gone after a reconnect and config edits made while
        offline were missed - redo both."""
        self._subscribed = set()
        self._sync_subscriptions()
        self.mqtt.publish("get/ds", CONFIG_DS, qos=1)
        self.publish_status()

    def _sync_subscriptions(self) -> None:
        """Subscribe to each downlink topic by exact name. Confirmed on real
        hardware (2026-10-19): Blynk's broker grants wildcard subscriptions
        (downlink/ds/+, #) on an MQTT v5 connection but never delivers
        anything to them - only exact topics work. So the set has to follow
        the config: ModbusConfig, ModbusProbe and every write point."""
        if self.mqtt is None:
            return
        with self._lock:
            wanted = {CONFIG_DS, PROBE_DS} | {p.name for p in self.config.points if p.writes}
        wanted = {TOPIC_DOWNLINK_PREFIX + name for name in wanted}
        for topic in sorted(wanted - self._subscribed):
            self.mqtt.subscribe(topic, qos=1)
        for topic in sorted(self._subscribed - wanted):
            self.mqtt.unsubscribe(topic)
        self._subscribed = wanted

    def handle_message(self, topic: str, payload) -> None:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        if not topic.startswith(TOPIC_DOWNLINK_PREFIX):
            return
        name = topic[len(TOPIC_DOWNLINK_PREFIX):]
        if name == CONFIG_DS:
            self.apply_config(payload)
        elif name == PROBE_DS:
            self._enqueue_probe(payload)
        elif name not in (STATUS_DS, PROBE_RESULT_DS):
            with self._lock:
                point = self.config.point(name)
            if point is not None and point.writes:
                self._write_point(point, payload)
            elif point is not None:
                logger.debug(f"Ignoring downlink for read-only point {name}")

    def _connected(self) -> bool:
        return self.mqtt is not None and self.mqtt.is_connected()

    def _wake_all(self) -> None:
        for wake in self._wakes:
            wake.set()

    # --- config ----------------------------------------------------------

    def _parse(self, payload) -> Config:
        """parse_config plus the one check that needs the hardware."""
        config = parse_config(payload)
        if len(config.ports) > len(self.buses):
            raise ConfigError(f"port {len(config.ports)}: this gateway has {_plural(len(self.buses), 'port')}")
        return config

    def _port_settings(self, port: int) -> BusSettings:
        """A port the config doesn't list still gets defaults, so a probe
        can look for devices on it before any are configured."""
        with self._lock:
            ports = self.config.ports
        return ports[port] if port < len(ports) else BusSettings()

    def _configure_buses(self) -> None:
        for port, bus in enumerate(self.buses):
            bus.configure(self._port_settings(port))

    def apply_config(self, payload: str, persist: bool = True) -> bool:
        try:
            config = self._parse(payload)
        except ConfigError as e:
            with self._lock:
                self.cfg_status = str(e)
            logger.warning(f"{CONFIG_DS} rejected, keeping rev {self.config.rev}: {e}")
            self.publish_status()
            return False

        with self._lock:
            unchanged = config == self.config
            was_rejected = self.cfg_status != "ok"
            self.cfg_status = "ok"
            if not unchanged:
                old = {p.name: p for p in self.config.points}
                self.config = config
                self._bad.clear()
                self._bad_since.clear()
                self._reset_schedule()
                # A redefined point's old value says nothing about the new register
                kept = {p.name for p in config.points if old.get(p.name) == p}
                self._confirmed = {n: v for n, v in self._confirmed.items() if n in kept}
                self._written = {n: v for n, v in self._written.items() if n in kept}
                # Alerts for slaves that left the config are just dropped -
                # no modbus_ok, nothing will ever poll them again
                slaves = {(p.port, p.slave) for p in config.points}
                self._slaves = {s: a for s, a in self._slaves.items() if s in slaves}
        if unchanged:
            # Every reconnect's get/ds re-sends the running config - don't
            # restart the schedule (or re-publish status, on_connected just
            # did) for that. Unless it clears an earlier reject: someone
            # reverted a bad edit
            logger.debug(f"{CONFIG_DS} rev {config.rev} unchanged")
            if was_rejected:
                self.publish_status()
            return True
        self._configure_buses()
        self._sync_subscriptions()
        logger.info(f"{CONFIG_DS} rev {config.rev} applied: {len(config.points)} points")
        if persist:
            self._save_state(payload)
        self._save_runtime()  # new rev, and anything dropped with the old config
        self.publish_status()
        self._wake_all()
        return True

    def _reset_schedule(self) -> None:
        now = self._clock()
        self._next_due = {p.name: now for p in self.config.points if p.reads}

    def _load_state(self) -> None:
        """The last accepted config, so a restart polls straight away - even
        with no network, or before Blynk answers get/ds."""
        if not self.state_path or not self.state_path.exists():
            return
        try:
            self.config = self._parse(self.state_path.read_text(encoding="utf-8"))
            logger.info(f"Loaded {CONFIG_DS} rev {self.config.rev} from {self.state_path}")
        except (OSError, ConfigError) as e:
            logger.warning(f"Ignoring saved config {self.state_path}: {e}")

    def _save_state(self, payload: str) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.state_path)  # never a half-written config on power loss
        except OSError as e:
            logger.warning(f"Could not save config to {self.state_path}: {e}")

    # --- runtime state (docs/config-format.md "Runtime state") ------------
    # Write-confirmed values and open alerts, so a restart mid-outage
    # neither repeats modbus_error nor loses the modbus_ok / snap-back value

    @property
    def _runtime_path(self) -> Optional[Path]:
        return self.state_path.with_name("runtime_state.json") if self.state_path else None

    def _runtime_doc(self) -> dict:
        with self._lock:
            return {
                "rev": self.config.rev,
                "confirmed": dict(sorted(self._written.items())),
                "alerts": [list(k) for k, a in sorted(self._slaves.items()) if a.alerted],
            }

    def _save_runtime(self) -> None:
        """Only when it changed - relay toggles are the worst case, polls
        never write."""
        path = self._runtime_path
        if not path:
            return
        with self._runtime_lock:
            doc = self._runtime_doc()
            if doc == self._saved_runtime:
                return
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(_compact(doc), encoding="utf-8")
                os.replace(tmp, path)
                self._saved_runtime = doc
            except OSError as e:
                logger.warning(f"Could not save runtime state to {path}: {e}")

    def _load_runtime(self) -> None:
        path = self._runtime_path
        if not path or not path.exists():
            return
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("rev") != self.config.rev:
                logger.info(f"Ignoring runtime state for rev {doc.get('rev')} (running rev {self.config.rev})")
                return
            names = {p.name for p in self.config.points}
            slaves = {(p.port, p.slave) for p in self.config.points}
            written = {str(n): str(v) for n, v in dict(doc.get("confirmed", {})).items() if n in names}
            alerts = []
            for a in doc.get("alerts", []):
                # A bare slave id is state from before v2: port 0
                key = (0, a) if isinstance(a, int) else tuple(a) if isinstance(a, list) else None
                if key in slaves:
                    alerts.append(key)
        except (OSError, ValueError, TypeError, AttributeError) as e:
            logger.warning(f"Ignoring runtime state {path}: {e}")
            return
        self._written = written
        self._confirmed = dict(written)
        # Already alerted (no duplicate modbus_error if still down); modbus_ok
        # needs a real success after this restart
        self._slaves = {s: _SlaveAlert(alerted=True) for s in alerts}
        self._saved_runtime = self._runtime_doc()
        if written or alerts:
            logger.info(f"Restored runtime state: confirmed {sorted(written)}, "
                        f"open alerts {[self._slave_label(k) for k in alerts]}")

    # --- status ----------------------------------------------------------

    def status_payload(self) -> str:
        with self._lock:
            doc = {
                "rev": self.config.rev,
                "cfg": self.cfg_status,
                "ts": int(self._wall_clock()),
                "up": int(self._clock() - self._started),
                "ports": len(self.buses),
                "n": len(self.config.points),
                "bad": dict(sorted(self._bad.items())),
            }
        text = _compact(doc)
        items = sorted(doc["bad"].items())
        dropped = 0
        while len(text) > MAX_STRING_DS_LENGTH and items:
            items.pop()
            dropped += 1
            doc["bad"] = dict(items)
            doc["more"] = dropped
            text = _compact(doc)
        return text

    def publish_status(self) -> None:
        if not self._connected():
            return  # on_connected() publishes a fresh one
        with self._lock:
            payload = self.status_payload()
            self._status_bad_names = frozenset(self._bad)
            self._last_status = self._clock()
        # QoS 0: a snapshot - if it's lost the next one replaces it
        self.mqtt.publish(f"ds/{STATUS_DS}", payload, qos=0)

    def _maybe_publish_status(self) -> None:
        with self._lock:
            due = (self._status_bad_names != frozenset(self._bad)
                   or self._last_status is None
                   or self._clock() - self._last_status >= STATUS_REFRESH_SECONDS)
        if due:
            self.publish_status()

    def _set_bad(self, name: str, reason: Optional[str], config: Config) -> None:
        with self._lock:
            if config is not self.config:
                return  # config replaced mid-read - this point may not exist any more
            before = self._bad.get(name)
            if reason is None:
                self._bad.pop(name, None)
                self._bad_since.pop(name, None)
            else:
                self._bad[name] = reason
                self._bad_since.setdefault(name, self._clock())
        if before != reason:
            if reason is None:
                logger.info(f"{name}: OK again")
            else:
                logger.warning(f"{name}: {reason}")

    # --- polling ---------------------------------------------------------

    def run_once(self, port: Optional[int] = None) -> float:
        """One pass for one port's thread (None: every port, in turn - for
        tests): a pending probe, then every due point, then status.
        Returns seconds until something is next due."""
        self._run_probe(port)
        with self._lock:
            config = self.config
            now = self._clock()
            due = sorted((t, n) for n, t in self._next_due.items()
                         if t <= now and (port is None or config.point(n).port == port))
            for _, name in due:
                # Fixed cadence, but after a long stall (bus contention, a
                # scan) restart from now instead of firing a burst of
                # catch-up reads
                nxt = self._next_due[name] + config.period(config.point(name))
                self._next_due[name] = nxt if nxt > now else now + config.period(config.point(name))
        for _, name in due:
            self._poll_point(config.point(name), config)
            if self._probe is not None:
                self._run_probe(port)  # between polls, not after a whole round
        self._maybe_publish_status()
        self._update_events()

        with self._lock:
            now = self._clock()
            next_due = min((t for n, t in self._next_due.items()
                            if port is None or self.config.point(n).port == port),
                           default=now + MAX_IDLE_SECONDS)
            status_due = (self._last_status or now) + STATUS_REFRESH_SECONDS
        return max(0.0, min(next_due - now, status_due - now, MAX_IDLE_SECONDS))

    def run_forever(self, stop: threading.Event, on_tick=None) -> None:
        """Port 0 runs on the calling thread (with on_tick), every other
        port on its own daemon thread."""
        for port in range(1, len(self.buses)):
            threading.Thread(target=self._run_port, args=(port, stop), daemon=True,
                             name=f"poll-port{port + 1}").start()
        self._run_port(0, stop, on_tick)

    def _run_port(self, port: int, stop: threading.Event, on_tick=None) -> None:
        wake = self._wakes[port]
        while not stop.is_set():
            wake.clear()
            wait = self.run_once(port)
            if on_tick:
                on_tick()
            if wait > 0:
                wake.wait(timeout=wait)

    def _poll_point(self, point: Point, config: Config) -> None:
        try:
            words = self.buses[point.port].read(point.table, point.slave, point.address,
                                                codec.register_count(point))
            value = codec.decode(point, words)
        except BusError as e:
            self._set_bad(point.name, str(e), config)
            return
        except codec.CodecError as e:
            self._set_bad(point.name, f"bad value: {e}", config)
            self._slave_answered(point)  # it replied, so it's alive (matches the ESP32)
            return
        self._set_bad(point.name, None, config)
        self._slave_answered(point)
        if point.writes:
            with self._lock:
                self._confirmed[point.name] = value
        logger.debug(f"{point.name} = {value}")
        if self._connected():
            # Skipped while offline rather than queued: a reading is only
            # worth sending while current, and paho's queue is unbounded
            self.mqtt.publish(f"ds/{point.name}", value, qos=1)

    # --- downlink writes --------------------------------------------------

    def _write_point(self, point: Point, payload: str) -> None:
        try:
            words = codec.encode(point, payload)
        except codec.CodecError as e:
            self._write_failed(point, f"bad value: {e}")
            return
        try:
            self.buses[point.port].write(point.table, point.slave, point.address, words, fc16=point.fc16)
        except BusError as e:
            self._write_failed(point, f"write {e}")
            return
        # Echo what was actually written (decoded back from the words), so
        # rounding or map normalisation ("1.0" -> "1") shows as the
        # confirmed state rather than the raw request
        confirmed = codec.decode(point, words)
        logger.info(f"{point.name}: wrote {confirmed} ({words})")
        if self._connected():
            self.mqtt.publish(f"ds/{point.name}", confirmed, qos=1)
        with self._lock:
            self._set_bad(point.name, None, self.config)
            self._confirmed[point.name] = confirmed
            self._written[point.name] = confirmed
        self._slave_answered(point)
        self._save_runtime()
        self._maybe_publish_status()

    def _write_failed(self, point: Point, reason: str) -> None:
        with self._lock:
            self._set_bad(point.name, reason, self.config)
            previous = self._confirmed.get(point.name)
        # Always, not just on a change of the failing set: the point may
        # already be listed with an older read error, and the user is
        # waiting on this write
        self.publish_status()
        # Snap the datastream back: Blynk already stored the requested value,
        # so a dashboard switch would otherwise show a state the device never
        # reached. Nothing known yet (fresh start) -> leave it.
        if previous is not None and self._connected():
            logger.info(f"{point.name}: write failed, restoring {previous}")
            self.mqtt.publish(f"ds/{point.name}", previous, qos=1)

    # --- events ----------------------------------------------------------

    def _slave_answered(self, point: Point) -> None:
        """A configured point's read or write to its slave succeeded (a
        Modbus exception reply doesn't count - see _poll_point/_write_point)."""
        with self._lock:
            alert = self._slaves.get((point.port, point.slave))
            if alert is not None:
                alert.answered_since_alert = True

    def _update_events(self) -> None:
        """modbus_error / modbus_ok per slave - see docs/config-format.md,
        "Events". Re-evaluated every pass rather than queued, so an outage
        that's over by the time MQTT is back sends nothing."""
        connected = self._connected()
        to_send = []
        with self._lock:
            now = self._clock()
            failing: Dict[SlaveKey, list] = {}
            since: Dict[SlaveKey, float] = {}  # earliest start of a current failure
            for p in self.config.points:  # config order, for the description
                reason = self._bad.get(p.name)
                if reason and not reason.startswith("bad value"):
                    key = (p.port, p.slave)
                    failing.setdefault(key, []).append(f"{p.name} {reason}")
                    since[key] = min(since.get(key, now), self._bad_since.get(p.name, now))
            for slave in sorted(set(failing) | set(self._slaves)):
                alert = self._slaves.setdefault(slave, _SlaveAlert())
                if slave in failing:
                    if (not alert.alerted and connected
                            and now - since[slave] >= EVENT_GRACE_SECONDS):
                        to_send.append((EVENT_ERROR, _event_text(f"{self._slave_label(slave)}: "
                                                                 + ", ".join(failing[slave]))))
                        alert.alerted = True
                        alert.answered_since_alert = False
                else:
                    if alert.alerted and alert.answered_since_alert and connected:
                        to_send.append((EVENT_OK, f"{self._slave_label(slave)} OK again"))
                        alert.alerted = False
                    if not alert.alerted:
                        del self._slaves[slave]  # nothing left to track
        for code, text in to_send:
            logger.warning(f"Event {code}: {text}")
            self.mqtt.publish(f"event/{code}", text, qos=1)
        if to_send:
            self._save_runtime()  # the set of open alerts changed

    def _slave_label(self, key: SlaveKey) -> str:
        """'Slave 1', or 'Port 2 slave 1' once the config has several ports."""
        port, slave = key
        if len(self.config.ports) > 1:
            return f"Port {port + 1} slave {slave}"
        return f"Slave {slave}"

    # --- probes ----------------------------------------------------------

    def _probe_reply(self, result: dict) -> None:
        if self._connected():
            self.mqtt.publish(f"ds/{PROBE_RESULT_DS}", _compact(result), qos=1)

    def _enqueue_probe(self, payload: str) -> None:
        try:
            data = json.loads(payload)
        except ValueError:
            self._probe_reply({"id": None, "ok": False, "err": "bad request: not valid JSON", "ms": 0})
            return
        pid = data.get("id") if isinstance(data, dict) else None
        try:
            probe = parse_probe(data)
        except ConfigError as e:
            self._probe_reply({"id": pid, "ok": False, "err": f"bad request: {e}", "ms": 0})
            return
        if probe.port >= len(self.buses):
            self._probe_reply({"id": pid, "ok": False, "ms": 0,
                               "err": f"bad request: port {probe.port + 1}: this gateway has "
                                      f"{_plural(len(self.buses), 'port')}"})
            return
        with self._lock:
            busy = self._probe is not None or self._probe_running
            if not busy:
                self._probe = probe
        if busy:
            self._probe_reply({"id": pid, "ok": False, "err": "busy", "ms": 0})
            return
        self._wakes[probe.port].set()

    def _run_probe(self, port: Optional[int] = None) -> None:
        """Runs the pending probe if it's for this port's thread."""
        with self._lock:
            probe = self._probe
            if probe is None or (port is not None and probe.port != port):
                return
            self._probe = None
            self._probe_running = True
        try:
            result = self._execute_probe(probe)
        finally:
            with self._lock:
                self._probe_running = False
        logger.info(f"Probe {probe.kind} {probe.id!r}: {result}")
        self._probe_reply(result)

    def _execute_probe(self, probe: Probe) -> dict:
        start = self._clock()
        result = {"id": probe.id, "ok": True}
        bus = self.buses[probe.port]
        try:
            if probe.kind == "read":
                result["regs"] = bus.read(probe.table, probe.slave, probe.address, probe.count,
                                          honor_backoff=False)
            elif probe.kind == "write":
                bus.write(probe.table, probe.slave, probe.address, [probe.value])
            else:
                tables = SCAN_ANY_TABLES if probe.table == SCAN_ANY else (probe.table,)
                found, via = [], {}
                # A real device answers in tens of ms; waiting the full bus
                # timeout on every empty id made a 1-10 scan take 16s on the
                # CompuLab. Scans run on their port's poll thread, so no poll
                # overlaps.
                normal = self._port_settings(probe.port)
                bus.configure(dataclasses.replace(
                    normal, timeout_ms=min(normal.timeout_ms, SCAN_TIMEOUT_MS)))
                try:
                    for slave in range(probe.first, probe.last + 1):
                        for table in tables:
                            try:
                                bus.read(table, slave, probe.address, 1, honor_backoff=False)
                            except ModbusExceptionReply:
                                pass  # it answered, so it's there
                            except ModbusTimeout:
                                continue
                            found.append(slave)
                            via[str(slave)] = table
                            break
                finally:
                    bus.configure(normal)
                result["found"] = found
                result["via"] = via
        except BusError as e:
            result = {"id": probe.id, "ok": False, "err": str(e)}
        result["ms"] = int((self._clock() - start) * 1000)
        return result
