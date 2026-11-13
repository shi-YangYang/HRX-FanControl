"""Linux local machine and BMC identity, required before enabling any write."""
import platform
import re
from pathlib import Path

from .protocol import FANS, POLICY_LABELS


def validate_control_config(config):
    if config["fan"]["installed"] != FANS or config["fan"]["levels"] != POLICY_LABELS:
        raise ValueError("Control requires the HR650X six-fan layout and policy labels 15/20/30/45/60")
    if not config["ipmi"]["read_control_status"]:
        raise ValueError("Control requires BMC readback")
    if config["ipmi"]["command_timeout_seconds"] > 20 or config["ipmi"]["status_timeout_seconds"] > 3:
        raise ValueError("Control requires sensor/status timeouts <= 20/3 seconds")
    if not 5 <= config["controller"]["poll_interval_seconds"] <= 15:
        raise ValueError("Control requires a polling interval between 5 and 15 seconds")
    if config["safety"]["sensor_timeout_seconds"] > 30:
        raise ValueError("Control requires sensor freshness <= 30 seconds")


def machine_identity(ipmi, dmi_dir=Path("/sys/class/dmi/id")):
    if platform.system() != "Linux":
        raise ValueError("Hardware control requires Linux local OpenIPMI")
    product = (dmi_dir / "product_name").read_text().strip()
    machine = (dmi_dir / "product_uuid").read_text().strip().lower()
    if "HR650X" not in product.upper() or not re.fullmatch(r"[0-9a-f-]{36}", machine):
        raise ValueError("Expected an HR650X with a valid DMI product UUID")
    if set(machine.replace("-", "")) <= {"0"} or set(machine.replace("-", "")) <= {"f"}:
        raise ValueError("DMI UUID is a placeholder")
    result = ipmi.identity_info()
    if not result.succeeded:
        raise ValueError(f"Cannot identify BMC: {result.error or result.stderr}")
    fields = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            if key.strip() in fields:
                raise ValueError("Duplicate BMC identity field")
            fields[key.strip()] = value.strip()
    manufacturer = fields.get("Manufacturer ID", "").split()
    product_id = fields.get("Product ID", "").split()
    firmware = fields.get("Firmware Revision", "")
    if (not manufacturer or re.fullmatch(r"[0-9]+", manufacturer[0]) is None or not product_id or
            re.fullmatch(r"[0-9]+", product_id[0]) is None or not firmware):
        raise ValueError("BMC identity requires numeric manufacturer/product IDs and firmware revision")
    return {"product": product, "machine_uuid": machine, "manufacturer_id": int(manufacturer[0]),
            "product_id": int(product_id[0]), "firmware": firmware}
