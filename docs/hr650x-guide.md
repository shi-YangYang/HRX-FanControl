# HR650X 使用指南

[返回首页](../README.zh-CN.md)

默认只读采集并给出建议，显式启动服务后可开启实际调速，无需校准文件。安装与基本运行见首页；配置和运行命令从项目根目录执行。

## 国内服务器安装

在 Mac 的 `dev` 分支项目目录中打包，替换下面的 SSH 登录地址：

```sh
mkdir -p dist
COPYFILE_DISABLE=1 tar -czf dist/hrx-fancontrol-source.tar.gz \
  --exclude='__pycache__' --exclude='*.pyc' \
  README.md README.zh-CN.md LICENSE pyproject.toml src config examples docs systemd
scp dist/hrx-fancontrol-source.tar.gz user@SERVER_IP:~/
```

按上面的明确文件列表打包当前工作区源码，包含尚未提交的实现；不包含 Mac 的 `.venv`、日志、机器身份或校准文件。不要将个人资料放进这些源码目录。服务器首次安装时，在 SSH 窗口执行：

```sh
sudo apt update
sudo apt install -y ipmitool python3-venv
mkdir -p ~/HRX-FanControl
tar -xzf ~/hrx-fancontrol-source.tar.gz -C ~/HRX-FanControl
cd ~/HRX-FanControl
python3 -m venv .venv
.venv/bin/python -m pip install -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple --upgrade pip
.venv/bin/python -m pip install -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple .
sudo .venv/bin/hrx-fancontrol --once
```

服务器无需连接 GitHub；apt 和 Python 依赖仍需联网。这里的 pip 命令临时使用[清华镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)，不会更改全局配置。持续运行时去掉 `--once`。

更新时，先在采集窗口按 Ctrl+C，传入新的源码包，再解压并重新安装：

```sh
mkdir -p ~/HRX-FanControl
tar -xzf ~/hrx-fancontrol-source.tar.gz -C ~/HRX-FanControl
cd ~/HRX-FanControl
.venv/bin/python -m pip install -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple .
sudo .venv/bin/hrx-fancontrol --config hr650x-observe.yaml --once
```

更新复用已有虚拟环境，首次安装仍按前面的步骤。源码包应排除 `.venv`、运行日志和自定义配置；现有仅设置 `command_timeout_seconds: 20` 的观察配置可以继续使用。上面的打包命令读取实际工作区源码，不依赖 HEAD。

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

启动后等待第一份有效采样，不预设档位。第一次读到正常低温就建议 L1，高温则直接按对应档位建议；`--once` 也一样。最初几次读取失败会继续显示等待，达到下面的失败阈值才建议保护档位。
短暂通信失败保持原建议；**连续失败 ≥5 次，或距离上次有效传感器采样 ≥300 秒**，任一满足就至少建议 L3。原来已在 L4/L5 时不会降到 L3。
故障后需 **连续 5 次完整、新鲜、无通信或硬件异常的采样** 才解除保持，显示为 `Recovery: 1/5` 到 `4/5`，第 5 次解除。中途失败、异常或采样间隔超过 30 秒会重新计数；间断后当前健康样本算新的第 1 次。解除保持不会跳过上面的温度降档回差。
一次完整有效读取清零连续读取失败次数；取消不计入成功或失败，同一轮两条命令失败只计一次。只有状态读回失败时，仍用本轮有效温度判断升档，但不计作健康恢复；状态读回成功不能刷新温度采样时间。最初一直没有数值建议、也未触发保护时，第一份有效采样直接决定档位，不额外等待 5 次。
完整表格中的关键温度传感器缺失仍立即建议 L5。六个必需风扇中任意一个停转、缺失、NA、低于 RPM 下限或报警，至少建议 L3；温度需求或已有恢复保持为 L4/L5 时取高。整表读取失败不等于六个风扇停转。通信失效期间不使用旧读数，也不累计降档时间。L3 是暂定的模拟降级下限，其散热效果尚未校准验证。

当前低负载快照（Inlet 26°C、PCH 63°C、Outlet 44°C）的组件需求为 L1；满载散热还需实机验证。

## BMC 预警值

以下来自 [HR650X 原始快照](../examples/hr650x-observed-sensors.txt)，单位为 °C。
UNC / UCR / UNR 分别为上限非临界、临界、不可恢复告警；NA 表示未报告。
**这些是本机 BMC 报告值，不是硬件损坏温度；程序不会修改它们。** 运行时采用每轮实际返回的阈值和告警状态，不把这张快照表写死进配置。

有曲线的 CPU、PCH、DIMM、VRM、NIC、PSU、Outlet 只按曲线给档位；BMC 状态保留在诊断记录中，不单独通知，也不额外升档或启动恢复保持。即使 BMC 阈值低于曲线门槛，档位也由曲线决定。

无曲线的 Inlet、NVME、RAID、DTS 等出现 BMC 告警时，仍建议 L5，并需连续 5 次健康采样解除保持。正在恢复其他故障时，有曲线传感器的 BMC 告警也会重置健康次数。BMC 没有提供数值阈值时不补造温度，仍识别其告警状态。

不论哪条曲线或哪项故障导致 L5，终端统一输出一次“警告：当前建议档位已达到 L5。”。持续 L5 时不因原因变化重复通知，降档后再次进入 L5 才重新报警。例如 PCH 80°C 进入 L5 就报警，83°C 出现 BMC 告警不会再报一条。

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
  command_timeout_seconds: 20
  status_timeout_seconds: 3
safety:
  recovery_success_count: 5
  read_failure_count: 5
  read_failure_timeout_seconds: 300
  read_failure_level: 3
curves:
  pch: [65, 70, 75, 80]
```

```sh
sudo .venv/bin/hrx-fancontrol --config profile.yaml
```

默认目标间隔为 15 秒。同一调速进程内，整表读取、BMC 状态读回和调速命令串行执行；当前命令未结束时，不启动下一条或下一轮，也不积压补发轮次。整表读取限时 20 秒，BMC 状态读回及调速写入各限时 3 秒。超时后先终止并回收该命令，再继续；失败的部分输出不用于温度或 RPM 判断。采集失败后从本轮结束起再等待 15 秒，温度或风扇告警不延缓正常采样。用户另开的只读进程或外部工具不受这个串行循环管理。
样本过期检查仍为 30 秒；各命令超时及启用状态读回时的总预算都必须小于这个值。配置错误会阻止启动，`controller.enabled` 必须保持 `false`。
`recovery_success_count` 和 `read_failure_count` 为正整数；`read_failure_level` 为 1～5 的整数，`read_failure_timeout_seconds` 为正的有限秒数。旧配置中的 `startup_level` 和 `recovery_delay_seconds` 已移除，升级前请删除；恢复次数使用 `recovery_success_count`，默认 5。阈值按每轮完成时判定；程序暂停期间无法准点发出建议，恢复后的首次采样也会检查中断时长。
`emergency_celsius` 默认均为 `null`。旧配置中的非空值仍是用户自定强制紧急线，会触发 L5 和恢复保持；设为 `null` 才恢复上述默认行为。

### 日志内容

相对路径按当前工作目录解析。默认日志为 `logs/hrx-YYYY-MM-DD.jsonl`，按 UTC 日期保留今天及前六天；每 60 秒汇总一次，档位、告警或恢复进度变化立即记录。每行一个 JSON 对象，新记录带 `schema_version: 2`；已有记录不改写，同一天可同时包含旧版与 v2。

文件日志保留中文 `message`、建议 `level`、有效温度 `temperatures_c`、风扇 `fans_rpm` 和采样信息 `sampling`。`level` 只是建议，只读模式下 `applied: false` 表示未调速。实际控制中 `applied: false` 表示设置尚未通过 BMC 回读确认，命令可能已经发送；以 `control.status` 区分等待、失败与已验证。`source` 区分服务器本机采集 `local-openipmi` 与离线回放 `offline-snapshot`。无效或缺失值为 `null`，不会把 NA 或未安装硬件的零值当作正常温度；DTS 语义未确认，不列入绝对温度表。

以下为随附实机快照的**离线回放示例**，为阅读方便展开；文件中每条记录占一行：

```json
{
  "schema_version": 2,
  "time": "2026-11-11T08:54:51+00:00",
  "event": "transition",
  "run_id": "053036adfd5641bea20a08c48492b574",
  "poll_id": 1,
  "source": "offline-snapshot",
  "mode": "dry-run",
  "applied": false,
  "message": "采样完成，按温度策略建议 L1。",
  "level": "L1",
  "temperatures_c": {
    "Inlet_Temp": 26.0, "Outlet_Temp": 44.0,
    "CPU1_Temp": 43.0, "CPU2_Temp": 40.0, "PCH_Temp": 63.0,
    "DIMMG1_Temp": 37.0, "DIMMG2_Temp": 37.0,
    "CPU1_VR_Temp": 39.0, "CPU2_VR_Temp": 38.0, "NIC_Temp": 47.0,
    "PSU1_Temp": 34.0, "NVME_Temp": null, "RAID_Temp": null, "PSU2_Temp": null
  },
  "fans_rpm": {
    "FAN1_R_Speed": 3680.0, "FAN2_R_Speed": 3680.0,
    "FAN3_R_Speed": 3680.0, "FAN4_R_Speed": 3680.0,
    "FAN5_R_Speed": 3450.0, "FAN6_R_Speed": 3335.0
  },
  "sampling": {
    "status": "ok", "sensor_data_available": true, "elapsed_seconds": 0.001,
    "counts": {"started": 1, "completed": 1, "successful": 1, "failed": 0, "timed_out": 0, "cancelled": 0}
  }
}
```

`run_id` 和 `poll_id` 标识运行与轮次，同一轮可能产生多条日志。`sampling.counts` 统计全部轮次，包含开始、完成、成功、失败、超时、取消；不能用日志行数代替。`status: ok` 仅表示读取和解析完成，不代表设备没有告警。`elapsed_seconds` 为本轮到记录生成时的耗时，最多三位小数。停止事件 `shutdown` 只保留原因和累计计数等基本信息。

有异常时才增加以下字段：

- `issues`：传感器、风扇或 BMC 异常。超出有效范围的温度为 `null`，实际异常读数留在说明中；未知诊断原样保留。整表读取失败不会展开为多个风扇缺失。
- `errors`：失败操作、类型、说明、耗时、超时预算、退出码和有界输出尾部。输出尾部最多 2048 字符，只表示已收到的内容，不能据此确定 BMC 卡在哪条请求。
- `recovery`：保持期间的健康采样次数与目标次数；第 5 次健康采样后移除，摘要说明解除保持。
- `read_failures`：连续读取失败次数、距有效温度数据的秒数，以及尚未解除的保护触发原因。`failure-count` 表示连续失败达到阈值，`no-valid-sample-timeout` 表示有效温度采样超时。

进入 L5 的事件为 `safety_alert`；提前报警会明确写“采样尚未结束”，`sampling.status` 为 `in-progress`，累计计数尚未结算本轮。BMC 异常只写诊断，不单独通知。

加 `--json` 输出完整诊断报告，保留原始 BMC 字节、DTS 原值、命令详情和未校准策略标签，供专项排查使用；文件日志不复制这些字段。加 `--no-log` 关闭文件日志。程序不安装开机服务；Ctrl+C 或 SIGTERM 会取消在途读取和等待，回收子进程并记录正常停止。

只读退出码：0 表示采样正常或用户主动停止，1 表示配置、文件或日志错误，2 表示建议 L5，或采集、关键数据、风扇故障。低于 L5 的单独 BMC 温度状态不改变退出码。故障明细仍显示为诊断并写入日志，主动停止不会清除记录。

## 传感器说明

- `FAN1_R_Speed` 至 `FAN6_R_Speed` 是当前六个必需转速传感器；任意一个缺失或变成 NA 都会报告故障。
- `FAN*_F_Speed` 和 `FAN7_*_Speed` 当前为 NA，不列为必需风扇；离散 `FAN*_Status` 状态位尚未解码。
- RAID/PSU2 的 0°C 和 NVME 的 NA 按无效读数处理，不单独触发故障。Inlet、有效的 RAID/NVME 温度仅监测。
- DTS 的含义尚未确认，仅在完整诊断报告展示原值，不当作 CPU 绝对温度。新增温度传感器需在配置中匹配名称。

`examples/hr650x-observed-sensors.txt` 是用户提供的 163 行实机快照；`examples/hr650x-sensors.txt` 是合成示例。离线输入必须同时使用 `--once`。

## 编码与 RPM

现有机器记录为 ipmitool 1.8.19、BMC 11.99。状态读回为 `20 0f 0f 0f 0f 0f 0f 0f 0f`，随后六风扇读数为 3680、3680、3680、3680、3450、3335 RPM，平均约 3584 RPM。
厂商协议定义首字节 bit 0 为手动 0 / 自动 1，bit 1 为自动检测 / OEM 热配置选择，bits 7:2 保留。因此 `0x20`、`0x0c` 都不能直接当作“非手动”。后面是八个 duty 槽位；本机控制核对前六个，保留未安装槽原值。实际效果仍须同机逐风扇测量确认。

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
实际写入固定使用 `raw 0x2e 0x30 0x00 0x00 byte`，L1～L5 字节为 `0x0f / 0x14 / 0x1e / 0x2d / 0x3c`，独立于策略标签；状态读取 `raw 0x2e 0x31`。不会把百分数字符串当成十六进制参数。默认观察报告仍为 `calibration.status: unverified`，控制报告中的 `verified` 仅表示 BMC 设置回读匹配，不代表实际 RPM 已校准。

协议依据为 [Lenovo BMC Command List v0.1.1 §§3.3–3.4](https://download.lenovo.com/servers_pdf/BMC_Command_List_Specification_v0.1.1.pdf)（PDF 第 13–14 页）。协议文档不等于 BMC 11.99 的实机验证，历史平均 RPM 也不能生成完整校准。

参考：[ipmitool 1.8.19 参数解析](https://github.com/ipmitool/ipmitool/blob/IPMITOOL_1_8_19/lib/helper.c)、[HR630X 社区实操](https://blog.lyc8503.net/post/19-first-rack-server-hr630x/)（不同机型，仅作参考）。


## 校准与实际控制

所有写模式仅支持 Linux 本机 OpenIPMI；DMI 产品必须为 HR650X、UUID 有效，`mc info` 制造商/产品为 8774/503。服务不加载校准文件；旧 `--profile` 参数兼容接受但忽略。

```sh
sudo .venv/bin/hrx-fancontrol --serve
```

可选测量：先停止服务，再运行 `sudo .venv/bin/hrx-fancontrol --calibrate calibration.json`。它会实际按 L5→L1 调速，测量结果只供分析，不是服务的启用条件。`--calibrate` 的路径必须尚不存在，父目录须可写。程序先取得完整健康采样，再按 L5→L1 写入；降低前检查温度需求、故障及恢复保持。每档写后至少等待 10 秒，收集三次间隔至少 5 秒的连续稳定新样本（默认轮次之间等待 15 秒），六个 RPM 和手动模式/duty 字节都保存在 JSON。每档等待最多 180 秒；期间需求超过当前档位时立即升档并中止校准。

各风扇的稳定分布须逐档递增。容差由测量范围扩展 15%（至少 200 RPM），再按相邻实测均值的中点裁剪，防止旧档 RPM 被认作新档；测量范围跨过中点时拒绝资料。完整五档成功后才原子发布文件，既有文件不会覆盖。取消或失败不生成完整资料；事件日志保留已测步骤。校准完成/正常取消均保持最后设置。

服务常驻后，在另一个 SSH 终端操作：

```sh
sudo .venv/bin/hrx-fancontrol auto on
sudo .venv/bin/hrx-fancontrol auto off
sudo .venv/bin/hrx-fancontrol manual 25
sudo .venv/bin/hrx-fancontrol status
sudo .venv/bin/hrx-fancontrol --json status
```

安装为系统服务后，将上述 `.venv/bin/hrx-fancontrol` 换成 `/opt/hrx-fancontrol/.venv/bin/hrx-fancontrol`。

| 指令 | 作用 |
|---|---|
| `auto on` | 开启周期采样，使用新鲜数据按五档曲线自动调控；主动恢复已让位的控制 |
| `auto off` | 关闭新的周期采样和调控，保留风扇当前设置，服务继续接收指令 |
| `manual N` | 先关闭自动，再设置十进制整数1～100；温度曲线不改变此固定值 |
| `status` | 查看自动开关、手动目标、执行确认状态和最近读数的时间；缓存不是实时采样 |

例如 `manual 25` 写入字节 `0x19`，`manual 100` 写入 `0x64`；不接受十六进制写法、小数或范围外参数，也不把它们当作 RPM 百分比。原 `--manual-level` 最低档位模式已删除。`--auto` 仅保留为 `--serve` 的兼容入口，不覆盖保存的关闭状态。

第一次启动服务默认自动运行。`auto off` 或 `manual` 后，模式保存到 `/var/lib/hrx-fancontrol/service-mode.json`；服务或服务器重启后仍关闭自动，且不重新发送保存的手动值。BMC 可能被其他程序或重启改变，保存的目标只是操作意图；重新设置请明确发送 `manual N`。

写入后立即串行查询 BMC：手动模式和六个槽位的参数匹配即显示“设置已确认”。回读失败或尚不匹配时继续等待；自动和手动均每隔 5 秒重查 BMC，期间暂停新温度采样；自动模式确认完成后恢复采样。90 秒后不再发起查询，已在途的查询最多再等待 3 秒。不会为等待确认而重复写入同一目标。手动确认后停止采样，服务继续接收指令。

`pending-verification` 表示设置尚未确认，`verified` 只表示 BMC 回读匹配。不再要求三次稳定 RPM，也不检查 RPM 区间；RPM 仍用于展示和风扇异常检测。所有 IPMI 命令串行；新指令等待当前调用结束，关闭/手动优先于旧采样产生的自动写入。客户端等待最多 60 秒，不自动重发；超时后用 `status` 核对。自动重新开启时使用新策略时钟，不把暂停期间算作采样失败。

自动模式保留原曲线与5次/300秒读取失败规则。真实执行失败仍最多尝试一次固定L3，再以失败退出；手动操作完成、主动关闭或外部接管后不兜底改速。服务恢复同时检查持久模式、当前invocation和活动操作标记，只有尚在执行/验证的手动操作才可能恢复，空闲手动模式不会因崩溃而写L3。

**已确认的 BMC 模式或六槽参数被改变后，程序让位并停止写入。** 服务仍接收指令，但普通重启不会抢回控制；明确 `auto on` 或 `manual N` 才代表新的主动控制意图。单纯RPM波动或读取超时不算外部修改；暂停后没有周期采样，也无法实时发现外部变化。读写间竞态、改成相同值或两次读取间改后恢复不能保证检测。

客户端通过 `/run/hrx-fancontrol/control.sock` 交给唯一服务执行，不直接操作BMC。socket仅服务用户可访问，服务停止时指令明确报不可连接。服务、校准和恢复共用writer.lock；默认无控制选项仍只读，`--once`/`--input`不能与服务或校准混用。同机身份核对保留，原owner记录中的确认与让位证据继续有效。

Ctrl+C/SIGTERM或正常停止服务保持最后设置，不恢复BMC自动模式。写入途中取消可能留下未知状态，日志如实记录。校准仍需停止服务后运行；此前已让位时，可给 `--calibrate` 加 `--resume-control` 明确恢复校准权限，此选项不用于服务启动。

## systemd 服务

使用 `.deb` 时，服务单元已安装；执行 `sudo systemctl enable --now hrx-fancontrol` 即可。下面是**源码安装**专用步骤，不要在 deb 安装后再复制 `/etc/systemd/system` 模板。

安装随附模板。以下在服务器执行，源码放 `/opt/hrx-fancontrol`，YAML 放 `/etc/hrx-fancontrol`，日志放 `/var/log/hrx-fancontrol`。最后的 `enable --now` 启动常驻服务并设置开机自启；首次启动默认自动，后续遵守保存的模式。

```sh
sudo mkdir -p /opt/hrx-fancontrol /etc/hrx-fancontrol
sudo tar -xzf ~/hrx-fancontrol-source.tar.gz -C /opt/hrx-fancontrol
sudo python3 -m venv /opt/hrx-fancontrol/.venv
sudo /opt/hrx-fancontrol/.venv/bin/python -m pip install /opt/hrx-fancontrol
sudo tee /etc/hrx-fancontrol/config.yaml >/dev/null <<'YAML'
logging:
  directory: /var/log/hrx-fancontrol
YAML
sudo install -m 644 /opt/hrx-fancontrol/systemd/hrx-fancontrol.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hrx-fancontrol
sudo systemctl status hrx-fancontrol
sudo systemctl is-enabled hrx-fancontrol
sudo journalctl -u hrx-fancontrol -f
```

日常管理：

```sh
sudo systemctl stop hrx-fancontrol       # 停止，保持最后档位
sudo systemctl start hrx-fancontrol      # 启动
sudo systemctl restart hrx-fancontrol    # 重启服务
sudo systemctl disable --now hrx-fancontrol  # 停止并取消开机自启
```

更新源码前先停服务，重新安装程序与服务模板，执行 `sudo systemctl daemon-reload` 后再启动。外部接管后的暂停状态会跨服务/系统重启保留，需按上节明确恢复；普通 `start/restart` 不会抢回控制。

服务有 90 秒看门狗，由主事件循环发送进展通知（包括有意暂停时的空闲循环），不会用独立心跳线程掩盖卡住的IPMI；异常退出最多每十分钟重启三次。`ExecStopPost` 只有异常服务结果、当前 invocation 的有效接管标记、同机身份匹配且未尝试过兜底时，持同一锁尝试一次 L3。恢复前也核对外部变化；尚未核实控制权时不写，发现变化后让位，后续自动启动不能再写。正常停止、启动失败未接管、旧进程标记、已做过兜底都不写入。`RuntimeDirectoryPreserve=yes` 保留锁的 inode，`StateDirectory` 保存跨重启的让位状态。程序不会自行安装或启用模板。

前台 SIGKILL、机器断电、BMC 不响应或服务恢复程序无法运行时，都不能保证执行保护；此软件不替代硬件热保护。实机校准、服务重启和满载散热须在目标服务器验证。

导出日志：

```sh
sudo journalctl -u hrx-fancontrol --since today --no-pager > hrx-service.log
sudo tar -czf hrx-logs.tar.gz -C /var/log hrx-fancontrol
```

## 从源码服务迁移到 deb

旧模板位于 `/etc/systemd/system/hrx-fancontrol.service`，会覆盖 deb 的 `/usr/lib/systemd/system/hrx-fancontrol.service`。先停服务并备份，确认它确实是此前手工安装的 HRX 模板后再移除；自定义 drop-in 也需检查有无旧 `/opt` 路径。

```sh
sudo systemctl stop hrx-fancontrol
sudo cp -a /etc/hrx-fancontrol /etc/hrx-fancontrol.backup
sudo cp -a /etc/systemd/system/hrx-fancontrol.service /etc/hrx-fancontrol/source-service.backup
sudo rm /etc/systemd/system/hrx-fancontrol.service
sudo systemctl daemon-reload
sudo apt install ./hrx-fancontrol_0.1.0_all.deb
# 若 dpkg 询问配置文件，保留当前版本（默认 N）。
sudo systemctl daemon-reload
systemctl cat hrx-fancontrol
```

确认 `ExecStart` 与 `ExecStopPost` 使用 `/usr/bin/hrx-fancontrol`，且配置与 `/var/lib/hrx-fancontrol/` 原模式文件仍在，再执行 `sudo systemctl start hrx-fancontrol`。需要开机自启时执行 `sudo systemctl enable hrx-fancontrol`。启动后 `sudo hrx-fancontrol status` 查看原模式；迁移不需要重新校准，也不要删除 owner 或模式文件。

仅用虚拟环境前台采集、没有安装系统服务的用户可以直接安装 deb。源码目录与虚拟环境不由 deb 管理，确认迁移成功后再自行清理。

## 发布版本与升级

维护者先在 `dev` 将 `pyproject.toml` 的 `version` 和 `src/hrx_fancontrol/__init__.py` 的 `__version__` 改为同一个 `X.Y.Z`，创建 PR，等待 Linux CI 成功后再合入 `main`。

1. 在 GitHub 仓库打开 **Releases → Draft a new release**。
2. 新建标签，例如 `v0.1.0`，Target 选 `main`，填写发布说明，然后 **Publish release**。
3. 在 Actions 等待 **Release Debian package** 成功。它测试标签对应代码、构建并检查 Linux 安装/升级，再把 `.deb` 与 `SHA256SUMS` 加到这条 Release。只保存草稿不会触发。

标签必须是 `vX.Y.Z`，与源码两处版本一致，提交须已进入 `main`。此流程要求仓库关闭 **immutable releases**，因为附件在发布后才生成。构建失败时先看 Actions 日志，Release 页面暂时没有可安装附件；需要改代码时发布新版本。相同源码的失败任务可以重跑；已存在且内容相同的附件会跳过，内容不同则拒绝覆盖。

CI 在 Ubuntu 24.04 临时 runner 上安装包，使用测试服务进程验证首装、运行/停止及启用/禁用四种升级状态、卸载与配置保留；测试不访问 IPMI，不代表 HR650X 实机控制已验证。

用户升级时下载新版本和校验文件，然后：

```sh
sha256sum --check SHA256SUMS
sudo apt install ./hrx-fancontrol_0.1.0_all.deb  # 替换为新版本文件名
sudo hrx-fancontrol status
```

升级会先正常停止原运行中的服务，更新后恢复运行；原本停止的服务仍停止，开机启用状态不变。配置文件遇到提示时保留当前版本；校准、日志、自动开关/手动模式及外部让位记录保留。自动关闭模式不会因升级变成自动开启。第一次安装无需校准文件，不会启动服务。

这是本地 deb 安装，不是 APT 软件源：`apt upgrade` 不会自动找到 GitHub 上的新版本。依赖由服务器已有 Ubuntu 镜像安装。普通卸载 `sudo apt remove hrx-fancontrol` 停服务并保留配置和机器数据；`apt purge` 还会删除包提供的 `config.yaml`，校准、模式与日志仍保留。
