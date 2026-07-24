# Agent Memory 接手交接

> 更新时间：2026-07-24。目标：新接手者无需依赖聊天记录即可继续工作；生产运行态则必须单独授权交接。

## 1. 一句话状态

V1 核心能力、阶段 C 关系星系、阶段 E0 展示身份和 rc.8 生产边界已完成。真实生产当前运行 `1.0.0-rc.8` / `47a1506ae94a5a08fde6b4066f1daa24e2d27608`，状态为 `canary_active`；`jiuyue:production-jiuyue` 正在持续写入，模型关闭，尚未生成晋级记录。

本分支负责把已部署的 rc.8 实现与 `main` 的 CI 修复收敛，并修正文档；它不会重启生产容器、修改 Hermes profile 或写入生产数据。收敛后的新 Git SHA 与运行 revision 不同，因此必须重新通过源码 Gate，但不要求仅因文档/合并提交重建健康的生产容器。

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
11. [`V1.0-生产来源治理与部署冻结设计.md`](V1.0-生产来源治理与部署冻结设计.md)：多 profile source policy、部署 bundle、备份新鲜度和升级授权边界。
12. [`V1.0-rc8生产边界验证报告.md`](V1.0-rc8生产边界验证报告.md)：隔离 Gate、负例矩阵、失败修复和剩余上线动作。

## 3. 代码与版本基线

- 收敛分支：`codex/reconcile-rc8-main`，由 `main` 合入已部署的 `codex/production-canary-boundaries`；
- 阶段 C 功能提交：`935faf8 feat: complete phase C relation galaxies`；
- 阶段 C 验收记录：`02656de docs: record phase C acceptance`；
- `VERSION` / Python package：`1.0.0-rc.8` / `1.0.0rc8`；
- 生产已部署 rc.8 revision `47a1506…`；源码正在收敛回主线，尚未打正式 tag 或晋级 V1.0；
- 工作区中的 `data/`、`backups/`、`secrets/`、`release-artifacts/` 全部是 Git 忽略的本地资产。

## 4. 当前运行状态

2026-07-24 只读生产核验（计数会随新会话增长；接手时重新核对）：

| 入口/组件 | 状态 | 说明 |
| --- | --- | --- |
| `127.0.0.1:7810` | 生产 canary API healthy | project `agent-memory-production`，运行镜像 revision `47a1506…`，模型关闭 |
| `jiuyue:production-jiuyue` | 当前 live canary 来源 | 3 sessions / 6 turns / 494 events；首次 source-bound 验证为 361 events |
| `qishuo:hermes-session-export` | 既有历史导入来源 | 只读核验时 9,117 events；当前不是 live Agent Memory Provider |
| `facts` / `failed_jobs` | 12 / 0 | 只读核验值 |

隔离 Review 栈使用 `7802/7804/7805` 和独立 project/data/network；它不属于正式运行面，可在记录结果后停止。

`de7b82c` 的最终 Gate 使用独立 `agent-memory-release-final-de7b82c`、
`172.16.246/247` 和 `7812–7815`，通过后已删除临时容器与网络。同一提交又使用
`agent-memory-production` 完成正式 namespace 空库、安全属性、状态清单、备份恢复和 Vault
往返演练；演练后同样已无损停止，未接入 Hermes、未启用模型。

生产运行目录是 `$HOME/.local/share/agent-memory/production`。不得读取或复制其中的 env、token、数据库密码、UI secret、模型 key 或 Vault root key。当前 deployment state 已绑定 rc.8 bundle/source policy，最新恢复验证备份为 `20260722T134722Z`，覆盖首次验证时的 361 个 live events；之后新增的数据尚未进入新的恢复验证备份。

## 5. 接手后前 15 分钟

```bash
git status --short
git log -5 --oneline --decorate
test -f .env && echo env-present || echo env-missing
test -f secrets/vault_root_key && echo vault-key-present || echo vault-key-missing
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
curl --fail http://127.0.0.1:7810/health/ready
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
- 阶段 E0 Subject 显示身份与迁移 Gate；
- rc.8 source-bound canary、多来源角色、未知来源失败关闭、部署 bundle/镜像冻结和备份新鲜度 Gate；
- 生产 rc.8 更新、`jiuyue` 真实链路、恢复验证和 Vault 往返；
- 正式关系提升的双 SHA/备份清单/change ID 授权路径；
- 审计事件确定性排序与可靠撤销（迁移 `0014_audit_event_order`）；
- 脱敏器 v4、影子库重建、API/Hermes 只读召回、幂等、备份恢复和 Vault 解密验证；
- 用户接受当前数据规模下的阶段 C 验收结论。

## 8. 下一任务队列

| 优先级 | 任务 | 完成标准 |
| --- | --- | --- |
| P0 | 收敛 rc.8 到主线 | 合并生产边界分支与 CI 修复，更新文档，最终 SHA 的本地/隔离/handoff Gate PASS |
| P0 | 完成 `jiuyue` 72 小时观察 | 到 2026-07-25 13:44 UTC 后复核健康、来源、失败任务、召回、追溯、星图与脱敏 |
| P0 | 创建最新恢复验证备份 | 72 小时门禁满足后单独执行生产备份，覆盖首次验证后新增 events，并验证恢复/Vault |
| P1 | 真实数据质量校准 | 对 canary 新增事实做证据追溯、重复率、虚假实体和分类人工抽检 |
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
并知道生产 canary 正在 rc.8 revision `47a1506…` 上运行，主线收敛提交不会自动改变运行容器。提交推送后必须通过 `scripts/handoff-check.sh`；72 小时、最新恢复验证备份、主观验收和用户批准缺一不可，禁止提前晋级。
