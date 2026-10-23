"""The RS485 bus: pymodbus ModbusSerialClient behind one lock, plus per-slave
timeout backoff. The only module that touches pymodbus - and only lazily,
in the default client factory, so tests can drive Bus with a fake client.

Errors come out as modbus_gateway.errors classes, never pymodbus ones."""

from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional

from .config import BusSettings
from .errors import BusError, ModbusExceptionReply, ModbusTimeout, SerialError, SlaveOffline

logger = logging.getLogger(__name__)

# Per-slave backoff: a dead slave would otherwise cost a full timeout per
# point per poll, holding the bus (and every other slave's polls and any
# downlink write) hostage. After this many consecutive timeouts the slave's
# polls are skipped for BACKOFF_START_S, doubling on each further timeout
# up to BACKOFF_MAX_S; any answer (even a Modbus exception) resets it.
BACKOFF_AFTER_TIMEOUTS = 3
BACKOFF_START_S = 5
BACKOFF_MAX_S = 60

_READS = {"co": "read_coils", "di": "read_discrete_inputs",
          "hr": "read_holding_registers", "ir": "read_input_registers"}


def pymodbus_client(port: str, settings: BusSettings):
    try:
        from pymodbus.client import ModbusSerialClient
        extra = {}
    except ImportError:  # pymodbus 2.x
        from pymodbus.client.sync import ModbusSerialClient
        extra = {"method": "rtu"}
    # retries=0: pymodbus 3.x retries 3 times by default, so one absent
    # slave would hold the bus for 4x timeout_ms instead of the configured
    # per-request timeout
    return ModbusSerialClient(port=port, baudrate=settings.baud, bytesize=8, parity=settings.parity,
                              stopbits=settings.stop, timeout=settings.timeout_ms / 1000,
                              retries=0, **extra)


class Bus:
    def __init__(self, port: str, client_factory=pymodbus_client, clock=time.monotonic):
        self.port = port
        self._factory = client_factory
        self._clock = clock
        # Poll reads (poll thread), downlink writes (paho's thread) and
        # probes must never interleave on the wire - RTU has no request ids,
        # a reply is only "the next frame"
        self._lock = threading.Lock()
        self._settings: Optional[BusSettings] = None
        self._client = None
        self._slave_kw: Optional[str] = None
        self._timeouts = {}   # slave -> consecutive timeouts before backoff
        self._backoff = {}    # slave -> (until, current delay)

    def configure(self, settings: BusSettings) -> None:
        with self._lock:
            if settings == self._settings:
                return
            self._close()  # rebuilt with the new settings on next use
            self._settings = settings

    def close(self) -> None:
        with self._lock:
            self._close()

    def in_backoff(self, slave: int) -> bool:
        with self._lock:
            return self._backing_off(slave)

    def read(self, table: str, slave: int, address: int, count: int, honor_backoff: bool = True) -> List[int]:
        """-> raw words (or 0/1 per bit for co/di). honor_backoff=False for
        probes: someone is commissioning that slave right now."""
        def op(client):
            return self._call(getattr(client, _READS[table]), {"address": address, "count": count},
                              slave, address, count)
        result = self._transact(slave, honor_backoff, op)
        if table in ("co", "di"):
            return [1 if b else 0 for b in list(result.bits)[:count]]
        return list(result.registers)[:count]

    def write(self, table: str, slave: int, address: int, values: List[int], fc16: bool = False) -> None:
        """Writes never honor backoff - they're user actions, and a slave
        that answers one is back anyway."""
        def op(client):
            if table == "co":
                return self._call(client.write_coil, {"address": address, "value": bool(values[0])},
                                  slave, address, bool(values[0]))
            if len(values) == 1 and not fc16:
                return self._call(client.write_register, {"address": address, "value": values[0]},
                                  slave, address, values[0])
            return self._call(client.write_registers, {"address": address, "values": list(values)},
                              slave, address, list(values))
        if table not in ("co", "hr"):
            raise BusError(f"can't write {table}")
        self._transact(slave, False, op)

    def _call(self, method, kwargs: dict, slave: int, *positional):
        """pymodbus has renamed the slave keyword more than once (unit= in
        2.x, slave= in most 3.x, device_id= in newer) - same shim as the
        validated root scripts, remembering the one that worked so a
        TypeError from anything else can't make us resend a write."""
        keys = (self._slave_kw,) if self._slave_kw else ("slave", "unit", "device_id")
        for key in keys:
            try:
                result = method(**kwargs, **{key: slave})
            except TypeError:
                continue
            self._slave_kw = key
            return result
        return method(*positional, slave)

    def _transact(self, slave: int, honor_backoff: bool, op):
        with self._lock:
            if honor_backoff and self._backing_off(slave):
                raise SlaveOffline()
            client = self._ensure_client()
            try:
                result = op(client)
            except Exception as e:
                if _is_serial_failure(e):
                    logger.warning(f"Serial port {self.port} failed ({e!r}) - reopening on next request")
                    self._close()
                    raise SerialError(str(e) or type(e).__name__) from None
                self._note_timeout(slave)
                raise ModbusTimeout() from None
            if result is None or result.isError():
                code = getattr(result, "exception_code", None)
                if code is not None:
                    self._note_alive(slave)
                    raise ModbusExceptionReply(code)
                # pymodbus 2.x returns (rather than raises) ModbusIOException
                # on no response
                self._note_timeout(slave)
                raise ModbusTimeout()
            self._note_alive(slave)
            return result

    def _ensure_client(self):
        if self._settings is None:
            raise SerialError("bus not configured")
        if self._client is None:
            client = self._factory(self.port, self._settings)
            if not client.connect():
                raise SerialError(f"can't open {self.port}")
            logger.info(f"Opened {self.port}: {self._settings.baud} 8{self._settings.parity}{self._settings.stop}, "
                        f"timeout {self._settings.timeout_ms}ms")
            self._client = client
        return self._client

    def _close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _backing_off(self, slave: int) -> bool:
        entry = self._backoff.get(slave)
        return entry is not None and self._clock() < entry[0]

    def _note_timeout(self, slave: int) -> None:
        entry = self._backoff.get(slave)
        if entry is not None:
            # Already backing off and the retry after it failed too
            delay = min(entry[1] * 2, BACKOFF_MAX_S)
        else:
            self._timeouts[slave] = self._timeouts.get(slave, 0) + 1
            if self._timeouts[slave] < BACKOFF_AFTER_TIMEOUTS:
                return
            delay = BACKOFF_START_S
        self._timeouts.pop(slave, None)
        self._backoff[slave] = (self._clock() + delay, delay)
        logger.warning(f"Slave {slave} not answering - skipping its polls for {delay}s")

    def _note_alive(self, slave: int) -> None:
        self._timeouts.pop(slave, None)
        if self._backoff.pop(slave, None) is not None:
            logger.info(f"Slave {slave} answering again")


def _is_serial_failure(e: Exception) -> bool:
    """Port-level failure (reopen it) vs no/garbled answer from one slave.
    By class name, so this module doesn't need pymodbus imported at the top.
    pymodbus's own exceptions other than ConnectionException mean the
    request went out but no valid frame came back - a timeout."""
    if type(e).__name__ == "ConnectionException":
        return True
    if type(e).__module__.startswith("pymodbus"):
        return False
    return True  # OSError/SerialException and anything unexpected
