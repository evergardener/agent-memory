# ADR-002：阶段 C 社区算法结论

状态：Accepted（2026-07-19）

## 决策

V1 选择 `weighted-core-expansion-v1` 作为关系星系默认算法：先过滤有证据的类型化关系，再按关系族
形成确定性核心连通分量；同一 canonical entity 可在不同关系族中获得多个 membership。成员角色由
关系类型的端点语义投票得到，所有 membership 保存 relation/fact/evidence 解释链。

`threshold-components-v1` 保留为诊断基线；`greedy-modularity-v1` 保留为分区对照。二者在三个互不
重叠的真实金标上同样达到 Pair F1 1.0，但在共享实体 fixture 中都无法同时保留两个社区归属，因此
不满足产品已经确认的软归属要求。

V1 不引入 Leiden：当前环境没有 `igraph`/`leidenalg`，引入原生依赖会扩大 ARM64 构建和升级面；
更重要的是标准 Leiden 输出互斥分区，仍需额外的次级成员层才能满足重叠语义。未来图规模或社区
质量证明其收益后，可把 Leiden 用于核心分区，但不能替代类型化关系、次级 membership 和人工治理。

## 依据

[`V1.0-阶段C社区算法评测报告.md`](V1.0-阶段C社区算法评测报告.md) 使用 3 个已接受金标社区、
6 条可追溯类型化关系，以及别名自环、内部组件、Tailscale 共同出现、二节点关系、重叠社区和增量
证据 fixture。选中算法通过：

- 金标成员与角色精确恢复；
- 负例零社区；
- 输入乱序确定性；
- 非破坏性证据增长不改变稳定社区 ID；
- 同一 Hindsight 在数据库与观测两个社区保持唯一实体、两个 membership；
- 每个社区都有 relation 与 evidence 哈希解释。

## 后果

- 社区边界依赖关系类型质量，不允许把原始共同出现直接送入算法；
- `relation_type -> family/endpoint role` 映射必须版本化并接受回归测试；
- 稳定社区 ID 由算法关系族和规范成员集合生成，证据计数增长不改变 ID；
- 人工固定、排除、命名和布局优先于自动结果，且不被重建任务覆盖；
- C2 持久化必须支持同一实体多 membership、关系证据链和算法版本。
