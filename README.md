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

## Usage

On the server, read once or keep sampling; local BMC permissions are required:

```sh
sudo .venv/bin/hrx-fancontrol --once
sudo .venv/bin/hrx-fancontrol
```

The default interval is 10 seconds. Press Ctrl+C to stop.
The initial L5 recommendation is a startup hold: 60 seconds of healthy samples are required before it can decrease.

Replay the supplied snapshot without a BMC or ipmitool:

```sh
.venv/bin/hrx-fancontrol --once --input examples/hr650x-observed-sensors.txt --no-log
```

Add `--json` for JSON output or `--no-log` to disable file logging. Logs are saved in `logs/` and retained for seven UTC dates.

## Documentation

[HR650X guide — Chinese](docs/hr650x-guide.md) · [Default configuration](config/default.yaml) · [Source](src/hrx_fancontrol/)

[MIT License](LICENSE) · Copyright © 2026 小洋
