# HRX-FanControl

English · [简体中文](README.zh-CN.md)

Temperature monitoring and five-level fan control for **Lenovo ThinkSystem HR650X running Ubuntu 24.04**, through local IPMI.

- View component temperatures, six fan RPM readings and the recommended fan level.
- Use automatic temperature control or a fixed manual parameter through a systemd service.
- Receive L5 email alerts and keep local JSONL logs.

## Install or upgrade

Download the v0.1.2 package and checksum file from [Releases](https://github.com/shi-YangYang/HRX-FanControl/releases), verify them, then install:

```sh
version=0.1.2
base_url="https://github.com/shi-YangYang/HRX-FanControl/releases/download/v${version}"
deb_file="hrx-fancontrol_${version}_all.deb"
curl -fL "$base_url/$deb_file" -o "$deb_file" &&
curl -fL "$base_url/SHA256SUMS" -o SHA256SUMS &&
sha256sum --check SHA256SUMS &&
sudo apt update &&
sudo apt install "./$deb_file"
```

APT installs Python, PyYAML and ipmitool. **A first installation does not start or enable the service.** No calibration file is required.

To upgrade, change `version` and run the same commands. Configuration, saved modes and logs are retained; a running service is stopped for the update and restarted afterward. A stopped service stays stopped. If prompted about `/etc/hrx-fancontrol/config.yaml`, keep the current file (the default `N`).

For an existing source installation, follow the [migration steps](docs/hr650x-guide.md#从源码服务迁移到-deb) first. If GitHub is slow, download the files on your computer and transfer them with SCP.

## Start and control

Optionally read temperatures, RPM and recommendations before starting the service. This command does not change fan settings:

```sh
sudo hrx-fancontrol --config /etc/hrx-fancontrol/config.yaml --once
```

Start the service and enable it at boot. The first start uses automatic control; later starts respect the saved mode:

```sh
sudo systemctl enable --now hrx-fancontrol
sudo systemctl status hrx-fancontrol
```

With the service running:

```sh
sudo hrx-fancontrol auto on    # Enable periodic sampling and automatic control
sudo hrx-fancontrol auto off   # Pause sampling and automatic control; keep the service running
sudo hrx-fancontrol manual 25  # Set a fixed parameter of 25 and disable automatic control
sudo hrx-fancontrol status     # Show the mode and cached readings with their age
```

- **Manual parameters are decimal integers from 1 to 100**, not RPM percentages. Manual control stops sampling after the setting is confirmed; the thermal curve does not override it.
- After `auto off` or manual control, service/server restarts keep automatic control disabled and do not resend the saved manual value.
- `status` reads the service cache without taking a new hardware sample. Normal stops keep the last fan setting and do not restore BMC automatic mode.
- If an external change to a confirmed BMC setting is detected, the service yields control. Use `auto on` or `manual N` explicitly to resume.

Cooling under load still needs observation on the target server. See the [control guide](docs/hr650x-guide.md#校准与实际控制) for BMC confirmation and optional RPM calibration.

## Email alerts

```sh
sudo hrx-fancontrol email setup
```

Enter the SMTP host, account and authorization code (hidden input). The recipient defaults to your own address; an account that is not an email address also needs a sender address. Use the provider's settings: SSL/465 is the default, with STARTTLS/587 also supported. Saving enables alerts and takes effect without restarting the service.

```sh
sudo hrx-fancontrol email test    # Send a test email; check your inbox
sudo hrx-fancontrol email status  # Show settings and enabled state, without the authorization code
sudo hrx-fancontrol email off     # Disable email alerts
sudo hrx-fancontrol email on      # Re-enable alerts with the saved settings
```

Alerts are triggered when the service's automatic sampling recommends **L5**, once per continuous L5 episode. A valid reading below L5 allows a new alert on the next entry into L5. Restarting the service while still at L5 may send another alert. Manual and paused modes do not keep monitoring for email alerts. Details: [email alerts](docs/hr650x-guide.md#邮箱告警).

## Logs and documentation

The installed configuration is `/etc/hrx-fancontrol/config.yaml`. JSONL logs are stored in `/var/log/hrx-fancontrol/` and retained for seven UTC dates. To follow service events and email results:

```sh
sudo journalctl -u hrx-fancontrol -f
```

- [HR650X guide](docs/hr650x-guide.md): fan levels, fault handling, configuration, calibration and source installation (Chinese).
- [Default configuration](src/hrx_fancontrol/default.yaml) · [Configuration and logs](docs/hr650x-guide.md#配置与日志)
- [Development guide](docs/development.md): project layout, local checks and packaging (Chinese).

[MIT License](LICENSE) · Copyright © 2026 小洋
