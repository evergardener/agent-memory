import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  ConsolidationReport,
  GraphData,
  QualityReport,
  ReviewQueue,
  ReviewQueueItem,
  StateData,
  VaultEntry,
  VaultGrant
} from "./api";
import { StarMap } from "./StarMap";

const emptyGraph: GraphData = {
  projection: {
    version: "planetary-v2",
    community_projection: "phase-c-pending",
    active_lenses: {
      profiles: [], fact_types: [], lifecycle_states: [], activities: [], sensitivities: [], updated_after: null
    }
  },
  nodes: [],
  edges: [],
  facts: [],
  episodes: [],
  arcs: [],
  vault_markers: [],
  facets: { profiles: [], fact_types: [], lifecycle_states: [], activities: [], sensitivities: [] }
};
const emptyReviewQueue: ReviewQueue = {
  items: [],
  total: 0,
  limit: 25,
  offset: 0,
  profiles: []
};
const REVIEW_PAGE_SIZE = 25;
const ENTITY_TYPES = ["person", "agent", "project", "service", "location", "organization", "tool", "technology", "device", "concept", "event", "other"];
type GovernanceAction = "correct" | "forget" | "isolate" | "purge";
type EntityGovernanceAction =
  | { kind: "merge" }
  | { kind: "split" }
  | { kind: "attach" }
  | { kind: "detach"; factId: string; factName: string }
  | { kind: "unmerge"; sourceId: string; sourceName: string };

function splitIds(value = "") {
  return value.split("|").filter(Boolean);
}

function compactText(value = "", maximum = 96) {
  const compact = value.replace(/\s+/g, " ").trim();
  return compact.length <= maximum ? compact : `${compact.slice(0, maximum - 1)}…`;
}

function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    try {
      await api.login(password);
      onLogin();
    } catch {
      setError("密码不正确");
    }
  }
  return (
    <main className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <div className="mark">✦</div>
        <p className="eyebrow">LOCAL MEMORY SYSTEM</p>
        <h1>记忆星图</h1>
        <p className="muted">查看、追溯与治理 Hermes 的长期记忆</p>
        <input
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          placeholder="本地管理密码"
          autoFocus
        />
        {error && <p className="error">{error}</p>}
        <button type="submit">进入星图</button>
      </form>
    </main>
  );
}

export default function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [graph, setGraph] = useState<GraphData>(emptyGraph);
  const [vault, setVault] = useState<VaultEntry[]>([]);
  const [grants, setGrants] = useState<VaultGrant[]>([]);
  const [selected, setSelected] = useState<Record<string, string> | null>(null);
  const [trace, setTrace] = useState<Record<string, unknown> | null>(null);
  const [stateData, setStateData] = useState<StateData | null>(null);
  const [reports, setReports] = useState<ConsolidationReport[]>([]);
  const [qualityReport, setQualityReport] = useState<QualityReport | null>(null);
  const [reviewQueue, setReviewQueue] = useState<ReviewQueue>(emptyReviewQueue);
  const [reviewReason, setReviewReason] = useState<"all" | "candidate" | "untrusted_tool">("all");
  const [reviewProfile, setReviewProfile] = useState("all");
  const [reviewOffset, setReviewOffset] = useState(0);
  const [tab, setTab] = useState<"map" | "state" | "reports" | "vault">("map");
  const [message, setMessage] = useState("");
  const [search, setSearch] = useState("");
  const [profileFilter, setProfileFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [stateFilter, setStateFilter] = useState("all");
  const [timeFilter, setTimeFilter] = useState("all");
  const [activityFilter, setActivityFilter] = useState("all");
  const [sensitivityFilter, setSensitivityFilter] = useState("all");
  const [showNoise, setShowNoise] = useState(false);
  const [showStardust, setShowStardust] = useState(false);
  const [showNotebook, setShowNotebook] = useState(true);
  const [motionEnabled, setMotionEnabled] = useState(() => {
    const saved = window.localStorage.getItem("agent-memory:entity-motion");
    if (saved) return saved === "dynamic";
    return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  });
  const [governanceAction, setGovernanceAction] = useState<GovernanceAction | null>(null);
  const [governanceStatement, setGovernanceStatement] = useState("");
  const [governanceReason, setGovernanceReason] = useState("");
  const [purgeConfirmation, setPurgeConfirmation] = useState("");
  const [governanceBusy, setGovernanceBusy] = useState(false);
  const [entityGovernance, setEntityGovernance] = useState<EntityGovernanceAction | null>(null);
  const [entityReason, setEntityReason] = useState("");
  const [mergeTarget, setMergeTarget] = useState("");
  const [splitName, setSplitName] = useState("");
  const [splitType, setSplitType] = useState("other");
  const [splitFactIds, setSplitFactIds] = useState<string[]>([]);
  const [attachFactId, setAttachFactId] = useState("");
  const [entityBusy, setEntityBusy] = useState(false);
  const [subjectEditor, setSubjectEditor] = useState(false);
  const [subjectName, setSubjectName] = useState("");
  const [subjectColor, setSubjectColor] = useState("#91cfb2");
  const [subjectReason, setSubjectReason] = useState("");
  const [subjectBusy, setSubjectBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      await api.configure();
      const [graphData, vaultEntries, activeGrants, status, reportItems, quality] = await Promise.all([
        api.graph(),
        api.vaultEntries(),
        api.vaultGrants(),
        api.state(),
        api.reports(),
        api.qualityReport()
      ]);
      setGraph(graphData);
      setVault(vaultEntries);
      setGrants(activeGrants);
      setStateData(status);
      setReports(reportItems);
      setQualityReport(quality);
      setAuthenticated(true);
    } catch (error) {
      if ((error as Error & { status?: number }).status === 401) setAuthenticated(false);
      else setMessage(String(error));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const loadReviewQueue = useCallback(async () => {
    if (!authenticated) return;
    try {
      const result = await api.reviewQueue({
        reason: reviewReason,
        sourceProfile: reviewProfile === "all" ? undefined : reviewProfile,
        limit: REVIEW_PAGE_SIZE,
        offset: reviewOffset
      });
      if (result.items.length === 0 && result.total > 0 && reviewOffset > 0) {
        setReviewOffset(Math.floor((result.total - 1) / REVIEW_PAGE_SIZE) * REVIEW_PAGE_SIZE);
        return;
      }
      setReviewQueue(result);
    } catch (error) {
      setMessage(`治理队列加载失败：${String(error)}`);
    }
  }, [authenticated, reviewOffset, reviewProfile, reviewReason]);

  useEffect(() => {
    loadReviewQueue();
  }, [loadReviewQueue]);

  useEffect(() => {
    window.localStorage.setItem("agent-memory:entity-motion", motionEnabled ? "dynamic" : "static");
  }, [motionEnabled]);

  const counts = useMemo(
    () => ({
      facts: graph.facts.length,
      entities: graph.nodes.filter((node) => node.data.kind === "entity").length,
      protected: graph.vault_markers.length
    }),
    [graph]
  );

  const profiles = graph.facets.profiles;

  const entityOptions = useMemo(
    () => graph.nodes
      .filter((node) => node.data.kind === "entity" && node.data.record_id !== selected?.record_id)
      .map((node) => node.data)
      .sort((left, right) => (left.label || "").localeCompare(right.label || "")),
    [graph, selected?.record_id]
  );

  const selectedEntityFacts = useMemo(() => {
    if (selected?.kind !== "entity") return [];
    return graph.facts
      .filter((fact) => splitIds(fact.data.entity_ids).includes(selected.id))
      .map((node) => node.data);
  }, [graph, selected]);

  const availableEntityFacts = useMemo(() => {
    const attached = new Set(selectedEntityFacts.map((fact) => fact.record_id));
    return graph.facts
      .map((node) => node.data)
      .filter((data) => data.kind === "fact" && !attached.has(data.record_id))
      .sort((left, right) => (right.updated_at || "").localeCompare(left.updated_at || ""));
  }, [graph, selectedEntityFacts]);

  const selectedMergedAliases = useMemo(() => {
    if (selected?.kind !== "entity" || !selected.merged_aliases) return [] as Array<{ id: string; name: string }>;
    try {
      return JSON.parse(selected.merged_aliases) as Array<{ id: string; name: string }>;
    } catch {
      return [] as Array<{ id: string; name: string }>;
    }
  }, [selected]);

  const filteredGraph = useMemo(() => {
    const normalizedSearch = search.trim().toLocaleLowerCase();
    const visibleByPolicy = (data: Record<string, string>) => {
      if (stateFilter !== "all" && data.state === stateFilter) return true;
      if (
        stateFilter === "all" &&
        ["isolated", "forgotten", "superseded", "purge_requested"].includes(data.state || "")
      ) return false;
      return showNoise || !["automated", "internal", "interaction", "untrusted"].includes(
        data.visibility || "normal"
      );
    };
    const allowedNodes = graph.nodes.filter((node) => visibleByPolicy(node.data));
    const allowedNodeIds = new Set(allowedNodes.map((node) => node.data.id));
    const temporalCutoff = timeFilter === "all"
      ? null
      : Date.now() - Number(timeFilter) * 86_400_000;
    const typeIsFactLens = ["long_term", "stage", "current", "observed"].includes(typeFilter);
    const matchesFactAxes = (data: Record<string, string>) => {
      if (profileFilter !== "all" && data.source_profile !== profileFilter) return false;
      if (stateFilter !== "all" && data.state !== stateFilter) return false;
      if (activityFilter !== "all" && data.activity !== activityFilter) return false;
      if (sensitivityFilter !== "all" && sensitivityFilter !== "protected" && data.sensitivity !== sensitivityFilter) return false;
      if (temporalCutoff !== null) {
        const updatedAt = Date.parse(data.updated_at || "");
        if (!Number.isFinite(updatedAt) || updatedAt < temporalCutoff) return false;
      }
      return true;
    };
    const matchingFacts = graph.facts.filter((fact) => {
      const data = fact.data;
      if (!visibleByPolicy(data)) return false;
      if (
        normalizedSearch &&
        !`${data.label || ""} ${data.source_profile || ""} ${data.fact_type || ""}`
          .toLocaleLowerCase()
          .includes(normalizedSearch)
      ) return false;
      if (typeIsFactLens && data.fact_type !== typeFilter) return false;
      return matchesFactAxes(data);
    });
    const matchingFactIds = new Set(matchingFacts.map((fact) => fact.data.id));

    const overlayMatches = (data: Record<string, string>) => {
      if (!visibleByPolicy(data)) return false;
      if (normalizedSearch && !`${data.label || ""} ${data.summary || ""}`.toLocaleLowerCase().includes(normalizedSearch)) return false;
      if (temporalCutoff !== null) {
        const updatedAt = Date.parse(data.updated_at || "");
        if (!Number.isFinite(updatedAt) || updatedAt < temporalCutoff) return false;
      }
      const factIds = splitIds(data.fact_ids);
      return profileFilter === "all" && stateFilter === "all" && activityFilter === "all" && sensitivityFilter === "all"
        ? true
        : factIds.some((factId) => matchingFactIds.has(factId));
    };
    const episodes = graph.episodes.filter((item) => overlayMatches(item.data));
    const arcs = graph.arcs.filter((item) => overlayMatches(item.data));
    const vaultMarkers = graph.vault_markers.filter((item) => visibleByPolicy(item.data));
    const scoped =
      Boolean(normalizedSearch) ||
      profileFilter !== "all" ||
      typeFilter !== "all" ||
      stateFilter !== "all" ||
      timeFilter !== "all" ||
      activityFilter !== "all" ||
      sensitivityFilter !== "all";
    const visiblePlanetIds = new Set<string>();
    if (!scoped) {
      allowedNodes.filter((node) => node.data.kind === "entity").forEach((node) => visiblePlanetIds.add(node.data.id));
    } else if (typeFilter === "episode") {
      episodes.forEach((item) => splitIds(item.data.entity_ids).forEach((id) => visiblePlanetIds.add(id)));
    } else if (typeFilter === "arc") {
      arcs.forEach((item) => splitIds(item.data.entity_ids).forEach((id) => visiblePlanetIds.add(id)));
    } else if (typeFilter === "vault" || sensitivityFilter === "protected") {
      vaultMarkers.forEach((item) => splitIds(item.data.target_ids).forEach((id) => visiblePlanetIds.add(id)));
    } else if (typeFilter === "entity") {
      allowedNodes.filter((node) => node.data.kind === "entity" && (!normalizedSearch || `${node.data.label} ${node.data.entity_type}`.toLocaleLowerCase().includes(normalizedSearch)))
        .forEach((node) => visiblePlanetIds.add(node.data.id));
    } else {
      matchingFacts.forEach((fact) => splitIds(fact.data.entity_ids).forEach((id) => visiblePlanetIds.add(id)));
      allowedNodes.filter((node) => node.data.kind === "entity" && normalizedSearch && `${node.data.label} ${node.data.entity_type}`.toLocaleLowerCase().includes(normalizedSearch))
        .forEach((node) => visiblePlanetIds.add(node.data.id));
    }

    const relevantFactIds = new Set(
      ["episode", "arc", "vault"].includes(typeFilter)
        ? []
        : matchingFacts.map((fact) => fact.data.id)
    );
    if (typeFilter === "episode") episodes.forEach((item) => splitIds(item.data.fact_ids).forEach((id) => relevantFactIds.add(id)));
    if (typeFilter === "arc") arcs.forEach((item) => splitIds(item.data.fact_ids).forEach((id) => relevantFactIds.add(id)));
    let edges = graph.edges.filter((edge) => {
      if (edge.data.kind !== "relation") return false;
      if (!visiblePlanetIds.has(edge.data.source) || !visiblePlanetIds.has(edge.data.target)) return false;
      return !scoped || splitIds(edge.data.fact_ids).some((id) => relevantFactIds.has(id));
    });
    if (normalizedSearch && visiblePlanetIds.size > 0 && typeFilter === "all") {
      const seeds = new Set(visiblePlanetIds);
      graph.edges.filter((edge) => edge.data.kind === "relation" && (seeds.has(edge.data.source) || seeds.has(edge.data.target)))
        .forEach((edge) => {
          if (allowedNodeIds.has(edge.data.source)) visiblePlanetIds.add(edge.data.source);
          if (allowedNodeIds.has(edge.data.target)) visiblePlanetIds.add(edge.data.target);
        });
      edges = graph.edges.filter((edge) =>
        edge.data.kind === "relation" &&
        visiblePlanetIds.has(edge.data.source) &&
        visiblePlanetIds.has(edge.data.target)
      );
    }
    const nodes = allowedNodes.filter((node) => node.data.kind === "subject" || visiblePlanetIds.has(node.data.id));
    const activeFacts = typeFilter === "episode" || typeFilter === "arc"
      ? graph.facts.filter((fact) =>
        relevantFactIds.has(fact.data.id) &&
        visibleByPolicy(fact.data) &&
        matchesFactAxes(fact.data)
      )
      : matchingFacts;
    return {
      ...graph,
      projection: {
        ...graph.projection,
        active_lenses: {
          profiles: profileFilter === "all" ? [] : [profileFilter],
          fact_types: typeIsFactLens ? [typeFilter] : [],
          lifecycle_states: stateFilter === "all" ? [] : [stateFilter],
          activities: activityFilter === "all" ? [] : [activityFilter],
          sensitivities: sensitivityFilter === "all" ? [] : [sensitivityFilter],
          updated_after: temporalCutoff === null ? null : new Date(temporalCutoff).toISOString()
        }
      },
      nodes,
      edges,
      facts: activeFacts,
      episodes,
      arcs,
      vault_markers: vaultMarkers
    };
  }, [activityFilter, graph, profileFilter, search, sensitivityFilter, showNoise, stateFilter, timeFilter, typeFilter]);

  const selectedOverlayFacts = useMemo(() => {
    if (!selected || !["episode", "arc"].includes(selected.kind)) return [];
    const factIds = new Set(splitIds(selected.fact_ids));
    return filteredGraph.facts
      .map((node) => node.data)
      .filter((fact) => factIds.has(fact.id))
      .sort((left, right) => (left.updated_at || "").localeCompare(right.updated_at || ""));
  }, [filteredGraph.facts, selected]);

  const searchEntityResults = useMemo(
    () => search.trim()
      ? filteredGraph.nodes.filter((node) => node.data.kind === "entity").slice(0, 12)
      : [],
    [filteredGraph, search]
  );

  const selectNode = useCallback((data: Record<string, string> | null) => {
    setSelected(data);
    setTrace(null);
  }, []);

  const clearLenses = useCallback(() => {
    setSearch("");
    setProfileFilter("all");
    setTypeFilter("all");
    setStateFilter("all");
    setTimeFilter("all");
    setActivityFilter("all");
    setSensitivityFilter("all");
    setSelected(null);
  }, []);

  async function loadTrace() {
    if (!selected || !["fact", "episode", "arc"].includes(selected.kind)) return;
    try {
      setTrace(await api.trace(selected.record_id));
      setMessage("");
    } catch (error) {
      setMessage(`证据追溯失败：${String(error)}`);
    }
  }

  function openGovernance(action: GovernanceAction) {
    if (selected?.kind !== "fact") return;
    setGovernanceAction(action);
    setGovernanceStatement(selected.label || "");
    setGovernanceReason("");
    setPurgeConfirmation("");
    setMessage("");
  }

  async function submitGovernance(event: FormEvent) {
    event.preventDefault();
    if (!selected || selected.kind !== "fact" || !governanceAction) return;
    setGovernanceBusy(true);
    try {
      if (governanceAction === "correct") {
        await api.correct(selected.record_id, governanceStatement.trim(), governanceReason.trim());
      } else if (governanceAction === "purge") {
        if (purgeConfirmation.trim() !== selected.record_id) {
          setMessage("记忆 ID 不匹配，未执行永久清除。");
          return;
        }
        await api.purge(selected.record_id, governanceReason.trim());
      } else {
        await api.changeState(selected.record_id, governanceAction, governanceReason.trim());
      }
      const completed = governanceAction;
      setGovernanceAction(null);
      setSelected(null);
      setTrace(null);
      await refresh();
      await loadReviewQueue();
      setMessage(
        completed === "correct"
          ? "记忆已修正，新版本正在重建。"
          : completed === "forget"
            ? "记忆已忘记，不再参与普通召回。"
            : completed === "isolate"
              ? "记忆已删除关联，不再参与召回。"
              : "永久清除请求已提交。"
      );
    } catch (error) {
      setMessage(`治理操作失败：${String(error)}`);
    } finally {
      setGovernanceBusy(false);
    }
  }

  function openEntityGovernance(action: EntityGovernanceAction) {
    if (selected?.kind !== "entity") return;
    setEntityGovernance(action);
    setEntityReason("");
    setMergeTarget(entityOptions[0]?.record_id || "");
    setSplitName("");
    setSplitType(ENTITY_TYPES.includes(selected.entity_type || "") ? selected.entity_type : "other");
    setSplitFactIds([]);
    setAttachFactId(availableEntityFacts[0]?.record_id || "");
    setMessage("");
  }

  async function submitEntityGovernance(event: FormEvent) {
    event.preventDefault();
    if (!selected || selected.kind !== "entity" || !entityGovernance) return;
    setEntityBusy(true);
    try {
      if (entityGovernance.kind === "merge") {
        await api.mergeEntity(selected.record_id, mergeTarget, entityReason.trim());
      } else if (entityGovernance.kind === "split") {
        await api.splitEntity(
          selected.record_id,
          splitName.trim(),
          splitType,
          splitFactIds,
          entityReason.trim()
        );
      } else if (entityGovernance.kind === "unmerge") {
        await api.unmergeEntity(entityGovernance.sourceId, entityReason.trim());
      } else {
        await api.changeEntityRelation(
          selected.record_id,
          entityGovernance.kind === "attach" ? attachFactId : entityGovernance.factId,
          entityGovernance.kind,
          entityReason.trim()
        );
      }
      const completed = entityGovernance.kind;
      setEntityGovernance(null);
      setSelected(null);
      await refresh();
      setMessage(
        completed === "merge"
          ? "实体已合并；原始实体与关联仍保留，可随时撤销。"
          : completed === "split"
            ? "所选事实已拆分为新实体，派生记忆正在重建。"
            : completed === "unmerge"
              ? "实体合并已撤销，原有关联已恢复。"
              : completed === "attach"
                ? "事实关系已添加，派生记忆正在重建。"
                : "事实关系已解除，原始证据保持不变。"
      );
    } catch (error) {
      setMessage(`实体治理失败：${String(error)}`);
    } finally {
      setEntityBusy(false);
    }
  }

  function openSubjectEditor() {
    if (selected?.kind !== "subject") return;
    setSubjectName(selected.label || "");
    setSubjectColor(selected.color || (selected.subject_kind === "user" ? "#efd095" : "#91cfb2"));
    setSubjectReason("");
    setSubjectEditor(true);
    setMessage("");
  }

  async function submitSubjectEditor(event: FormEvent) {
    event.preventDefault();
    if (!selected || selected.kind !== "subject") return;
    setSubjectBusy(true);
    try {
      await api.updateSubject(
        selected.record_id,
        subjectName.trim(),
        subjectColor,
        subjectReason.trim()
      );
      setSubjectEditor(false);
      setSelected(null);
      await refresh();
      setMessage("主体恒星显示已更新；来源映射和原始证据保持不变。");
    } catch (error) {
      setMessage(`主体治理失败：${String(error)}`);
    } finally {
      setSubjectBusy(false);
    }
  }

  if (authenticated === null) return <main className="loading">正在连接本地记忆库…</main>;
  if (!authenticated) return <Login onLogin={refresh} />;

  return (
    <main className={`app-shell ${tab === "map" ? "universe-shell" : ""}`}>
      {tab !== "map" && <header>
        <div>
          <p className="eyebrow">AGENT MEMORY · HERMES</p>
          <h1>记忆星图</h1>
        </div>
        <nav>
          <button onClick={() => setTab("map")}>
            星图
          </button>
          <button className={tab === "state" ? "active" : ""} onClick={() => setTab("state")}>
            当前状态
          </button>
          <button className={tab === "reports" ? "active" : ""} onClick={() => setTab("reports")}>
            整理报告
          </button>
          <button className={tab === "vault" ? "active" : ""} onClick={() => setTab("vault")}>
            受保护资源
          </button>
          <button
            onClick={async () => {
              await api.logout();
              setAuthenticated(false);
            }}
          >
            退出
          </button>
        </nav>
      </header>}

      {tab !== "map" && <section className="stats">
        <span><strong>{counts.facts}</strong> 记忆</span>
        <span><strong>{counts.entities}</strong> 实体</span>
        <span><strong>{counts.protected}</strong> 受保护资源</span>
      </section>}

      {message && <div className="banner">{message}</div>}

      {tab === "map" ? (
        <section className="workspace">
          <div className="map-stage">
            <button className="universe-breadcrumb" type="button" onClick={clearLenses}>
              {typeFilter === "all" ? "宇宙 · 深空" : `宇宙 › 观察镜片 · ${{ long_term: "长期", stage: "阶段", current: "当前", episode: "情节星座", arc: "长期星流", vault: "保护标记", entity: "行星" }[typeFilter] || typeFilter}`}
            </button>
            <div className="universe-topbar">
              <span className="universe-title">MEMORY UNIVERSE</span>
              <nav className="galaxy-pills" aria-label="观察镜片">
                {[["all", "全部"], ["long_term", "长期"], ["stage", "阶段"], ["current", "当前"], ["episode", "情节"], ["arc", "脉络"], ["vault", "保护"]].map(([value, label]) => (
                  <button key={value} type="button" className={typeFilter === value ? "active" : ""} aria-pressed={typeFilter === value} onClick={() => {
                    if (value === "all") clearLenses();
                    else { setTypeFilter(value); setSelected(null); }
                  }}>{label}</button>
                ))}
              </nav>
              <span className="topbar-separator" />
              <button
                className={`motion-toggle ${motionEnabled ? "active" : ""}`}
                type="button"
                title={motionEnabled ? "实体动态已开启，点击切换为静止" : "实体已静止，点击开启动态"}
                aria-label={motionEnabled ? "关闭实体动态" : "开启实体动态"}
                aria-pressed={motionEnabled}
                onClick={() => setMotionEnabled((value) => !value)}
              >{motionEnabled ? "动" : "静"}</button>
              <button className={`stardust-button ${showStardust ? "active" : ""}`} type="button" title="星尘 · 筛选与观测" aria-label="打开星尘筛选" onClick={() => setShowStardust((value) => !value)}>✦</button>
              <span className="universe-count">{filteredGraph.nodes.filter((node) => node.data.kind === "entity").length}</span>
            </div>
            <nav className="universe-navigation" aria-label="记忆系统页面">
              <button type="button" onClick={() => setTab("state")}>状态</button>
              <button type="button" onClick={() => setTab("reports")}>报告</button>
              <button type="button" onClick={() => setTab("vault")}>Vault</button>
              <button type="button" aria-label="退出" onClick={async () => { await api.logout(); setAuthenticated(false); }}>↗</button>
            </nav>
            <StarMap
              graph={filteredGraph}
              motionEnabled={motionEnabled}
              activeLens={typeFilter}
              selected={selected}
              onSelect={selectNode}
            />
            <section className={`universe-notebook ${showNotebook ? "open" : ""}`}>
              <button className="panel-heading" type="button" onClick={() => setShowNotebook((value) => !value)}><span>观星手记</span><i>{showNotebook ? "⌄" : "⌃"}</i></button>
              {showNotebook && <div className="notebook-body">
                <p>恒星表示主体，行星表示唯一实体；镜片只改变观察方式，不改变实体归属。</p>
                <div className="notebook-stats"><span><strong>{counts.facts}</strong> 记忆</span><span><strong>{counts.entities}</strong> 实体</span><span><strong>{counts.protected}</strong> 保护</span></div>
                <div className="notebook-legend"><span><i className="legend-entity" />实体行星</span><span><i className="legend-fact" />证据关系</span><span><i className="legend-episode" />星座/星流</span><span><i className="legend-vault" />保护标记</span></div>
              </div>}
            </section>
            <p className="universe-hint">拖拽平移 · 滚轮缩放 · 点击行星查看详情 · 镜片不改变行星身份</p>
            {showStardust && <div className="stardust-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowStardust(false); }}>
              <section className="stardust-lens" role="dialog" aria-modal="true" aria-label="星尘筛选">
                <header className="stardust-header"><div><span>✦</span><strong>星尘</strong><small>观测与筛选</small></div><button type="button" aria-label="关闭" onClick={() => setShowStardust(false)}>×</button></header>
                <div className="stardust-controls">
                  <label className="map-search"><span>⌕</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索项目、实体或记忆…" aria-label="搜索星图" autoFocus /></label>
                  <div className="filter-grid">
                    <label>来源 profile<select value={profileFilter} onChange={(event) => setProfileFilter(event.target.value)} aria-label="profile 过滤"><option value="all">全部 profile</option>{profiles.map((profile) => <option key={profile} value={profile}>{profile}</option>)}</select></label>
                    <label>观察投影<select value={typeFilter} onChange={(event) => { setTypeFilter(event.target.value); setSelected(null); }} aria-label="观察投影过滤"><option value="all">全部观察</option><option value="long_term">长期事实镜片</option><option value="stage">阶段事实镜片</option><option value="current">当前状态镜片</option><option value="observed">环境观察镜片</option><option value="episode">情节星座</option><option value="arc">长期星流</option><option value="vault">保护标记</option><option value="entity">仅行星</option></select></label>
                    <label>记忆状态<select value={stateFilter} onChange={(event) => setStateFilter(event.target.value)} aria-label="记忆状态过滤"><option value="all">全部状态</option><option value="active">活跃</option><option value="candidate">待确认</option><option value="forgotten">已忘记</option><option value="isolated">已脱离</option><option value="superseded">已替代</option></select></label>
                    <label>时间<select value={timeFilter} onChange={(event) => setTimeFilter(event.target.value)} aria-label="时间过滤"><option value="all">全部时间</option><option value="1">最近 24 小时</option><option value="7">最近 7 天</option><option value="30">最近 30 天</option><option value="365">最近一年</option></select></label>
                    <label>活跃度<select value={activityFilter} onChange={(event) => setActivityFilter(event.target.value)} aria-label="活跃度过滤"><option value="all">全部活跃度</option><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select></label>
                    <label>敏感性<select value={sensitivityFilter} onChange={(event) => setSensitivityFilter(event.target.value)} aria-label="敏感性过滤"><option value="all">全部敏感性</option><option value="normal">普通</option><option value="redacted">已脱敏</option><option value="protected">受保护</option></select></label>
                  </div>
                  <label className="noise-toggle"><input type="checkbox" checked={showNoise} onChange={(event) => setShowNoise(event.target.checked)} />显示测试、内部、不可信工具与低价值问询记录</label>
                </div>
                {searchEntityResults.length > 0 && <div className="entity-search-results" aria-label="实体搜索结果">
                  <span>匹配实体</span>
                  {searchEntityResults.map((node) => <button key={node.data.id} type="button" onClick={() => {
                    selectNode(node.data);
                    setShowStardust(false);
                  }}>
                    <strong>{node.data.label}</strong>
                    <small>{node.data.entity_type || "other"}</small>
                  </button>)}
                </div>}
                <div className="stardust-summary"><span>可见行星</span><strong>{filteredGraph.nodes.filter((node) => node.data.kind === "entity").length}</strong><small>共 {graph.nodes.filter((node) => node.data.kind === "entity").length} 个唯一实体</small></div>
                <footer>镜片可组合使用 · 不会写入或改变实体归属</footer>
              </section>
            </div>}
            <aside className={`detail-panel ${selected ? "show" : ""}`} aria-hidden={!selected}>
              {selected && <>
                <button className="detail-close" type="button" aria-label="关闭详情" onClick={() => selectNode(null)}>×</button>
                <p className="eyebrow">{selected.kind}</p>
                <h2>{selected.label}</h2>
                {selected.summary && <p className="overlay-summary">{selected.summary}</p>}
                {selected.hint && <p className="vault-hint">{selected.hint}</p>}
                <dl>
                  {selected.state && <><dt>状态</dt><dd>{selected.state}</dd></>}
                  {selected.source_profile && <><dt>来源</dt><dd>{selected.source_profile}</dd></>}
                  {selected.subject_kind && <><dt>主体类型</dt><dd>{selected.subject_kind === "user" ? "用户" : "Hermes profile 人格"}</dd></>}
                  {selected.source_count && <><dt>来源实例</dt><dd>{selected.source_count}</dd></>}
                  {selected.entity_type && <><dt>行星类型</dt><dd>{selected.entity_type}</dd></>}
                  {selected.overlay_kind && <><dt>投影类型</dt><dd>{{ annotation: "事实注释", constellation: "情节星座", stream: "长期星流", protection: "保护标记" }[selected.overlay_kind] || selected.overlay_kind}</dd></>}
                  {selected.fact_type && <><dt>类型</dt><dd>{selected.fact_type}</dd></>}
                  {selected.confidence && <><dt>可信度</dt><dd>{Math.round(Number(selected.confidence) * 100)}%</dd></>}
                  {selected.evidence_count && <><dt>证据数</dt><dd>{selected.evidence_count}</dd></>}
                  {selected.activity && <><dt>活跃度</dt><dd>{{ high: "高", medium: "中", low: "低" }[selected.activity] || selected.activity}</dd></>}
                  {selected.sensitivity && <><dt>敏感性</dt><dd>{{ normal: "普通", redacted: "已脱敏", protected: "受保护" }[selected.sensitivity] || selected.sensitivity}</dd></>}
                  {selected.extraction_method && <><dt>抽取方式</dt><dd>{selected.extraction_method}</dd></>}
                  {selected.extraction_version && <><dt>抽取版本</dt><dd>{selected.extraction_version}</dd></>}
                  {selected.model_name && <><dt>整理模型</dt><dd>{selected.model_name}</dd></>}
                  {selected.updated_at && <><dt>更新</dt><dd>{new Date(selected.updated_at).toLocaleString()}</dd></>}
                </dl>
                {selected.kind === "subject" && <div className="actions subject-actions">
                  <button type="button" onClick={openSubjectEditor}>编辑名称与颜色</button>
                </div>}
                {selected.kind === "fact" && <div className="actions">
                    <button type="button" onClick={loadTrace}>追溯证据</button>
                    <button type="button" onClick={() => openGovernance("correct")}>修正</button>
                    <button type="button" onClick={() => openGovernance("forget")}>忘记</button>
                    <button type="button" className="danger" onClick={() => openGovernance("isolate")}>删除关联</button>
                    <button type="button" className="danger purge" onClick={() => openGovernance("purge")}>永久清除</button>
                  </div>}
                {["episode", "arc"].includes(selected.kind) && <div className="actions">
                  <button type="button" onClick={loadTrace}>追溯支撑证据</button>
                </div>}
                {["episode", "arc"].includes(selected.kind) && selectedOverlayFacts.length > 0 && (
                  <section className="overlay-timeline" aria-label={selected.kind === "arc" ? "星流事实轨迹" : "星座支撑事实"}>
                    <h3>{selected.kind === "arc" ? "星流事实轨迹" : "星座支撑事实"}</h3>
                    {selectedOverlayFacts.map((fact, index) => (
                      <button key={fact.id} type="button" onClick={() => selectNode(fact)}>
                        <i>{index + 1}</i>
                        <span>{compactText(fact.label, 120)}</span>
                        <small>{fact.updated_at ? new Date(fact.updated_at).toLocaleString() : "时间未知"}</small>
                      </button>
                    ))}
                  </section>
                )}
                {selected.kind === "entity" && <>
                  <div className="actions entity-actions">
                    <button
                      type="button"
                      onClick={() => openEntityGovernance({ kind: "merge" })}
                      disabled={entityOptions.length === 0}
                    >合并到…</button>
                    <button
                      type="button"
                      onClick={() => openEntityGovernance({ kind: "split" })}
                      disabled={selectedEntityFacts.length === 0}
                    >拆分事实</button>
                    <button
                      type="button"
                      onClick={() => openEntityGovernance({ kind: "attach" })}
                      disabled={availableEntityFacts.length === 0}
                    >关联事实…</button>
                  </div>
                  {selectedEntityFacts.length > 0 && <section className="entity-relations">
                    <h3>直接事实关系</h3>
                    {selectedEntityFacts.slice(0, 12).map((fact) => <div key={fact.record_id}>
                      <span title={fact.label}>{compactText(fact.label)}</span>
                      <button type="button" onClick={() => openEntityGovernance({
                        kind: "detach",
                        factId: fact.record_id,
                        factName: fact.label
                      })}>解除</button>
                    </div>)}
                  </section>}
                  {selectedMergedAliases.length > 0 && <section className="merged-aliases">
                    <h3>已合并实体</h3>
                    {selectedMergedAliases.map((alias) => <div key={alias.id}>
                      <span>{alias.name}</span>
                      <button type="button" onClick={() => openEntityGovernance({
                        kind: "unmerge",
                        sourceId: alias.id,
                        sourceName: alias.name
                      })}>撤销合并</button>
                    </div>)}
                  </section>}
                </>}
                {trace && <pre>{JSON.stringify(trace, null, 2)}</pre>}
              </>}
            </aside>
          </div>
        </section>
      ) : tab === "vault" ? (
        <VaultPanel
          entries={vault}
          grants={grants}
          linkedMemory={selected?.kind === "fact" ? selected : null}
          onChange={refresh}
        />
      ) : tab === "state" ? (
        <StatePanel data={stateData} onChange={refresh} />
      ) : (
        <ReportsPanel
          reports={reports}
          quality={qualityReport}
          queue={reviewQueue}
          reason={reviewReason}
          profile={reviewProfile}
          onReasonChange={(value) => {
            setReviewReason(value);
            setReviewOffset(0);
          }}
          onProfileChange={(value) => {
            setReviewProfile(value);
            setReviewOffset(0);
          }}
          onPageChange={setReviewOffset}
          onReview={(memory) => {
            setSelected({
              id: `fact:${memory.memory_id}`,
              record_id: memory.memory_id,
              kind: "fact",
              label: memory.statement,
              fact_type: memory.fact_type,
              state: memory.state,
              source_profile: memory.source_profile,
              confidence: String(memory.confidence),
              evidence_count: String(memory.evidence_count),
              extraction_method: memory.extraction_method,
              updated_at: memory.updated_at
            });
            setTrace(null);
            setShowNoise(true);
            setTab("map");
          }}
        />
      )}
      {governanceAction && selected?.kind === "fact" && (
        <div className="modal-backdrop">
          <form className="governance-dialog" onSubmit={submitGovernance} role="dialog" aria-modal="true" aria-labelledby="governance-title">
            <p className="eyebrow">MEMORY GOVERNANCE</p>
            <h2 id="governance-title">
              {governanceAction === "correct"
                ? "修正记忆"
                : governanceAction === "forget"
                  ? "忘记记忆"
                  : governanceAction === "isolate"
                    ? "删除关联"
                    : "永久清除"}
            </h2>
            <p className="dialog-memory">{selected.label}</p>
            {governanceAction === "correct" && (
              <label>
                修正后的事实
                <textarea
                  value={governanceStatement}
                  onChange={(event) => setGovernanceStatement(event.target.value)}
                  required
                  autoFocus
                />
              </label>
            )}
            {governanceAction === "forget" && (
              <p className="dialog-explanation">普通对话不再主动召回；明确谈及该主题时仍可进行历史唤醒。</p>
            )}
            {governanceAction === "isolate" && (
              <p className="dialog-explanation">从关联记忆中脱离，普通召回和明确主题召回均不再返回；只读证据仍保留。</p>
            )}
            {governanceAction === "purge" && (
              <>
                <p className="dialog-warning">永久清除不可恢复。请输入下面的完整记忆 ID 进行确认：</p>
                <code>{selected.record_id}</code>
                <label>
                  确认记忆 ID
                  <input
                    value={purgeConfirmation}
                    onChange={(event) => setPurgeConfirmation(event.target.value)}
                    autoComplete="off"
                    required
                    autoFocus
                  />
                </label>
              </>
            )}
            <label>
              操作原因
              <textarea
                value={governanceReason}
                onChange={(event) => setGovernanceReason(event.target.value)}
                placeholder="该原因将写入治理审计"
                required
                autoFocus={governanceAction !== "correct" && governanceAction !== "purge"}
              />
            </label>
            <div className="dialog-actions">
              <button type="button" onClick={() => setGovernanceAction(null)} disabled={governanceBusy}>取消</button>
              <button
                type="submit"
                className={governanceAction === "purge" || governanceAction === "isolate" ? "danger" : ""}
                disabled={
                  governanceBusy ||
                  !governanceReason.trim() ||
                  (governanceAction === "correct" && (!governanceStatement.trim() || governanceStatement.trim() === selected.label)) ||
                  (governanceAction === "purge" && purgeConfirmation.trim() !== selected.record_id)
                }
              >
                {governanceBusy ? "正在提交…" : "确认执行"}
              </button>
            </div>
          </form>
        </div>
      )}
      {subjectEditor && selected?.kind === "subject" && (
        <div className="modal-backdrop">
          <form className="governance-dialog subject-dialog" onSubmit={submitSubjectEditor} role="dialog" aria-modal="true" aria-labelledby="subject-editor-title">
            <p className="eyebrow">SUBJECT GOVERNANCE</p>
            <h2 id="subject-editor-title">编辑主体恒星</h2>
            <p className="dialog-explanation">仅修改星图显示，不改写 profile、来源实例、事实或原始证据。</p>
            <label>显示名称<input value={subjectName} onChange={(event) => setSubjectName(event.target.value)} maxLength={128} required autoFocus /></label>
            <label className="subject-color">恒星颜色<input type="color" value={subjectColor} onChange={(event) => setSubjectColor(event.target.value)} /></label>
            <label>操作原因<textarea value={subjectReason} onChange={(event) => setSubjectReason(event.target.value)} placeholder="该原因将写入治理审计" required /></label>
            <div className="dialog-actions">
              <button type="button" onClick={() => setSubjectEditor(false)} disabled={subjectBusy}>取消</button>
              <button type="submit" disabled={subjectBusy || !subjectName.trim() || !subjectReason.trim()}>{subjectBusy ? "正在提交…" : "保存"}</button>
            </div>
          </form>
        </div>
      )}
      {entityGovernance && selected?.kind === "entity" && (
        <div className="modal-backdrop">
          <form className="governance-dialog entity-dialog" onSubmit={submitEntityGovernance} role="dialog" aria-modal="true" aria-labelledby="entity-governance-title">
            <p className="eyebrow">ENTITY GOVERNANCE</p>
            <h2 id="entity-governance-title">
              {entityGovernance.kind === "merge"
                ? "合并实体"
                : entityGovernance.kind === "split"
                  ? "拆分实体"
                  : entityGovernance.kind === "unmerge"
                    ? "撤销实体合并"
                    : entityGovernance.kind === "attach"
                      ? "关联事实"
                      : "解除事实关系"}
            </h2>
            <p className="dialog-memory">{selected.label}</p>
            {entityGovernance.kind === "merge" && <>
              <p className="dialog-explanation">原实体、证据和关联不会删除；系统只把它解析到所选规范实体，可从目标实体详情撤销。</p>
              <label>合并到
                <select value={mergeTarget} onChange={(event) => setMergeTarget(event.target.value)} required autoFocus>
                  {entityOptions.map((entity) => <option key={entity.record_id} value={entity.record_id}>{entity.label} · {entity.entity_type || "other"}</option>)}
                </select>
              </label>
            </>}
            {entityGovernance.kind === "split" && <>
              <p className="dialog-explanation">只有勾选的事实关联会移动到新实体；原始证据保持不变。</p>
              <label>新实体名称<input value={splitName} onChange={(event) => setSplitName(event.target.value)} maxLength={256} required autoFocus /></label>
              <label>实体类型<select value={splitType} onChange={(event) => setSplitType(event.target.value)}>
                {ENTITY_TYPES.map((value) => <option key={value} value={value}>{value}</option>)}
              </select></label>
              <fieldset className="split-facts"><legend>移入新实体的事实</legend>
                {selectedEntityFacts.map((fact) => <label key={fact.record_id}>
                  <input
                    type="checkbox"
                    checked={splitFactIds.includes(fact.record_id)}
                    onChange={(event) => setSplitFactIds((current) => event.target.checked
                      ? [...current, fact.record_id]
                      : current.filter((id) => id !== fact.record_id))}
                  />
                  <span>{fact.label}</span>
                </label>)}
              </fieldset>
            </>}
            {entityGovernance.kind === "unmerge" && <p className="dialog-explanation">
              将恢复“{entityGovernance.sourceName}”为独立实体，其原始事实关联会重新显示。
            </p>}
            {entityGovernance.kind === "attach" && <>
              <p className="dialog-explanation">只建立可撤销的实体—事实关系，不会修改原始证据或事实正文。</p>
              <label>选择事实<select value={attachFactId} onChange={(event) => setAttachFactId(event.target.value)} required autoFocus>
                {availableEntityFacts.map((fact) => <option key={fact.record_id} value={fact.record_id}>{fact.label}</option>)}
              </select></label>
            </>}
            {entityGovernance.kind === "detach" && <p className="dialog-explanation">
              将解除与“{entityGovernance.factName}”的关系；事实与原始证据仍会保留。
            </p>}
            <label>操作原因<textarea value={entityReason} onChange={(event) => setEntityReason(event.target.value)} placeholder="该原因将写入治理审计" required /></label>
            <div className="dialog-actions">
              <button type="button" onClick={() => setEntityGovernance(null)} disabled={entityBusy}>取消</button>
              <button type="submit" disabled={
                entityBusy || !entityReason.trim() ||
                (entityGovernance.kind === "merge" && !mergeTarget) ||
                (entityGovernance.kind === "split" && (!splitName.trim() || splitFactIds.length === 0)) ||
                (entityGovernance.kind === "attach" && !attachFactId)
              }>{entityBusy ? "正在提交…" : "确认执行"}</button>
            </div>
          </form>
        </div>
      )}
    </main>
  );
}

const AXIS_LABELS: Record<string, string> = {
  interaction_need: "互动需求",
  restraint: "表达克制",
  valence: "情感效价",
  arousal: "激活度",
  immersion: "任务沉浸"
};

function StatePanel({ data, onChange }: { data: StateData | null; onChange: () => Promise<void> }) {
  const [draft, setDraft] = useState(data?.config || null);
  useEffect(() => setDraft(data?.config || null), [data?.config]);

  async function saveConfig() {
    if (!draft) return;
    await api.configureState(draft);
    await onChange();
  }

  async function resetState() {
    if (!window.confirm("重置互动状态到当前初始参数？历史状态快照将被清除。")) return;
    await api.resetState();
    await onChange();
  }

  async function simulateState() {
    const content = window.prompt("输入用于无副作用模拟的消息", "紧急排障 project:atlas");
    if (!content) return;
    const result = await api.simulateState(content);
    window.alert(`${result.summary}\n${JSON.stringify(result.axes, null, 2)}`);
  }

  return (
    <section className="status-page">
      <div className="page-intro">
        <p className="eyebrow">DETERMINISTIC · NO AUTONOMOUS ACTIONS</p>
        <h2>当前状态</h2>
        <p>{data?.interaction?.summary || "尚未生成互动状态快照。"}</p>
      </div>
      {data?.interaction && (
        <div className="axis-grid">
          {Object.entries(data.interaction.axes).map(([key, value]) => (
            <article key={key}>
              <span>{data.config.axis_labels[key] || AXIS_LABELS[key] || key}</span><strong>{Math.round(value * 100)}%</strong>
              <i><b style={{ width: `${value * 100}%` }} /></i>
            </article>
          ))}
        </div>
      )}
      {draft && (
        <section className="state-governance">
          <div>
            <h3>状态参数治理</h3>
            <p>暂停只停止生成新的互动状态快照；当前事实与跨 profile 连续性仍照常维护。</p>
          </div>
          <label className="state-toggle">
            <input
              type="checkbox"
              checked={draft.enabled}
              onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })}
            />
            {draft.enabled ? "状态计算已启用" : "状态计算已暂停"}
          </label>
          <label>
            回归初始值周期（小时）
            <input
              type="number"
              min="1"
              max="720"
              value={draft.drift_hours}
              onChange={(event) => setDraft({ ...draft, drift_hours: Number(event.target.value) })}
            />
          </label>
          <div className="state-axis-settings">
            {Object.entries(draft.axes_initial).map(([key, value]) => (
              <div className="axis-setting" key={key}>
                <label className="axis-enable">
                  <input
                    type="checkbox"
                    checked={draft.axis_enabled[key]}
                    onChange={(event) => setDraft({
                      ...draft,
                      axis_enabled: { ...draft.axis_enabled, [key]: event.target.checked }
                    })}
                  />
                  启用
                </label>
                <input
                  aria-label={`${key} 名称`}
                  value={draft.axis_labels[key]}
                  maxLength={64}
                  onChange={(event) => setDraft({
                    ...draft,
                    axis_labels: { ...draft.axis_labels, [key]: event.target.value }
                  })}
                />
                <label>
                  初始值 · {Math.round(value * 100)}%
                  <input
                    type="range"
                    min={draft.axis_ranges[key].min}
                    max={draft.axis_ranges[key].max}
                    step="0.01"
                    value={value}
                    onChange={(event) => setDraft({
                      ...draft,
                      axes_initial: { ...draft.axes_initial, [key]: Number(event.target.value) }
                    })}
                  />
                </label>
                <div className="axis-range">
                  <label>最小<input type="number" min="0" max={draft.axis_ranges[key].max - 0.01} step="0.01" value={draft.axis_ranges[key].min} onChange={(event) => setDraft({
                    ...draft,
                    axis_ranges: { ...draft.axis_ranges, [key]: { ...draft.axis_ranges[key], min: Number(event.target.value) } }
                  })} /></label>
                  <label>最大<input type="number" min={draft.axis_ranges[key].min + 0.01} max="1" step="0.01" value={draft.axis_ranges[key].max} onChange={(event) => setDraft({
                    ...draft,
                    axis_ranges: { ...draft.axis_ranges, [key]: { ...draft.axis_ranges[key], max: Number(event.target.value) } }
                  })} /></label>
                </div>
              </div>
            ))}
          </div>
          <div className="state-actions">
            <button onClick={saveConfig}>保存参数</button>
            <button onClick={simulateState}>模拟</button>
            <button className="danger" onClick={resetState}>重置状态</button>
          </div>
        </section>
      )}
      <div className="state-columns">
        <section><h3>有效当前事实</h3>{data?.current_items.map((item) => (
          <article key={item.id}><strong>{item.summary}</strong><span>到期 {new Date(item.expires_at).toLocaleString()}</span></article>
        ))}</section>
        <section><h3>跨 profile 连续性</h3>{data?.continuities.map((item) => (
          <article key={item.topic_key}><strong>{item.summary}</strong><span>保留至 {new Date(item.expires_at).toLocaleString()}</span></article>
        ))}</section>
      </div>
      {data?.interaction?.suggestions.map((suggestion) => <p className="suggestion" key={suggestion}>建议 · {suggestion}</p>)}
    </section>
  );
}

function ReportsPanel({
  reports,
  quality,
  queue,
  reason,
  profile,
  onReasonChange,
  onProfileChange,
  onPageChange,
  onReview
}: {
  reports: ConsolidationReport[];
  quality: QualityReport | null;
  queue: ReviewQueue;
  reason: "all" | "candidate" | "untrusted_tool";
  profile: string;
  onReasonChange: (value: "all" | "candidate" | "untrusted_tool") => void;
  onProfileChange: (value: string) => void;
  onPageChange: (offset: number) => void;
  onReview: (memory: ReviewQueueItem) => void;
}) {
  const page = Math.floor(queue.offset / queue.limit) + 1;
  const pages = Math.max(1, Math.ceil(queue.total / queue.limit));
  return (
    <section className="reports-page">
      <div className="page-intro"><p className="eyebrow">DEFAULT · EVERY 7 DAYS</p><h2>整理报告</h2><p>后台整理结果仅保存在星图，默认不主动外发。</p></div>
      {reports.length === 0 ? <p className="muted">尚无整理报告。</p> : reports.map((report) => (
        <article className="report-card" key={report.id}>
          <div><strong>{new Date(report.period_start).toLocaleDateString()} – {new Date(report.period_end).toLocaleDateString()}</strong><span>生成于 {new Date(report.created_at).toLocaleString()}</span></div>
          <dl><dt>新增证据</dt><dd>{report.summary.evidence_added}</dd><dt>工具结果</dt><dd>{report.summary.tool_results}</dd><dt>敏感脱敏</dt><dd>{report.summary.redactions}</dd><dt>待确认</dt><dd>{report.summary.pending_confirmation}</dd><dt>不可信工具事实</dt><dd>{report.summary.untrusted_tool_facts ?? "—"}</dd></dl>
        </article>
      ))}
      {quality && <section className={`quality-card ${quality.automatic_ready ? "ready" : "blocked"}`}>
        <div>
          <p className="eyebrow">IMPORT QUALITY · AGGREGATES ONLY</p>
          <h3>导入质量门禁</h3>
          <strong>{quality.automatic_ready ? "自动门禁通过，仍需人工抽检" : "尚不允许进入主记忆"}</strong>
          <span>生成于 {new Date(quality.generated_at).toLocaleString()}</span>
        </div>
        <dl>
          <dt>证据可追溯</dt><dd>{quality.metrics.traceable_facts}/{quality.metrics.facts}</dd>
          <dt>模型原子事实</dt><dd>{quality.metrics.model_atomic_facts}</dd>
          <dt>逐字跨度有效</dt><dd>{quality.metrics.valid_atomic_spans}</dd>
          <dt>实体提及跨度</dt><dd>{quality.metrics.valid_entity_mentions}/{quality.metrics.entity_mentions}</dd>
          <dt>重复事实支撑</dt><dd>{quality.metrics.duplicate_fact_support}</dd>
          <dt>普通事实敏感泄漏</dt><dd>{quality.metrics.raw_sensitive_facts}</dd>
          <dt>不可信工具事实</dt><dd>{quality.metrics.untrusted_tool_facts}</dd>
        </dl>
        <div className="quality-gates">
          {Object.entries(quality.gates).map(([name, passed]) => <span key={name} className={passed ? "passed" : "failed"}>{passed ? "✓" : "×"} {name}</span>)}
        </div>
        <p>该报告只输出统计量，不返回真实对话正文。人工抽检通过前，系统始终保持 promotion_ready=false。</p>
      </section>}
      <section className="review-queue">
        <div><h3>待治理记忆</h3><p>不可信工具结果不会再自动提升。历史记录保留证据，需由你决定更正、忘记或脱离。</p></div>
        <div className="review-toolbar">
          <label>治理原因<select value={reason} onChange={(event) => onReasonChange(event.target.value as typeof reason)}><option value="all">全部</option><option value="untrusted_tool">不可信工具</option><option value="candidate">待确认</option></select></label>
          <label>来源 profile<select value={profile} onChange={(event) => onProfileChange(event.target.value)}><option value="all">全部 profile</option>{queue.profiles.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
          <span>共 {queue.total} 条 · 第 {page}/{pages} 页</span>
        </div>
        {queue.items.length === 0 ? <p className="muted">当前没有待治理记忆。</p> : (
          <div className="review-list">
            {queue.items.map((item) => (
              <button key={item.memory_id} type="button" onClick={() => onReview(item)}>
                <span>{item.review_reasons.includes("untrusted_tool") ? "不可信工具" : "待确认"} · {item.fact_type || "事实"}</span>
                <strong>{item.statement}</strong>
                <small>{item.source_profile || "未知来源"} · {item.tool_names.join(", ") || "用户证据"} · 点击查看证据与治理</small>
              </button>
            ))}
          </div>
        )}
        <div className="review-pagination">
          <button type="button" disabled={queue.offset === 0} onClick={() => onPageChange(Math.max(0, queue.offset - queue.limit))}>上一页</button>
          <button type="button" disabled={queue.offset + queue.limit >= queue.total} onClick={() => onPageChange(queue.offset + queue.limit)}>下一页</button>
        </div>
      </section>
    </section>
  );
}

function VaultPanel({
  entries,
  grants,
  linkedMemory,
  onChange
}: {
  entries: VaultEntry[];
  grants: VaultGrant[];
  linkedMemory: Record<string, string> | null;
  onChange: () => Promise<void>;
}) {
  const [label, setLabel] = useState("");
  const [hint, setHint] = useState("");
  const [secret, setSecret] = useState("");
  const [grantProfile, setGrantProfile] = useState<Record<string, string>>({});

  async function create(event: FormEvent) {
    event.preventDefault();
    await api.createVaultEntry({
      kind: "credential",
      display_label: label,
      redacted_hint: hint,
      secret_value: secret,
      linked_memory_id: linkedMemory?.record_id
    });
    setLabel(""); setHint(""); setSecret("");
    await onChange();
  }

  async function grant(entryId: string) {
    await api.grantVault(entryId, grantProfile[entryId] || "default", 15);
    await onChange();
  }

  async function revoke(grantId: string) {
    await api.revokeVault(grantId);
    await onChange();
  }

  return (
    <section className="vault-page">
      <div className="vault-intro">
        <p className="eyebrow">ENCRYPTED · MANUAL ONLY</p>
        <h2>受保护资源</h2>
        <p>密文与普通记忆隔离。Hermes 只能在你创建了限时、指定 profile 的授权后读取。</p>
      </div>
      <form className="vault-form" onSubmit={create}>
        <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="显示名称" required />
        <input value={hint} onChange={(e) => setHint(e.target.value)} placeholder="脱敏提示，例如 GitHub …A7F2" required />
        <input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder="敏感值" required />
        {linkedMemory && <p className="linked">关联到：{linkedMemory.label}</p>}
        <button type="submit">加密保存</button>
      </form>
      <div className="vault-list">
        {entries.map((entry) => (
          <VaultEntryCard
            key={entry.id}
            entry={entry}
            grantProfile={grantProfile[entry.id] || ""}
            onGrantProfile={(value) => setGrantProfile({ ...grantProfile, [entry.id]: value })}
            onGrant={() => grant(entry.id)}
            onChange={onChange}
          />
        ))}
      </div>
      <section className="grant-list">
        <div>
          <p className="eyebrow">ACTIVE GRANTS</p>
          <h2>当前授权</h2>
        </div>
        {grants.length === 0 ? (
          <p className="muted">当前没有可用授权。</p>
        ) : (
          grants.map((grantItem) => (
            <article key={grantItem.id}>
              <div>
                <strong>{grantItem.display_label}</strong>
                <span>Hermes · {grantItem.target_profile}</span>
                <span>到期：{new Date(grantItem.expires_at).toLocaleString()}</span>
              </div>
              <button className="danger" onClick={() => revoke(grantItem.id)}>立即撤销</button>
            </article>
          ))
        )}
      </section>
    </section>
  );
}

function VaultEntryCard({
  entry,
  grantProfile,
  onGrantProfile,
  onGrant,
  onChange
}: {
  entry: VaultEntry;
  grantProfile: string;
  onGrantProfile: (value: string) => void;
  onGrant: () => Promise<void>;
  onChange: () => Promise<void>;
}) {
  const [password, setPassword] = useState("");
  const [displayLabel, setDisplayLabel] = useState(entry.display_label);
  const [redactedHint, setRedactedHint] = useState(entry.redacted_hint);
  const [replacement, setReplacement] = useState("");
  const [revealed, setRevealed] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setDisplayLabel(entry.display_label);
    setRedactedHint(entry.redacted_hint);
  }, [entry.display_label, entry.redacted_hint]);

  useEffect(() => {
    if (!revealed) return;
    const timer = window.setTimeout(() => setRevealed(""), 60_000);
    return () => window.clearTimeout(timer);
  }, [revealed]);

  async function perform(action: () => Promise<unknown>, success: string) {
    setBusy(true);
    setMessage("");
    try {
      await action();
      setPassword("");
      setMessage(success);
      await onChange();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function reveal() {
    await perform(async () => {
      const result = await api.revealVaultEntry(entry.id, password);
      setRevealed(result.secret_value);
    }, "明文将在 60 秒后自动隐藏");
  }

  async function updateMetadata() {
    await perform(
      () => api.updateVaultEntry(entry.id, displayLabel, redactedHint, password),
      "显示信息已更新"
    );
  }

  async function replaceSecret() {
    await perform(async () => {
      await api.replaceVaultSecret(entry.id, replacement, password);
      setReplacement("");
      setRevealed("");
    }, "敏感值已替换，旧授权已全部撤销");
  }

  async function toggleStatus() {
    const next = entry.status === "active" ? "disabled" : "active";
    await perform(
      () => api.setVaultEntryStatus(entry.id, next, password),
      next === "active" ? "条目已启用" : "条目已停用，授权已撤销"
    );
  }

  async function remove() {
    if (!window.confirm(`永久删除“${entry.display_label}”？密文和所有授权将无法恢复。`)) return;
    await perform(
      () => api.deleteVaultEntry(entry.id, password),
      "条目已永久删除"
    );
  }

  return (
    <article className={entry.status === "disabled" ? "vault-disabled" : ""}>
      <div className="vault-card-title">
        <span className="lock">◆</span>
        <h3>{entry.display_label}</h3>
        <span className="vault-status">{entry.status === "active" ? "已启用" : "已停用"}</span>
        <p>{entry.redacted_hint}</p>
      </div>
      <div className="grant-row">
        <input
          value={grantProfile}
          onChange={(event) => onGrantProfile(event.target.value)}
          placeholder="Hermes profile"
          disabled={entry.status !== "active" || busy}
        />
        <button type="button" onClick={onGrant} disabled={entry.status !== "active" || busy}>
          授权 15 分钟
        </button>
      </div>
      <details className="vault-management">
        <summary>人工管理</summary>
        <p className="vault-warning">以下操作只在本地 UI 执行，并需再次输入管理密码。</p>
        <label>显示名称<input value={displayLabel} onChange={(event) => setDisplayLabel(event.target.value)} /></label>
        <label>脱敏提示<input value={redactedHint} onChange={(event) => setRedactedHint(event.target.value)} /></label>
        <button type="button" onClick={updateMetadata} disabled={busy || !password}>保存显示信息</button>
        <label>替换敏感值<input type="password" value={replacement} onChange={(event) => setReplacement(event.target.value)} autoComplete="new-password" /></label>
        <button type="button" onClick={replaceSecret} disabled={busy || !password || !replacement}>替换并撤销授权</button>
        <label>管理密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label>
        <div className="vault-actions">
          <button type="button" onClick={reveal} disabled={busy || !password}>查看 60 秒</button>
          <button type="button" onClick={toggleStatus} disabled={busy || !password}>{entry.status === "active" ? "停用" : "启用"}</button>
          <button type="button" className="danger" onClick={remove} disabled={busy || !password}>永久删除</button>
        </div>
        {revealed && <div className="vault-revealed"><code>{revealed}</code><button type="button" onClick={() => setRevealed("")}>立即隐藏</button></div>}
        {message && <p className="vault-message" role="status">{message}</p>}
      </details>
    </article>
  );
}
