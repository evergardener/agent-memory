import cytoscape, { Core, EdgeSingular, EventObjectNode, NodeSingular } from "cytoscape";
import { CSSProperties, useEffect, useMemo, useRef } from "react";
import type { Galaxy, GraphData, GraphElement, LayoutPreference } from "./api";
import { presentRelations, relationMatchesOverlay, subjectOrbitLayout } from "./starMapModel";

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
  onSaveSubjectLayout: (subjectId: string, position: { x: number; y: number }) => void;
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

function safeCelestialColor(color: string, fallback: string) {
  return /^#[0-9a-f]{6}$/i.test(color) ? color : fallback;
}

function celestialSprite(inputColor: string, subject: boolean) {
  const color = safeCelestialColor(inputColor, subject ? "#91cfb2" : PLANET_COLORS.other);
  const size = subject ? 112 : 36;
  const center = size / 2;
  const core = subject ? 4 : 2;
  const glow = subject ? 23 : 8;
  const rays = subject
    ? `<path d="M16 ${center}H${size - 16}" stroke="${color}" stroke-opacity=".28" stroke-width=".7"/>
    <path d="M${center} 4V${size - 4}" stroke="${color}" stroke-opacity=".28" stroke-width=".7"/>`
    : "";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
    <defs>
      <radialGradient id="g">
        <stop offset="0" stop-color="${color}" stop-opacity=".62"/>
        <stop offset=".34" stop-color="${color}" stop-opacity=".25"/>
        <stop offset="1" stop-color="${color}" stop-opacity="0"/>
      </radialGradient>
      <filter id="b" x="-60%" y="-60%" width="220%" height="220%">
        <feGaussianBlur stdDeviation="${subject ? 5.8 : 3.5}"/>
      </filter>
    </defs>
    <circle cx="${center}" cy="${center}" r="${glow}" fill="url(#g)" filter="url(#b)"/>
    ${rays}
    <circle cx="${center}" cy="${center}" r="${core + 2.4}" fill="${color}" fill-opacity=".22"/>
    <circle cx="${center}" cy="${center}" r="${core}" fill="#f8fff9"/>
  </svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

const RELATION_LABELS: Record<string, string> = {
  uses_database: "使用数据库",
  pushes_logs_to: "推送日志",
  sends_alerts_to: "发送告警",
  uses_email_connector: "使用邮件连接器",
  connects_mailbox: "连接邮箱",
  university_classmate: "大学同学",
  classmate: "同学",
  friend: "朋友",
  colleague: "同事"
};
const RELATION_KINDS = new Set([
  "relation",
  "typed_relation",
  "episode_relation",
  "relationship"
]);

function isRelationKind(kind = "") {
  return RELATION_KINDS.has(kind);
}

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
  if (isRelationKind(selected.kind)) {
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
  onSaveEntityLayout,
  onSaveSubjectLayout
}: Props) {
  const host = useRef<HTMLDivElement>(null);
  const instance = useRef<Core | null>(null);
  const focusedGalaxy = useRef<Galaxy | null>(null);
  const transitionArmed = useRef(true);
  const handlers = useRef({
    onSelect,
    onEnterGalaxy,
    onExitGalaxy,
    onSaveEntityLayout,
    onSaveSubjectLayout
  });
  useEffect(() => {
    handlers.current = {
      onSelect,
      onEnterGalaxy,
      onExitGalaxy,
      onSaveEntityLayout,
      onSaveSubjectLayout
    };
  }, [
    onEnterGalaxy,
    onExitGalaxy,
    onSaveEntityLayout,
    onSaveSubjectLayout,
    onSelect
  ]);
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
  const subjectNodes = useMemo(
    () => graph.nodes.filter((node) => node.data.kind === "subject"),
    [graph.nodes]
  );
  const subjectIds = useMemo(
    () => new Set(subjectNodes.map((node) => node.data.id)),
    [subjectNodes]
  );
  const savedSubjectPositions = useMemo(() => {
    const values = new Map<string, { x: number; y: number }>();
    layoutPreferences
      .filter((item) =>
        item.target_kind === "subject" &&
        item.scope_kind === "universe" &&
        item.pinned &&
        typeof item.position.x === "number" &&
        typeof item.position.y === "number"
      )
      .forEach((item) => values.set(item.target_id, {
        x: Number(item.position.x),
        y: Number(item.position.y)
      }));
    return values;
  }, [layoutPreferences]);
  const subjectLayout = useMemo(
    () => subjectOrbitLayout(subjectNodes).map((item) => ({
      ...item,
      ...(savedSubjectPositions.get(item.node.data.record_id) || {})
    })),
    [savedSubjectPositions, subjectNodes]
  );
  const celestialLabels = useMemo(
    () => new Map(
      [...planetNodes, ...subjectNodes].map((node) => [node.data.id, node.data.label])
    ),
    [planetNodes, subjectNodes]
  );
  const relationItems = useMemo<GraphElement[]>(
    () => presentRelations(
      graph.edges.filter((edge) => isRelationKind(edge.data.kind)),
      celestialLabels,
      RELATION_LABELS
    ),
    [celestialLabels, graph.edges]
  );
  const visibleRelationItems = useMemo(
    () => view === "universe"
      ? relationItems
      : relationItems.filter((edge) =>
        !subjectIds.has(edge.data.source) && !subjectIds.has(edge.data.target)
      ),
    [relationItems, subjectIds, view]
  );
  const relationCounts = useMemo(() => {
    const counts = new Map<string, number>();
    visibleRelationItems.forEach((edge) => {
      counts.set(edge.data.source, (counts.get(edge.data.source) || 0) + 1);
      counts.set(edge.data.target, (counts.get(edge.data.target) || 0) + 1);
    });
    return counts;
  }, [visibleRelationItems]);
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
    const entityIds = new Set(splitIds(data.entity_ids));
    const relatedEdges = cy.edges()
      .filter((edge) => relationMatchesOverlay(edge.data(), data))
      .slice(0, 8);
    if (relatedEdges.length === 0) {
      entityIds.forEach((id) => cy.getElementById(id).addClass("overlay-member"));
      return;
    }
    relatedEdges.forEach((edge, index) => {
      const particle = cy.add({
        group: "nodes",
        data: {
          id: `particle:${data.id}:${index}:${Date.now()}`,
          kind: "particle",
          celestial_image: celestialSprite("#fff0ba", false)
        },
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
    const hostElement = host.current;
    const elements = [
      ...planetNodes.map((element) => ({
        ...element,
        position: positions.get(element.data.id),
        data: {
          ...element.data,
          relation_count: relationCounts.get(element.data.id) || 0,
          planet_color: PLANET_COLORS[element.data.entity_type] || PLANET_COLORS.other,
          celestial_image: celestialSprite(
            PLANET_COLORS[element.data.entity_type] || PLANET_COLORS.other,
            false
          ),
          protected: protectedIds.has(element.data.id) ? "true" : "false",
          lens: activeLens
        }
      })),
      ...(view === "universe" ? subjectLayout.map(({ node, x, y }) => ({
        ...node,
        position: { x: 600 + x, y: 360 + y },
        data: {
          ...node.data,
          relation_count: 0,
          celestial_image: celestialSprite(node.data.color || "#91cfb2", true)
        }
      })) : []),
      ...visibleRelationItems.map((element) => ({
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
          label: "", width: "mapData(relation_count, 0, 12, 5, 8)", height: "mapData(relation_count, 0, 12, 5, 8)",
          shape: "ellipse", "background-color": "transparent", "background-opacity": 0, "border-width": 0,
          "background-image": "data(celestial_image)", "background-fit": "none",
          "background-width": 36, "background-height": 36, "background-clip": "none",
          "background-image-containment": "over", "background-image-opacity": 0.76,
          "bounds-expansion": 20, "underlay-opacity": 0,
          "transition-property": "opacity, background-image-opacity, text-opacity", "z-index": 6,
          "transition-duration": 220
        } },
        { selector: 'node[kind = "subject"]', style: {
          width: 7, height: 7, shape: "ellipse", label: "data(label)", opacity: 1,
          "background-color": "transparent", "background-opacity": 0, "border-width": 0,
          "background-image": "data(celestial_image)", "background-fit": "none",
          "background-width": 112, "background-height": 112, "background-clip": "none",
          "background-image-containment": "over", "bounds-expansion": 58,
          "underlay-opacity": 0,
          color: "data(color)", "font-family": "Georgia, Noto Serif SC, serif",
          "font-size": 14, "font-weight": 600, "text-valign": "bottom", "text-margin-y": 31,
          "text-outline-color": "#020410", "text-outline-width": 3,
          "transition-property": "opacity, background-image-opacity, text-opacity",
          "transition-duration": 220, "z-index": 12
        } },
        { selector: 'node[kind = "subject"][subject_kind = "user"]', style: {
          width: 8, height: 8
        } },
        { selector: 'node[protected = "true"]', style: {
          "border-width": 1.4, "border-color": "#e5a3b7", "border-style": "double"
        } },
        { selector: 'node[lens = "long_term"]', style: { "background-image-opacity": 1 } },
        { selector: 'node[lens = "stage"]', style: { "border-width": 1.2, "border-color": "#82c9ad" } },
        { selector: 'node[lens = "current"], node[lens = "observed"]', style: { "background-image-opacity": 1 } },
        { selector: 'node[kind = "particle"]', style: {
          width: 4, height: 4, label: "", "background-color": "transparent",
          "background-opacity": 0, "border-width": 0,
          "background-image": "data(celestial_image)", "background-fit": "none",
          "background-width": 36, "background-height": 36, "background-clip": "none",
          "background-image-containment": "over", "bounds-expansion": 20,
          "underlay-opacity": 0, "events": "no", "z-index": 40
        } },
        { selector: 'edge[kind = "relation"], edge[kind = "typed_relation"], edge[kind = "relationship"]', style: {
          width: "mapData(strength, 0, 1, 0.3, 2)", "line-color": "#7188b0",
          opacity: (edge: EdgeSingular) => 0.04 + Number(edge.data("strength") || 0.4) * 0.3,
          "curve-style": "bezier", "transition-property": "opacity, width, line-color", "transition-duration": 220
        } },
        { selector: 'edge[kind = "episode_relation"]', style: {
          width: 1, "line-color": "#9a87bd", opacity: 0.08, "line-style": "dashed",
          "line-dash-pattern": [3, 8], "curve-style": "unbundled-bezier",
          "control-point-distances": 34, "control-point-weights": 0.5,
          "transition-property": "opacity, width, line-color", "transition-duration": 220
        } },
        { selector: ".is-dimmed", style: { opacity: 0.05, "text-opacity": 0 } },
        { selector: "node.is-neighbor", style: { opacity: 1, "background-image-opacity": 1 } },
        { selector: "edge.is-neighbor", style: { opacity: 0.95, width: 2.5, "line-color": "#c7d9ff" } },
        { selector: 'edge[kind = "episode_relation"].is-hovered, edge[kind = "episode_relation"].is-neighbor', style: {
          width: 1.8, opacity: 0.92, "line-color": "#d9c6f0", "line-style": "dashed",
          label: "data(bridge_label)", color: "#eadff7", "font-size": 8,
          "text-background-color": "#080a18", "text-background-opacity": 0.88,
          "text-background-padding": "5px", "text-background-shape": "roundrectangle",
          "text-border-color": "#6c5a86", "text-border-width": 1, "text-border-opacity": 0.65,
          "text-rotation": "none", "text-margin-y": -9
        } },
        { selector: "node.overlay-member", style: { opacity: 1, "border-width": 1.2, "border-color": "#fff0ba", "background-image-opacity": 1 } },
        { selector: "edge.overlay-member", style: { opacity: 1, width: 2.8, "line-color": "#f2d99d" } },
        { selector: 'node[kind = "entity"].is-hovered, node[kind = "entity"]:selected', style: {
          label: "data(label)", color: "#f2f6ff", "font-size": 8, "font-weight": 600,
          "text-wrap": "ellipsis", "text-max-width": "150px", "text-valign": "bottom", "text-margin-y": 12,
          "text-outline-color": "#020410", "text-outline-width": 3, "border-width": 0,
          "background-image-opacity": 1, "z-index": 20
        } }
      ],
      layout: { name: "preset", fit: false, animate: false }
    });

    const fitCelestial = () => {
      const celestial = view === "galaxy"
        ? cy.nodes('[kind = "entity"]')
        : cy.nodes('[kind = "entity"], [kind = "subject"]');
      if (celestial.length === 0) return;
      cy.fit(celestial, view === "galaxy" ? 150 : 74);
    };
    fitCelestial();
    const fitFrame = window.requestAnimationFrame(fitCelestial);
    const fitTimer = window.setTimeout(() => {
      cy.resize();
      fitCelestial();
    }, 120);
    const resizeObserver = new ResizeObserver(() => {
      cy.resize();
    });
    resizeObserver.observe(hostElement);
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
    cy.on("tap select", 'node[kind = "entity"], node[kind = "subject"]', (event: EventObjectNode) => {
      clearFocus();
      focusNeighborhood(event.target);
      handlers.current.onSelect(event.target.data());
    });
    cy.on("tap select", 'edge[kind = "relation"], edge[kind = "typed_relation"], edge[kind = "episode_relation"], edge[kind = "relationship"]', (event) => {
      clearFocus();
      event.target.addClass("is-neighbor");
      event.target.connectedNodes().addClass("is-neighbor");
      handlers.current.onSelect(event.target.data());
    });
    cy.on("mouseover", 'edge[kind = "episode_relation"]', (event) => {
      event.target.addClass("is-hovered");
    });
    cy.on("mouseout", 'edge[kind = "episode_relation"]', (event) => {
      event.target.removeClass("is-hovered");
    });
    cy.on("tap", (event) => {
      if (event.target !== cy) return;
      clearFocus();
      handlers.current.onSelect(null);
    });
    cy.on("dragfree", 'node[kind = "entity"]', (event: EventObjectNode) => {
      const position = event.target.position();
      handlers.current.onSaveEntityLayout(String(event.target.data("record_id")), {
        x: Math.round(position.x * 100) / 100,
        y: Math.round(position.y * 100) / 100
      });
    });
    cy.on("dragfree", 'node[kind = "subject"]', (event: EventObjectNode) => {
      const position = event.target.position();
      handlers.current.onSaveSubjectLayout(String(event.target.data("record_id")), {
        x: Math.round((position.x - 600) * 100) / 100,
        y: Math.round((position.y - 360) * 100) / 100
      });
    });
    const transitionReadyAt = window.performance.now() + 700;
    cy.on("zoom", () => {
      if (!transitionArmed.current || window.performance.now() < transitionReadyAt) return;
      if (view === "universe" && cy.zoom() >= 2.6 && focusedGalaxy.current) {
        transitionArmed.current = false;
        handlers.current.onEnterGalaxy(focusedGalaxy.current);
      } else if (view === "galaxy" && cy.zoom() <= 0.14) {
        transitionArmed.current = false;
        handlers.current.onExitGalaxy();
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
      resizeObserver.disconnect();
      cy.destroy();
    };
  }, [
    activeLens,
    motionEnabled,
    planetNodes,
    positions,
    protectedIds,
    relationCounts,
    subjectLayout,
    visibleRelationItems,
    view
  ]);

  useEffect(() => {
    const cy = instance.current;
    if (!cy || cy.destroyed()) return;
    cy.elements().removeClass("overlay-member is-neighbor is-dimmed");
    const entityIds = overlayEntityIds(selected);
    const factIds = overlayFactIds(selected);
    entityIds.forEach((id) => cy.getElementById(id).addClass("overlay-member"));
    cy.edges().filter((edge) =>
      splitIds(String(edge.data("fact_ids") || "")).some((id) => factIds.has(id))
    ).addClass("overlay-member");
    if (selected && isRelationKind(selected.kind)) {
      const edge = cy.getElementById(selected.id);
      edge.addClass("is-neighbor");
      edge.connectedNodes().addClass("is-neighbor");
    } else if (selected && ["entity", "subject"].includes(selected.kind)) {
      const node = cy.getElementById(selected.id);
      const neighborhood = node.closedNeighborhood();
      cy.elements().not(neighborhood).addClass("is-dimmed");
      neighborhood.addClass("is-neighbor");
    }
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
        draggable
        aria-label={`行星 ${node.data.label}`}
        onFocus={() => onSelect(node.data)}
        onClick={(event) => { event.stopPropagation(); onSelect(node.data); }}
      >{node.data.label}</button>)}
    </nav>
    {view === "universe" && <nav className="sr-only" aria-label="可访问主体恒星列表">
      {subjectNodes.map((node) => <button
        key={node.data.id}
        type="button"
        aria-label={`主体恒星 ${node.data.label}`}
        onFocus={() => onSelect(node.data)}
        onClick={(event) => { event.stopPropagation(); onSelect(node.data); }}
      >{node.data.label}</button>)}
    </nav>}
    <nav className="sr-only" aria-label="可访问关系列表">
      {visibleRelationItems.map((edge) => <button
          key={edge.data.id}
          type="button"
          aria-label={`关系 ${edge.data.label}`}
          onFocus={() => onSelect(edge.data)}
          onClick={(event) => { event.stopPropagation(); onSelect(edge.data); }}
        >{edge.data.label}</button>)}
    </nav>
    {view === "galaxy" && visibleRelationItems.length > 0 && <section className="relation-index" aria-label="关系证据">
      <p>关系证据<small>事实是解释层，不是星体</small></p>
      {visibleRelationItems.map((edge) => <button key={`visible:${edge.data.id}`} type="button" onClick={() => onSelect(edge.data)}>
        <strong>{edge.data.label}</strong>
        <span>{edge.data.kind === "episode_relation"
          ? `${edge.data.episode_count} 个共同情节 · 临时关系桥`
          : `${edge.data.evidence_count} 条证据 · ${edge.data.transport}`}</span>
      </button>)}
    </section>}

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
