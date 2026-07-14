# Spike 运行结果

- 日期：2026-07-13
- 平台：Apple Silicon / Docker Desktop
- PostgreSQL：18.4（aarch64）
- 结果：PASS

## 已验证

| 检查 | 结果 |
| --- | --- |
| schema 与 pgvector 扩展初始化 | PASS |
| 重复回合幂等写入 | PASS |
| 两个 profile 共用 namespace 并保留来源 | PASS |
| 过期 worker 租约恢复 | PASS，attempt 1 → 2 |
| 词法、伪语义、实体候选 RRF | PASS |
| 敏感 canary 不进入 evidence/retrieval 明文 | PASS |
| evidence/job/fact/document 一致计数 | PASS，均为 3 |
| `pg_dump` → 空库 `pg_restore` | PASS |

首次运行曾因测试错误地要求专用 AWS 占位符而失败；实际输入先匹配通用 `token=` 规则并正确生成 `[REDACTED]`。修正测试并补充 RRF、租约恢复和空库恢复后复测通过，未改变架构或需求方向。
