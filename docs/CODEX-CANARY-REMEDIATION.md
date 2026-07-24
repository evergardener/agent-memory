# Codex 修复任务：生产 canary 范围、验证与运行版本冻结

> 创建时间：2026-07-22（基于只读运行核验）。
> 目标：修复可自动化修复的 canary 验证与部署冻结缺口；**不清库、不删除真实 evidence、不改 Hermes profile、不重启/升级当前生产容器**。
>
> 状态：历史修复任务，已由 `587c263`、`aeafcea`、`47a1506` 实现并通过隔离 Gate；
> `47a1506` 随后已按独立授权部署为 rc.8 生产 canary。本文件保留为需求与安全边界来源，
> 不再作为可直接执行的当前操作清单。当前状态见 `docs/handoff.md` 和
> `docs/V1.0-rc8生产边界验证报告.md`。

## 0. 当前现场（只读证据）

| 项目 | 值 |
| --- | --- |
| 生产 project | `agent-memory-production` |
| 运行状态 | `canary_active` |
| canary profile | `jiuyue` |
| namespace | `hermes:user-primary` |
| 运行镜像 revision | `c222a86c0a74f570e3c9a4052126ba66155f5d7c` |
| 当前 `main` | `f3f925cc643283da51783b817d520c7cf80ac353` |
| 运行 API | `http://127.0.0.1:7810`（loopback only） |
| model-worker | 未启用 |
| 最新恢复验证备份 | `20260722T045834Z`，验证时间 `2026-07-22T04:58:39Z` |
| canary 开始时间 | `2026-07-22T06:36:12Z` |
| 当前 `jiuyue` evidence | 14 条，`source_instance=production-jiuyue` |
| 当前 `qishuo` evidence | 9,117 条，`source_instance=hermes-session-export` |
| `failed_jobs` | 0 |

确认的 source/evidence 聚合：

```text
jiuyue events=14 first=2026-07-22 06:32:54Z last=2026-07-22 06:32:54Z
qishuo events=9117 first=2026-06-21 12:29:08Z last=2026-07-17 02:17:21Z
```

`jiuyue` 当前已经启用 `agent_memory` 外部 MemoryProvider；`qishuo` 当前仍使用 Hindsight，并未运行 Agent Memory Provider。

## 1. 强制边界

- 禁止执行任何会修改生产运行态的命令：
  - 不运行 `production-up.sh`、`production-stop.sh`、`production-promote.sh`；
  - 不运行 Docker restart/rebuild/up/down；
  - 不写入 `$HOME/.local/share/agent-memory/production/`；
  - 不修改 Hermes `jiuyue` / `qishuo` profile、gateway、plugin 或环境变量。
- 禁止删除、清空、物理清除或迁移当前生产数据库中的 qishuo / jiuyue evidence、facts、audit 或 Vault 数据。
- 禁止读取、打印、复制或提交 `production.env`、`hermes-production-*.env`、Vault root key、service token、模型 key、数据库密码、UI session secret。
- 仅修改 Git 跟踪的源码、测试和文档；在独立分支中完成。
- 所有验证必须使用独立 release runtime / namespace / Docker project，不能指向 production runtime 或 `hermes:user-primary`。

## 2. P0：canary verification 未能强制单 profile 数据边界

### 现象

当前生产数据包含两个来源：`jiuyue` 和历史导入 `qishuo`。但 `scripts/predeploy-verify.sh` 的 `canary` 模式仅验证：

1. `core.sources` 中存在指定 `source_profile`；
2. 全局 events 非零；
3. 全局 failed jobs 为 0。

它不会拒绝非预期 source，也不会证明非零 events 来自指定 canary profile/instance。因此，已有其他来源的 events 可能掩盖 canary 实际未写入或写入错 source 的问题。

### 目标行为

为 canary verification 增加明确、可审计的 source-bound 验证策略：

1. 验证预期 source 同时匹配：
   - `source_profile=<expected-profile>`；
   - `source_instance=production-<expected-profile>`（或由 deployment state 中已记录值作为唯一预期）。
2. 验证该 source 自己至少有 1 条 event，并输出该 source 的 event count（不输出正文）。
3. 构建全量 source inventory：每个 source 输出仅 `source_profile`、`source_instance`、event count、首次/最近 event 时间。
4. 默认 fail-closed：canary 模式发现任何不在显式批准 allowlist 中的 source 时失败，且错误信息列出脱敏/无正文 inventory。
5. 支持**显式、受控**的既存来源 allowlist，不能默默接受其他来源。建议使用一个不含 secret、可审计的 env/config 字段，例如：
   - `AGENT_MEMORY_CANARY_ALLOWED_SOURCES=jiuyue:production-jiuyue,qishuo:hermes-session-export`
   - 或独立 JSON manifest（存放 production runtime，但生成/修改不应由本任务执行）。
6. 对 allowlist 中的历史来源仍需明确区分：它不能计入 canary profile 的 event gate。
7. `DEPLOYMENT-STATE.json` 中记录（不含秘密）：
   - canary expected profile / instance；
   - observed source inventory 的 hash；
   - source-bound canary event count；
   - verification timestamp。

### 安全要求

- 所有 SQL 查询必须参数安全；不要把未验证 profile 值直接拼接进 SQL。
- 当前 `f3f925c` 已修复 `psql -c` 下 `:'expected_profile'` 不展开的问题。保留该修复，并把 profile / source instance 的合法字符约束与 SQL 参数方案做成可测试的统一实现。
- 不能把 evidence payload、session ID、token、Vault 内容输出到 stdout、状态文件或测试快照。

### 验收测试

至少覆盖：

1. 空库 canary 验证失败；
2. 仅 `jiuyue:production-jiuyue` source 且该 source 有 event 时通过；
3. `jiuyue` source 存在但只有其他 profile 有 event 时失败；
4. 发现 `qishuo:hermes-session-export` 而未列入 allowlist 时失败，错误含无正文 inventory；
5. 该 qishuo source 明确列入 allowlist 后，jiuyue 自己仍有 event 时通过；
6. allowlist 中的 qishuo events 不能让“jiuyue 无 event”的 case 通过；
7. profile/source instance 中的非法字符失败关闭；
8. 状态文件写入只包含允许的无敏感字段。

## 3. P0：部署运行版本与当前运维源码可漂移

### 现象

运行容器 image revision 为 `c222a86c…`，但运行容器的 Compose working directory 是仓库工作树，当前 `main` 已为 `f3f925c…`。当前容器没有自动更新，这是正确的；但后续执行 `production-verify.sh`、备份、停止或升级脚本会使用已经变化的工作树脚本。

这会使“固定到已验证 commit”只覆盖运行镜像，不覆盖运行操作脚本/Compose 解释。

### 目标行为

让 production runtime 操作具有可验证的版本绑定：

1. `init-production-env.sh` 或受控的打包步骤生成一个只读/受限权限的 deployment manifest，至少记录：
   - Git full revision；
   - `VERSION`；
   - `compose.yaml`、`compose.production.yaml`、关键 production scripts 的 SHA-256；
   - API/worker/migrate image ID 与 OCI revision。
2. `production-up.sh`、`production-verify.sh`、`production-backup.sh`、`production-stop.sh`、`production-promote.sh` 在生产运行模式启动前：
   - 对比当前源码 revision / 文件 hash 与 deployment manifest；
   - 不匹配时 fail-closed，并提示从记录的部署 revision checkout 或使用正式发布包；
   - 不得因此自动 checkout、git pull、rebuild 或更新容器。
3. 设计一个显式的、单独批准的“运行版本升级/重新绑定”流程；它只能在新 release Gate、备份恢复和人工批准后执行。
4. 生产 Compose invocation 应明确来自固定 deployment bundle 或经 hash 验证的 checkout，而不是隐式相信 mutable `main`。

### 验收测试

1. 同 revision / 同 hash 时 production verify 可以继续；
2. 修改任何关键 Compose/production script 后，verify/backup/promote fail-closed；
3. 只有非运行无关文档变更时，定义并测试期望行为（建议允许纯 docs 变更，但以明确 allowlist 实现）；
4. 错误输出只包含 revision/path/hash，不包含环境变量与 secret；
5. 新流程不破坏 release 环境、开发环境或现有生产状态文件兼容性。

## 4. P1：canary 前的备份新鲜度可见性不足

### 现象

最新已恢复验证的备份时间早于 canary 开始时间。`production-promote.sh` 会在晋级前创建新备份并检查它晚于 canary 开始时间，因此晋级最终会失败关闭；但常规 `production-verify.sh runtime` 不会突出该风险。

### 目标行为

1. `production-verify.sh canary` 增加只读 freshness 结果：
   - 最新 verified backup 是否晚于 `canary_started_at`；
   - latest backup 的 path / manifest hash / verified timestamp（均无 secret）。
2. 设计为：
   - `runtime` 模式：警告/显式状态字段；
   - `canary` 与 `promote` 模式：默认 fail-closed，或必须提供明确的 `--allow-stale-backup-for-observation` 仅用于尚未开始晋级的观察阶段。
3. 不在 verify 中自动生成备份；备份是有状态操作，必须仍由 `production-backup.sh` 单独完成。

### 验收测试

- latest backup 早于 canary start：canary 检查按设计失败/明确告警；
- latest backup 晚于 canary start：通过；
- 缺失 backup state：失败关闭；
- 不修改现有 backup、state 或生产数据。

## 5. P1：运行态数据来源治理与人工决策接口

这是**不应由 Codex 自动执行数据变更**的问题，但需要补齐只读诊断与受控治理入口。

### 需要交付

1. 增加只读 `production-source-inventory` 脚本/子命令：
   - 输入 production env / state path；
   - 输出 source profile、source instance、event/fact/vault count、first/last timestamps、allowlist status；
   - 默认不输出 payload、session ID 或任何密钥；
   - 支持 `--json`，适合作为交接包。
2. 文档明确三种人工决策路径：
   - **保留**：把已有 qishuo 历史数据明确批准为 canary allowlist 的既存来源；
   - **隔离/休眠**：走已有审计治理流程，不物理删除；
   - **重新初始化**：仅在用户明确批准、完成备份和恢复验证后，创建全新生产资产；不得用脚本默认执行。
3. 不实现“根据 profile 批量 delete/purge”的便利命令。

## 6. 非本仓库修复范围：Hermes gateway stale definition

`hermes --profile jiuyue gateway status` 提示：

```text
Service definition is stale relative to the current Hermes install
Run: hermes gateway start
```

这属于 Hermes profile / launchd 运维变更，不在本仓库自动修复范围内。请只在文档或运行报告中记录为独立待批准项目；不要在本任务中执行 `hermes gateway start` 或修改 `jiuyue`。

## 7. 交付要求

1. 从当前远程 `main` 新建独立分支，例如 `fix/production-canary-boundaries`；
2. 先阅读并遵守：
   - `docs/handoff.md`；
   - `docs/V1.0-生产候选接入与原地晋级手册.md`；
   - `docs/V1.0-上线前Review报告.md`；
   - `docs/跨主机开发与交接标准.md`；
3. 提交源码、测试、文档；不提交环境文件、数据库、备份、日志或 secrets；
4. 至少执行：

```bash
bash scripts/handoff-check.sh
```

5. 容器/运行态相关变更必须在独立 release runtime 完成隔离 Gate；禁止使用：
   - `/private/tmp` 既有演练目录；
   - `$HOME/.local/share/agent-memory/production`；
   - 现有 Docker production project/network/data；
   - 旧 Vault key、旧测试数据库、旧模型 key。
6. 最终报告必须包含：
   - 分支、完整 commit SHA；
   - 变更文件；
   - 执行的完整命令、退出码和失败修复过程；
   - 测试 / release Gate 结果；
   - 是否写入真实数据或连接具体 Hermes profile（预期：否）；
   - 若发现阻断项，附脱敏 `DEPLOYMENT-STATE.json`、容器状态、相关日志、端口/网段/运行目录；
   - 不得回传任何 env、token、key、密码或 Vault root key。

## 8. 完成定义

- canary 验证能证明指定 profile + instance 自己写入了 evidence；
- 未批准的其他来源会 fail-closed，而不是被全局计数掩盖；
- 运行脚本与部署 revision 可验证绑定，源码变动不会静默影响生产操作；
- canary 观察能明确提示备份是否覆盖 canary 数据；
- 所有新增行为有自动化测试和隔离验证；
- 当前生产运行态、数据与 Hermes profile 未被本修复任务修改。
