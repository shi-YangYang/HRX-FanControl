# 规格管理

规格使用 `spec-XXX-short-name/` 命名，编号递增，名称概括可独立交付的目标。

| 文件 | 职责 |
|---|---|
| `spec.md` | 背景、范围、行为、约束及可逐项检查的验收标准 |
| `plan.md` | 已确认范围的实施步骤、文件、验证与风险 |
| `acceptance.md` | 独立验收的 PASS / FAIL、检查证据、问题与返工要求 |

生命周期：草稿 → 关键问题明确后可实施 → 实施 → 独立验收 → 完成。
返工时按需补充 `rework.md` / `implementation.md`；由新的实施 Agent 处理，再由新的验收 Agent 检查。
验收报告形成后结果只能为 PASS 或 FAIL；未验收时不要伪造结果。
开发验收与 Git 操作状态分开，只有已授权的 commit / push 才执行。

模板位于 [_template/](./_template/)。当前规格：

| Spec | 目标 |
|---|---|
| [001：项目组织](spec-001-project-organization/spec.md) | 接入治理结构、整理双语 README 与指南、建立 dev 并按授权提交推送 |
