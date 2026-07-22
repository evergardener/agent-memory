# Agent Memory 接手交接

> 更新时间：2026-07-22。目标：新接手者无需依赖聊天记录即可继续工作；生产运行态则必须单独授权交接。

## 1. 一句话状态

V1 核心能力、阶段 C 关系星系和阶段 E0 展示身份已完成。`1.0.0-rc.8` 在 `codex/production-canary-boundaries` 隔离分支完成多 profile 来源门禁、部署冻结、备份一致性实现及首次完整 Release Gate；最终文档提交后须对最终 SHA 重跑 Gate。

当前真实生产 canary 已存在，但仍运行旧 revision；本分支没有修改生产数据库、容器或 Hermes。rc.8 通过隔离 Gate 后，任何生产 rebuild/restart/重新绑定仍须用户再次确认。

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

- 开发分支：`codex/production-canary-boundaries`，完成 Gate 后推送；
- 阶段 C 功能提交：`935faf8 feat: complete phase C relation galaxies`；
- 阶段 C 验收记录：`02656de docs: record phase C acceptance`；
- `VERSION` / Python package：`1.0.0-rc.8` / `1.0.0rc8`；
- rc.8 尚未打 tag、合并或部署到生产；
- 工作区中的 `data/`、`backups/`、`secrets/`、`release-artifacts/` 全部是 Git 忽略的本地资产。

## 4. 当前运行状态

2026-07-22 只读生产核验（可能随新会话增长；接手时重新核对）：

| 入口/组件 | 状态 | 说明 |
| --- | --- | --- |
| `127.0.0.1:7810` | 生产 canary API healthy | project `agent-memory-production`，运行镜像 revision `c222a86…`，模型关闭 |
| `jiuyue:production-jiuyue` | 当前 live canary 来源 | 只读核验时 14 events；后续可能增长 |
| `qishuo:hermes-session-export` | 既有历史导入来源 | 只读核验时 9,117 events；当前不是 live Agent Memory Provider |
| `failed_jobs` | 0 | 只读核验值 |

隔离 Review 栈使用 `7802/7804/7805` 和独立 project/data/network；它不属于正式运行面，可在记录结果后停止。

`de7b82c` 的最终 Gate 使用独立 `agent-memory-release-final-de7b82c`、
`172.16.246/247` 和 `7812–7815`，通过后已删除临时容器与网络。同一提交又使用
`agent-memory-production` 完成正式 namespace 空库、安全属性、状态清单、备份恢复和 Vault
往返演练；演练后同样已无损停止，未接入 Hermes、未启用模型。

生产运行目录是 `$HOME/.local/share/agent-memory/production`。不得读取或复制其中的 env、token、数据库密码、UI secret、模型 key 或 Vault root key。rc.8 新脚本会要求 `SOURCE-POLICY.json` 和 deployment bundle；旧运行态缺少它们时失败关闭，不得手工伪造为已验证。

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
- 正式关系提升的双 SHA/备份清单/change ID 授权路径；
- 审计事件确定性排序与可靠撤销（迁移 `0014_audit_event_order`）；
- 脱敏器 v4、影子库重建、API/Hermes 只读召回、幂等、备份恢复和 Vault 解密验证；
- 用户接受当前数据规模下的阶段 C 验收结论。

## 8. 下一任务队列

| 优先级 | 任务 | 完成标准 |
| --- | --- | --- |
| P0 | 完成 rc.8 隔离 Gate | 来源矩阵、文件/镜像漂移、备份恢复、全量 release/handoff Gate PASS |
| P0 | 提交生产升级方案并等待确认 | 明确旧/新 revision、来源角色、升级前备份、回滚点；不得先更新容器 |
| P1 | 经批准更新并复验 | rebuild 后验证 API/worker/migration、inventory、星图、备份和 Hermes 业务 |
| P1 | 多 Hermes profile 真实灰度 | 每个 live source 独立观察 2/24/72 小时，fail-soft、召回、追溯、治理和备份通过 |
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
并知道生产 canary 正在旧 revision 上运行，rc.8 源码完成不等于生产已升级。提交推送后必须通过 `scripts/handoff-check.sh`；容器更新前再次获得用户确认，更新后验证真实业务。
