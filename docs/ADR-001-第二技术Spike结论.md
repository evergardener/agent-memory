# ADR-001：第二技术 Spike 结论

- 状态：已接受
- 日期：2026-07-13
- 影响范围：V1 正式服务骨架

## 背景

技术选型要求在正式实现前验证 PostgreSQL 18 + pgvector、API/worker 分离、持久任务、跨 profile namespace、混合召回、脱敏与恢复。该验证不得依赖真实模型质量。

## 决策

1. PostgreSQL 18 + pgvector 继续作为唯一数据核心。ARM64 环境运行 PostgreSQL 18.4 与 `vector(8)` Spike 正常。
2. API 事务内只负责证据、审计和任务入队；worker 通过 `FOR UPDATE SKIP LOCKED` 与租约恢复持久任务。
3. 三路候选在应用层执行 RRF，数据库仅负责各通道排序。返回 `channels`、`rrf_score` 与来源 profile。
4. Provider 提供稳定回合幂等键；数据库唯一约束是最终去重防线。
5. 敏感检测必须位于证据持久化之前；检索投影只接受脱敏文本。
6. release 采用迁移工具而非 init 脚本；Spike 的 init SQL 仅验证 schema 能力。
7. `pg_dump` 自定义格式 + 空库 `pg_restore` 作为正式恢复演练基线。

## 证据

执行：

```bash
docker compose -f spikes/postgres_api_worker_spike/compose.yaml up \
  --build --abort-on-container-exit --exit-code-from recovery-tests
```

结果：测试容器与恢复容器退出码均为 0；数据计数为 3 条 evidence、3 条 job、3 条 fact、3 条 retrieval document；过期租约任务 `attempt_count=2`；召回结果同时覆盖 `default`、`work`、`recovery` 来源；主命中包含 `lexical + semantic + entity`；canary 被替换为 `[REDACTED]`；空库恢复后的 evidence 与完成任务计数均为 3。

## 限制与后续验证

- Spike 使用确定性 8 维伪 embedding，仅证明向量存储、排序和融合路径，不证明语义质量。
- Spike API 使用 Python 标准库，仅证明进程/契约边界；正式实现仍采用 FastAPI/Pydantic。
- Spike 未验证 Vault 加密恢复、并发压力、模型适配器与浏览器 UI，这些仍属于 release gate。
- 正式镜像必须在 release 前固定 digest；Spike 为获取当前 ARM64 可用性使用版本 tag。
