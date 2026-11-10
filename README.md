# HRX-FanControl

English · [简体中文](README.zh-CN.md)

Read-only thermal monitoring and fan-level simulation for Lenovo ThinkSystem HR650X servers.

**Version 0.1.0 is a Read-only / Dry-run prototype. It never applies a recommendation to hardware.**
IPMI argument encoding, BMC readback, and physical RPM response are not fully calibrated. Automatic fan control is not implemented.

## Features

- Read temperatures and six configured fan tachometers through local OpenIPMI.
- Calculate five levels from per-sensor curves, hysteresis, and startup/fault recovery holds.
- Report sensor faults, BMC temperature alarms, raw status bytes, and measured RPM separately.
- Display text or JSON and keep local JSONL logs for seven UTC dates; replay saved sensor snapshots without hardware.

The current target is HR650X. HR630X community reports are reference material, not evidence of model compatibility.

## Quick start

Target server environment: Ubuntu 24.04, Python 3.9+, and local BMC access. The supplied HR650X snapshot reports BMC 11.99 and ipmitool 1.8.19; continuous operation on the server still needs verification.

```sh
sudo apt update
sudo apt install -y git ipmitool python3-venv
git clone --branch dev https://github.com/shi-YangYang/HRX-FanControl.git
cd HRX-FanControl
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
```

The prototype is on the `dev` branch. Offline replay needs only the Python environment and package dependencies; it does not require ipmitool or a BMC.

## Usage

Replay the supplied HR650X sensor snapshot without accessing hardware:

```sh
.venv/bin/hrx-fancontrol --once --input examples/hr650x-observed-sensors.txt --no-log
```

Read one live snapshot on the server, or keep sampling:

```sh
sudo .venv/bin/hrx-fancontrol --once
sudo .venv/bin/hrx-fancontrol
```

Live reads need local BMC permissions. The default polling interval is 10 seconds; press Ctrl+C to stop.
Startup and fault recovery require 60 seconds of healthy samples before lowering the recommendation. With the default profile, `--once` therefore shows an initial L5 hold even when the component demand is L1. This is a simulated recommendation, not a fan-speed change.

| Option | Purpose |
|---|---|
| `--json` | Emit one JSON snapshot per line |
| `--no-log` | Disable local file logging |
| `--input PATH` | Replay a sensor text file; requires `--once` |
| `--config PATH` | Apply YAML overrides to the bundled defaults |

Logs default to `logs/` relative to the working directory. No systemd service or automatic startup is installed.

## Configuration

[config/default.yaml](config/default.yaml) links to the single bundled default profile. Use it directly or supply a YAML override:

```sh
sudo .venv/bin/hrx-fancontrol --config config/default.yaml
```

The five policy labels are **15 / 20 / 30 / 45 / 60%**. They are not verified BMC percentages or RPM ratios.
PCH enters L2 at 65°C and returns from L2 to L1 only after staying at or below 62°C for 60 seconds. Calibration remains `unverified`; changing YAML cannot enable hardware control.

## Project layout

| Path | Purpose |
|---|---|
| `src/hrx_fancontrol/` | Python package and bundled default profile |
| `config/` | Link to the default profile |
| `examples/` | Separate synthetic and observed HR650X sensor snapshots |
| `tests/` | Prototype and HR650X snapshot regression tests |
| `docs/` | Detailed operating and calibration guide |
| `constitution/` | Project mission, roadmap, and technical facts |
| `specs/` | Specifications, plans, acceptance records, and templates |
| `.ai/` | Project rules, decisions, workflow, and task handoff |
| `AGENTS.md` | Agent development and Git rules |

## Documentation

- [HR650X guide — Chinese](docs/hr650x-guide.md): five-level curves, hysteresis, BMC thresholds, logs, observed data, and calibration requirements.
- [Technical stack and module responsibilities — Chinese](constitution/tech-stack.md).
- [Project roadmap — Chinese](constitution/roadmap.md).

## Development

Work on `dev`; integration into `main` uses a `dev → main` pull request handled by the maintainer. Commit, push, PR, merge, and synchronization are separate authorized operations under [AGENTS.md](AGENTS.md).

For editable development and the focused HR650X regression checks:

```sh
.venv/bin/python -m pip install -e .
PYTHONPATH=tests .venv/bin/python -m unittest test_hr650x_observation -v
```

Choose checks according to the change; documentation-only edits need a diff review. Tests and offline replay do not establish hardware control response or full-load thermal safety. See the [technical notes](constitution/tech-stack.md#命令) for other test scopes.

## License

[MIT](LICENSE), copyright © 2026 小洋.
