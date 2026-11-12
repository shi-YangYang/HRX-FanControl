# HRX-FanControl

English · [简体中文](README.zh-CN.md)

Temperature monitoring and five-level fan control for **Lenovo ThinkSystem HR650X**.

- View component temperatures and six fan RPM readings in the terminal.
- Preview the recommended level, or enable automatic control with an optional manual minimum.
- Save JSONL logs for analysis and run at boot with systemd.

**Read-only by default.** Fan control requires an explicit option and calibration on the target machine. Hardware calibration, control response, and cooling under load still need real-server validation.

## Install

Target environment: Ubuntu 24.04, Python 3.9+, and local BMC access. The commands below install the `dev` branch.

```sh
sudo apt update
sudo apt install -y git ipmitool python3-venv
git clone --branch dev https://github.com/shi-YangYang/HRX-FanControl.git
cd HRX-FanControl
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
```

If GitHub is slow from the server, [transfer a source archive over SSH](docs/hr650x-guide.md#国内服务器安装) instead. The detailed guide is in Chinese.

## Read temperatures and RPM

These commands display readings and recommendations without changing fan settings:

```sh
# Read once
sudo .venv/bin/hrx-fancontrol --once
# Keep sampling
sudo .venv/bin/hrx-fancontrol
```

The default target interval is 15 seconds. Press **Ctrl+C** to stop. JSONL logs with Chinese summaries are saved in `logs/` and retained for seven UTC dates. Add `--json` for full diagnostics or `--no-log` to disable file logging.

To try the supplied snapshot without a server or ipmitool:

```sh
.venv/bin/hrx-fancontrol --once --input examples/hr650x-observed-sensors.txt --no-log
```

## Enable fan control

First calibrate on the target HR650X. **This actually changes the fans from L5 down to L1** and takes several minutes; temperature demand can interrupt the process. Choose a calibration filename that does not already exist.

```sh
sudo .venv/bin/hrx-fancontrol --calibrate calibration.json
```

After successful calibration, choose one mode using that machine's profile:

```sh
# Automatic control
sudo .venv/bin/hrx-fancontrol --auto --profile calibration.json
# Or set a minimum of L2; temperature demand can still raise it
sudo .venv/bin/hrx-fancontrol --manual-level 2 --profile calibration.json
```

- **Manual mode sets a floor:** L2 keeps the fans at least at L2; a demand for L4 still selects L4.
- **Normal stops keep the last setting:** Ctrl+C or stopping the service does not restore BMC automatic mode.
- **Detected external changes take priority:** control stops and preserves the external setting, including across restarts. Explicit resumption requires `--resume-control`; see the [control guide](docs/hr650x-guide.md#校准与实际控制).

For unattended operation, follow the [systemd installation guide](docs/hr650x-guide.md#systemd-服务) to install the service, enable startup at boot, and manage it with `systemctl`.

## Documentation

- [HR650X guide](docs/hr650x-guide.md): fan levels, BMC thresholds, fault handling, calibration, and deployment.
- [Configuration and logs](docs/hr650x-guide.md#配置与日志) · [Default configuration](config/default.yaml)
- [Source](src/hrx_fancontrol/) · [Service template](systemd/hrx-fancontrol.service)

[Linux CI](.github/workflows/ci.yml) checks installation, unit tests, and offline CLI replay on Ubuntu 24.04 with Python 3.9 and 3.12. It runs for pull requests targeting `main` and pushes to `main`.

[MIT License](LICENSE) · Copyright © 2026 小洋
