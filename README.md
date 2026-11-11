# HRX-FanControl

English · [简体中文](README.zh-CN.md)

Monitor temperatures and fan RPM on Lenovo ThinkSystem HR650X servers, then use a same-machine calibration for automatic or monitored manual fan control. **The default command remains read-only; writing requires an explicit control option.**

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
IPMI commands within one process run serially: each finishes or completes timeout cleanup before the next starts, without overlapping polls. Sensor reads have a 20-second timeout; BMC status reads and fan writes each have a 3-second timeout. Ctrl+C cancels the active read and stops.
The first valid sample determines the recommendation immediately; while waiting for data, no level is assumed. After a fault, five consecutive healthy samples release the hold. Thermal downshifts still require the configured 60-second cooldown and 3°C margin.
Brief read failures hold the previous recommendation. Five consecutive failures or 300 seconds without a valid sensor sample set a floor of L3; an existing L4/L5 is kept. Fan faults also set a floor of L3; higher thermal demand or an existing recovery hold is kept. Sensors with a curve follow that curve. BMC alarms on sensors without a curve still immediately recommend L5.
Entering L5 from any source prints one terminal warning. It repeats only after leaving and re-entering L5. BMC details remain in diagnostic logs without separate notifications.

Replay the supplied snapshot without a BMC or ipmitool:

```sh
.venv/bin/hrx-fancontrol --once --input examples/hr650x-observed-sensors.txt --no-log
```

Add `--json` for the full diagnostic report or `--no-log` to disable file logging. File logs use compact JSONL v2 with Chinese summaries, temperatures, RPM, sampling totals, and diagnostics when needed; they differ from `--json`. Logs are saved in `logs/` and retained for seven UTC dates. See the [log example](docs/hr650x-guide.md#日志内容).

## Enable fan control

On the target Linux HR650X, create a new calibration file first. This actively steps from L5 to L1 and takes several minutes; temperature demand can prevent completion.

```sh
sudo .venv/bin/hrx-fancontrol --calibrate calibration.json
sudo .venv/bin/hrx-fancontrol --auto --profile calibration.json
# Or keep at least L2, while allowing the thermal policy to raise the level:
sudo .venv/bin/hrx-fancontrol --manual-level 2 --profile calibration.json
```

Manual mode sets a minimum level: with L2 selected, a curve demand of L1 runs at L2, while a demand of L4 runs at L4. Temperature monitoring continues; the fans are not locked at L2.

Calibration binds the machine UUID, BMC firmware, six fans, command bytes and measured RPM. Automatic/manual control requires matching evidence. A successful command is only marked verified after readback and three new stable RPM samples. Historical averages are not calibration.

**Ctrl+C, SIGTERM and normal service stops keep the last setting without another write.** They do not restore BMC automatic mode. If stopped during a write, the resulting setting may be uncertain. An execution failure attempts L3 once, verifies within a bounded window, and exits with failure; the normal thermal curve can still select L4/L5. Foreground SIGKILL or a power loss cannot run Python cleanup.

If a previously confirmed BMC mode or duty setting changes, control stops and preserves the external setting, without fallback or automatic restart taking over. This suspension survives reboot. To resume explicitly, add `--resume-control` to your control command; see the guide for service usage. RPM fluctuations or failed reads alone do not count as external changes.

See the [calibration and systemd instructions](docs/hr650x-guide.md#校准与实际控制) and the [service template](systemd/hrx-fancontrol.service). After installation and calibration, `sudo systemctl enable --now hrx-fancontrol` starts the service and enables it at boot. Manage it with `systemctl start/stop/restart/status hrx-fancontrol`. Software checks and calibration do not certify cooling under load.

## Documentation

[HR650X guide — Chinese](docs/hr650x-guide.md) · [Default configuration](config/default.yaml) · [Source](src/hrx_fancontrol/)

## CI

[Linux CI](.github/workflows/ci.yml) runs on pull requests targeting `main` and pushes to `main`, including merges. It checks package installation, unit tests, and offline CLI replay on Ubuntu 24.04 with Python 3.9 and 3.12.

[MIT License](LICENSE) · Copyright © 2026 小洋
