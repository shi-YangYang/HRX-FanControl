# HRX-FanControl

English · [简体中文](README.zh-CN.md)

Temperature monitoring and five-level fan control for **Lenovo ThinkSystem HR650X**.

- View component temperatures and six fan RPM readings in the terminal.
- Preview the recommended level, or switch a running service between automatic control and a fixed manual parameter.
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

After calibration, install the service using the [systemd guide](docs/hr650x-guide.md#systemd-服务), or start it in the foreground:

```sh
sudo .venv/bin/hrx-fancontrol --serve --profile calibration.json
```

Keep the service running and send commands from another terminal:

```sh
sudo .venv/bin/hrx-fancontrol auto on    # Enable periodic sampling and automatic control
sudo .venv/bin/hrx-fancontrol auto off   # Disable automatic control; keep the service running
sudo .venv/bin/hrx-fancontrol manual 25  # Set a fixed parameter of 25 and disable automatic control
sudo .venv/bin/hrx-fancontrol status     # Show the mode and last observed readings
```

- **Manual parameters are decimal integers from 1 to 100:** `25` sends `0x19`, not a percentage of RPM. The thermal curve does not override this setting. The former minimum-level mode has been removed.
- **Off mode is remembered:** after manual control or `auto off`, service/server restarts keep automatic control disabled and do not resend the saved manual value.
- **Stopping and yielding:** normal stops keep the last setting. Detected external changes suspend control until an explicit `auto on` or `manual` command. See the [control guide](docs/hr650x-guide.md#校准与实际控制).

Manual writes use bounded verification reads only. A returned command does not mean RPM has been verified. `status` shows confirmation progress and cache age; parameters outside the five calibrated levels are never reported as calibrated RPM verification.

## Documentation

- [HR650X guide](docs/hr650x-guide.md): fan levels, BMC thresholds, fault handling, calibration, and deployment.
- [Configuration and logs](docs/hr650x-guide.md#配置与日志) · [Default configuration](config/default.yaml)
- [Source](src/hrx_fancontrol/) · [Service template](systemd/hrx-fancontrol.service)

[Linux CI](.github/workflows/ci.yml) checks installation, unit tests, and offline CLI replay on Ubuntu 24.04 with Python 3.9 and 3.12. It runs for pull requests targeting `main` and pushes to `main`.

[MIT License](LICENSE) · Copyright © 2026 小洋
