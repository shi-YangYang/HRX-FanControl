# HRX-FanControl

[English](README.md) · 简体中文

读取 Lenovo ThinkSystem HR650X 的温度和风扇 RPM，并模拟建议档位。**当前版本只读，不会改变风扇转速。**

## 快速开始

目标环境：Ubuntu 24.04、Python 3.9+。原型位于 `dev` 分支。

```sh
sudo apt update
sudo apt install -y git ipmitool python3-venv
git clone --branch dev https://github.com/shi-YangYang/HRX-FanControl.git
cd HRX-FanControl
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
```

服务器访问 GitHub 慢时，可以通过 SSH [传包安装](docs/hr650x-guide.md#国内服务器安装)。

## 使用

在服务器上读取一次，或持续采样；需要本地 BMC 读取权限：

```sh
sudo .venv/bin/hrx-fancontrol --once
sudo .venv/bin/hrx-fancontrol
```

默认每 10 秒采样一次，按 Ctrl+C 退出。
首次显示的 L5 是启动保持，需要连续健康采样 60 秒后才允许降低建议档位。

离线回放提供的快照，不需要 BMC 或 ipmitool：

```sh
.venv/bin/hrx-fancontrol --once --input examples/hr650x-observed-sensors.txt --no-log
```

加 `--json` 输出 JSON，加 `--no-log` 关闭文件日志。日志保存在 `logs/`，保留七个 UTC 日期。

## 文档

[HR650X 使用指南](docs/hr650x-guide.md) · [默认配置](config/default.yaml) · [源码](src/hrx_fancontrol/)

## CI

[Linux CI](.github/workflows/ci.yml) 在面向 `main` 的 PR 和 `main` 推送（含合并）时运行，使用 Ubuntu 24.04、Python 3.9 / 3.12 检查安装、单元测试和 CLI 离线回放。

[MIT 许可证](LICENSE) · 版权所有 © 2026 小洋
