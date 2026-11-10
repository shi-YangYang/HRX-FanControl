# HRX-FanControl

[English](README.md) · 简体中文

面向 Lenovo ThinkSystem HR650X 的只读温度监测与风扇档位模拟工具。

**0.1.0 版本是 Read-only / Dry-run 原型，建议值不会应用到硬件。**
IPMI 参数编码、BMC 读回与实际 RPM 响应的校准尚未完成，当前没有自动风扇控制功能。

## 功能

- 通过本地 OpenIPMI 读取温度和六个已配置风扇的转速。
- 根据各传感器曲线、回差和启动/故障恢复保持，计算五档建议。
- 分别展示传感器故障、BMC 温度告警、原始状态字节和实测 RPM。
- 提供文本、JSON 和本地 JSONL 日志，保留七个 UTC 日期；可离线回放传感器快照。

当前仅以 HR650X 为目标。HR630X 社区记录仅作参考，不代表已支持该型号。

## 快速开始

目标服务器环境：Ubuntu 24.04、Python 3.9+、本地 BMC。现有 HR650X 快照报告 BMC 11.99 和 ipmitool 1.8.19；服务器上的持续运行仍需验证。

```sh
sudo apt update
sudo apt install -y git ipmitool python3-venv
git clone --branch dev https://github.com/shi-YangYang/HRX-FanControl.git
cd HRX-FanControl
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
```

原型位于 `dev` 分支。离线回放只需要 Python 环境和包依赖，不需要 ipmitool 或 BMC。

## 使用

回放提供的 HR650X 传感器快照，不访问硬件：

```sh
.venv/bin/hrx-fancontrol --once --input examples/hr650x-observed-sensors.txt --no-log
```

在服务器上读取一次实时快照，或持续采样：

```sh
sudo .venv/bin/hrx-fancontrol --once
sudo .venv/bin/hrx-fancontrol
```

实机采集需要本地 BMC 读取权限。默认每 10 秒采样一次，按 Ctrl+C 退出。
启动和故障恢复后，需要连续健康采样 60 秒才允许降低建议档位。因此默认配置下，`--once` 即使看到组件需求 L1，也会给出启动保持 L5。它只是模拟建议，不会改变风扇转速。

| 参数 | 用途 |
|---|---|
| `--json` | 每行输出一条 JSON 快照 |
| `--no-log` | 禁用本地文件日志 |
| `--input PATH` | 回放传感器文本，必须同时使用 `--once` |
| `--config PATH` | 在包内默认配置上应用 YAML 覆盖 |

日志默认写入当前工作目录下的 `logs/`。程序不安装 systemd 服务或开机启动项。

## 配置

[config/default.yaml](config/default.yaml) 链接到唯一的包内默认配置。可以直接使用，也可以提供自己的 YAML 覆盖：

```sh
sudo .venv/bin/hrx-fancontrol --config config/default.yaml
```

五档策略标签为 **15 / 20 / 30 / 45 / 60%**，不是已验证的 BMC 百分比或 RPM 比例。
PCH 在 65°C 进入 L2，从 L2 回到 L1 需要 ≤62°C 连续 60 秒。校准状态保持 `unverified`，修改 YAML 无法启用硬件控制。

## 项目目录

| 路径 | 职责 |
|---|---|
| `src/hrx_fancontrol/` | Python 包与包内默认配置 |
| `config/` | 默认配置链接 |
| `examples/` | 分开保存的合成数据与 HR650X 实机传感器快照 |
| `tests/` | 原型与 HR650X 快照回归测试 |
| `docs/` | 详细使用与校准指南 |
| `constitution/` | 项目使命、路线与技术事实 |
| `specs/` | 规格、计划、验收记录和模板 |
| `.ai/` | 项目规则、决策、工作流与任务交接 |
| `AGENTS.md` | Agent 开发与 Git 规范 |

## 文档

- [HR650X 指南（中文）](docs/hr650x-guide.md)：五档曲线、回差、BMC 阈值、日志、实测数据与校准门槛。
- [技术栈与模块职责（中文）](constitution/tech-stack.md)。
- [项目路线（中文）](constitution/roadmap.md)。

## 开发

在 `dev` 开发，通过 `dev → main` PR 集成，由维护者处理 PR。提交、推送、PR、合并与同步分别遵守 [AGENTS.md](AGENTS.md) 的授权边界。

开发时可使用可编辑安装，并按需执行 HR650X 定向回归检查：

```sh
.venv/bin/python -m pip install -e .
PYTHONPATH=tests .venv/bin/python -m unittest test_hr650x_observation -v
```

按改动范围选择检查，纯文档调整只需审阅 diff。测试和离线回放不能证明实际调速响应或满载散热安全。其他测试范围见 [技术说明](constitution/tech-stack.md#命令)。

## 许可证

[MIT](LICENSE)，版权所有 © 2026 小洋。
