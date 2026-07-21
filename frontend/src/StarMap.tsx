import cytoscape, { Core, EdgeSingular, EventObjectNode, NodeSingular } from "cytoscape";
import { CSSProperties, useEffect, useMemo, useRef } from "react";
import type { Galaxy, GraphData, GraphElement, LayoutPreference } from "./api";

type Props = {
  graph: GraphData;
  view: "universe" | "galaxy";
  layoutPreferences: LayoutPreference[];
  motionEnabled: boolean;
  activeLens: string;
  selected: Record<string, string> | null;
  onSelect: (data: Record<string, string> | null) => void;
  onEnterGalaxy: (galaxy: Galaxy) => void;
  onExitGalaxy: () => void;
  onSaveEntityLayout: (entityId: string, position: { x: number; y: number }) => void;
};

const PLANET_COLORS: Record<string, string> = {
  person: "#e5b894",
  agent: "#91cfb2",
  project: "#9cafe6",
  service: "#77c3d1",
  location: "#a7d2a0",
  organization: "#c8a6dc",
  tool: "#d6bd83",
  technology: "#85b8df",
  device: "#91c4c2",
  concept: "#b9add6",
  event: "#d59bb4",
  other: "#aebdd8"
};

const GALAXY_COLORS: Record<string, string> = {
  data: "#8fb9e8",
  observability: "#b6a0e6",
  communication: "#e2a9c4",
  infrastructure: "#82c9ad",
  manual: "#d7b48a",
  other: "#9cafe6"
};

const RELATION_LABELS: Record<string, string> = {
  uses_database: "使用数据库",
  pushes_logs_to: "推送日志",
  sends_alerts_to: "发送告警",
  uses_email_connector: "使用邮件连接器",
  connects_mailbox: "连接邮箱"
};

const LENS_LABELS: Record<string, string> = {
  all: "全部观察",
  long_term: "长期事实",
  stage: "阶段事实",
  current: "当前状态",
  observed: "环境观察",
  episode: "情节星座",
  arc: "长期星流",
  vault: "保护标记",
  entity: "实体行星"
};

function splitIds(value = "") {
  return value.split("|").filter(Boolean);
}

function summarize(value = "", maximum = 180) {
  const compact = value.replace(/\s+/g, " ").trim();
  return compact.length <= maximum ? compact : `${compact.slice(0, maximum - 1)}…`;
}

function hash(value: string) {
  let result = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    result ^= value.charCodeAt(index);
    result = Math.imul(result, 16777619);
  }
  return result >>> 0;
}

function positionPlanets(
  nodes: GraphElement[],
  saved: Map<string, { x: number; y: number }>,
  compact: boolean
) {
  const positions = new Map<string, { x: number; y: number }>();
  const ordered = [...nodes].sort((left, right) => left.data.id.localeCompare(right.data.id));
  const total = Math.max(ordered.length, 1);
  ordered.forEach((node, index) => {
    const savedPosition = saved.get(node.data.record_id);
    if (savedPosition) {
      positions.set(node.data.id, savedPosition);
      return;
    }
    const seed = hash(node.data.id);
    const angle = index * 2.399963229728653 + (seed % 360) * Math.PI / 1800;
    const normalized = Math.sqrt((index + 1) / (total + 1));
    const radiusX = (compact ? 72 : 145) + normalized * (compact ? 260 : 410) + (seed % 23);
    const radiusY = (compact ? 54 : 92) + normalized * (compact ? 175 : 255) + ((seed >> 4) % 17);
    positions.set(node.data.id, {
      x: 600 + Math.cos(angle) * radiusX,
      y: 360 + Math.sin(angle) * radiusY
    });
  });
  return positions;
}

function overlayEntityIds(selected: Record<string, string> | null) {
  if (!selected) return new Set<string>();
  if (selected.kind === "entity") return new Set([selected.id]);
  if (["relation", "typed_relation"].includes(selected.kind)) {
    return new Set([selected.source, selected.target].filter(Boolean));
  }
  return new Set([
    ...splitIds(selected.entity_ids),
    ...splitIds(selected.target_ids),
    ...splitIds(selected.subject_ids)
  ]);
}

function overlayFactIds(selected: Record<string, string> | null) {
  if (!selected) return new Set<string>();
  if (selected.kind === "fact") return new Set([selected.id]);
  return new Set(splitIds(selected.fact_ids));
}

export function StarMap({
  graph,
  view,
  layoutPreferences,
  motionEnabled,
  activeLens,
  selected,
  onSelect,
  onEnterGalaxy,
  onExitGalaxy,
  onSaveEntityLayout
}: Props) {
  const host = useRef<HTMLDivElement>(null);
  const instance = useRef<Core | null>(null);
  const focusedGalaxy = useRef<Galaxy | null>(null);
  const transitionArmed = useRef(true);
  const planetNodes = useMemo(
    () => graph.nodes.filter((node) => node.data.kind === "entity"),
    [graph.nodes]
  );
  const savedPositions = useMemo(() => {
    const positions = new Map<string, { x: number; y: number }>();
    layoutPreferences
      .filter((item) =>
        item.target_kind === "entity" &&
        item.scope_kind === view &&
        (view === "universe" || item.scope_id === graph.projection.galaxy_id)
      )
      .forEach((item) => {
        if (typeof item.position.x === "number" && typeof item.position.y === "number") {
          positions.set(item.target_id, { x: item.position.x, y: item.position.y });
        }
      });
    return positions;
  }, [graph.projection.galaxy_id, layoutPreferences, view]);
  const positions = useMemo(
    () => positionPlanets(planetNodes, savedPositions, view === "galaxy"),
    [planetNodes, savedPositions, view]
  );
  const relationCounts = useMemo(() => {
    const counts = new Map<string, number>();
    graph.edges.filter((edge) => ["relation", "typed_relation"].includes(edge.data.kind)).forEach((edge) => {
      counts.set(edge.data.source, (counts.get(edge.data.source) || 0) + 1);
      counts.set(edge.data.target, (counts.get(edge.data.target) || 0) + 1);
    });
    return counts;
  }, [graph.edges]);
  const planetLabels = useMemo(
    () => new Map(planetNodes.map((node) => [node.data.id, node.data.label])),
    [planetNodes]
  );
  const relationItems = useMemo<GraphElement[]>(
    () => graph.edges
      .filter((edge) => ["relation", "typed_relation"].includes(edge.data.kind))
      .map((edge) => ({
        ...edge,
        data: {
          ...edge.data,
          label: `${planetLabels.get(edge.data.source) || "行星"} — ${RELATION_LABELS[edge.data.relation_type] || edge.data.relation_type || "关联"} → ${planetLabels.get(edge.data.target) || "行星"}`,
          evidence_count: String(splitIds(edge.data.evidence_ids).length)
        }
      })),
    [graph.edges, planetLabels]
  );
  const galaxies = useMemo(
    () => view === "universe"
      ? graph.galaxies.filter((galaxy) =>
        galaxy.lifecycle_state === "active" && galaxy.visibility === "visible" && galaxy.member_count >= 3
      )
      : [],
    [graph.galaxies, view]
  );
  const galaxyLandmarks = useMemo(
    () => [...galaxies]
      .sort((left, right) => left.id.localeCompare(right.id))
      .map((galaxy, index, values) => {
        const seed = hash(galaxy.id);
        const angle = -Math.PI / 2 + (index * Math.PI * 2) / Math.max(values.length, 1) + (seed % 17) / 50;
        const ring = index % 2 === 0 ? 1 : 0.82;
        return {
          galaxy,
          left: 50 + Math.cos(angle) * 35 * ring,
          top: 50 + Math.sin(angle) * 30 * ring,
          color: GALAXY_COLORS[galaxy.family] || GALAXY_COLORS.other
        };
      }),
    [galaxies]
  );
  const protectedIds = useMemo(() => new Set(
    graph.vault_markers.flatMap((marker) => splitIds(marker.data.target_ids))
  ), [graph.vault_markers]);
  const subjectNodes = graph.nodes.filter((node) => node.data.kind === "subject");
  const userSubject = subjectNodes.find((node) => node.data.subject_kind === "user");
  const profileSubjects = subjectNodes
    .filter((node) => node.data.subject_kind === "profile_persona")
    .sort((left, right) => left.data.label.localeCompare(right.data.label));
  const subjectLayout = [
    ...(userSubject ? [{ node: userSubject, x: profileSubjects.length === 1 ? 46 : 0, y: 0 }] : []),
    ...profileSubjects.map((node, index) => {
      if (profileSubjects.length === 1) return { node, x: -46, y: 0 };
      const ring = profileSubjects.length > 6 && index >= 6 ? 1 : 0;
      const ringStart = ring * 6;
      const ringCount = Math.min(6, profileSubjects.length - ringStart);
      const angle = -Math.PI / 2 + ((index - ringStart) * Math.PI * 2) / ringCount + ring * 0.28;
      return {
        node,
        x: Math.cos(angle) * (ring ? 190 : 110),
        y: Math.sin(angle) * (ring ? 128 : 74)
      };
    })
  ];
  const overlayItems = activeLens === "episode"
    ? graph.episodes
    : activeLens === "arc"
      ? graph.arcs
      : activeLens === "vault"
        ? graph.vault_markers
        : [];
  const overlayTitle = activeLens === "episode"
    ? "情节星座"
    : activeLens === "arc"
      ? "长期星流"
      : "保护标记";
  const annotations = graph.facts
    .filter((fact) => splitIds(fact.data.entity_ids).length >= (activeLens === "all" ? 2 : 1))
    .sort((left, right) => Number(right.data.confidence || 0) - Number(left.data.confidence || 0))
    .slice(0, 6);

  const animateOverlay = (data: Record<string, string>) => {
    onSelect(data);
    const cy = instance.current;
    if (!cy || cy.destroyed()) return;
    const factIds = data.kind === "fact" ? new Set([data.id]) : new Set(splitIds(data.fact_ids));
    const entityIds = new Set(splitIds(data.entity_ids));
    const relatedEdges = cy.edges().filter((edge) =>
      splitIds(String(edge.data("fact_ids") || "")).some((id) => factIds.has(id))
    ).slice(0, 8);
    if (relatedEdges.length === 0) {
      entityIds.forEach((id) => cy.getElementById(id).addClass("overlay-member"));
      return;
    }
    relatedEdges.forEach((edge, index) => {
      const particle = cy.add({
        group: "nodes",
        data: { id: `particle:${data.id}:${index}:${Date.now()}`, kind: "particle" },
        position: edge.source().position(),
        selectable: false,
        grabbable: false
      }).nodes()[0];
      const start = window.performance.now() + index * 90;
      const animate = (now: number) => {
        if (cy.destroyed() || !particle || particle.removed()) return;
        const progress = Math.max(0, Math.min(1, (now - start) / 1200));
        const source = edge.source().position();
        const target = edge.target().position();
        particle.position({
          x: source.x + (target.x - source.x) * progress,
          y: source.y + (target.y - source.y) * progress
        });
        particle.style("opacity", Math.sin(progress * Math.PI));
        if (progress < 1) window.requestAnimationFrame(animate);
        else particle.remove();
      };
      window.requestAnimationFrame(animate);
    });
  };

  useEffect(() => {
    if (!host.current) return;
    instance.current?.destroy();
    const elements = [
      ...planetNodes.map((element) => ({
        ...element,
        position: positions.get(element.data.id),
        data: {
          ...element.data,
          relation_count: relationCounts.get(element.data.id) || 0,
          planet_color: PLANET_COLORS[element.data.entity_type] || PLANET_COLORS.other,
          protected: protectedIds.has(element.data.id) ? "true" : "false",
          lens: activeLens
        }
      })),
      ...relationItems.map((element) => ({
        ...element,
        data: {
          ...element.data,
          strength: Number(element.data.strength || 0.55)
        }
      }))
    ];
    const cy = cytoscape({
      container: host.current,
      elements,
      minZoom: 0.12,
      maxZoom: 6,
      pixelRatio: "auto",
      style: [
        { selector: 'node[kind = "entity"]', style: {
          label: "", width: "mapData(relation_count, 0, 12, 11, 20)", height: "mapData(relation_count, 0, 12, 11, 20)",
          shape: "ellipse", "background-color": "data(planet_color)", "border-width": 0.8, "border-color": "#ffffff",
          "underlay-padding": 8, "underlay-color": "data(planet_color)", "underlay-opacity": 0.18,
          "underlay-shape": "ellipse", "transition-property": "opacity, border-width, underlay-opacity, text-opacity",
          "transition-duration": 220
        } },
        { selector: 'node[protected = "true"]', style: {
          "border-width": 2, "border-color": "#e5a3b7", "border-style": "double",
          "underlay-color": "#c982a0", "underlay-opacity": 0.35
        } },
        { selector: 'node[lens = "long_term"]', style: { "underlay-opacity": 0.34, "underlay-padding": 10 } },
        { selector: 'node[lens = "stage"]', style: { "border-width": 2.2, "border-color": "#82c9ad" } },
        { selector: 'node[lens = "current"], node[lens = "observed"]', style: { "underlay-opacity": 0.55, "underlay-padding": 13 } },
        { selector: 'node[kind = "particle"]', style: {
          width: 5, height: 5, label: "", "background-color": "#fff0ba", "border-width": 0,
          "underlay-padding": 9, "underlay-color": "#ffd98c", "underlay-opacity": 0.66, "events": "no", "z-index": 40
        } },
        { selector: 'edge[kind = "relation"], edge[kind = "typed_relation"]', style: {
          width: "mapData(strength, 0, 1, 0.3, 2)", "line-color": "#7188b0",
          opacity: (edge: EdgeSingular) => 0.04 + Number(edge.data("strength") || 0.4) * 0.3,
          "curve-style": "bezier", "transition-property": "opacity, width, line-color", "transition-duration": 220
        } },
        { selector: ".is-dimmed", style: { opacity: 0.05, "text-opacity": 0 } },
        { selector: "node.is-neighbor", style: { opacity: 1, "border-width": 1.8, "underlay-opacity": 0.48 } },
        { selector: "edge.is-neighbor", style: { opacity: 0.95, width: 2.5, "line-color": "#c7d9ff" } },
        { selector: "node.overlay-member", style: { opacity: 1, "border-width": 2.4, "border-color": "#fff0ba", "underlay-color": "#ffd98c", "underlay-opacity": 0.62, "underlay-padding": 14 } },
        { selector: "edge.overlay-member", style: { opacity: 1, width: 2.8, "line-color": "#f2d99d" } },
        { selector: 'node[kind = "entity"].is-hovered, node[kind = "entity"]:selected', style: {
          label: "data(label)", color: "#f2f6ff", "font-size": 8, "font-weight": 600,
          "text-wrap": "ellipsis", "text-max-width": "150px", "text-valign": "bottom", "text-margin-y": 12,
          "text-outline-color": "#020410", "text-outline-width": 3, "border-width": 2, "border-color": "#ffffff",
          "underlay-opacity": 0.58, "z-index": 20
        } }
      ],
      layout: { name: "preset", fit: false, animate: false }
    });

    const fitPlanets = () => {
      const planets = cy.nodes('[kind = "entity"]');
      if (planets.length === 0) return;
      cy.fit(planets, view === "galaxy" ? 150 : 74);
    };
    fitPlanets();
    const fitFrame = window.requestAnimationFrame(fitPlanets);
    const fitTimer = window.setTimeout(() => { cy.resize(); fitPlanets(); }, 120);
    const clearFocus = () => cy.elements().removeClass("is-neighbor is-dimmed");
    const focusNeighborhood = (node: NodeSingular) => {
      const neighborhood = node.closedNeighborhood();
      cy.elements().not(neighborhood).addClass("is-dimmed");
      neighborhood.addClass("is-neighbor");
    };
    cy.on("mouseover", 'node[kind = "entity"]', (event: EventObjectNode) => {
      event.target.addClass("is-hovered");
      focusNeighborhood(event.target);
    });
    cy.on("mouseout", 'node[kind = "entity"]', (event: EventObjectNode) => {
      event.target.removeClass("is-hovered");
      clearFocus();
    });
    cy.on("tap select", 'node[kind = "entity"]', (event: EventObjectNode) => {
      clearFocus();
      focusNeighborhood(event.target);
      onSelect(event.target.data());
    });
    cy.on("tap select", 'edge[kind = "relation"], edge[kind = "typed_relation"]', (event) => {
      clearFocus();
      event.target.addClass("is-neighbor");
      event.target.connectedNodes().addClass("is-neighbor");
      onSelect(event.target.data());
    });
    cy.on("tap", (event) => {
      if (event.target !== cy) return;
      clearFocus();
      onSelect(null);
    });
    cy.on("dragfree", 'node[kind = "entity"]', (event: EventObjectNode) => {
      const position = event.target.position();
      onSaveEntityLayout(String(event.target.data("record_id")), {
        x: Math.round(position.x * 100) / 100,
        y: Math.round(position.y * 100) / 100
      });
    });
    const transitionReadyAt = window.performance.now() + 700;
    cy.on("zoom", () => {
      if (!transitionArmed.current || window.performance.now() < transitionReadyAt) return;
      if (view === "universe" && cy.zoom() >= 2.6 && focusedGalaxy.current) {
        transitionArmed.current = false;
        onEnterGalaxy(focusedGalaxy.current);
      } else if (view === "galaxy" && cy.zoom() <= 0.14) {
        transitionArmed.current = false;
        onExitGalaxy();
      }
    });

    let motionFrame = 0;
    let lastMotionFrame = 0;
    const motionStart = window.performance.now();
    const motionNodes = cy.nodes('[kind = "entity"]').map((node) => {
      const seed = hash(node.id());
      return {
        node,
        base: positions.get(node.id()) || node.position(),
        phase: (seed % 628) / 100,
        speed: 0.00012 + (seed % 9) * 0.000008,
        amplitudeX: 4.5 + (seed % 11) / 10,
        amplitudeY: 3.3 + ((seed >> 5) % 10) / 10
      };
    });
    const animateMotion = (now: number) => {
      if (now - lastMotionFrame >= 42 && !document.hidden) {
        const elapsed = now - motionStart;
        cy.batch(() => motionNodes.forEach(({ node, base, phase, speed, amplitudeX, amplitudeY }) => {
          if (node.grabbed() || node.hasClass("is-hovered") || node.selected()) return;
          node.position({
            x: base.x + Math.sin(elapsed * speed + phase) * amplitudeX,
            y: base.y + Math.cos(elapsed * speed * 0.83 + phase * 1.37) * amplitudeY
          });
        }));
        lastMotionFrame = now;
      }
      motionFrame = window.requestAnimationFrame(animateMotion);
    };
    if (motionEnabled) motionFrame = window.requestAnimationFrame(animateMotion);
    instance.current = cy;
    transitionArmed.current = true;
    return () => {
      window.clearTimeout(fitTimer);
      window.cancelAnimationFrame(fitFrame);
      window.cancelAnimationFrame(motionFrame);
      cy.destroy();
    };
  }, [
    activeLens,
    motionEnabled,
    onEnterGalaxy,
    onExitGalaxy,
    onSaveEntityLayout,
    onSelect,
    planetNodes,
    relationItems,
    positions,
    protectedIds,
    relationCounts,
    view
  ]);

  useEffect(() => {
    const cy = instance.current;
    if (!cy || cy.destroyed()) return;
    cy.elements().removeClass("overlay-member");
    const entityIds = overlayEntityIds(selected);
    const factIds = overlayFactIds(selected);
    entityIds.forEach((id) => cy.getElementById(id).addClass("overlay-member"));
    cy.edges().filter((edge) =>
      splitIds(String(edge.data("fact_ids") || "")).some((id) => factIds.has(id))
    ).addClass("overlay-member");
  }, [selected]);

  return <div className={`star-map-shell ${view === "galaxy" ? "galaxy-view" : "universe-view"} planetary-view`} data-motion={motionEnabled ? "floating-5px" : "static"}>
    <div className="galaxy-band" />
    <div className="deep-space-halo" aria-hidden="true" />
    {view === "universe" && galaxyLandmarks.map(({ galaxy, left, top, color }) => <div
      key={`aura:${galaxy.id}`}
      className="galaxy-aura"
      style={{ left: `${left}%`, top: `${top}%`, "--galaxy-color": color } as CSSProperties}
      aria-hidden="true"
    />)}
    <div className="star-map" ref={host} aria-label={`记忆主宇宙 · ${LENS_LABELS[activeLens] || activeLens}`} />
    {view === "universe" && <nav className="galaxy-labels" aria-label="关系星系入口">
      {galaxyLandmarks.map(({ galaxy, left, top, color }) => <button
        key={galaxy.id}
        type="button"
        style={{ left: `${left}%`, top: `${top}%`, color } as CSSProperties}
        onMouseEnter={() => { focusedGalaxy.current = galaxy; }}
        onMouseLeave={() => {
          if (focusedGalaxy.current?.id === galaxy.id) focusedGalaxy.current = null;
        }}
        onFocus={() => { focusedGalaxy.current = galaxy; }}
        onBlur={() => {
          if (focusedGalaxy.current?.id === galaxy.id) focusedGalaxy.current = null;
        }}
        onClick={() => onEnterGalaxy(galaxy)}
        aria-label={`进入${galaxy.display_name}，${galaxy.member_count}颗行星`}
      >
        {galaxy.display_name}
        <small>{galaxy.member_count} 行星 · {galaxy.evidence_count} 证据</small>
      </button>)}
    </nav>}
    <nav className="sr-only" aria-label="可访问行星列表">
      {planetNodes.map((node) => <button
        key={node.data.id}
        type="button"
        aria-label={`行星 ${node.data.label}`}
        onFocus={() => onSelect(node.data)}
        onClick={(event) => { event.stopPropagation(); onSelect(node.data); }}
      >{node.data.label}</button>)}
    </nav>
    <nav className="sr-only" aria-label="可访问关系列表">
      {relationItems.map((edge) => <button
          key={edge.data.id}
          type="button"
          aria-label={`关系 ${edge.data.label}`}
          onFocus={() => onSelect(edge.data)}
          onClick={(event) => { event.stopPropagation(); onSelect(edge.data); }}
        >{edge.data.label}</button>)}
    </nav>
    {view === "galaxy" && relationItems.length > 0 && <section className="relation-index" aria-label="关系证据">
      <p>关系证据<small>事实是解释层，不是星体</small></p>
      {relationItems.map((edge) => <button key={`visible:${edge.data.id}`} type="button" onClick={() => onSelect(edge.data)}>
        <strong>{edge.data.label}</strong>
        <span>{edge.data.evidence_count} 条证据 · {edge.data.transport}</span>
      </button>)}
    </section>}

    {view === "universe" && <div className="subject-cores" aria-label="主体恒星群">
      {subjectLayout.map(({ node, x, y }) => <button
        key={node.data.id}
        type="button"
        className={`stellar-core ${node.data.subject_kind === "user" ? "stellar-user" : "stellar-profile"}`}
        style={{ left: `calc(50% + ${x}px)`, top: `calc(50% + ${y}px)`, color: node.data.color } as CSSProperties}
        onClick={() => onSelect(node.data)}
        title={`${node.data.label} · ${node.data.subject_kind === "user" ? "用户恒星" : "Hermes Profile 恒星"}`}
        aria-label={`${node.data.label} · ${node.data.subject_kind === "user" ? "用户恒星" : "Hermes Profile 恒星"}`}
      >
        <i className="stellar-glow" /><i className="stellar-ray ray-horizontal" /><i className="stellar-ray ray-vertical" /><b /><span>{node.data.label}</span>
      </button>)}
    </div>}

    {overlayItems.length > 0 && <section className="overlay-index" aria-label={`${overlayTitle}列表`}>
      <p>{overlayTitle}<small>{activeLens === "vault" ? "只显示脱敏引用，不加载敏感明文" : "临时投影，不改变行星位置"}</small></p>
      {overlayItems.slice(0, 10).map((item) => <button key={item.data.id} type="button" className={selected?.id === item.data.id ? "active" : ""} onClick={() => activeLens === "vault" ? onSelect(item.data) : animateOverlay(item.data)}>
        <strong>{summarize(item.data.label, 90)}</strong>
        <span>{activeLens === "vault"
          ? `${splitIds(item.data.target_ids).length} 行星 · ${splitIds(item.data.reference_ids).length} 脱敏引用`
          : `${splitIds(item.data.entity_ids).length} 行星 · ${item.data.evidence_count || 0} 证据`}</span>
      </button>)}
    </section>}

    {annotations.length > 0 && !["episode", "arc", "entity"].includes(activeLens) && <section className="observation-notes" aria-label="事实注释">
      <p>{LENS_LABELS[activeLens] || "事实"}<small>事实是注释，不是星体</small></p>
      {annotations.map((fact) => <button key={fact.data.id} type="button" onClick={() => animateOverlay(fact.data)}>
        <span>{fact.data.state === "candidate" ? "待确认" : "已记录"} · {Math.round(Number(fact.data.confidence || 0) * 100)}%</span>
        {summarize(fact.data.label)}
      </button>)}
    </section>}
  </div>;
}
