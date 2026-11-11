# HRX-FanControl

[English](README.md) · 简体中文

监测 Lenovo ThinkSystem HR650X 的温度和风扇 RPM，取得同机校准后支持自动或受监控的手动调速。**默认命令仍只读，必须显式选择控制选项才会写入。**

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
同一进程内的 IPMI 命令串行执行；上一条结束或超时清理完成后才继续，不会叠加轮询。整表读取超时为 20 秒，BMC 状态读回和调速写入各限时 3 秒。按 Ctrl+C 会取消正在进行的读取并退出。
启动后按第一份有效采样直接给出建议；还没读到数据时显示等待，不预设档位。故障后连续健康采样 5 次才解除保持；温度降档仍需满足 60 秒和 3°C 回差。
短暂读取失败保持原建议；连续失败 5 次或 300 秒没有有效传感器采样时，至少建议 L3，原来已在 L4/L5 则保持。风扇异常也至少建议 L3；温度需求或已有恢复保持更高时取高。有曲线的温度按曲线给档位；无曲线温度的 BMC 告警仍立即建议 L5。
所有来源统一在进入 L5 时输出一条报警，持续 L5 不重复，降档后再次进入会重新报警。BMC 明细保留在诊断日志中，不单独通知。

离线回放提供的快照，不需要 BMC 或 ipmitool：

```sh
.venv/bin/hrx-fancontrol --once --input examples/hr650x-observed-sensors.txt --no-log
```

加 `--json` 输出完整诊断报告，加 `--no-log` 关闭文件日志。文件日志采用精简 JSONL v2，包含中文摘要、温度、RPM、采样总数和必要异常，与 `--json` 内容不同。日志保存在 `logs/`，保留七个 UTC 日期；格式见[日志示例](docs/hr650x-guide.md#日志内容)。

## 开启调速

先在目标 Linux HR650X 上创建新的校准文件。这会实际从 L5 逐档降至 L1，需要数分钟；温度需求可能阻止完成。

```sh
sudo .venv/bin/hrx-fancontrol --calibrate calibration.json
sudo .venv/bin/hrx-fancontrol --auto --profile calibration.json
# 或至少保持 L2，温度策略仍可升档：
sudo .venv/bin/hrx-fancontrol --manual-level 2 --profile calibration.json
```

手动模式设置的是最低档位：设为 L2 时，曲线只需要 L1 就运行 L2，需要 L4 就运行 L4。它持续监测温度，不把风扇锁死在 L2。

校准绑定机器 UUID、BMC 固件、六个风扇、命令字节及实测 RPM。自动/手动控制都要求资料匹配。命令成功返回后，还要核对读回和三次新的稳定 RPM 采样，才标为已验证。历史均值不能代替校准。

**Ctrl+C、SIGTERM 和正常停止服务会保持最后设置，不追加写入。** 不恢复 BMC 自动模式；在写入途中停止时，最终档位可能未知。执行异常统一最多尝试一次 L3 兜底，在限定时间内验证后以失败退出；正常温度曲线仍可使用 L4/L5。前台 SIGKILL 或断电无法执行 Python 清理。

已确认的 BMC 模式或调速参数被改变后，程序停止调速、保留外部设置，不执行兜底，也不靠自动重启重新接管。重启服务器后仍保持暂停。主动恢复时，在控制命令中加 `--resume-control`；服务用法见指南。单纯 RPM 波动或读取失败不算外部修改。

[校准与 systemd 说明](docs/hr650x-guide.md#校准与实际控制) · [服务模板](systemd/hrx-fancontrol.service)。按指南安装并完成校准后，用 `sudo systemctl enable --now hrx-fancontrol` 启动服务并设置开机自启；日常使用 `systemctl start/stop/restart/status hrx-fancontrol` 管理。软件检查与校准不代表满载散热认证。

## 文档

[HR650X 使用指南](docs/hr650x-guide.md) · [默认配置](config/default.yaml) · [源码](src/hrx_fancontrol/)

## CI

[Linux CI](.github/workflows/ci.yml) 在面向 `main` 的 PR 和 `main` 推送（含合并）时运行，使用 Ubuntu 24.04、Python 3.9 / 3.12 检查安装、单元测试和 CLI 离线回放。

[MIT 许可证](LICENSE) · 版权所有 © 2026 小洋
