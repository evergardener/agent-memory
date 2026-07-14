# Changelog

## 1.0.0-rc.1 — 2026-07-13

首个可接入测试候选版本：

- Hermes MemoryProvider 多 profile 共享 namespace、回合幂等写入、显式召回、来源追溯、更正、当前状态和 Vault 工具；
- 只读证据、规则脱敏、长期/阶段/当前/低价值分类，以及 evidence-linked episode/arc；
- 词法、本地语义和实体三路 RRF，普通/显式召回分离，forgotten 仅显式主题唤醒；
- isolated、forgotten、更正替代与二次确认永久清除；
- AES-GCM 信封加密 Vault、限时 profile grant、撤销和星图脱敏关联；
- 五轴确定性状态、完整参数治理、无副作用模拟、当前事实和跨 profile 连续性；
- PostgreSQL 任务租约、故障恢复、周期整理报告、版本化 Compose 和恢复演练。

已知限制：V1 只支持单用户和 Hermes；API 仅设计为 localhost；实体类型和复杂关系抽取仍采用保守规则；星图前端 bundle 尚未代码分割。
