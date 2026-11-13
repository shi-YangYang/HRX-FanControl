# 开发说明

[返回首页](../README.zh-CN.md) · [安装与使用](hr650x-guide.md)

## 目录

| 位置 | 内容 |
|---|---|
| `src/hrx_fancontrol/` | Python 业务包；`default.yaml` 是唯一默认配置 |
| `tests/` | 离线行为、打包和安装生命周期测试 |
| `examples/` | 合成示例与 HR650X 原始传感器快照 |
| `packaging/debian/` | Debian 安装、升级和卸载脚本 |
| `packaging/systemd/` | 源码安装服务模板；deb 构建时转换为系统安装路径 |
| `scripts/` | deb 构建、Release 校验上传、Linux 安装检查 |
| `docs/` | 使用指南和开发说明；双语 README 提供快速开始 |
| `.github/workflows/` | PR 检查和 Release 构建发布 |
| `constitution/`、`specs/`、`.ai/` | 项目约束、任务规格与决策记录 |

`.venv/`、`logs/`、`dist/` 分别存放本地环境、运行日志和构建产物，不提交到仓库。

业务代码统一放在 `src/hrx_fancontrol/`。包根保留 CLI 入口、配置加载与默认 YAML，内部按职责分组：

| 子目录 | 内容 |
|---|---|
| `hardware/` | IPMI、协议、身份检查、传感器解析与采集 |
| `policy/` | 温控需求、回差与故障策略 |
| `control/` | 调速执行、服务指令和状态、生命周期、可选校准 |
| `reporting/` | 指令输出、日志写入与记录转换 |

## 本地开发

需要 Python 3.9 或更新版本，命令从仓库根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/hrx-fancontrol --once --input examples/hr650x-observed-sensors.txt --no-log
```

快照回放不需要服务器或 ipmitool。测试按改动选择对应模块，例如打包相关检查：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests \
  .venv/bin/python -m unittest test_packaging test_debian_lifecycle -v
```

这些检查只操作临时目录和模拟服务命令。缺少 `deb-systemd-helper` 的平台会跳过依赖它的用例；完整安装生命周期由 Linux CI 检查。

## Linux 打包

在 Linux 仓库检出目录安装 `dpkg-deb` 后执行：

```sh
python3 scripts/build_deb.py
```

输出为 `dist/hrx-fancontrol_X.Y.Z_all.deb` 和 `dist/SHA256SUMS`，版本取自源码。可用 `--output` 指定输出目录，`--tag vX.Y.Z` 校验版本一致性。构建只生成文件，不安装或启用服务。

源码传输打包命令见[源码安装](hr650x-guide.md#国内服务器安装)。

## 发布版本

先在 `dev` 将 `pyproject.toml` 的 `version` 和 `src/hrx_fancontrol/__init__.py` 的 `__version__` 改为同一个 `X.Y.Z`，创建 PR，等待 Linux CI 成功后再合入 `main`。

1. 在 GitHub 仓库打开 **Releases → Draft a new release**。
2. 新建标签，例如 `v0.1.0`，Target 选 `main`，填写发布说明，然后 **Publish release**。
3. 在 Actions 等待 **Release Debian package** 成功。它测试标签对应代码、构建并检查 Linux 安装/升级，再把 `.deb` 与 `SHA256SUMS` 加到这条 Release。只保存草稿不会触发。

标签必须是 `vX.Y.Z`，与源码两处版本一致，提交须已进入 `main`。仓库需关闭 **immutable releases**，因为附件在发布后才生成。构建失败时查看 Actions 日志；需要改代码时发布新版本。相同源码的失败任务可以重跑；内容相同的已有附件会跳过，内容不同则拒绝覆盖。

[Linux CI](../.github/workflows/ci.yml) 在 Ubuntu 24.04 使用 Python 3.9 / 3.12 检查安装、单元测试和离线回放。安装生命周期检查运行于临时 runner，以测试进程替代硬件服务，覆盖首装、运行/停止及启用/禁用四种升级状态、卸载与配置保留。[Release 工作流](../.github/workflows/release.yml) 复用构建和安装检查；这些检查不访问 IPMI，不代表实机控制验证。
