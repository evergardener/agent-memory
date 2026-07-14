# PostgreSQL + API + Worker Spike

该 Spike 验证第二技术 Gate，不是正式 V1 实现。它刻意使用小型同步 API 和确定性 embedding，避免模型质量影响基础架构结论。

验证范围：

1. PostgreSQL/pgvector 可创建证据、事实、实体、关系、检索和持久任务表。
2. 两个 Hermes profile 在同一 namespace 共享召回且来源可追溯。
3. 同一回合重复提交幂等。
4. worker 使用数据库租约，重启后可取回过期任务。
5. 词法、伪语义、实体三路召回通过 RRF 融合并返回解释。
6. 敏感 canary 只留下脱敏值，不进入检索投影。
7. `pg_dump` 能恢复到空数据库并通过计数核验。

## 运行

```bash
docker compose -f spikes/postgres_api_worker_spike/compose.yaml up --build --abort-on-container-exit --exit-code-from recovery-tests
```

`tests` 和 `recovery-tests` 均退出为 0 即表示在线验证通过。服务、数据卷和网络均使用项目名 `agent-memory-spike` 隔离。
