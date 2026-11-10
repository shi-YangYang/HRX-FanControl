# HR650X 使用指南

[返回首页](../README.zh-CN.md)

当前版本只读采集并模拟档位。安装与基本运行见首页；配置和运行命令从项目根目录执行。

## 国内服务器安装

在 Mac 的 `dev` 分支项目目录中打包，替换下面的 SSH 登录地址：

```sh
mkdir -p dist
git archive --format=tar.gz --prefix=HRX-FanControl/ -o dist/hrx-fancontrol-source.tar.gz HEAD
scp dist/hrx-fancontrol-source.tar.gz user@SERVER_IP:~/
```

压缩包只包含已提交的文件，不包含 Mac 的 `.venv`。服务器首次安装时，在 SSH 窗口执行：

```sh
sudo apt update
sudo apt install -y ipmitool python3-venv
tar -xzf ~/hrx-fancontrol-source.tar.gz -C ~
cd ~/HRX-FanControl
python3 -m venv .venv
.venv/bin/python -m pip install -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple --upgrade pip
.venv/bin/python -m pip install -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple .
sudo .venv/bin/hrx-fancontrol --once
```

服务器无需连接 GitHub；apt 和 Python 依赖仍需联网。这里的 pip 命令临时使用[清华镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)，不会更改全局配置。持续运行时去掉 `--once`。

## 五档与回差

每个传感器独立计算，取其中最高需求作为组件档位。表中为进入各档的温度（°C），包含边界。
**15/20/30/45/60% 是策略标签，尚未校准为 BMC 百分比或 RPM 比例。**

| 组件 | L1：15% | L2：20% | L3：30% | L4：45% | L5：60% |
|---|---|---|---|---|---|
| CPU1 / CPU2 | <56 | 56 | 65 | 75 | 80 |
| PCH | <65 | 65 | 70 | 75 | 80 |
| DIMM | <46 | 46 | 50 | 60 | 70 |
| CPU VRM | <61 | 61 | 70 | 85 | 100 |
| NIC | <56 | 56 | 60 | 70 | 85 |
| PSU | <46 | 46 | 50 | 55 | 60 |
| Outlet | <47 | 47 | 52 | 58 | 65 |

升档立即生效，可跨级。降档需要温度 **≤ 当前档位进入阈值 − 3°C，持续 60 秒**，每次降一档。

- PCH 到 **65°C** 升 L2；降回 L1 要求 **≤62°C 连续 60 秒**。
- 因此初始 63°C 可以是 L1，但升过 L2 后回到 63°C，仍保持 L2。
- Outlet 到 47°C 升 L2，≤44°C 连续 60 秒可降回 L1。
- 超过回落边界、读数无效或采样间断超过 30 秒，会重置降档计时；继续降下一档需重新计时。

默认启动和故障恢复期间保持 L5，连续健康采样 60 秒才允许降低，所以 `--once` 会看到启动 L5。
关键传感器缺失、风扇停转或采集失败时不降低建议；达到软件 L5 温度或出现 BMC 温度告警时，立即建议 L5。

当前低负载快照（Inlet 26°C、PCH 63°C、Outlet 44°C）的组件需求为 L1；满载散热还需实机验证。

## BMC 预警值

以下来自 [HR650X 原始快照](../examples/hr650x-observed-sensors.txt)，单位为 °C。
UNC / UCR / UNR 分别为上限非临界、临界、不可恢复告警；NA 表示未报告。
**这些是 BMC 报告值，与上面的软件档位门槛分开；程序不会修改它们。**

| 传感器 | 快照读数 | UNC | UCR | UNR |
|---|---:|---:|---:|---:|
| Inlet_Temp | 26 | 42 | 45 | 46 |
| Outlet_Temp | 44 | 70 | 85 | 100 |
| CPU1_Temp / CPU2_Temp | 43 / 40 | 86 | 93 | 98 |
| PCH_Temp | 63 | 83 | 85 | 105 |
| DIMMG1_Temp / DIMMG2_Temp | 37 / 37 | 81 | 82 | 100 |
| CPU1_VR_Temp / CPU2_VR_Temp | 39 / 38 | 112 | 123 | 133 |
| NIC_Temp | 47 | 100 | 105 | 115 |
| PSU1_Temp | 34 | 62 | 65 | 68 |
| PSU2_Temp | 0（无效） | 62 | 65 | 68 |
| RAID_Temp | 0（无效） | 100 | 105 | 115 |
| NVME_Temp | NA | 70 | 75 | 78 |
| CPU1_DTS / CPU2_DTS | 45 / 48（语义未确认） | NA | NA | NA |

风扇的 BMC 下限预警为 690 RPM、下限临界为 575 RPM。软件另用 `fan.minimum_rpm: 1` 检测停转，并检查 BMC 告警状态。
Inlet 下限预警为 7°C，NIC 下限三列均为 0°C；这些数值下限保留在原始快照中，未用作独立软件门槛。

## 配置与日志

[config/default.yaml](../config/default.yaml) 链接到包内默认配置。自定义文件只需写要覆盖的字段，例如保存为 `profile.yaml`：

```yaml
logging:
  directory: /var/log/hrx-fancontrol
ipmi:
  command_timeout_seconds: 5
curves:
  pch: [65, 70, 75, 80]
```

```sh
sudo .venv/bin/hrx-fancontrol --config profile.yaml
```

默认命令超时为 3 秒；实际采集较慢时可按上例调整。配置错误会阻止启动，`controller.enabled` 必须保持 `false`。
相对路径按当前工作目录解析。默认日志为 `logs/hrx-YYYY-MM-DD.jsonl`，按 UTC 日期保留今天及前六天；每 60 秒汇总一次，档位或告警变化立即记录。
加 `--no-log` 关闭日志，`--json` 输出逐行 JSON。程序不安装开机服务，Ctrl+C 即可停止。

退出码：0 表示采样正常，1 表示配置、文件或日志错误，2 表示采集故障或紧急告警。

## 传感器说明

- `FAN1_R_Speed` 至 `FAN6_R_Speed` 是当前六个必需转速传感器；任意一个缺失或变成 NA 都会报告故障。
- `FAN*_F_Speed` 和 `FAN7_*_Speed` 当前为 NA，不列为必需风扇；离散 `FAN*_Status` 状态位尚未解码。
- RAID/PSU2 的 0°C 和 NVME 的 NA 按无效读数处理，不单独触发故障。Inlet、有效的 RAID/NVME 温度仅监测。
- DTS 的含义尚未确认，仅展示原值，不当作 CPU 绝对温度。新增温度传感器需在配置中匹配名称。

`examples/hr650x-observed-sensors.txt` 是用户提供的 163 行实机快照；`examples/hr650x-sensors.txt` 是合成示例。离线输入必须同时使用 `--once`。

## 编码与 RPM

现有机器记录为 ipmitool 1.8.19、BMC 11.99。状态读回为 `20 0f 0f 0f 0f 0f 0f 0f 0f`，随后六风扇读数为 3680、3680、3680、3680、3450、3335 RPM，平均约 3584 RPM。
首字节含义及八个返回槽位与六风扇的对应关系尚未确认。

下表是用户历史观察；未记录的读回不补成推算值：

| 当时输入的末尾参数 | 当时报告的读回 | 约平均 RPM |
|---|---|---:|
| `60` | `0x3c` | 10500 |
| `50` | `0x32` | 8976 |
| `45` | 未记录 | 8218 |
| `40` | 未记录 | 7457 |
| `35` | 未记录 | 6689 |
| `30` | 未记录 | 5827 |
| `20` | 未记录 | 4406 |
| `15` | 未记录 | 3584 |

ipmitool 1.8.19 的上游解析中，裸参数 `15` 对应字节 `0x0f`，`0x15` 则是十进制 21；`60` 对应 `0x3c`。
这说明输入的进制规则，BMC 如何解释字节、RPM 如何响应仍需校准。程序保持 `calibration.status: unverified`，只分别报告策略标签、原始读回和实测 RPM。

下一步需在同一机器记录完整命令、固件版本、时间戳、读回、逐风扇 RPM 与温度/负载，并确认稳定时间和可用的恢复方式，再进入手动控制阶段。

参考：[ipmitool 1.8.19 参数解析](https://github.com/ipmitool/ipmitool/blob/IPMITOOL_1_8_19/lib/helper.c)、[HR630X 社区实操](https://blog.lyc8503.net/post/19-first-rack-server-hr630x/)（不同机型，仅作参考）。
