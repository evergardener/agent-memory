# Agent Memory 接手交接

> 更新时间：2026-08-08。目标：新接手者无需依赖聊天记录即可继续工作；生产运行态则必须单独授权交接。

## 1. 一句话状态

V1 基础证据/事实/召回、统一经历与程序性记忆、H0–H6 治理和 rc.9 生产 canary 已完成。
真实生产当前运行 `1.0.0-rc.9` / `cb12c5185b5c4c7305685b5860202174a1b00de2`
的 GHCR 精确 SHA 镜像，状态保持 `canary_active`；`jiuyue:production-jiuyue` 持续写入，
外部模型已启用，历史自动回填关闭。2026-08-08 生产 H5 apply 将 candidate 从 38 降至 2，
events `12,217`、facts `107` 与 evidence/Vault hash 均保持不变；两次恢复验证备份、Hermes
browse/recall/trace 和前端加载均 PASS。当前质量状态仍为 DEGRADED，原因是 3 个授权前已存在的
失败任务尚未重放；尚未生成晋级记录。

源码包含当前回合切分、中文偏好/问句判定、最近记忆浏览、同文召回去重、重复工具事件质量
指标，以及 H0–H5 的 current、偏好、召回和历史 candidate 治理。profile 级 Provider 升级、
可重定位 Alembic 调用及 production `--upgrade` 绑定流程已经过 rc.9 生产验证。

GHCR 工作流使用 `sha-<full revision>` 发布三个 amd64/arm64 镜像，并生成
SBOM/provenance/attestation；生产预检拒绝 `main/latest`。Actions run `31232820388` 已完成
`cb12c518…` 的源码质量、三镜像发布、精确 SHA 拉回和完整 Hermes Release Gate，生产随后采用
pull-only 升级。应用继续使用固定非 root 用户、只读根文件系统和 `cap_drop: ALL`。

## 2. 必读顺序

1. [`V1.0-项目需求文档.md`](V1.0-项目需求文档.md)：产品边界；
2. [`V1.0-统一经历与程序性记忆设计.md`](V1.0-统一经历与程序性记忆设计.md)：下一阶段核心语义、边界和冻结场景；
3. [`V1.0-总体架构设计.md`](V1.0-总体架构设计.md)：服务和数据流；
4. [`V1.0-开发部署与运维手册.md`](V1.0-开发部署与运维手册.md)：实际运行方式；
5. [`V1.0-正式迁移与灰度发布方案.md`](V1.0-正式迁移与灰度发布方案.md)：下一阶段；
6. [`V1.0-阶段C实施验证报告.md`](V1.0-阶段C实施验证报告.md)：已验证证据；
7. [`V1.0-release验收矩阵.md`](V1.0-release验收矩阵.md)：逐项 Gate。
8. [`V1.0-上线前Review报告.md`](V1.0-上线前Review报告.md)：最新风险、修复与上线阻断项。
9. [`V1.0-生产候选接入与原地晋级手册.md`](V1.0-生产候选接入与原地晋级手册.md)：真实 canary 和晋级操作。
10. [`跨主机开发与交接标准.md`](跨主机开发与交接标准.md)：每次提交的完成定义。
11. [`V1.0-后续阶段开发计划.md`](V1.0-后续阶段开发计划.md)：F0–F8、真实数据灰度和原地晋级顺序。
12. [`V1.0-生产来源治理与部署冻结设计.md`](V1.0-生产来源治理与部署冻结设计.md)：多 profile source policy、部署 bundle、备份新鲜度和升级授权边界。
13. [`V1.0-rc8生产边界验证报告.md`](V1.0-rc8生产边界验证报告.md)：隔离 Gate、负例矩阵、失败修复和剩余上线动作。
14. [`V1.0-jiuyue写入召回缺陷修复报告.md`](V1.0-jiuyue写入召回缺陷修复报告.md)：真实写入/召回缺陷、源码修复、验证和生产数据边界。
15. [`V1.0-F1-F7隔离实施验证报告.md`](V1.0-F1-F7隔离实施验证报告.md)：统一经历实现、故障恢复、恢复计数、泄漏和性能证据。

## 3. 代码与版本基线

- 收敛分支：`codex/reconcile-rc8-main`，由 `main` 合入已部署的 `codex/production-canary-boundaries`；
- 阶段 C 功能提交：`935faf8 feat: complete phase C relation galaxies`；
- 阶段 C 验收记录：`02656de docs: record phase C acceptance`；
- `VERSION` / Python package：`1.0.0-rc.9` / `1.0.0rc9`；
- 生产已部署 rc.9 revision `cb12c518…`；尚未打正式 tag 或晋级 V1.0；
- 工作区中的 `data/`、`backups/`、`secrets/`、`release-artifacts/` 全部是 Git 忽略的本地资产。

## 4. 当前运行状态

2026-08-08 更新后生产核验（计数会随新会话增长；接手时重新核对）：

| 入口/组件 | 状态 | 说明 |
| --- | --- | --- |
| `127.0.0.1:7810` | 生产 canary API healthy | project `agent-memory-production`，GHCR revision `cb12c518…`，模型启用 |
| `jiuyue:production-jiuyue` | 当前 live canary 来源 | 生产 namespace 共 12,217 events / 107 facts；来源计数接手时重查 |
| H5 历史治理 | 已应用 | 36 条受治理、22 份 evidence 归并、candidate `38→2`，幂等重放写入 0 |
| Hermes Provider | 已激活 7 tools | `browse`、`recall`、`trace` 只读端到端检查 PASS |
| 质量状态 | DEGRADED | 1 个历史模型超时、2 个旧 purge RestrictViolation；未经授权未重放 |

隔离 Review 栈使用 `7802/7804/7805` 和独立 project/data/network；它不属于正式运行面，可在记录结果后停止。

`de7b82c` 的最终 Gate 使用独立 `agent-memory-release-final-de7b82c`、
`172.16.246/247` 和 `7812–7815`，通过后已删除临时容器与网络。同一提交又使用
`agent-memory-production` 完成正式 namespace 空库、安全属性、状态清单、备份恢复和 Vault
往返演练；演练后同样已无损停止，未接入 Hermes、未启用模型。

生产运行目录是 `$HOME/.local/share/agent-memory/production`。不得输出或复制其中的 env、token、数据库密码、UI secret、模型 key 或 Vault root key。当前 deployment state 已绑定 `cb12c518…` bundle/source policy；升级前恢复验证备份为 `20260808T030119Z`，H5 后备份为 `20260808T030901Z`。

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
- 质量门禁后的 GHCR amd64/arm64 发布、不可变 SHA 标签、attestation、发布后拉回全量
  Release Gate 和生产 pull-only 契约（`b9668d1` / run `30152123281`）；
- 生产 rc.8 更新、`jiuyue` 真实链路、恢复验证和 Vault 往返；
- 正式关系提升的双 SHA/备份清单/change ID 授权路径；
- 审计事件确定性排序与可靠撤销（迁移 `0014_audit_event_order`）；
- 脱敏器 v4、影子库重建、API/Hermes 只读召回、幂等、备份恢复和 Vault 解密验证；
- 用户接受当前数据规模下的阶段 C 验收结论。
- 统一经历 F1–F6 隔离实现：多实体时间情节、关系/纪念日/偏好、artifact、程序、融合召回、
  多边形主体布局和跨类型治理；
- F7 客观故障恢复：worker 停机积压、过期租约、API fail-soft、情节幂等、统一表空库恢复、
  evidence hash、Vault 解密、泄漏扫描和性能基线。
- `09a225681e875542e664d22390b6a39d843693fe` 的 F1–F7 完整隔离 Release Gate：
  22 个集成用例、10 个 Hermes Provider 用例、镜像修订、恢复与 Vault Gate 全部 PASS。
- rc.9 `cb12c518…` 的 GHCR 发布后 Gate、生产迁移、H5 apply、幂等重放、两次恢复验证备份、
  Hermes Provider 只读端到端和前端无错误加载。

## 8. 下一任务队列

| 优先级 | 任务 | 完成标准 |
| --- | --- | --- |
| P0 | 人工审核 2 条 review | 先追溯证据；持久格式偏好修正后确认，Surge 请求按证据修正或脱离 |
| P0 | 治理 3 个既有失败任务 | 先 dry-run 分类；模型超时与 purge 任务均需单独重放授权 |
| P0 | 观察 rc.9 canary | 复核健康、来源、失败任务、召回、追溯、星图和模型错误率 |
| P0 | 维护恢复验证备份 | 当前 H5 后备份 `20260808T030901Z`；后续写入增长后重新创建 |
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
并知道生产 canary 正在 rc.8 revision `e05a492…` 上运行，主线后续提交不会自动改变运行容器。
接手者还必须能说明 F1–F6 仅在隔离分支实现、生产仍为 rc.8，F7 用户主观验收和 F8 独立生产
授权仍未完成。提交推送后必须通过 `scripts/handoff-check.sh`；72 小时、最新生产恢复验证备份、
主观验收和用户批准缺一不可，禁止提前晋级。
