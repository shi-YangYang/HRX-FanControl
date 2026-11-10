# 技术栈与工程事实

## 技术栈

| 项目 | 当前实现 |
|---|---|
| 语言 / Runtime | Python，包声明 `>=3.9` |
| 包管理与构建 | `pip`、`venv`、`pyproject.toml`；构建后端 `setuptools.build_meta`，要求 `setuptools>=61` |
| 运行依赖 | `PyYAML>=6.0,<7` |
| CLI | `argparse`；安装入口 `hrx-fancontrol = hrx_fancontrol.main:main` |
| 测试 | Python 标准库 `unittest`，无额外测试依赖 |
| 数据存储 | 本地按 UTC 日期滚动的 JSONL，无数据库、无云日志 |
| 硬件接口 | 本地 OpenIPMI，经 `subprocess` 执行固定只读 `ipmitool` 命令 |
| 当前版本 | `0.1.0`；版本定义见 `pyproject.toml` 和包 `__init__.py`，不表示已有正式 Release |

目标服务器环境为 Ubuntu 24.04 / HR650X；用户快照报告 ipmitool 1.8.19、BMC 11.99、IPMI 2.0。
已进行的本地 Python 安装/离线验证不能替代服务器上的安装、持续采集和硬件验证。

## 项目目录与职责

保留既有 Python `src` 布局，不迁移源码、测试、配置或示例。

| 位置 | 职责 |
|---|---|
| `src/hrx_fancontrol/` | 唯一业务包与包内默认 YAML |
| `config/default.yaml` | 指向 `../src/hrx_fancontrol/default.yaml` 的符号链接，避免重复默认值 |
| `examples/` | 合成示例及用户提供的 HR650X 原始传感器快照 |
| `tests/` | 原型行为和 HR650X 观察数据回归测试 |
| `README.md` / `README.zh-CN.md` | 对等的英文 / 中文项目入口和快速开始 |
| `docs/hr650x-guide.md` | 详细配置、架构职责、实测依据、BMC 阈值及校准门槛 |
| `constitution/` | 稳定使命、阶段路线与实际技术栈 |
| `specs/` | 任务规格、实施计划、独立验收与模板 |
| `.ai/decisions/` | 重要决策及后续影响 |
| `.ai/rules/` | 项目专用约束 |
| `.ai/workflows/` | 实施流程补充 |
| `.ai/prompts/` | 当前任务交接；不替代 Spec |
| `.venv/`、`logs/`、`dist/` | 本地环境、运行数据和构建产物；均不提交 |

## 模块边界

- `main.py`：CLI 参数、单次/循环采集编排、文本/JSON 报告和退出状态。
- `ipmi.py`：只读命令白名单、超时、进程结果、九字节原始状态解析。
- `sensors.py` / `fan_reader.py`：温度/阈值解析、有效性与已安装风扇检查。
- `config.py` / `default.yaml`：包内默认值、YAML 覆盖合并与约束检查。
- `thermal_engine.py` / `hysteresis.py` / `safety.py`：独立传感器需求、回差、最高需求与安全保持。
- `logger.py`：有界异步日志队列、UTC 日志和七天保留。

策略层不导入 IPMI；采集层不接收策略目标。当前不存在执行器。

## 命令

以下命令均从仓库根目录执行。开发环境可执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
.venv/bin/hrx-fancontrol --once --input examples/hr650x-observed-sensors.txt --no-log
```

服务器安装和权限说明见 [中文 README](../README.zh-CN.md)。
本机硬件访问仅允许 `ipmitool -I open sensor` 和 `ipmitool -I open raw 0x2e 0x31`。

检查命令按改动范围选择，不要求每次全部运行：

```sh
# 原型子系统
PYTHONPATH=tests .venv/bin/python -m unittest test_prototype -v
# HR650X 快照与回差适配
PYTHONPATH=tests .venv/bin/python -m unittest test_hr650x_observation -v
# 仅在改动范围确实覆盖全部原型时
.venv/bin/python -m unittest discover -s tests -v
```

仓库尚未配置 CI、Lint 或 Format 工具，不得声称这些检查已通过。
构建元数据已存在；当前没有额外构建脚本、发布流水线、Docker 或 systemd 单元。
必要时可用 `.venv/bin/python -m pip wheel --no-deps . --wheel-dir dist` 构建 wheel；文档调整不触发构建。

## Git

远端为 `origin`（`git@github.com:shi-YangYang/HRX-FanControl.git`），开发分支为 `dev`，主分支为 `main`。
默认通过 `dev → main` PR 集成；commit、push、PR、合并和同步分别遵守根目录 [AGENTS.md](../AGENTS.md) 的授权边界。
