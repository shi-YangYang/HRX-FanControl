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

默认目标间隔为 15 秒；采集失败后再等待 15 秒重试。
整表读取超时为 20 秒，状态读回单独限时 3 秒。按 Ctrl+C 会取消正在进行的读取并退出。
启动后按第一份有效采样直接给出建议；还没读到数据时显示等待，不预设档位。故障后连续健康采样 5 次才解除保持；温度降档仍需满足 60 秒和 3°C 回差。
短暂读取失败保持原建议；连续失败 5 次或 300 秒没有有效传感器采样时，至少建议 L3，原来已在 L4/L5 则保持。有曲线的温度按曲线给档位；无曲线温度的 BMC 告警和风扇故障仍立即建议 L5。
所有来源统一在进入 L5 时输出一条报警，持续 L5 不重复，降档后再次进入会重新报警。BMC 明细保留在诊断日志中，不单独通知。

离线回放提供的快照，不需要 BMC 或 ipmitool：

```sh
.venv/bin/hrx-fancontrol --once --input examples/hr650x-observed-sensors.txt --no-log
```

加 `--json` 输出完整诊断报告，加 `--no-log` 关闭文件日志。文件日志采用精简 JSONL v2，包含中文摘要、温度、RPM、采样总数和必要异常，与 `--json` 内容不同。日志保存在 `logs/`，保留七个 UTC 日期；格式见[日志示例](docs/hr650x-guide.md#日志内容)。

## 文档

[HR650X 使用指南](docs/hr650x-guide.md) · [默认配置](config/default.yaml) · [源码](src/hrx_fancontrol/)

## CI

[Linux CI](.github/workflows/ci.yml) 在面向 `main` 的 PR 和 `main` 推送（含合并）时运行，使用 Ubuntu 24.04、Python 3.9 / 3.12 检查安装、单元测试和 CLI 离线回放。

[MIT 许可证](LICENSE) · 版权所有 © 2026 小洋
