# Changelog

## Unreleased

- 准备 `1.0.0-rc.7`：完成阶段 C 类型化关系星系、可重叠社区、人工治理、证据追溯及主/子宇宙联动；
- 发布 Gate 改为独立 Docker project、镜像前缀、data/backup/Vault 路径、端口与网段，并加入失败关闭预检、只读根文件系统、最小 capability、readiness 与 OCI version/revision 校验；
- 正式已审关系提升增加计划 SHA、namespace 原文、固定确认短语、change ID、备份清单 SHA 和逐文件校验；默认仍拒绝生产写入；
- 新增 `0014_audit_event_order`，修复同事务内多次星系治理后撤销顺序不确定；补齐阶段 C 数据库/API 发布回归；
- 收紧安全边界：原始 turn ingest 仅允许 service token，Hermes Provider 仅允许本机 loopback HTTP API；
- 前端构建改为无增量的显式 app/node typecheck，避免复用 `noEmit` build state 导致 Gate 挂起；
- 完成全新隔离环境的前后端、API/Hermes、故障恢复、社区投影、备份恢复与 Vault 解密 Gate；未修改生产 Hermes 或正式 namespace。
- 新增生产预部署运行层：全新容器/data/Vault/namespace、动态 Docker 网段与端口检查、空数据 Gate、运行状态清单、备份恢复、单 profile canary 环境生成和无损停止；默认不连接 Hermes、不启动模型。

- 完成 `weighted-core-expansion-v1` 关系社区、可重叠成员、关系证据链、人工治理/撤销、布局偏好和主/子宇宙 UI；阶段 C 代码提交为 `935faf8`，当前小样本验收状态提交为 `02656de`；
- 新增 `0013_relation_galaxies`、社区评测与投影、已审关系影子写入器，以及 API/数据库/恢复回归；
- 修复通用 provider key 脱敏边界并将规则升级至 v4；影子库由只读备份重新生成，未修改生产 Hermes；
- 增加开发部署、正式迁移/灰度发布和接手交接文档；阶段 C 尚未发布为新版本，也未启用正式 namespace。
- 新增只读 `agent-memory-community-report`：从脱敏星图 API 评估实体覆盖、关系边支撑、单事实 clique 膨胀和候选社区最低门槛，不写数据库、不调用模型；
- 生成首份主空间与真实 Hermes staging 基线报告，当前结论为 `BLOCKED_INPUT_COVERAGE`，阶段 C 不应通过降低防单体门槛继续。
- Hermes 固定选择增加 `--exclude-automated`，候选复核再次防御性排除 cron session，避免自动任务注入内容污染实体；
- 新增本地只读 `agent-memory-candidate-review` 和阶段 C 数据选择审计：84 个导出 session 中仅 5 个真实交互 session，保守抽取后为 6 个实体、3 条二元关系、0 个合格社区，需补充代表性交互数据。
- 新增只读历史备份规范化器、互斥选择批次和金标草案验证器；校验七天前备份完整性后，以 50 + 45 两批覆盖 95 个非 cron session，本地再次脱敏且不导出 system prompt/reasoning；
- 候选复核排除上下文压缩、提示包装、非直接环境观察工具结果、代码标识和多实体 clique；当前 3 个社区草案及 core/bridge/satellite 软归属角色结构通过但保持 `REVIEW_REQUIRED`，等待用户确认后才能成为阶段 C 金标。

## 1.0.0-rc.6 — 2026-07-19

- 图 API 升级为 `planetary-v2`：天体节点只允许 Subject 恒星与 canonical entity 行星，事实、episode、arc 和 Vault 条目均退出天体集合；
- 将长期、阶段、当前、环境观察、生命周期、活跃度、敏感性、profile 和更新时间实现为可组合、只读的观察镜片，切换镜片不改变行星稳定 ID；
- 依据共同支撑事实聚合行星—行星关系边，边保留事实 ID、强度和支撑数，不因画面接近推断关系；
- episode/arc 分别投影为临时星座与长期星流，支持成员高亮、关系光点、支撑事实时间线和证据追溯；
- Vault 只投影脱敏保护标记和引用目标，星图不加载受保护明文；
- UI 移除长期/阶段/当前/情节/脉络/Vault 固定星系，增加实体类型行星、约 5px 缓慢漂移、悬停/选中静止、动静开关及 Canvas 外键盘入口；
- 在真实隔离 Hermes staging 数据上验证镜片切换不改变行星身份，并完成浏览器、API、Provider、故障恢复与发布门禁回归。

关系社区、重叠成员、人工星系治理和主/子星系缩放联动属于阶段 C，不在本版本完成范围。

## 1.0.0-rc.5 — 2026-07-19

- 新增稳定 Subject 身份层和 source 映射：本地用户与 Hermes profile 人格成为恒星，同 profile 多实例复用同一主体；
- 星图移除写死的 `core:user`/`core:hermes`，Subject 引用的 canonical entity 不再重复显示为行星，子星系不重复绘制主体恒星群；
- 增加 Subject 查询、名称/颜色编辑、来源人工映射与撤销 API，所有变更写入治理审计；
- 收紧实体候选与图投影策略，阻止来源 ID、时间戳、自动化测试标签和句子片段成为天体，同时保留底层只读证据；
- 主宇宙支持可读的多 profile 主体恒星群布局、Subject 详情和直接编辑入口；
- 发布门禁强制运行于 `hermes:automated-tests*`，干净隔离空间可安全创建 worker 恢复探针；
- 完成迁移前备份、空库升级/降级/再升级、真实 staging、浏览器、Hermes Provider、故障恢复、备份恢复和 Vault 解密验证。

阶段 B/C 的关系社区、观察镜片、星系成员与布局治理仍未实现，不属于本版本完成范围。

## 1.0.0-rc.4 — 2026-07-18

- 修复首页星系实体计数与进入子星系后可见数量不一致；
- 进入子星系不再错误复用原始节点类别过滤器，实体归属统一由事实—实体关系投影计算；
- 继续保持事实只作为关系与侧边陈述，不参与星体布局。

## 1.0.0-rc.3 — 2026-07-18

- 使用 OpenCode Go `deepseek-v4-flash` 对 RC2 候选空间的 28 个无敏感命中 turn 完成受 allowlist 约束的逐字原子抽取；
- 28/28 任务完成，22 个 turn 无合格事实，6 条候选经本地精确去重后保留 3 条设备状态，每条关联 2 份原始证据；
- 修正通用质量门禁把 `isolated`、`superseded` 事实计入晋升指标的问题；治理审计仍保留这些记录；
- 增加集成回归，确保 isolated 事实不会重新污染质量分类和有效事实总数。

## 1.0.0-rc.2 — 2026-07-18

第二个可接入测试候选版本：

- 星图重构为实体星体、User/Hermes 固定双星、宇宙/子星系缩放联动、低幅漂移与动静开关，并补齐事实和实体治理操作；
- 原子事实升级为证据逐字约束的 `atomic-verbatim-v2`，过滤自动化提示、指令、通知外壳、结构字段和非命名实体；
- 历史导入增加 20–50 session 的确定性分层选择、源文件与选择计划双 SHA 确认和防篡改审计；
- Hermes 历史导入在模型关闭时只进入只读证据层，禁止整句降级事实和伪实体污染召回/星图；
- 增加独立分页治理队列、质量门禁、实体合并/撤销/拆分、实体关系脱离与凭据 Vault 完整人工管理；
- 默认隐藏自动化、隔离 UAT 和历史测试来源，同时保留原始证据与治理审计。

已知限制：未经本地模型抽取或对固定数据范围明确授权的外部模型抽取，历史导入不会自动形成事实；自动门禁通过后仍需人工语义抽检才能晋升。

## 1.0.0-rc.1 — 2026-07-13

首个可接入测试候选版本：

- Hermes MemoryProvider 多 profile 共享 namespace、回合幂等写入、显式召回、来源追溯、更正、当前状态和 Vault 工具；
- 只读证据、规则脱敏、长期/阶段/当前/低价值分类，以及 evidence-linked episode/arc；
- 词法、本地语义和实体三路 RRF，普通/显式召回分离，forgotten 仅显式主题唤醒；
- isolated、forgotten、更正替代与二次确认永久清除；
- AES-GCM 信封加密 Vault、限时 profile grant、撤销和星图脱敏关联；
- 五轴确定性状态、完整参数治理、无副作用模拟、当前事实和跨 profile 连续性；
- PostgreSQL 任务租约、故障恢复、周期整理报告、版本化 Compose 和恢复演练。

已知限制：V1 只支持单用户和 Hermes；API 仅设计为 localhost；实体类型和复杂关系抽取仍采用保守规则；星图前端 bundle 尚未代码分割。
