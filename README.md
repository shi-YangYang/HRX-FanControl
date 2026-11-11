# HRX-FanControl

English · [简体中文](README.zh-CN.md)

Read temperatures and fan RPM on Lenovo ThinkSystem HR650X servers, and simulate fan-level recommendations. **The current version is read-only and does not change fan speeds.**

## Quick start

Target environment: Ubuntu 24.04 and Python 3.9+. The prototype is on `dev`.

```sh
sudo apt update
sudo apt install -y git ipmitool python3-venv
git clone --branch dev https://github.com/shi-YangYang/HRX-FanControl.git
cd HRX-FanControl
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
```

If GitHub is slow from the server, [transfer a source archive over SSH](docs/hr650x-guide.md#国内服务器安装) (Chinese).

## Usage

On the server, read once or keep sampling; local BMC permissions are required:

```sh
sudo .venv/bin/hrx-fancontrol --once
sudo .venv/bin/hrx-fancontrol
```

The target interval is 15 seconds; a failed collection waits another 15 seconds before retrying.
Sensor reads have a 20-second timeout; status reads have a separate 3-second timeout. Ctrl+C cancels the active read and stops.
The first valid sample determines the recommendation immediately; while waiting for data, no level is assumed. After a fault, five consecutive healthy samples release the hold. Thermal downshifts still require the configured 60-second cooldown and 3°C margin.
Brief read failures hold the previous recommendation. Five consecutive failures or 300 seconds without a valid sensor sample set a floor of L3; an existing L4/L5 is kept. Sensors with a curve follow that curve. BMC alarms on sensors without a curve, and fan faults, still immediately recommend L5.
Entering L5 from any source prints one terminal warning. It repeats only after leaving and re-entering L5. BMC details remain in diagnostic logs without separate notifications.

Replay the supplied snapshot without a BMC or ipmitool:

```sh
.venv/bin/hrx-fancontrol --once --input examples/hr650x-observed-sensors.txt --no-log
```

Add `--json` for the full diagnostic report or `--no-log` to disable file logging. File logs use compact JSONL v2 with Chinese summaries, temperatures, RPM, sampling totals, and diagnostics when needed; they differ from `--json`. Logs are saved in `logs/` and retained for seven UTC dates. See the [log example](docs/hr650x-guide.md#日志内容).

## Documentation

[HR650X guide — Chinese](docs/hr650x-guide.md) · [Default configuration](config/default.yaml) · [Source](src/hrx_fancontrol/)

## CI

[Linux CI](.github/workflows/ci.yml) runs on pull requests targeting `main` and pushes to `main`, including merges. It checks package installation, unit tests, and offline CLI replay on Ubuntu 24.04 with Python 3.9 and 3.12.

[MIT License](LICENSE) · Copyright © 2026 小洋
