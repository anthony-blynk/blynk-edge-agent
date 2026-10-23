"""ModbusConfig / ModbusProbe parsing and validation - docs/config-format.md
is the contract. Pure: no I/O, no pymodbus.

Any invalid field rejects the whole config with a short reason (it ends up
in ModbusStatus.cfg, so it has to make sense to a human in the widget) - a
partly-applied register map is worse than a stale one. Unknown keys are
rejected too: `v` is the versioning mechanism, and a silently ignored typo
("mode" for "m") would turn a write point into a polled one."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional, Tuple, Union

FORMAT_VERSION = 2
LEGACY_VERSION = 1   # single port: "bus" + top-level "pts"; still accepted
MAX_PORTS = 4
RESERVED_DS = ("ModbusConfig", "ModbusStatus", "ModbusProbe", "ModbusProbeResult")
TABLES = ("co", "di", "hr", "ir")
TYPES = ("u16", "i16", "u32", "i32", "f32")
WIDE_TYPES = ("u32", "i32", "f32")
MAX_POINTS = 32
MAX_DP = 6
# f32 values are real numbers already - rounding them to the dp implied by
# the default scale of 1 would publish 23.47 as 23
F32_MIN_DEFAULT_DP = 3
MAX_REASON = 200
MAX_PROBE_COUNT = 16
MAX_SCAN_IDS = 16
SCAN_ANY = "any"
SCAN_ANY_TABLES = ("hr", "ir")   # tried in this order per id

INT_RANGES = {
    "u16": (0, 0xFFFF),
    "i16": (-0x8000, 0x7FFF),
    "u32": (0, 0xFFFFFFFF),
    "i32": (-0x80000000, 0x7FFFFFFF),
}

Number = Union[int, float]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class BusSettings:
    baud: int = 9600
    parity: str = "N"
    stop: int = 1
    timeout_ms: int = 1000


@dataclass(frozen=True)
class Point:
    name: str
    slave: int
    table: str
    address: int
    type: str = "u16"
    port: int = 0                     # index into Config.ports
    scale: Number = 1
    mode: str = "r"
    offset: Number = 0
    word_order: str = "be"
    bit: Optional[int] = None
    dp: int = 0
    period: Optional[Number] = None   # None = config's poll_s
    map: Tuple[Tuple[str, Number], ...] = ()  # (normalised key, raw), config order
    fc16: bool = False

    @property
    def reads(self) -> bool:
        return self.mode in ("r", "rw")

    @property
    def writes(self) -> bool:
        return self.mode in ("w", "rw")

    @property
    def is_bit(self) -> bool:
        return self.table in ("co", "di") or self.bit is not None


@dataclass(frozen=True)
class Config:
    """Always the v2 model - a v1 config parses to the same thing as its v2
    equivalent, so re-saving it as v2 doesn't count as a change."""
    v: int = FORMAT_VERSION
    rev: int = 0
    ports: Tuple[BusSettings, ...] = (BusSettings(),)
    poll_s: Number = 5
    points: Tuple[Point, ...] = ()   # all ports', in config order

    @property
    def bus(self) -> BusSettings:
        """The first port's settings (all a single-port config has)."""
        return self.ports[0]

    def period(self, point: Point) -> Number:
        return point.period if point.period is not None else self.poll_s

    def point(self, name: str) -> Optional[Point]:
        for p in self.points:
            if p.name == name:
                return p
        return None


EMPTY_CONFIG = Config()


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x) -> bool:
    return (_is_int(x) or isinstance(x, float)) and math.isfinite(x)


def _int_in(x, lo, hi, what) -> int:
    if not _is_int(x) or not lo <= x <= hi:
        raise ConfigError(f"{what} must be an integer {lo}-{hi}")
    return x


def _check_keys(obj: dict, allowed, what) -> None:
    unknown = sorted(set(obj) - set(allowed))
    if unknown:
        raise ConfigError(f"{what}: unknown key {unknown[0]!r}")


def _decimals(x: Number) -> int:
    exponent = Decimal(repr(float(x))).normalize().as_tuple().exponent
    return max(0, -exponent)


def normalize_key(value) -> str:
    """Map keys and downlink values compare in this form, so "1.0", " 1"
    and 1 all hit the "1" entry. Non-numeric strings are kept as they are."""
    s = str(value).strip()
    try:
        f = float(s)
    except ValueError:
        return s
    if not math.isfinite(f):
        return s
    return str(int(f)) if f.is_integer() else repr(f)


def _raw_range(table: str, type_: str, bit) -> Optional[Tuple[int, int]]:
    if table in ("co", "di") or bit is not None:
        return (0, 1)
    return INT_RANGES.get(type_)  # None for f32: any finite number


def parse_config(data) -> Config:
    """data: the datastream's JSON text, or an already-decoded object."""
    if isinstance(data, (str, bytes)):
        if not str(data if isinstance(data, str) else data.decode("utf-8", "replace")).strip():
            raise ConfigError("empty")
        try:
            data = json.loads(data)
        except ValueError:
            raise ConfigError("not valid JSON") from None
    try:
        return _parse_config(data)
    except ConfigError as e:
        raise ConfigError(str(e)[:MAX_REASON]) from None


def _parse_config(data) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("must be a JSON object")
    if "v" not in data:
        raise ConfigError("missing v")
    v = data["v"]
    if not _is_int(v) or v not in (LEGACY_VERSION, FORMAT_VERSION):
        raise ConfigError(f"unsupported v {v!r} (this gateway knows v{LEGACY_VERSION}-v{FORMAT_VERSION})")
    if v == LEGACY_VERSION:
        _check_keys(data, ("v", "rev", "bus", "poll_s", "pts"), "config")
        bus_obj = data.get("bus", {})
        if not isinstance(bus_obj, dict):
            raise ConfigError("bus must be an object")
        _check_keys(bus_obj, ("baud", "parity", "stop", "timeout_ms"), "bus")
        port_objs = [(bus_obj, data.get("pts", []), "bus", "pts")]
    else:
        _check_keys(data, ("v", "rev", "poll_s", "ports"), "config")
        if "ports" not in data:
            raise ConfigError("missing ports")
        objs = data["ports"]
        if not isinstance(objs, list) or not 1 <= len(objs) <= MAX_PORTS:
            raise ConfigError(f"ports must be a list of 1-{MAX_PORTS} ports")
        port_objs = []
        for i, obj in enumerate(objs):
            if not isinstance(obj, dict):
                raise ConfigError(f"ports[{i}] must be an object")
            _check_keys(obj, ("baud", "parity", "stop", "timeout_ms", "pts"), f"ports[{i}]")
            port_objs.append((obj, obj.get("pts", []), f"ports[{i}]", f"ports[{i}].pts"))
    rev = _int_in(data.get("rev", 0), 0, 2**53, "rev")

    poll_s = data.get("poll_s", 5)
    if not _is_num(poll_s) or poll_s < 1:
        raise ConfigError("poll_s must be a number >= 1")

    ports = []
    points = []
    names = set()
    for port, (obj, pts, where, pts_where) in enumerate(port_objs):
        ports.append(_parse_bus(obj, where))
        if not isinstance(pts, list):
            raise ConfigError(f"{pts_where} must be a list")
        if len(points) + len(pts) > MAX_POINTS:
            raise ConfigError(f"too many points ({len(points) + len(pts)}, max {MAX_POINTS})")
        for i, raw in enumerate(pts):
            point = _parse_point(raw, f"{pts_where}[{i}]", port)
            if point.name in names:
                raise ConfigError(f"{pts_where}[{i}]: duplicate datastream {point.name!r}")
            names.add(point.name)
            points.append(point)
    return Config(v=FORMAT_VERSION, rev=rev, ports=tuple(ports), poll_s=poll_s, points=tuple(points))


def _parse_bus(obj: dict, where: str) -> BusSettings:
    bus = BusSettings(
        baud=_int_in(obj.get("baud", 9600), 300, 921600, f"{where}.baud"),
        parity=obj.get("parity", "N"),
        stop=obj.get("stop", 1),
        timeout_ms=_int_in(obj.get("timeout_ms", 1000), 10, 60000, f"{where}.timeout_ms"),
    )
    if bus.parity not in ("N", "E", "O"):
        raise ConfigError(f"{where}.parity must be N, E or O")
    if bus.stop not in (1, 2) or not _is_int(bus.stop):
        raise ConfigError(f"{where}.stop must be 1 or 2")
    return bus


def _parse_point(raw, where: str, port: int = 0) -> Point:
    if not isinstance(raw, list) or not 4 <= len(raw) <= 7:
        raise ConfigError(f"{where} must be a list of 4-7 fields")
    name = raw[0]
    if not isinstance(name, str) or not name.strip() or len(name) > 64 or any(c in name for c in "/+#"):
        raise ConfigError(f"{where}: datastream must be a name of 1-64 chars without / + #")
    if name in RESERVED_DS:
        raise ConfigError(f"{where}: {name} is reserved")
    where = f"{where} ({name})"

    slave = _int_in(raw[1], 1, 247, f"{where} slave")
    table = raw[2]
    if table not in TABLES:
        raise ConfigError(f"{where}: table must be co, di, hr or ir")
    address = _int_in(raw[3], 0, 65535, f"{where} address")
    # null keeps the default, so options can be given without spelling out
    # type/scale
    type_ = raw[4] if len(raw) > 4 and raw[4] is not None else "u16"
    if type_ not in TYPES:
        raise ConfigError(f"{where}: type must be one of {', '.join(TYPES)}")
    if table in ("co", "di"):
        type_ = "u16"  # ignored for bits, per the spec
    scale = raw[5] if len(raw) > 5 and raw[5] is not None else 1
    if not _is_num(scale) or scale == 0:
        raise ConfigError(f"{where}: scale must be a non-zero number")
    opts = raw[6] if len(raw) > 6 and raw[6] is not None else {}
    if not isinstance(opts, dict):
        raise ConfigError(f"{where}: options must be an object")
    _check_keys(opts, ("m", "o", "wo", "b", "dp", "p", "map", "fc16"), where)

    mode = opts.get("m", "r")
    if mode not in ("r", "w", "rw"):
        raise ConfigError(f"{where}: m must be r, w or rw")
    if mode != "r" and table not in ("co", "hr"):
        raise ConfigError(f"{where}: m {mode} needs table co or hr")
    offset = opts.get("o", 0)
    if not _is_num(offset):
        raise ConfigError(f"{where}: o must be a number")
    word_order = opts.get("wo", "be")
    if word_order not in ("be", "le"):
        raise ConfigError(f"{where}: wo must be be or le")
    if type_ in WIDE_TYPES and address > 65534:
        raise ConfigError(f"{where}: a 32-bit value needs address <= 65534")

    bit = opts.get("b")
    if bit is not None:
        _int_in(bit, 0, 15, f"{where} b")
        if table not in ("hr", "ir") or type_ in WIDE_TYPES:
            raise ConfigError(f"{where}: b needs a 16-bit hr or ir register")
        if mode != "r":
            raise ConfigError(f"{where}: b is read-only")

    if "dp" in opts:
        dp = _int_in(opts["dp"], 0, MAX_DP, f"{where} dp")
    else:
        # Offset counts too: scale 1 with o -0.5 would otherwise round the
        # offset away
        dp = min(MAX_DP, max(_decimals(scale), _decimals(offset)))
        if type_ == "f32" and table in ("hr", "ir"):
            dp = max(dp, F32_MIN_DEFAULT_DP)

    period = opts.get("p")
    if period is not None and (not _is_num(period) or period < 1):
        raise ConfigError(f"{where}: p must be a number >= 1")

    fc16 = opts.get("fc16", False)
    if not isinstance(fc16, bool):
        raise ConfigError(f"{where}: fc16 must be true or false")

    mapping = ()
    if "map" in opts:
        mapping = _parse_map(opts["map"], table, type_, bit, where)

    return Point(name=name, slave=slave, table=table, address=address, type=type_, port=port, scale=scale,
                 mode=mode, offset=offset, word_order=word_order, bit=bit, dp=dp, period=period,
                 map=mapping, fc16=fc16)


def _parse_map(obj, table, type_, bit, where) -> Tuple[Tuple[str, Number], ...]:
    if not isinstance(obj, dict) or not obj:
        raise ConfigError(f"{where}: map must be a non-empty object")
    rng = _raw_range(table, type_, bit)
    out = {}
    for key, raw in obj.items():
        nkey = normalize_key(key)
        if not nkey:
            raise ConfigError(f"{where}: map has an empty key")
        if nkey in out:
            raise ConfigError(f"{where}: map key {key!r} duplicates another")
        if rng is None:
            if not _is_num(raw):
                raise ConfigError(f"{where}: map value for {key!r} must be a number")
        elif not _is_int(raw) or not rng[0] <= raw <= rng[1]:
            raise ConfigError(f"{where}: map value for {key!r} must be an integer {rng[0]}-{rng[1]}")
        out[nkey] = raw
    return tuple(out.items())


# --- ModbusProbe ---------------------------------------------------------

@dataclass(frozen=True)
class Probe:
    id: object
    kind: str            # "read", "write" or "scan"
    table: str
    address: int
    port: int = 0        # index; the gateway checks it against its real ports
    slave: int = 0       # read/write
    count: int = 1       # read
    value: int = 0       # write
    first: int = 0       # scan range, inclusive
    last: int = 0


def parse_probe(data) -> Probe:
    """Raises ConfigError with the <why> of "bad request: <why>"."""
    if not isinstance(data, dict):
        raise ConfigError("must be a JSON object")
    pid = data.get("id")
    _check_keys(data, ("id", "p", "s", "t", "a", "c", "w", "scan"), "probe")
    port = _int_in(data.get("p", 0), 0, MAX_PORTS - 1, "p")
    if "scan" in data and ("w" in data or "s" in data or "c" in data):
        raise ConfigError("scan can't be combined with s, c or w")
    if "scan" in data:
        # Default "any": devices commonly support only one of FC3/FC4 (the
        # tested relay is silent on FC4), so try holding then input
        table = data.get("t", SCAN_ANY)
        if table not in TABLES + (SCAN_ANY,):
            raise ConfigError("t must be any, co, di, hr or ir")
        address = _int_in(data.get("a", 0), 0, 65535, "a")
    else:
        table = data.get("t")
        if table not in TABLES:
            raise ConfigError("t must be co, di, hr or ir")
        if "a" not in data:
            raise ConfigError("missing a")
        address = _int_in(data["a"], 0, 65535, "a")

    if "scan" in data:
        rng = data["scan"]
        if (not isinstance(rng, list) or len(rng) != 2 or not all(_is_int(x) for x in rng)
                or not 1 <= rng[0] <= rng[1] <= 247):
            raise ConfigError("scan must be [first, last] slave ids within 1-247")
        if rng[1] - rng[0] + 1 > MAX_SCAN_IDS:
            raise ConfigError(f"scan covers at most {MAX_SCAN_IDS} ids")
        return Probe(id=pid, kind="scan", table=table, address=address, port=port, first=rng[0], last=rng[1])

    if "s" not in data:
        raise ConfigError("missing s")
    slave = _int_in(data["s"], 1, 247, "s")
    if "w" in data:
        if "c" in data:
            raise ConfigError("c is for reads only")
        if table == "co":
            value = _int_in(data["w"], 0, 1, "w")
        elif table == "hr":
            value = _int_in(data["w"], 0, 0xFFFF, "w")
        else:
            raise ConfigError("only co and hr can be written")
        return Probe(id=pid, kind="write", table=table, address=address, port=port, slave=slave, value=value)

    count = _int_in(data.get("c", 1), 1, MAX_PROBE_COUNT, "c")
    if address + count - 1 > 65535:
        raise ConfigError("a + c runs past 65535")
    return Probe(id=pid, kind="read", table=table, address=address, port=port, slave=slave, count=count)
