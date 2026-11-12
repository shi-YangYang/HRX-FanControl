"""Closed HR650X write protocol, separate from the read-only command allowlist."""
from .ipmi import ReadOnlyIPMI, parse_control_status

LEVEL_BYTES = {1: 0x0f, 2: 0x14, 3: 0x1e, 4: 0x2d, 5: 0x3c}
POLICY_LABELS = [15, 20, 30, 45, 60]
FANS = [f"FAN{index}_R_Speed" for index in range(1, 7)]


class ControlIPMI(ReadOnlyIPMI):
    def identity_info(self):
        return self._execute(("ipmitool", "-I", "open", "mc", "info"), self.status_timeout)

    def set_level(self, level):
        if type(level) is not int or level not in LEVEL_BYTES:
            raise ValueError("Only calibrated HR650X levels 1..5 are supported")
        return self.set_parameter(LEVEL_BYTES[level])

    def set_parameter(self, parameter):
        validate_parameter(parameter)
        return self._execute(("ipmitool", "-I", "open", "raw", "0x2e", "0x30", "0x00", "0x00",
                              f"0x{parameter:02x}"), self.status_timeout)


def decode_status(raw):
    if isinstance(raw, str):
        raw = parse_control_status(raw)
    if not isinstance(raw, list) or len(raw) != 9:
        raise ValueError("Missing nine-byte BMC readback")
    if any(not isinstance(value, str) or len(value) != 4 or not value.startswith("0x") for value in raw):
        raise ValueError("Invalid BMC readback bytes")
    values = [int(value, 16) for value in raw]
    if any(value < 0 or value > 255 for value in values):
        raise ValueError("Invalid BMC readback byte")
    return {"mode": "automatic" if values[0] & 1 else "manual", "header": raw[0],
            "installed_duty_bytes": raw[1:7], "unused_duty_bytes": raw[7:9], "raw_bytes": raw[:]}


def validate_parameter(parameter):
    if type(parameter) is not int or not 1 <= parameter <= 100:
        raise ValueError("Manual parameter must be a decimal integer from 1 to 100")
    return parameter


def level_for_parameter(parameter):
    return next((level for level, value in LEVEL_BYTES.items() if value == parameter), None)


def matches_parameter(raw, parameter):
    status = decode_status(raw)
    return status["mode"] == "manual" and all(
        int(value, 16) == parameter for value in status["installed_duty_bytes"])


def matches_level(raw, level):
    return matches_parameter(raw, LEVEL_BYTES[level])
