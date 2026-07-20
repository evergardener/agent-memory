# Agent Memory 接手交接

> 更新时间：2026-07-20。目标：新接手者无需依赖聊天记录即可在不影响生产 Hermes 的前提下继续工作。

## 1. 一句话状态

V1 计划内核心能力和阶段 C 关系星系已完成工程实现并通过当前小样本验收；最新不可变发布仍是
`1.0.0-rc.6`，阶段 C 尚未进入正式 namespace、生产 Hermes 或新 release。

## 2. 必读顺序

1. [`V1.0-项目需求文档.md`](V1.0-项目需求文档.md)：产品边界；
2. [`V1.0-总体架构设计.md`](V1.0-总体架构设计.md)：服务和数据流；
3. [`V1.0-开发部署与运维手册.md`](V1.0-开发部署与运维手册.md)：实际运行方式；
4. [`V1.0-正式迁移与灰度发布方案.md`](V1.0-正式迁移与灰度发布方案.md)：下一阶段；
5. [`V1.0-阶段C实施验证报告.md`](V1.0-阶段C实施验证报告.md)：已验证证据；
6. [`V1.0-release验收矩阵.md`](V1.0-release验收矩阵.md)：逐项 Gate。

## 3. 代码与版本基线

- 分支：`main`；
- 阶段 C 功能提交：`935faf8 feat: complete phase C relation galaxies`；
- 阶段 C 验收记录：`02656de docs: record phase C acceptance`；
- `VERSION` / Python package：`1.0.0-rc.6` / `1.0.0rc6`；
- 下一建议候选：`1.0.0-rc.7`，尚未改版本、打 tag 或打包；
- 工作区中的 `data/`、`backups/`、`secrets/`、`release-artifacts/` 全部是 Git 忽略的本地资产。

## 4. 当前运行状态

2026-07-20 只读检查：

| 入口/组件 | 状态 | 说明 |
| --- | --- | --- |
| `127.0.0.1:7788` | 标准 API 容器 healthy | rc.6 镜像，正式 Compose project |
| `127.0.0.1:7790` | import API healthy | 历史导入 staging |
| `127.0.0.1:7796` | 阶段 C API | 临时验收容器 |
| `127.0.0.1:7797` | 阶段 C UI | 临时前端对比容器 |
| `127.0.0.1:7798` | 阶段 C shadow API/UI | 最终阶段 C 验收入口 |
| PostgreSQL | healthy | 项目 `data/postgres`，不发布宿主端口 |

根目录 `.env` 当前不存在；现有容器仍运行，但任何 Compose 重建前必须恢复正确 `.env`。最近的候选
运行配置备份为 `backups/20260719T043257Z/runtime.env`，使用前必须验证该目录 SHA 和时间点匹配。
`secrets/vault_root_key` 存在，禁止重新生成或提交。

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
- 不因根目录 `.env` 缺失而运行 `scripts/init-local.sh`；
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
- 脱敏器 v4、影子库重建、API/Hermes 只读召回、幂等、备份恢复和 Vault 解密验证；
- 用户接受当前数据规模下的阶段 C 验收结论。

## 8. 下一任务队列

| 优先级 | 任务 | 完成标准 |
| --- | --- | --- |
| P0 | 恢复 `.env` 管理面 | SHA/时间点匹配，Compose config 与现有容器配置一致，不泄露密钥 |
| P0 | 隔离 release-check 数据和网段 | 新 release project 不接触在线 `data/postgres`，可与在线栈并行或在受控维护窗口运行 |
| P0 | 设计正式关系提升路径 | 固定计划、双 SHA、审计、失败关闭；不复制影子行 |
| P0 | 生成并验证 rc.7 | 全量 Gate、恢复、Vault、Hermes、前端、ARM64 和安全金丝雀通过 |
| P1 | 单 Hermes profile 灰度 | 72 小时观察，离线 fail-soft，无数据/安全回归 |
| P1 | 修正旧图元数据 | `/v1/graph` 的 `community_projection` 仍标为 `phase-c-pending`，应与新 Galaxy API 状态统一并补回归 |
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

全量 `scripts/release-check.sh` 当前只能在满足发布隔离门禁后执行，且传入 namespace 必须是
`hermes:automated-tests*`。不要把生产 `.env` 直接交给该脚本。

## 10. 交接完成标准

接手者能说明版本与工作树的区别，能找到正确备份但不查看密钥，能安全检查两套入口，能运行本地回归，
并知道正式迁移必须先解决发布隔离与关系提升两项 P0。达到这些条件即可继续开发，无需重新分析需求。
