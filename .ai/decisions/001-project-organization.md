# Decision：接入治理结构并保留原型边界

## Context

用户要求按提供的通用 AGENTS.md 完善目录结构、README 与 Git 分支，并完成一次 commit + push。
仓库已有 Python 包、配置、测试和示例，属于 `EXISTING`；这些原型文件在本次工作开始时尚未提交。
刷新远端后，本地与远端 `main` 均位于 `9964115`，没有 `dev`。

## Decision

1. 原样纳入用户提供的通用 AGENTS.md，项目约束放在 `.ai/rules/hr650x.md`。
2. 保留 `src/`、`tests/`、`config/`、`examples/` 及配置符号链接，补齐 `constitution/`、`specs/`、`.ai/`。
3. 根 README 改为精简英文入口，新增对应中文入口；原有详细内容集中整理到 `docs/hr650x-guide.md`。
4. 从已核实的初始 `main` 建立 `dev`，提交本会话已实现的原型及本次文档，推送到 `origin/dev`。
5. 保持 Read-only / Dry-run、五档及 PCH 65°C 门槛，不加入硬件控制。

## Reason

通用规范要求已有项目保留工程结构，双语 README 承担入口职责，开发默认使用 `dev`。
已有源码职责可辨，本次没有迁移包或重构业务的必要；分离详细说明后可让安装路径更容易找到。

## Alternatives

不移动业务包，不创建第二套 `app/` 或 `packages/`；不把完整设计与过程记录继续塞进 README。
不直接推送 `main`，不自动创建或合并 PR，不修改已有提交历史。

## Consequences

文档入口发生分工，源码导入路径、CLI、配置路径和测试路径不变，无运行时迁移。
README 的 Git 安装命令显式选择 `dev`；本次推送不等于已发布到 `main` 或完成实机验证。
未来若真正实施控制，需要新的 Spec、校准证据和授权。
