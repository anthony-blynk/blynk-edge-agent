"""Register words <-> datastream values for one Point. Pure.

decode(): what a poll publishes. encode(): the words a downlink write puts
on the wire. Both follow docs/config-format.md: value = raw * scale + o on
reads, raw = round((value - o) / scale) on writes, map overriding both."""

from __future__ import annotations

import math
import struct
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import List

from .config import INT_RANGES, Point, normalize_key


class CodecError(ValueError):
    pass


def register_count(point: Point) -> int:
    """Registers (or bits) one read of this point needs."""
    if point.table in ("co", "di") or point.bit is not None:
        return 1
    return 2 if point.type in ("u32", "i32", "f32") else 1


def _raw_from_words(point: Point, words: List[int]):
    """The un-scaled value: 0/1 for bits, signed int for i16/i32, float for f32."""
    if len(words) < register_count(point):
        raise CodecError("short response")
    if point.table in ("co", "di"):
        return 1 if words[0] else 0
    if point.bit is not None:
        return (words[0] >> point.bit) & 1
    if point.type == "u16":
        return words[0] & 0xFFFF
    if point.type == "i16":
        w = words[0] & 0xFFFF
        return w - 0x10000 if w >= 0x8000 else w
    hi, lo = words[0] & 0xFFFF, words[1] & 0xFFFF
    if point.word_order == "le":
        hi, lo = lo, hi
    fmt = {"u32": ">I", "i32": ">i", "f32": ">f"}[point.type]
    return struct.unpack(fmt, struct.pack(">HH", hi, lo))[0]


def format_number(value: float, dp: int) -> str:
    if not math.isfinite(value):
        raise CodecError("not a finite number")
    # Via Decimal, half away from zero: 215 * 0.1 is 21.500000000000004 in
    # binary, and f"{x:.0f}" would round 21.5 and 22.5 to the same 22
    # (prec 64: an f32 can be ~3.4e38, 39 digits before dp decimals)
    d = Decimal(repr(round(value, 9))).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP,
                                                context=Context(prec=64))
    text = f"{d:f}"
    # -0.04 rounded to 1 dp is "-0.0" - publish plain 0
    return text.lstrip("-") if d == 0 else text


def decode(point: Point, words: List[int]) -> str:
    """Words as read from the bus (bits as 0/1) -> the published payload."""
    raw = _raw_from_words(point, words)
    for key, mapped in point.map:
        if mapped == raw:
            return key
    return format_number(raw * point.scale + point.offset, point.dp)


def _round_half_away(x: float) -> int:
    # Not round(): banker's rounding would write 2.5 as 2. The round(x, 9)
    # first absorbs binary error, e.g. 0.35 / 0.1 = 3.4999999999999996
    x = round(x, 9)
    return int(math.copysign(math.floor(abs(x) + 0.5), x))


def encode(point: Point, payload) -> List[int]:
    """Downlink value -> words to write (bits as [0/1]). Raises CodecError
    with a short reason for anything that can't be written as asked -
    never clamps, since writing a different value than asked is worse
    than not writing."""
    text = str(payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload)
    if point.map:
        key = normalize_key(text)
        for k, mapped in point.map:
            if k == key:
                raw = mapped
                break
        else:
            raise CodecError(f"unmapped {key[:20]!r}")
    else:
        try:
            value = float(text.strip())
        except ValueError:
            raise CodecError(f"not a number {text.strip()[:20]!r}") from None
        if not math.isfinite(value):
            raise CodecError("not a finite number")
        raw = (value - point.offset) / point.scale
        if point.table == "co" or point.type != "f32":
            raw = _round_half_away(raw)

    if point.table == "co":
        if raw not in (0, 1):
            raise CodecError(f"coil takes 0/1, got {raw}")
        return [int(raw)]
    if point.type == "f32":
        try:
            packed = struct.pack(">f", raw)
        except OverflowError:
            raise CodecError("out of f32 range") from None
    else:
        lo, hi = INT_RANGES[point.type]
        if not lo <= raw <= hi:
            raise CodecError(f"{raw} out of {point.type} range")
        if point.type in ("u16", "i16"):
            return [raw & 0xFFFF]
        packed = struct.pack(">I", raw & 0xFFFFFFFF)
    hi_w, lo_w = struct.unpack(">HH", packed)
    return [lo_w, hi_w] if point.word_order == "le" else [hi_w, lo_w]
