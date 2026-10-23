"""Bus errors, kept free of pymodbus so the gateway logic (and its tests)
can catch them without it installed. str() of each is the short reason
that goes into ModbusStatus.bad / ModbusProbeResult.err."""


class BusError(Exception):
    pass


class ModbusTimeout(BusError):
    """No (valid) answer - also covers CRC errors/garbled frames, which on
    RS485 look the same as silence: wrong baud, swapped A/B, slave absent."""

    def __str__(self):
        return "timeout"


class ModbusExceptionReply(BusError):
    """The slave answered, but with a Modbus exception (2 = illegal address
    etc.) - it's present, the request is wrong."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code

    def __str__(self):
        return f"exception {self.code}"


class SlaveOffline(BusError):
    """Skipped without touching the bus - the slave is in timeout backoff."""

    def __str__(self):
        return "offline (backoff)"


class SerialError(BusError):
    """The port itself failed (can't open, unplugged USB adapter...)."""

    def __str__(self):
        return f"serial: {self.args[0] if self.args else '?'}"[:60]
