# HRX-FanControl

[English](README.md) · 简体中文

通过本机 IPMI，为 **运行 Ubuntu 24.04 的 Lenovo ThinkSystem HR650X** 提供温度监测与五档风扇控制。

- 查看各部件温度、六个风扇的实际 RPM 和建议档位。
- 通过 systemd 常驻服务使用自动温控，或设置固定手动参数。
- 接收 L5 邮件告警，保存本地 JSONL 日志。

## 安装与升级

从 [Releases](https://github.com/shi-YangYang/HRX-FanControl/releases) 下载 v0.1.2 安装包与校验文件，校验通过后安装：

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

APT 会安装 Python、PyYAML、ipmitool 等依赖。**首次安装不会启动或启用服务**，使用服务无需校准文件。

升级时修改 `version`，复用上面的命令。配置、已保存的模式和日志会保留；原先运行中的服务会先停止，更新后恢复运行，原先停止的服务仍停止。若提示处理 `/etc/hrx-fancontrol/config.yaml`，保留当前文件（默认选项 `N`）。

已有源码版服务请先按[迁移步骤](docs/hr650x-guide.md#从源码服务迁移到-deb)处理。GitHub 下载慢时，可在电脑下载后用 SCP 传到服务器。

## 启动与控制

当前源码默认每 30 秒自动采样；已安装版本需同时更新程序和[配置](docs/hr650x-guide.md#配置与日志)。

启动服务前，可先只读查看温度、RPM 和建议档位，以下命令不会改变风扇设置：

```sh
sudo hrx-fancontrol --config /etc/hrx-fancontrol/config.yaml --once
```

启动服务并设置开机自启。首次启动默认自动调控，后续遵守已保存的模式：

```sh
sudo systemctl enable --now hrx-fancontrol
sudo systemctl status hrx-fancontrol
```

服务运行后使用以下指令：

```sh
sudo hrx-fancontrol auto on    # 开启周期采样与自动调控
sudo hrx-fancontrol auto off   # 暂停采样与自动调控，服务继续运行
sudo hrx-fancontrol manual 25  # 固定参数 25，同时关闭自动
sudo hrx-fancontrol status     # 查看模式、缓存读数及数据年龄
```

- **手动参数为十进制整数 1～100**，不代表 RPM 百分比。手动设置确认后停止采样，温控曲线不会覆盖它。
- `auto off` 或手动控制后，服务/服务器重启仍保持自动关闭，且不会重新写入保存的手动值。
- `status` 读取服务缓存，不触发新的硬件采样。正常停止服务保持最后设置，不恢复 BMC 自动模式。
- 检测到已确认的 BMC 设置被外部改变时，服务会让出控制权；需显式执行 `auto on` 或 `manual N` 才恢复控制。

负载下的散热效果仍需在目标服务器观察。BMC 设置确认和可选 RPM 校准详见[控制指南](docs/hr650x-guide.md#校准与实际控制)。

## 邮箱告警

```sh
sudo hrx-fancontrol email setup
```

按提示输入 SMTP 地址、账号和授权码（隐藏输入）。收件人默认是自己；账号不是邮箱地址时，还会询问发件邮箱。加密方式按邮箱服务商要求填写，默认 SSL/465，也支持 STARTTLS/587。保存后开启通知，无需重启服务。

```sh
sudo hrx-fancontrol email test    # 发送测试邮件，请检查收件箱
sudo hrx-fancontrol email status  # 查看配置与开关，不显示授权码
sudo hrx-fancontrol email off     # 关闭邮件告警
sudo hrx-fancontrol email on      # 使用已保存的配置重新开启
```

服务自动采样的建议档位进入 **L5** 时发邮件，同一段持续 L5 只通知一次。有效采样确认降档后，再次进入 L5 会重新告警；服务重启后仍为 L5 时可能再发一次。手动或关闭自动后，不再持续监测并发送告警。详见[邮箱告警](docs/hr650x-guide.md#邮箱告警)。

## 日志与文档

安装后的配置文件为 `/etc/hrx-fancontrol/config.yaml`。JSONL 日志保存在 `/var/log/hrx-fancontrol/`，保留七个 UTC 日期。查看服务事件和邮件发送结果：

```sh
sudo journalctl -u hrx-fancontrol -f
```

- [HR650X 使用指南](docs/hr650x-guide.md)：五档策略、故障处理、配置、校准与源码安装。
- [默认配置](src/hrx_fancontrol/default.yaml) · [配置与日志](docs/hr650x-guide.md#配置与日志)
- [开发说明](docs/development.md)：目录职责、本地检查与打包。

[MIT 许可证](LICENSE) · 版权所有 © 2026 小洋
