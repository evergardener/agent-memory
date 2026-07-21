# Agent Memory 接手交接

> 更新时间：2026-07-21。目标：新接手者无需依赖聊天记录即可继续工作；生产运行态则必须单独授权交接。

## 1. 一句话状态

V1 计划内核心能力和阶段 C 关系星系已完成；`1.0.0-rc.7` 源码候选已通过上线前 Review 和隔离 Gate。阶段 E0 正在候选分支实现，尚未接入真实 Hermes。
生产候选工具提交 `de7b82c` 已于 2026-07-21 通过完整隔离 Release Gate 及最终生产形态空栈/备份恢复演练，临时容器和网络已无损停止。下一个持久化运行栈将直接用于真实单 profile canary，通过后原地晋级。

## 2. 必读顺序

1. [`V1.0-项目需求文档.md`](V1.0-项目需求文档.md)：产品边界；
2. [`V1.0-总体架构设计.md`](V1.0-总体架构设计.md)：服务和数据流；
3. [`V1.0-开发部署与运维手册.md`](V1.0-开发部署与运维手册.md)：实际运行方式；
4. [`V1.0-正式迁移与灰度发布方案.md`](V1.0-正式迁移与灰度发布方案.md)：下一阶段；
5. [`V1.0-阶段C实施验证报告.md`](V1.0-阶段C实施验证报告.md)：已验证证据；
6. [`V1.0-release验收矩阵.md`](V1.0-release验收矩阵.md)：逐项 Gate。
7. [`V1.0-上线前Review报告.md`](V1.0-上线前Review报告.md)：最新风险、修复与上线阻断项。
8. [`V1.0-生产候选接入与原地晋级手册.md`](V1.0-生产候选接入与原地晋级手册.md)：真实 canary 和晋级操作。
9. [`跨主机开发与交接标准.md`](跨主机开发与交接标准.md)：每次提交的完成定义。
10. [`V1.0-后续阶段开发计划.md`](V1.0-后续阶段开发计划.md)：canary 前开发、真实数据灰度和原地晋级顺序。

## 3. 代码与版本基线

- 分支：`main`；
- 阶段 C 功能提交：`935faf8 feat: complete phase C relation galaxies`；
- 阶段 C 验收记录：`02656de docs: record phase C acceptance`；
- `VERSION` / Python package：`1.0.0-rc.7` / `1.0.0rc7`；
- `rc.7` 尚未打 tag 或接入真实 Hermes；
- 工作区中的 `data/`、`backups/`、`secrets/`、`release-artifacts/` 全部是 Git 忽略的本地资产。

## 4. 当前运行状态

2026-07-21 只读检查：

| 入口/组件 | 状态 | 说明 |
| --- | --- | --- |
| `127.0.0.1:7788` | 主测试 API 容器 healthy | rc.6 测试快照，包含生产数据副本但不是生产服务 |
| `127.0.0.1:7790` | import API healthy | 历史导入 staging |
| `127.0.0.1:7796` | 阶段 C API | 临时验收容器 |
| `127.0.0.1:7797` | 阶段 C UI | 临时前端对比容器 |
| `127.0.0.1:7798` | 阶段 C shadow API/UI | 最终阶段 C 验收入口 |
| PostgreSQL | healthy | 测试数据目录，不发布宿主端口，不作为正式部署源 |

隔离 Review 栈使用 `7802/7804/7805` 和独立 project/data/network；它不属于正式运行面，可在记录结果后停止。

`de7b82c` 的最终 Gate 使用独立 `agent-memory-release-final-de7b82c`、
`172.16.246/247` 和 `7812–7815`，通过后已删除临时容器与网络。同一提交又使用
`agent-memory-production` 完成正式 namespace 空库、安全属性、状态清单、备份恢复和 Vault
往返演练；演练后同样已无损停止，未接入 Hermes、未启用模型。

根目录 `.env` 当前不存在，但这只影响管理现有测试栈。生产候选由
`init-production-env.sh` 在 `$HOME/.local/share/agent-memory/production` 生成最终 `production.env`、data、Vault key 和备份目录。现有测试 `secrets/vault_root_key` 禁止复制进去。

## 5. 接手后前 15 分钟

```bash
git status --short
git log -5 --oneline --decorate
test -f .env && echo env-present || echo env-missing
test -f secrets/vault_root_key && echo vault-key-present || echo vault-key-missing
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
curl --fail http://127.0.0.1:7788/health/ready
curl --fail http://127.0.0.1:7798/health/ready
```

然后只读检查最新备份清单，不打印 `.env`、runtime.env、模型 key、服务 token 或 Vault 内容。

## 6. 不得擅自执行

- 不修改生产 Hermes 会话、profile、SQLite/数据库或线上配置；
- 不因根目录 `.env` 缺失而重建当前测试栈或运行 `scripts/init-local.sh`；
- 不把影子关系或 galaxy 表直接复制到正式 namespace；
- 不删除 `data/`、`backups/`、Hermes 导出或阶段 C 影子容器；
- 不执行 `docker system prune`、全局 volume/network prune；
- 不启用外部模型或发送真实对话，除非用户对固定数据范围、模型和用途重新明确授权；
- 不在未备份、未验证恢复的情况下迁移或降级数据库。

## 7. 已完成且无需重做

- V1 需求、架构、数据模型、API/Hermes、Vault 和运行设计；
- 阶段 A Subject 恒星身份层；
- 阶段 B 行星/镜片/星座/星流/Vault overlay；
- 阶段 C 类型化关系、`weighted-core-expansion-v1`、重叠成员、治理、布局、撤销和证据追溯；
- 阶段 C 前端主/子宇宙、5px 漂移、悬停静止、动静开关和无障碍列表；
- 发布环境完全隔离、应用容器最小权限、service-only ingest、Hermes loopback-only；
- `de7b82c` 完整 Release Gate 和生产形态空栈/备份恢复演练；
- 正式关系提升的双 SHA/备份清单/change ID 授权路径；
- 审计事件确定性排序与可靠撤销（迁移 `0014_audit_event_order`）；
- 脱敏器 v4、影子库重建、API/Hermes 只读召回、幂等、备份恢复和 Vault 解密验证；
- 用户接受当前数据规模下的阶段 C 验收结论。

## 8. 下一任务队列

| 优先级 | 任务 | 完成标准 |
| --- | --- | --- |
| P0 | 阶段 E0 展示身份收尾 | 用户显示名、profile 纯人格名、`display_name_origin`、详情/无障碍类型标注及旧数据安全迁移通过 Gate |
| P0 | 准备真实 Hermes canary | 用户指定 profile 后生成 `0600` 生产配置，先在独立 Hermes HOME 试连 |
| P0 | 获取真实接入授权 | 明确 profile、数据范围、模型外发范围与回滚点 |
| P1 | 单 Hermes profile 真实灰度 | 同一生产数据库观察 2/24/72 小时，fail-soft、召回、追溯、治理和备份通过 |
| P1 | 原地晋级 | 最新备份恢复通过，用户批准，写入 `PROMOTION-RECORD.json`，不迁库/换 namespace |
| P2 | 扩大真实数据质量验证 | 不降低门槛，积累更多人际/项目/设备/服务关系样本 |
| P2 | 退役影子容器 | 正式发布、影子备份和用户确认后三段式清理 |

## 9. 验证命令

```bash
uv sync --frozen --extra dev --extra migrations
uv run ruff check src integrations tests migrations
uv run pytest -q
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

全量 Gate 必须用 `scripts/init-release-env.sh` 生成的隔离环境执行；`release-check.sh` 会拒绝脏工作树、
生产 namespace、标准网段/端口/数据目录以及版本或 OCI revision 不一致。不要把生产 `.env` 直接交给该脚本。

## 10. 交接完成标准

接手者能说明版本与工作树的区别，能找到正确备份但不查看密钥，能安全检查两套入口，能运行本地回归，
并知道生产 canary 从第一天就使用最终资产，现有测试 `.env` 和数据库不是生产源。提交推送后必须通过 `scripts/handoff-check.sh`。
