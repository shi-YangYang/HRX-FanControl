def fan_health(readings, installed, minimum_rpm):
    fans, errors = {}, []
    expected = {name.strip().casefold(): name for name in installed}
    for key, reading in readings.items():
        if reading.unit.casefold() == "rpm" or key in expected:
            error = reading.error
            if not error and reading.unit.casefold() != "rpm":
                error = "expected RPM unit"
            if not error and (reading.value <= 0 or reading.value < minimum_rpm):
                error = "fan stopped or below configured RPM floor"
            if not error and reading.status != "ok":
                error = f"BMC fan alarm: {reading.status}"
            fans[reading.name] = {"rpm": reading.value, "status": reading.status, "error": error}
            if error:
                errors.append(f"{reading.name}: {error}")
    for key, name in expected.items():
        if key not in readings:
            fans[name] = {"rpm": None, "status": "missing", "error": "missing installed fan"}
            errors.append(f"{name}: missing installed fan")
    return fans, errors
