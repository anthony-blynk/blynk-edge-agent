# Modbus gateway config & protocol - v2

The one contract shared by the console widget (`widget/modbus-console.html`),
the Linux gateway (`gateway/`) and the ESP32 firmware
(`firmware/esp32-t-vending/`). Change it here first, then in every
implementation.

## Datastreams (template "Modbus Gateway")

| Name | Type | Written by | Purpose |
|---|---|---|---|
| `ModbusConfig` | String | widget / humans only | The register map below |
| `ModbusStatus` | String | gateway only | Applied rev + per-point health |
| `ModbusProbe` | String | widget only | One-off test read/write/scan command |
| `ModbusProbeResult` | String | gateway only | Answer to the last probe |
| *anything else* | any | gateway (reads), widget/app (writes) | Point targets, e.g. `Temperature`, `Relay` |

One writer per datastream (same lesson as `blynk-robustel-sensor-bridge`):
the gateway never rewrites `ModbusConfig`, the widget never writes
`ModbusStatus`.

Point target datastreams **must already exist on the template** - Blynk
can't create datastreams at runtime. The widget offers every datastream
assigned to it (other than the four above) as a target.

Every String value must stay **≤ 1024 characters**. The widget refuses to
save a config over that; the gateway truncates its status to fit.

**Gateways must subscribe to each downlink topic by exact name**
(`downlink/ds/ModbusConfig`, `downlink/ds/ModbusProbe`, and
`downlink/ds/<name>` for every `w`/`rw` point, re-synced whenever the
config changes). Confirmed on real hardware 2026-10-19: on an MQTT v5
connection Blynk's broker grants wildcard subscriptions (`downlink/ds/+`,
`#`) but never delivers anything to them. MQTT 3.1.1 wildcards do work,
but `blynk-provisioning` uses v5 for connection stability.

`ModbusConfig` needs a template default value (the empty config below) -
Blynk doesn't answer `get/ds` for a datastream that was never written and
has no default.

## `ModbusConfig`

A gateway can have several RS485 ports (a Robustel EG5120 has two); each
port is its own bus with its own settings and its own devices.

```json
{
  "v": 2,
  "rev": 3,
  "poll_s": 5,
  "ports": [
    {"baud": 9600, "parity": "N", "stop": 1, "timeout_ms": 1000, "pts": [
      ["Temperature", 1, "ir", 1, "i16", 0.1],
      ["Humidity",    1, "ir", 2, "u16", 0.1],
      ["Relay",       2, "hr", 1, "u16", 1, {"m": "w", "map": {"1": 256, "0": 512}}]
    ]},
    {"baud": 19200, "parity": "E", "pts": [
      ["Flow", 1, "hr", 0, "f32"]
    ]}
  ]
}
```

| Key | Default | Meaning |
|---|---|---|
| `v` | - | Format version: 2 (1 is still accepted, below). Gateways reject a `v` they don't know. |
| `rev` | 0 | Bumped by the widget on every save; echoed in `ModbusStatus` so the widget can show "applied". |
| `poll_s` | 5 | Default poll interval for read points, seconds (min 1). |
| `ports` | - | 1-4 ports, in the gateway's port order (below). |
| `ports[].baud` | 9600 | Shared by every slave on that port. |
| `ports[].parity` | `"N"` | `N`, `E` or `O`. |
| `ports[].stop` | 1 | 1 or 2. |
| `ports[].timeout_ms` | 1000 | Per-request response timeout. |
| `ports[].pts` | `[]` | That port's points, below. Max 32 across all ports. |

Ports are identified by **position only**: `ports[0]` is the gateway's
first port, `ports[1]` its second. The serial device behind each position
is hardware, set locally on the gateway (`--port` given once per port on
Linux, e.g. `/dev/ttymxc0` on the CompuLab; board pins on an ESP32). Slave
ids are per port - slave 1 can exist on both. Datastream names are unique
across the whole config. In human-facing text (widget, events) ports are
numbered from 1: `ports[0]` is "Port 1".

A config listing more ports than the gateway has is rejected as a whole
(`cfg`: `port 2: this gateway has 1 port`); `ModbusStatus.ports` tells the
widget how many there are.

**v1** (the template default, and configs saved before v2) is one port
with its settings under `bus` and the points at the top level:
`{"v":1,"rev":0,"bus":{"baud":9600},"poll_s":5,"pts":[]}`. Gateways read
it as a v2 config with that single port; the widget reads it the same way
and always saves v2. The same empty config in v2:
`{"v":2,"rev":0,"poll_s":5,"ports":[{"baud":9600,"pts":[]}]}` (the template
default stays v1, so gateways from before v2 can still start from it).

### Points

Each port's `pts` holds that port's points as positional arrays, to fit ~20 points in 1024 chars:

`[datastream, slave, table, address, type?, scale?, options?]`

| # | Field | Values |
|---|---|---|
| 0 | datastream | Target datastream name (as used in MQTT `ds/<name>`). Unique across all ports. |
| 1 | slave | 1-247, on this point's port |
| 2 | table | `co` coil (FC1 / write FC5), `di` discrete input (FC2), `hr` holding register (FC3 / write FC6, FC16), `ir` input register (FC4) |
| 3 | address | 0-65535, **0-based protocol address** (what goes on the wire; vendor docs saying "30002" or "register 2" may need converting - check against a probe) |
| 4 | type | `u16` (default), `i16`, `u32`, `i32`, `f32`. Ignored for `co`/`di` (always 0/1). |
| 5 | scale | Default 1. Read: `value = raw * scale + offset`. |
| 6 | options | Object, all keys optional, below. |

Options:

| Key | Default | Meaning |
|---|---|---|
| `m` | `"r"` | `r` poll and publish; `w` write on downlink only; `rw` both. `w`/`rw` only valid for `co`/`hr`. |
| `o` | 0 | Offset, applied after scale. |
| `wo` | `"be"` | 32-bit word order: `be` = high word first (ABCD), `le` = low word first (CDAB). |
| `b` | - | Bit index 0-15: read that single bit of a 16-bit register as 0/1. Read-only. |
| `dp` | from scale | Decimal places to round to (scale 0.1 -> 1, 0.01 -> 2, 1 -> 0). |
| `p` | `poll_s` | This point's own poll interval, seconds. |
| `map` | - | Datastream value -> raw value, e.g. `{"1": 256, "0": 512}`. Writes look up the incoming value (normalised: `"1.0"` -> `"1"`); an unmapped value is rejected. Reads publish the matching key if the raw value is in the map, else the scaled number. |
| `fc16` | false | Write 16-bit values with FC16 (Write Multiple) instead of FC6 - some devices only accept FC16. |

Writes without `map`: `raw = round((value - offset) / scale)`, encoded
by type. After a successful write the gateway publishes the value back to
the datastream (confirmed state). After a failed one it reports the error
in `ModbusStatus` and **snaps the datastream back**: it re-publishes the
point's last confirmed value - the last value it successfully wrote, or
for `rw` points the last value it read, whichever is newer - so a
dashboard switch flips back instead of showing a state the device never
reached. If there's no confirmed value yet (nothing written or read since
the gateway started) it publishes nothing. Values confirmed by a
**write** survive a gateway restart (see "Runtime state" below); values
from `rw` reads are memory only - the first poll after a restart
re-reads them anyway, and persisting every reading would wear flash.

A failed `w` point stays in `bad` until a later write to it succeeds -
nothing polls a write-only point, so there's no other way to see it
recover.

Validation: the gateway rejects the **whole** config (keeps running the
previous one, reports why in `ModbusStatus.cfg`) if anything is invalid.
A partly-applied register map is worse than a stale one.

## `ModbusStatus` (gateway -> widget)

```json
{"rev": 3, "cfg": "ok", "ts": 1760870000, "up": 3600, "ports": 2, "n": 3, "bad": {"Humidity": "timeout"}}
```

| Key | Meaning |
|---|---|
| `rev` | `rev` of the config currently running. |
| `cfg` | `"ok"`, or why the last received config was rejected. |
| `ts` | Unix seconds when published. Omitted while the gateway's wall clock isn't set yet (an ESP32 before SNTP sync) - consumers must cope with it missing. |
| `up` | Gateway uptime, seconds. |
| `ports` | How many RS485 ports the gateway has (fixed hardware, not from the config). Missing from gateways older than v2 - read it as 1. |
| `n` | Number of points running. |
| `bad` | Points whose last read/write failed -> short reason. Dropped first (alphabetically from the end) if the status would exceed 1024 chars; `"more": k` then says how many were cut. |

Published on connect, on every config apply or reject, whenever the set of
failing points changes, and at least every 5 minutes.

## Events (`modbus_error` / `modbus_ok`)

So a failure reaches people who aren't looking at the console - a dead
sensor otherwise just leaves its last reading on the dashboard. Both
events are defined on the template (`modbus_error` WARNING, `modbus_ok`
INFORMATION); the gateway publishes MQTT topic `event/<code>` with the
description as the plain-text payload.

Grouped **per slave** - per (port, slave id), since ids are per port -
because one dead device fails all its points at once and should be one
alert:

- A slave is *failing* while at least one of its points is in `bad` for a
  reason other than `bad value: ...` (a nonsense value written from the
  dashboard is the user's input, not the device's fault).
- `modbus_error` is sent once a slave has been failing continuously for
  **10 s** - long enough that a single dropped reply stays quiet and every
  point of the slave has been tried, so the description lists them all.
  Description: `Slave <id>: <point> <reason>, <point> <reason>`, points in
  config order, cut to 300 chars with `...`. E.g.
  `Slave 1: Temperature offline (backoff), Humidity offline (backoff)`.
  When the config has more than one port the slave is named with its
  port: `Port 2 slave 1: ...` (and `Port 2 slave 1 OK again`).
- `modbus_ok` (`Slave <id> OK again`) is sent when a slave that had a
  `modbus_error` sent has no failing points any more **and** at least one
  read or write to it has succeeded since the alert (an empty `bad` alone
  isn't proof - a config apply clears it).
- At most one `modbus_error` per failure: no repeats while it stays down,
  even if its list of failing points or reasons changes.
- Only while MQTT is connected; a transition that happens while
  disconnected is sent once it's connected again (it's re-evaluated, not
  queued, so a failure that recovered meanwhile sends nothing).
- A config apply clears `bad` and forgets alert state for slaves no longer
  in the config (no `modbus_ok` for those); slaves still in the config keep
  their alerted state, so their `modbus_ok` still arrives.
- Open alerts survive a gateway restart (see "Runtime state"): a slave
  that's still down after a reboot doesn't get a second `modbus_error`,
  and one that recovered while the gateway was down still gets its
  `modbus_ok` - on its first successful read or write after the restart.

## Runtime state (gateway-local, survives restarts)

Found on real hardware: a restart mid-outage sent a duplicate
`modbus_error`, never sent the `modbus_ok`, and left a failed relay
write with nothing to snap back to. So two things are persisted locally
next to the saved config (Linux: `runtime_state.json` in the state dir;
ESP32: an NVS string), as one small JSON document:

```json
{"rev": 1, "confirmed": {"Relay": "1"}, "alerts": [[0, 2]]}
```

- `confirmed`: write-confirmed values (above), by point name.
- `alerts`: slaves with a `modbus_error` sent and no `modbus_ok` yet, as
  `[port index, slave id]` pairs. A plain slave id (state written before
  v2) means port index 0.
- `rev`: the config rev it belongs to. On load it's used only if it
  matches the running config's rev; confirmed values are also dropped for
  points no longer in the config, alerts for slaves no longer in it.
  A config apply that keeps a point unchanged carries its value over and
  rewrites the state with the new rev.
- Written only when it changes (a successful write with a new value, an
  alert raised or cleared, a config apply) - never per poll.
- A restored alert starts with "no success since the alert", so
  `modbus_ok` needs a real read/write after the restart.
- Lost or unreadable state is not an error: the gateway starts with none,
  as before.

## `ModbusProbe` / `ModbusProbeResult`

For commissioning: try a register before adding it as a point. `p` picks
the port (index, default 0); the probe uses that port's settings from the
running config (defaults if the config doesn't list that port) and runs
between that port's polls, never concurrently with them. A `p` past the
gateway's last port is `bad request: ...`. One probe at a time per
gateway: a second one while the first runs is `busy`.

Read: `{"id": "a1", "p": 0, "s": 1, "t": "ir", "a": 1, "c": 2}` (`c` = count, 1-16, default 1)
Write: `{"id": "a2", "s": 2, "t": "hr", "a": 1, "w": 256}` (`co` takes 0/1)
Scan: `{"id": "a3", "scan": [1, 16]}` - which slave ids in that range (at most 16 ids) answer a 1-register read at address `a` (default 0); any Modbus exception reply still counts as "present". `t` defaults to `"any"`: try a holding-register read (FC3), then an input-register read (FC4) if the first gets no answer - devices often support only one (the tested relay is silent on FC4). Scans use a 200 ms per-request timeout (or the port's `timeout_ms` if shorter) instead of the port's timeout - real devices answer in tens of ms - so a silent id costs up to 0.4 s with `"any"`. `t` can also be `co`/`di`/`hr`/`ir` to probe one table only.

Result:

```json
{"id": "a1", "ok": true, "regs": [0, 215], "ms": 42}
{"id": "a2", "ok": true, "ms": 38}
{"id": "a3", "ok": true, "found": [1, 2], "via": {"1": "ir", "2": "hr"}, "ms": 4120}
{"id": "a1", "ok": false, "err": "timeout", "ms": 1003}
```

`regs` holds raw unsigned 16-bit words (or 0/1 for `co`/`di`). Errors are
short strings: `timeout`, `exception 2` (Modbus exception code), `bad
request: <why>`, `busy`.
