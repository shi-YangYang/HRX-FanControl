# HRX-FanControl

English · [简体中文](README.zh-CN.md)

Temperature monitoring and five-level fan control for **Lenovo ThinkSystem HR650X**.

- View component temperatures and six fan RPM readings in the terminal.
- Preview the recommended level, or switch a running service between automatic control and a fixed manual parameter.
- Save JSONL logs for analysis and run at boot with systemd.

**Read-only by default.** Fan control starts only when you enable the service; no calibration file is required. Cooling under load still needs observation on the target server.

## Install

Target: Ubuntu 24.04 with local HR650X access. Once a release build finishes, download its `.deb` and `SHA256SUMS` from [Releases](https://github.com/shi-YangYang/HRX-FanControl/releases). In the download directory, replace the example filename with your version:

```sh
sha256sum --check SHA256SUMS
sudo apt update
sudo apt install ./hrx-fancontrol_0.1.0_all.deb
```

APT installs the Python, PyYAML and ipmitool dependencies. **Installation does not start or enable fan control.** If GitHub is slow on the server, download on your computer and transfer with SCP. Existing source-installed services need the [migration steps](docs/hr650x-guide.md#从源码服务迁移到-deb); [source installation](docs/hr650x-guide.md#国内服务器安装) remains available. The detailed guide is in Chinese.

## Read temperatures and RPM

These commands display readings and recommendations without changing fan settings:

```sh
# Read once
sudo hrx-fancontrol --config /etc/hrx-fancontrol/config.yaml --once
# Keep sampling
sudo hrx-fancontrol --config /etc/hrx-fancontrol/config.yaml
```

The default target interval is 15 seconds. Press **Ctrl+C** to stop. JSONL logs with Chinese summaries are saved in `/var/log/hrx-fancontrol/` and retained for seven UTC dates. Add `--json` for full diagnostics or `--no-log` to disable file logging.

To try the supplied snapshot without a server or ipmitool:

```sh
hrx-fancontrol --once --input /usr/share/doc/hrx-fancontrol/examples/hr650x-observed-sensors.txt --no-log
```

## Enable fan control

Start the installed service and enable it at boot. The first start uses automatic control; later starts respect the saved mode:

```sh
sudo systemctl enable --now hrx-fancontrol
sudo systemctl status hrx-fancontrol
```

Keep the service running and send commands from another terminal:

```sh
sudo hrx-fancontrol auto on    # Enable periodic sampling and automatic control
sudo hrx-fancontrol auto off   # Disable automatic control; keep the service running
sudo hrx-fancontrol manual 25  # Set a fixed parameter of 25 and disable automatic control
sudo hrx-fancontrol status     # Show the mode and last observed readings
```

- **Manual parameters are decimal integers from 1 to 100:** `25` sends `0x19`, not a percentage of RPM. The thermal curve does not override this setting. The former minimum-level mode has been removed.
- **Off mode is remembered:** after manual control or `auto off`, service/server restarts keep automatic control disabled and do not resend the saved manual value.
- **Stopping and yielding:** normal stops keep the last setting. Detected external changes suspend control until an explicit `auto on` or `manual` command. See the [control guide](docs/hr650x-guide.md#校准与实际控制).

Each write is followed immediately by a BMC read. Manual mode and six matching fan parameters confirm the setting, without waiting for RPM to settle. Unavailable readback is retried within a deadline; manual control stops sampling once confirmed. Optional [RPM calibration](docs/hr650x-guide.md#校准与实际控制) is separate from service operation.

## Documentation

- [HR650X guide](docs/hr650x-guide.md): fan levels, BMC thresholds, fault handling, calibration, and deployment.
- [Configuration and logs](docs/hr650x-guide.md#配置与日志) · [Default configuration](src/hrx_fancontrol/default.yaml)
- [Source](src/hrx_fancontrol/) · [Service template](packaging/systemd/hrx-fancontrol.service)
- [Development guide](docs/development.md): project layout, local checks, packaging, and releases (Chinese).

[MIT License](LICENSE) · Copyright © 2026 小洋
