# Task Handoff

## Role

Delivery。实施与独立验收均已完成，当前 Spec 的 `acceptance.md` 记录 PASS。
此文件保留本任务交接；Git 提交与推送由协调 Agent 按已获授权完成，实际状态以 Git 引用及交付为准。

## Goal

按用户提供的 AGENTS.md 接入既有项目，整理双语 README、详细指南和项目治理目录；最终由协调 Agent 完成一次 commit + push。

## Context

已有 HR650X Read-only / Dry-run 原型，代码在本会话先前已实现但未提交。
当前用户要求完善工程组织，不要求新增温控功能。PCH 已确认 65°C 进入 L2，≤62°C 连续 60 秒降回 L1。

## Project Mode

EXISTING。当前在 `dev`，从刷新过的 `main` / `origin/main` 的 `9964115` 建立。原型文件均保留。

## Spec

- [Spec](../../specs/spec-001-project-organization/spec.md)
- [Plan](../../specs/spec-001-project-organization/plan.md)
- [Decision](../decisions/001-project-organization.md)

## Relevant Files

现有 README、`pyproject.toml`、`src/hrx_fancontrol/`、`config/default.yaml`、`tests/`、`examples/`。

## Scope

实施 Agent 负责 `README.md`、`README.zh-CN.md`、`docs/hr650x-guide.md`。
协调 Agent 负责 `AGENTS.md`、`constitution/`、`specs/`、`.ai/` 和 Git 操作，避免并行编辑同一文件。

## Out of Scope

源码、配置值、测试逻辑、依赖、CI、发布产物、硬件写入、自动控制、服务部署、跨机型支持与 PR。

## Constraints

保持 Read-only / Dry-run。百分数为策略标签，校准未完成。
不生成架构图或 Mermaid；目录职责用表格或列表。详细证据集中一份指南，避免重复过程记录。
不创建子 Agent，不切分支，不暂存/提交/推送，不运行测试或构建。

## Acceptance Criteria

按 Spec 逐项核对；中英文入口对等，克隆明确使用 `dev`，路径与命令真实，原有校准/回差证据保留。

## Commands

读取文件及 diff。已有文件尚未跟踪，普通 `git diff` 不显示其内容，须对照原文件或暂存后的 diff 审阅。

## Expected Output

Implementation Report，记录文件、决定、S0 文档检查、未运行项目测试和已知限制。
验收报告遵循根 AGENTS.md，只返回 PASS/FAIL 和依据；协调 Agent 写入 `acceptance.md`。
