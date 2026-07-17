# Agent Memory for Hermes

Agent Memory 是本地优先、证据驱动的 Hermes 长期记忆系统。`1.0.0-rc.1` 测试 release 支持 Hermes 多 profile 共享身份、只读原始证据、事实/情节/长期脉络、三路召回、生命周期治理、确定性互动状态、整理报告、星图和独立加密 Vault。

这是供接入测试的候选版本，不应成为真实凭据或重要数据的唯一副本。需求与实现边界见 [`docs/V1.0-项目需求文档.md`](docs/V1.0-项目需求文档.md)，逐项验证状态见 [`docs/V1.0-release验收矩阵.md`](docs/V1.0-release验收矩阵.md)。

## 环境要求

- macOS ARM64（当前验证平台）或兼容的 Docker 主机；
- Docker Desktop / Docker Compose v2；
- Python 3.12、[uv](https://docs.astral.sh/uv/) 和 Node.js 24（仅开发与 release-check 需要）；
- 本机 Hermes Agent 源码运行时（正式 Provider 验收需要）。

## 安装与启动

以下操作只在项目目录创建 `.env`、`secrets/`、`data/` 和 `backups/`：

```bash
bash scripts/init-local.sh
docker compose --env-file .env up -d --build
docker compose --env-file .env ps
curl --fail http://127.0.0.1:7788/health/ready
```

初始化脚本只显示一次星图登录密码。星图默认位于 `http://127.0.0.1:7788/`；API 仅绑定 localhost。`.env.example` 含公开测试值，只能用于自动测试，不能代替初始化。

## Hermes 接入

先确认服务健康，再安装托管插件：

```bash
python3 scripts/hermes-plugin.py install --hermes-home "${HERMES_HOME:-$HOME/.hermes}"
hermes memory setup agent_memory
```

Provider 至少需要 `AGENT_MEMORY_API_URL`、`AGENT_MEMORY_SERVICE_TOKEN` 和共享 `AGENT_MEMORY_NAMESPACE`。不同 Hermes profile 使用同一 namespace，但保留各自 `source_profile`。升级和卸载命令：

```bash
python3 scripts/hermes-plugin.py upgrade --hermes-home "${HERMES_HOME:-$HOME/.hermes}"
python3 scripts/hermes-plugin.py uninstall --hermes-home "${HERMES_HOME:-$HOME/.hermes}"
```

脚本只覆盖带 `.agent-memory-managed` 标记的插件目录，不会删除同名非托管目录。完整说明见 [`integrations/hermes/README.md`](integrations/hermes/README.md)。

### 导入既有 Hermes 对话

历史效果验收优先使用 Hermes 官方完整 JSONL 导出；Markdown、仅用户提示词和 trace
格式不作为记忆证据导入。先由 Hermes 强制脱敏并在本地预览清单：

```bash
hermes sessions export data/imports/raw/hermes.jsonl \
  --format jsonl --redact --newer-than 30d --min-messages 2
uv run agent-memory-import-hermes data/imports/raw/hermes.jsonl \
  --profile personal
```

预览只输出会话、回合、事件、时间范围、敏感命中数和 SHA-256，不输出对话正文。
确认后按需启动隔离的导入 API/worker，并使用预览中的 SHA-256 执行：

```bash
docker compose --env-file .env --profile import up -d import-api import-worker
set -a; source .env; set +a
uv run agent-memory-import-hermes data/imports/raw/hermes.jsonl \
  --profile personal --apply --confirm-sha256 '<preview-sha256>'
docker compose --env-file .env --profile import stop import-api import-worker
```

暂存星图位于 `http://127.0.0.1:7790/`，不会出现在主命名空间。只有暂存质量通过后，
才允许把同一份已确认文件显式导入 `hermes:user-primary`；完整边界、失败恢复和验收项见
[`docs/V1.0-Hermes历史对话导入设计.md`](docs/V1.0-Hermes历史对话导入设计.md)。

## 配置与模型

保留、休眠、忘记、当前事实 TTL、报告周期、端口和 worker 租约均在 `.env` 配置，默认值及含义见 [`docs/V1.0-运行与配置设计.md`](docs/V1.0-运行与配置设计.md)。互动状态的轴名称、范围、初始值、启停、漂移、阈值和 profile override 在星图“当前状态”页面持久化管理。

模型默认关闭，系统仍使用本地确定性向量完成召回。启用外部 API 或本地 OpenAI-compatible 服务时设置：

```dotenv
AGENT_MEMORY_MODEL_ENABLED=true
AGENT_MEMORY_MODEL_NAME=openai/your-model
AGENT_MEMORY_MODEL_API_BASE=http://your-local-endpoint/v1
AGENT_MEMORY_MODEL_API_KEY=your-key
```

启用后，核心 worker 先完成不依赖模型的确定性投影，独立 model-worker 再异步验证候选；
API key 只注入 model-worker，证据先脱敏再进入任何模型请求。

## 升级

升级前必须备份数据库和 Vault 根密钥，然后拉取/切换目标版本并重建：

```bash
backup_dir="$(bash scripts/backup.sh .env)"
cp secrets/vault_root_key "$backup_dir/vault_root_key.separate-copy"
docker compose --env-file .env build
docker compose --env-file .env up -d
curl --fail http://127.0.0.1:7788/health/ready
```

`migrate` 容器必须成功退出后 API/worker 才会启动。不要修改已经执行过的迁移文件，也不要跳过版本升级路径。

### 历史派生记忆敏感值净化

质量报告若显示 `raw_sensitive_facts > 0`，先备份，再执行只读预览。预览只输出数量、规则类型和
确认 SHA，不输出记忆正文：

```bash
docker compose --env-file .env run --rm --no-deps api agent-memory-sanitize-derived
```

核对备份和预览后，使用当次输出的 SHA 二次确认。源 evidence 不修改；事实、情节、脉络和检索投影
仅替换敏感片段，写入安全审计并触发派生层重建：

```bash
docker compose --env-file .env run --rm --no-deps api \
  agent-memory-sanitize-derived --apply --confirm-sha256 '<preview-sha256>'
```

预览与执行之间数据变化会使 SHA 失效，必须重新预览。该命令只允许处理当前容器配置的 namespace。

## 备份与恢复验证

```bash
backup_dir="$(bash scripts/backup.sh .env)"
bash scripts/verify-restore.sh "$backup_dir" .env
```

备份包含 PostgreSQL 自包含 dump、Compose、运行配置、锁文件、版本和校验和。`secrets/vault_root_key` 必须通过独立安全介质保存，不能只放在数据库备份旁；丢失后 Vault 密文不可恢复。恢复脚本会创建临时空数据库，比较 evidence、fact、episode、arc、job、Vault、状态和报告计数，并实际解密 Vault 后自动清理临时库。

## 测试与发布检查

自动 API、真实 Hermes Provider 和 Worker 停机回归必须通过
`scripts/verify-isolated-regression.sh` 使用 `127.0.0.1:7789` 与
`hermes:automated-tests`。测试代码和故障脚本会拒绝
`hermes:user-primary` 等非自动化 namespace；普通单元测试不会连接真实 Provider。
历史自动化记录默认不进入星图投影，但可通过“显示测试、内部与低价值问询记录”查看。
合成数据仍用于幂等、安全和故障回归；真实脱敏 Hermes 历史用于分类、关联、召回和星图效果验收，
两者不能互相替代。

开发回归：

```bash
uv sync --frozen --extra dev --extra migrations
uv run ruff check src integrations tests migrations
uv run pytest -q
npm --prefix frontend ci
npm --prefix frontend run build
```

全量候选版本检查会构建版本化镜像、运行正式 API/Hermes 集成、worker 故障恢复以及备份恢复演练：

```bash
HERMES_AGENT_ROOT="${HERMES_AGENT_ROOT:-$HOME/.hermes/hermes-agent}" \
  bash scripts/release-check.sh .env
```

## 常见故障

- `migrate` 退出非零：运行 `docker compose --env-file .env logs migrate`，不要手工把 Alembic 版本标成最新。
- API 未就绪：先检查 `postgres` health、`migrate` exit code，再看 `api` 日志；`docker compose up -d` 不代表 healthcheck 已通过。
- Hermes 回合正常但无记忆：确认 Provider token/namespace、API 地址和 worker 是否在线；API 故障按设计 fail-soft。
- worker 任务积压：检查 `worker` 日志与 `ops.jobs.last_error_code`；过期租约会自动取回。
- Vault 无法解密：确认恢复的是与数据库同一时期的 `vault_root_key`，不要创建新 key 覆盖旧 key。
- 星图登录失败：重新运行初始化不会覆盖现有 `.env`；需要按运行文档显式生成并替换密码 hash。

## 安全边界

原始证据不可编辑；更正创建替代版本。普通召回不返回 forgotten，只有显式主题检索允许唤醒；isolated 和 purged 永不召回。Vault 明文不进入星图、检索投影或普通模型上下文，只有用户创建的未过期 profile grant 可授权读取。测试 release 仍建议只在可信本机使用，不向局域网或公网暴露 API。
