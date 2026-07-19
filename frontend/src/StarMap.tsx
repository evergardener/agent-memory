import cytoscape, { Core, EdgeSingular, EventObjectNode, NodeSingular } from "cytoscape";
import { CSSProperties, useEffect, useMemo, useRef } from "react";
import type { GraphData } from "./api";

type Props = {
  graph: GraphData;
  viewGalaxy: string | null;
  motionEnabled: boolean;
  onSelect: (data: Record<string, string> | null) => void;
  onGalaxySelect: (galaxyId: string) => void;
  onUniverse: () => void;
};

const GALAXIES = [
  { id: "long_term", label: "长期记忆星系", color: "#d7b48a", x: 0.22, y: 0.30 },
  { id: "stage", label: "阶段记忆星系", color: "#82c9ad", x: 0.76, y: 0.27 },
  { id: "current", label: "当前状态星系", color: "#8fb9e8", x: 0.79, y: 0.72 },
  { id: "episode", label: "情节星系", color: "#b6a0e6", x: 0.28, y: 0.76 },
  { id: "arc", label: "长期脉络星系", color: "#e2a9c4", x: 0.50, y: 0.82 },
  { id: "vault", label: "保护资源星系", color: "#e09aac", x: 0.51, y: 0.18 }
] as const;

const AUTOMATED_FACT_LABEL = /\b(?:project:isolated|service:relay)-\d{8}T\d{6}Z\b/i;

type GalaxyId = typeof GALAXIES[number]["id"];
type GraphNode = GraphData["nodes"][number];

function hash(value: string) {
  let result = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    result ^= value.charCodeAt(index);
    result = Math.imul(result, 16777619);
  }
  return result >>> 0;
}

function factGalaxy(data: Record<string, string>): GalaxyId {
  if (data.fact_type === "stage") return "stage";
  if (data.fact_type === "current" || data.fact_type === "observed") return "current";
  if (data.fact_type === "long_term") return "long_term";
  return "episode";
}

function nodeKind(id: string) {
  return id.split(":", 1)[0];
}

function buildProjection(graph: GraphData, viewGalaxy: string | null) {
  const nodeById = new Map(graph.nodes.map((node) => [node.data.id, node]));
  const entities = graph.nodes.filter((node) => node.data.kind === "entity");
  const facts = graph.nodes.filter((node) => node.data.kind === "fact");
  const factById = new Map(facts.map((node) => [node.data.id, node]));
  const factEntities = new Map<string, Set<string>>();
  const derivedFacts: Record<"episode" | "arc" | "vault", Set<string>> = {
    episode: new Set(), arc: new Set(), vault: new Set()
  };
  const memberships = new Map<string, Map<GalaxyId, number>>();

  const addMembership = (entityId: string, galaxy: GalaxyId, weight = 1) => {
    const scores = memberships.get(entityId) || new Map<GalaxyId, number>();
    scores.set(galaxy, (scores.get(galaxy) || 0) + weight);
    memberships.set(entityId, scores);
  };

  graph.edges.forEach((edge) => {
    const { source, target, kind } = edge.data;
    const sourceKind = nodeKind(source);
    const targetKind = nodeKind(target);
    if (kind === "evidence") {
      const entityId = sourceKind === "entity" ? source : target;
      const factId = sourceKind === "fact" ? source : target;
      if (nodeKind(entityId) === "entity" && nodeKind(factId) === "fact") {
        const related = factEntities.get(factId) || new Set<string>();
        related.add(entityId);
        factEntities.set(factId, related);
      }
    }
    if (kind === "derived") {
      const derivedKind = ([sourceKind, targetKind].find((value) => value === "episode" || value === "arc")) as "episode" | "arc" | undefined;
      const entityId = sourceKind === "entity" ? source : targetKind === "entity" ? target : null;
      const factId = sourceKind === "fact" ? source : targetKind === "fact" ? target : null;
      if (derivedKind && entityId) addMembership(entityId, derivedKind, 2);
      if (derivedKind && factId) derivedFacts[derivedKind].add(factId);
    }
    if (kind === "protected") {
      const entityId = sourceKind === "entity" ? source : targetKind === "entity" ? target : null;
      const factId = sourceKind === "fact" ? source : targetKind === "fact" ? target : null;
      if (entityId) addMembership(entityId, "vault", 2);
      if (factId) derivedFacts.vault.add(factId);
    }
  });

  factEntities.forEach((entityIds, factId) => {
    const fact = factById.get(factId);
    if (!fact) return;
    const galaxy = factGalaxy(fact.data);
    entityIds.forEach((entityId) => addMembership(entityId, galaxy));
  });
  derivedFacts.vault.forEach((factId) => factEntities.get(factId)?.forEach((entityId) => addMembership(entityId, "vault", 2)));

  const primaryGalaxy = new Map<string, GalaxyId>();
  entities.forEach((entity) => {
    const scores = memberships.get(entity.data.id);
    const primary = scores
      ? [...scores.entries()].sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))[0]?.[0]
      : "long_term";
    primaryGalaxy.set(entity.data.id, primary || "long_term");
  });

  const visibleEntities = entities.filter((entity) => {
    if (!viewGalaxy) return true;
    return memberships.get(entity.data.id)?.has(viewGalaxy as GalaxyId) || primaryGalaxy.get(entity.data.id) === viewGalaxy;
  });
  const visibleIds = new Set(visibleEntities.map((entity) => entity.data.id));

  const factMatchesView = (factId: string, fact: GraphNode) => {
    if (!viewGalaxy) return true;
    if (viewGalaxy === "episode" || viewGalaxy === "arc" || viewGalaxy === "vault") return derivedFacts[viewGalaxy].has(factId);
    return factGalaxy(fact.data) === viewGalaxy;
  };

  const projectedEdges = new Map<string, {
    source: string;
    target: string;
    strength: number;
    factIds: Set<string>;
  }>();
  facts.forEach((fact) => {
    if (!factMatchesView(fact.data.id, fact)) return;
    const entityIds = [...(factEntities.get(fact.data.id) || [])].filter((id) => visibleIds.has(id)).sort();
    for (let index = 1; index < entityIds.length; index += 1) {
      const pair = [entityIds[index - 1], entityIds[index]].sort();
      const key = pair.join("|");
      const existing = projectedEdges.get(key) || { source: pair[0], target: pair[1], strength: 0, factIds: new Set<string>() };
      existing.strength = Math.max(existing.strength, Number(fact.data.confidence || 0.55));
      existing.factIds.add(fact.data.id);
      projectedEdges.set(key, existing);
    }
  });

  const relationCount = new Map<string, number>();
  projectedEdges.forEach((edge) => {
    relationCount.set(edge.source, (relationCount.get(edge.source) || 0) + 1);
    relationCount.set(edge.target, (relationCount.get(edge.target) || 0) + 1);
  });

  const nodes = visibleEntities.map((node) => ({
    ...node,
    data: {
      ...node.data,
      galaxy: viewGalaxy || primaryGalaxy.get(node.data.id) || "long_term",
      relation_count: String(relationCount.get(node.data.id) || 0)
    }
  }));
  const edges = [...projectedEdges.entries()]
    .sort((left, right) => right[1].strength - left[1].strength)
    .slice(0, 280)
    .map(([key, edge]) => ({ data: {
      id: `relation:${key}`,
      source: edge.source,
      target: edge.target,
      kind: "relation",
      strength: String(edge.strength),
      fact_ids: [...edge.factIds].join("|")
    } }));

  const annotations = facts
    .filter((fact) => factMatchesView(fact.data.id, fact)
      && (factEntities.get(fact.data.id)?.size || 0) >= 2
      && !AUTOMATED_FACT_LABEL.test(fact.data.label || ""))
    .sort((left, right) => Number(right.data.confidence || 0) - Number(left.data.confidence || 0))
    .slice(0, 5);
  const galaxyCounts = new Map<GalaxyId, number>();
  entities.forEach((entity) => {
    const galaxy = primaryGalaxy.get(entity.data.id) || "long_term";
    galaxyCounts.set(galaxy, (galaxyCounts.get(galaxy) || 0) + 1);
  });
  if (viewGalaxy) galaxyCounts.set(viewGalaxy as GalaxyId, visibleEntities.length);
  return { nodes, edges, annotations, galaxyCounts };
}

function positionEntities(nodes: GraphData["nodes"], viewGalaxy: string | null) {
  const positions = new Map<string, { x: number; y: number }>();
  if (viewGalaxy) {
    [...nodes].sort((left, right) => left.data.id.localeCompare(right.data.id)).forEach((node, index) => {
      const seed = hash(node.data.id);
      const angle = index * 2.399963229728653 + (seed % 100) / 95;
      const radius = 42 + Math.sqrt(index + 1) * 42 + (seed % 21);
      positions.set(node.data.id, { x: 600 + Math.cos(angle) * radius, y: 360 + Math.sin(angle) * radius * 0.72 });
    });
    return positions;
  }
  GALAXIES.forEach((galaxy) => {
    const members = nodes.filter((node) => node.data.galaxy === galaxy.id).sort((left, right) => left.data.id.localeCompare(right.data.id));
    members.forEach((node, index) => {
      const seed = hash(node.data.id);
      const angle = index * 2.399963229728653 + (seed % 100) / 80;
      const radius = 20 + Math.sqrt(index + 1) * 31 + (seed % 17);
      positions.set(node.data.id, {
        x: galaxy.x * 1200 + Math.cos(angle) * radius,
        y: galaxy.y * 720 + Math.sin(angle) * radius * (galaxy.id === "arc" || galaxy.id === "vault" ? 0.62 : 0.78)
      });
    });
  });
  return positions;
}

export function StarMap({ graph, viewGalaxy, motionEnabled, onSelect, onGalaxySelect, onUniverse }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const instance = useRef<Core | null>(null);
  const projection = useMemo(() => buildProjection(graph, viewGalaxy), [graph, viewGalaxy]);
  const positions = useMemo(() => positionEntities(projection.nodes, viewGalaxy), [projection.nodes, viewGalaxy]);
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

  const activateFact = (fact: GraphNode) => {
    onSelect(fact.data);
    const cy = instance.current;
    if (!cy || cy.destroyed()) return;
    const relatedEdges = cy.edges().filter((edge) => String(edge.data("fact_ids") || "").split("|").includes(fact.data.id)).slice(0, 6);
    relatedEdges.forEach((edge, index) => {
      const particle = cy.add({
        group: "nodes",
        data: { id: `particle:${fact.data.id}:${index}:${Date.now()}`, kind: "particle" },
        position: edge.source().position(),
        selectable: false,
        grabbable: false
      }).nodes()[0];
      const start = window.performance.now() + index * 110;
      const animate = (now: number) => {
        if (cy.destroyed() || !particle || particle.removed()) return;
        const progress = Math.max(0, Math.min(1, (now - start) / 1250));
        const source = edge.source().position();
        const target = edge.target().position();
        particle.position({ x: source.x + (target.x - source.x) * progress, y: source.y + (target.y - source.y) * progress });
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
      ...projection.nodes.map((element) => ({
        ...element,
        position: positions.get((element.data as Record<string, string>).id),
        data: { ...element.data, relation_count: Number(element.data.relation_count || 0) }
      })),
      ...projection.edges.map((element) => ({
        ...element,
        data: { ...element.data, strength: Number(element.data.strength || 0.55) }
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
          label: "", width: "mapData(relation_count, 0, 12, 10, 18)", height: "mapData(relation_count, 0, 12, 10, 18)",
          shape: "ellipse", "background-color": "#b9c9e7", "border-width": 0.7, "border-color": "#ffffff",
          "underlay-padding": 7, "underlay-color": "#aec8ff", "underlay-opacity": 0.16,
          "underlay-shape": "ellipse",
          "transition-property": "opacity, border-width, underlay-opacity, text-opacity", "transition-duration": 220
        } },
        { selector: 'node[galaxy = "long_term"]', style: { "background-color": "#d7b48a", "underlay-color": "#d7b48a" } },
        { selector: 'node[galaxy = "stage"]', style: { "background-color": "#82c9ad", "underlay-color": "#82c9ad" } },
        { selector: 'node[galaxy = "current"]', style: { "background-color": "#8fb9e8", "underlay-color": "#8fb9e8" } },
        { selector: 'node[galaxy = "episode"]', style: { "background-color": "#b6a0e6", "underlay-color": "#b6a0e6" } },
        { selector: 'node[galaxy = "arc"]', style: { "background-color": "#e2a9c4", "underlay-color": "#e2a9c4" } },
        { selector: 'node[galaxy = "vault"]', style: { "background-color": "#e09aac", "underlay-color": "#e09aac" } },
        { selector: 'node[state = "forgotten"], node[state = "isolated"]', style: { opacity: 0.2 } },
        { selector: 'node[kind = "particle"]', style: {
          width: 5, height: 5, label: "", "background-color": "#fff0ba", "border-width": 0,
          "underlay-padding": 9, "underlay-color": "#ffd98c", "underlay-opacity": 0.66, "events": "no", "z-index": 40
        } },
        { selector: "edge", style: {
          width: "mapData(strength, 0, 1, 0.25, 1.8)", "line-color": "#7188b0",
          opacity: (edge: EdgeSingular) => 0.025 + Number(edge.data("strength") || 0.4) * 0.28,
          "curve-style": "bezier", "transition-property": "opacity, width, line-color", "transition-duration": 220
        } },
        { selector: ".is-dimmed", style: { opacity: 0.05, "text-opacity": 0 } },
        { selector: "node.is-neighbor", style: { opacity: 1, "border-width": 1.6, "underlay-opacity": 0.44 } },
        { selector: "edge.is-neighbor", style: { opacity: 0.95, width: 2.4, "line-color": "#c7d9ff" } },
        { selector: 'node[kind = "entity"].is-hovered, node[kind = "entity"]:selected', style: {
          label: "data(label)", color: "#f2f6ff", "font-size": 8, "font-weight": 600,
          "text-wrap": "ellipsis", "text-max-width": "150px", "text-valign": "bottom", "text-margin-y": 11,
          "text-outline-color": "#020410", "text-outline-width": 3, "border-width": 2, "border-color": "#ffffff",
          "underlay-opacity": 0.58, "z-index": 20
        } }
      ],
      layout: { name: "preset", fit: false, animate: false }
    });

    const fitEntities = () => {
      const entityNodes = cy.nodes('[kind = "entity"]');
      if (entityNodes.length > 0) cy.fit(entityNodes, viewGalaxy ? 120 : 72);
    };
    fitEntities();
    const fitFrame = window.requestAnimationFrame(fitEntities);
    const fitTimer = window.setTimeout(() => {
      cy.resize();
      fitEntities();
    }, 120);

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
    cy.on("tap", 'node[kind = "entity"]', (event: EventObjectNode) => {
      clearFocus(); focusNeighborhood(event.target); onSelect(event.target.data());
    });
    // Selection is a second, input-method-independent path. It keeps entity
    // governance reachable when a browser/device does not emit Cytoscape's
    // synthetic `tap` event consistently.
    cy.on("select", 'node[kind = "entity"]', (event: EventObjectNode) => {
      clearFocus(); focusNeighborhood(event.target); onSelect(event.target.data());
    });
    cy.on("tap", (event) => {
      if (event.target !== cy) return;
      clearFocus();
      if (viewGalaxy) { onSelect(null); return; }
      const nearest = GALAXIES
        .map((galaxy) => ({ galaxy, distance: Math.hypot(event.position.x - galaxy.x * 1200, (event.position.y - galaxy.y * 720) / 0.78) }))
        .filter(({ galaxy }) => (projection.galaxyCounts.get(galaxy.id) || 0) > 0)
        .sort((left, right) => left.distance - right.distance)[0];
      if (nearest && nearest.distance < 180) onGalaxySelect(nearest.galaxy.id);
      else onSelect(null);
    });

    let wheelIntent = 0;
    let wheelIntentAt = 0;
    const handleSemanticWheel = (event: WheelEvent) => {
      const now = window.performance.now();
      if (now - wheelIntentAt > 420 || Math.sign(wheelIntent) !== Math.sign(event.deltaY)) wheelIntent = 0;
      wheelIntentAt = now;
      wheelIntent += event.deltaY;
      if (viewGalaxy && wheelIntent > 320) {
        wheelIntent = 0;
        onUniverse();
        return;
      }
      if (!viewGalaxy && wheelIntent < -420 && host.current) {
        const rect = host.current.getBoundingClientRect();
        const nearest = GALAXIES
          .map((galaxy) => ({
            galaxy,
            distance: Math.hypot(
              event.clientX - (rect.left + galaxy.x * rect.width),
              event.clientY - (rect.top + galaxy.y * rect.height)
            )
          }))
          .filter(({ galaxy }) => (projection.galaxyCounts.get(galaxy.id) || 0) > 0)
          .sort((left, right) => left.distance - right.distance)[0];
        if (nearest && nearest.distance < Math.min(rect.width, rect.height) * 0.25) {
          wheelIntent = 0;
          onGalaxySelect(nearest.galaxy.id);
        }
      }
    };
    host.current.addEventListener("wheel", handleSemanticWheel, { passive: true });

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
          if (node.grabbed() || node.hasClass("is-hovered")) return;
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
    return () => {
      window.clearTimeout(fitTimer);
      window.cancelAnimationFrame(fitFrame);
      window.cancelAnimationFrame(motionFrame);
      host.current?.removeEventListener("wheel", handleSemanticWheel);
      cy.destroy();
    };
  }, [motionEnabled, onGalaxySelect, onSelect, onUniverse, positions, projection, viewGalaxy]);

  const visibleGalaxies = viewGalaxy ? GALAXIES.filter((galaxy) => galaxy.id === viewGalaxy) : GALAXIES.filter((galaxy) => (projection.galaxyCounts.get(galaxy.id) || 0) > 0);
  return <div className={`star-map-shell ${viewGalaxy ? "galaxy-view" : "universe-view"}`} data-motion={motionEnabled ? "floating-5px" : "static"}>
    <div className="galaxy-band" />
    {visibleGalaxies.map((galaxy) => <div key={galaxy.id} className={`galaxy-aura galaxy-${galaxy.id}`} style={{
      "--galaxy-color": galaxy.color,
      left: viewGalaxy ? "50%" : `${galaxy.x * 100}%`,
      top: viewGalaxy ? "50%" : `${galaxy.y * 100}%`
    } as CSSProperties} />)}
    <div className="star-map" ref={host} aria-label={viewGalaxy ? `${viewGalaxy} 子星系实体图` : "记忆主宇宙"} />
    <nav className="sr-only" aria-label="可访问实体列表">
      {projection.nodes
        .filter((node) => (node.data as Record<string, string>).kind === "entity")
        .map((node) => {
          const data = node.data as Record<string, string>;
          return <button
            key={data.id}
            type="button"
            aria-label={`实体 ${data.label}`}
            onClick={() => onSelect(data)}
          >{data.label}</button>;
        })}
    </nav>

    {!viewGalaxy && <div className="subject-cores" aria-label="主体恒星群">
      {subjectLayout.map(({ node, x, y }) => <button
        key={node.data.id}
        type="button"
        className={`stellar-core ${node.data.subject_kind === "user" ? "stellar-user" : "stellar-profile"}`}
        style={{ left: `calc(50% + ${x}px)`, top: `calc(50% + ${y}px)`, color: node.data.color } as CSSProperties}
        onClick={() => onSelect(node.data)}
        aria-label={`${node.data.label} · ${node.data.subject_kind === "user" ? "用户主体" : "profile 人格主体"}`}
      >
        <i className="stellar-glow" /><i className="stellar-ray ray-horizontal" /><i className="stellar-ray ray-vertical" /><b /><span>{node.data.label}</span>
      </button>)}
    </div>}

    <div className="galaxy-labels">
      {visibleGalaxies.map((galaxy) => <button type="button" aria-label={`进入${galaxy.label}`} title={`进入${galaxy.label}`} onClick={() => onGalaxySelect(galaxy.id)} key={galaxy.id} style={{
        left: viewGalaxy ? "50%" : `${galaxy.x * 100}%`,
        top: viewGalaxy ? "17%" : `${galaxy.y * 100 + 10}%`,
        color: galaxy.color
      }}>{galaxy.label}<small>{projection.galaxyCounts.get(galaxy.id) || 0} 实体</small></button>)}
    </div>

    {viewGalaxy && projection.annotations.length > 0 && <section className="galaxy-facts" aria-label="关联陈述">
      <p>关联陈述</p>
      {projection.annotations.map((fact) => <button key={fact.data.id} type="button" onClick={() => activateFact(fact)}>
        <span>{fact.data.state === "candidate" ? "待确认" : "已记录"} · {Math.round(Number(fact.data.confidence || 0) * 100)}%</span>
        {fact.data.label}
      </button>)}
    </section>}
  </div>;
}
