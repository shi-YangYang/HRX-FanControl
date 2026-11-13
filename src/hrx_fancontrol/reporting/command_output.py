"""Small human-readable command responses, separate from full diagnostic reports."""


def render_command(result):
    lines = [result["message"]]
    state = result.get("status")
    if not state:
        return lines[0]
    mode = {"automatic": "自动", "paused": "暂停", "manual": "手动", "suspended": "外部接管，已让位"}[state["mode"]]
    lines.append(f"模式：{mode}；自动采样/调控：{'开启' if state['automatic'] else '关闭'}")
    if state["manual_parameter"] is not None:
        lines.append(f"手动目标：{state['manual_parameter']}（0x{state['manual_parameter']:02x}，保存的设置意图）")
    control = state["control"]
    lines.append("设置已确认：BMC 回读匹配。" if control["verified"] else
                 f"设置尚未确认；状态：{control['status']}")
    if state["last_sample_time"]:
        lines.append(f"最近采样：{state['last_sample_time']}，距今 {state['sample_age_seconds']} 秒（缓存）")
        lines.append("RPM：" + ", ".join(f"{name}={rpm}" for name, rpm in state["fans_rpm"].items()))
    else:
        lines.append("本次启动尚无采样，当前物理转速未知。")
    if state["error"]:
        lines.append(f"异常：{state['error']}")
    return "\n".join(lines)
