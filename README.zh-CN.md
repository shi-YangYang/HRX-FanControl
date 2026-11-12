# HRX-FanControl

[English](README.md) · 简体中文

为 **Lenovo ThinkSystem HR650X** 提供温度监测与五档风扇控制。

- 在终端查看各部件温度和六个风扇的实际 RPM。
- 预览建议档位，或通过常驻服务切换自动调控与固定手动参数。
- 保存 JSONL 日志，支持通过 systemd 服务开机运行。

**默认只读。** 调速需要显式开启，并先在目标机器上完成校准。硬件校准、实际调速响应和负载下的散热效果仍待实机验证。

## 安装

目标环境为 Ubuntu 24.04、Python 3.9+，需要本地 BMC 访问权限。以下命令安装 `dev` 分支：

```sh
sudo apt update
sudo apt install -y git ipmitool python3-venv
git clone --branch dev https://github.com/shi-YangYang/HRX-FanControl.git
cd HRX-FanControl
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
```

服务器访问 GitHub 慢时，可以改用 SSH [传包安装](docs/hr650x-guide.md#国内服务器安装)。详细指南以中文提供。

## 查看温度与转速

以下命令只显示读数和建议档位，不改变风扇设置：

```sh
# 读取一次
sudo .venv/bin/hrx-fancontrol --once
# 持续采样
sudo .venv/bin/hrx-fancontrol
```

默认目标采样间隔为 15 秒，按 **Ctrl+C** 停止。带中文摘要的 JSONL 日志保存在 `logs/`，保留七个 UTC 日期。加 `--json` 查看完整诊断，加 `--no-log` 关闭文件日志。

没有服务器或 ipmitool 时，可以回放随附快照：

```sh
.venv/bin/hrx-fancontrol --once --input examples/hr650x-observed-sensors.txt --no-log
```

## 开启调速

先在目标 HR650X 上校准。**这会实际将风扇从 L5 逐档调至 L1**，需要数分钟；温度需求可能中止校准。校准文件名必须尚不存在。

```sh
sudo .venv/bin/hrx-fancontrol --calibrate calibration.json
```

校准成功后，按 [systemd 部署指南](docs/hr650x-guide.md#systemd-服务)安装服务；也可在前台启动：

```sh
sudo .venv/bin/hrx-fancontrol --serve --profile calibration.json
```

保持服务运行，在另一个终端发指令：

```sh
sudo .venv/bin/hrx-fancontrol auto on    # 开启周期采样与自动调控
sudo .venv/bin/hrx-fancontrol auto off   # 关闭自动，服务继续运行
sudo .venv/bin/hrx-fancontrol manual 25  # 固定参数 25，同时关闭自动
sudo .venv/bin/hrx-fancontrol status     # 查看模式及最近读数
```

- **手动参数为十进制 1～100：** `25` 发送 `0x19`，不代表 RPM 百分比；曲线不会覆盖手动设置。旧的最低档位模式已删除。
- **关闭状态会记住：** 手动或关闭自动后，服务/服务器重启仍保持自动关闭，且不会重新写入保存的手动值。
- **停止与让位：** 正常停止保持最后设置；检测到外部修改后暂停，须显式 `auto on` 或 `manual` 恢复。详见[控制指南](docs/hr650x-guide.md#校准与实际控制)。

手动写入后仅进行有时限的验证读取；命令返回不等于 RPM 已验证。`status` 显示确认进度和缓存时间，非五档参数不会冒充已校准 RPM。

## 文档

- [HR650X 使用指南](docs/hr650x-guide.md)：五档策略、BMC 阈值、故障处理、校准与部署。
- [配置与日志](docs/hr650x-guide.md#配置与日志) · [默认配置](config/default.yaml)
- [源码](src/hrx_fancontrol/) · [服务模板](systemd/hrx-fancontrol.service)

[Linux CI](.github/workflows/ci.yml) 使用 Ubuntu 24.04、Python 3.9 / 3.12 检查安装、单元测试和 CLI 离线回放，在面向 `main` 的 PR 和 `main` 推送时运行。

[MIT 许可证](LICENSE) · 版权所有 © 2026 小洋
