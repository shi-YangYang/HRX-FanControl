# HRX-FanControl

[English](README.md) · 简体中文

为 **Lenovo ThinkSystem HR650X** 提供温度监测与五档风扇控制。

- 在终端查看各部件温度和六个风扇的实际 RPM。
- 预览建议档位，或通过常驻服务切换自动调控与固定手动参数。
- 保存 JSONL 日志，支持通过 systemd 服务开机运行。

**默认只读。** 启动服务后才会调速，无需校准文件。负载下的散热效果仍需在目标服务器观察。

## 安装

目标环境为 Ubuntu 24.04 和本地 HR650X。发布完成后，从 [Releases](https://github.com/shi-YangYang/HRX-FanControl/releases) 下载 `.deb` 与 `SHA256SUMS`，在下载目录执行（文件名按版本替换）：

```sh
sha256sum --check SHA256SUMS
sudo apt update
sudo apt install ./hrx-fancontrol_0.1.0_all.deb
```

安装会自动补齐 Python、PyYAML、ipmitool 等系统依赖，**不会启动或启用调速服务**。GitHub 慢时可在电脑下载后用 SCP 传到服务器。已有源码版服务先按[迁移步骤](docs/hr650x-guide.md#从源码服务迁移到-deb)处理；[源码安装](docs/hr650x-guide.md#国内服务器安装)仍可使用。

## 查看温度与转速

以下命令只显示读数和建议档位，不改变风扇设置：

```sh
# 读取一次
sudo hrx-fancontrol --config /etc/hrx-fancontrol/config.yaml --once
# 持续采样
sudo hrx-fancontrol --config /etc/hrx-fancontrol/config.yaml
```

默认目标采样间隔为 15 秒，按 **Ctrl+C** 停止。带中文摘要的 JSONL 日志保存在 `/var/log/hrx-fancontrol/`，保留七个 UTC 日期。加 `--json` 查看完整诊断，加 `--no-log` 关闭文件日志。

没有服务器或 ipmitool 时，可以回放随附快照：

```sh
hrx-fancontrol --once --input /usr/share/doc/hrx-fancontrol/examples/hr650x-observed-sensors.txt --no-log
```

## 开启调速

启动已安装的服务并设置开机自启。首次启动默认自动调控，后续遵守已保存的模式：

```sh
sudo systemctl enable --now hrx-fancontrol
sudo systemctl status hrx-fancontrol
```

保持服务运行，在另一个终端发指令：

```sh
sudo hrx-fancontrol auto on    # 开启周期采样与自动调控
sudo hrx-fancontrol auto off   # 关闭自动，服务继续运行
sudo hrx-fancontrol manual 25  # 固定参数 25，同时关闭自动
sudo hrx-fancontrol status     # 查看模式及最近读数
```

- **手动参数为十进制 1～100：** `25` 发送 `0x19`，不代表 RPM 百分比；曲线不会覆盖手动设置。旧的最低档位模式已删除。
- **关闭状态会记住：** 手动或关闭自动后，服务/服务器重启仍保持自动关闭，且不会重新写入保存的手动值。
- **停止与让位：** 正常停止保持最后设置；检测到外部修改后暂停，须显式 `auto on` 或 `manual` 恢复。详见[控制指南](docs/hr650x-guide.md#校准与实际控制)。

写入后立即读取 BMC，手动模式和六个风扇的参数都匹配即显示“设置已确认”；不等待 RPM 稳定。回读失败时有界重查，手动设置确认后停止采样。可选的 [RPM 校准测量](docs/hr650x-guide.md#校准与实际控制)不影响服务使用。

## 文档

- [HR650X 使用指南](docs/hr650x-guide.md)：五档策略、BMC 阈值、故障处理、校准与部署。
- [配置与日志](docs/hr650x-guide.md#配置与日志) · [默认配置](config/default.yaml)
- [源码](src/hrx_fancontrol/) · [服务模板](systemd/hrx-fancontrol.service)

[Linux CI](.github/workflows/ci.yml) 使用 Ubuntu 24.04、Python 3.9 / 3.12 检查安装、单元测试和 CLI 离线回放，在面向 `main` 的 PR 时运行。

[Release CD](.github/workflows/release.yml) 在网页发布 Release 后测试并上传 `.deb` 与校验文件。[发布与升级步骤](docs/hr650x-guide.md#发布版本与升级)说明版本号、附件和配置保留。

[MIT 许可证](LICENSE) · 版权所有 © 2026 小洋
