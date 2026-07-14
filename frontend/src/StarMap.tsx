import cytoscape, { Core, EventObjectNode } from "cytoscape";
import { useEffect, useRef } from "react";
import type { GraphData } from "./api";

type Props = {
  graph: GraphData;
  onSelect: (data: Record<string, string> | null) => void;
};

export function StarMap({ graph, onSelect }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const instance = useRef<Core | null>(null);

  useEffect(() => {
    if (!host.current) return;
    instance.current?.destroy();
    const cy = cytoscape({
      container: host.current,
      elements: [...graph.nodes, ...graph.edges],
      minZoom: 0.18,
      maxZoom: 2.4,
      wheelSensitivity: 0.18,
      style: [
        {
          selector: "node",
          style: {
            label: "data(label)",
            color: "#c9d8eb",
            "font-family": "Inter, ui-sans-serif, system-ui",
            "font-size": 8,
            "text-wrap": "ellipsis",
            "text-max-width": "120px",
            "text-valign": "bottom",
            "text-margin-y": 8,
            "background-color": "#7798c7",
            width: 16,
            height: 16,
            "border-width": 1,
            "border-color": "#b9d7ff",
            "underlay-padding": 7,
            "underlay-color": "#5f8ed5",
            "underlay-opacity": 0.18
          }
        },
        {
          selector: 'node[kind = "core"]',
          style: {
            width: 42,
            height: 42,
            "font-size": 11,
            "font-weight": 700,
            "background-color": "#e4b768",
            "border-color": "#ffe0a3",
            "underlay-color": "#efbd67",
            "underlay-opacity": 0.35
          }
        },
        {
          selector: 'node[kind = "entity"]',
          style: { width: 26, height: 26, "background-color": "#70a7c9" }
        },
        {
          selector: 'node[kind = "fact"]',
          style: { width: 12, height: 12, "background-color": "#8b78bd" }
        },
        {
          selector: 'node[kind = "episode"]',
          style: { width: 32, height: 32, shape: "hexagon", "background-color": "#4f8b86" }
        },
        {
          selector: 'node[kind = "arc"]',
          style: { width: 38, height: 38, shape: "star", "background-color": "#b1844f" }
        },
        {
          selector: 'node[kind = "vault"]',
          style: {
            shape: "diamond",
            width: 25,
            height: 25,
            "background-color": "#b96d7f",
            "border-color": "#f1a7b6",
            "underlay-color": "#c96880"
          }
        },
        {
          selector: 'node[state = "forgotten"], node[state = "isolated"]',
          style: { opacity: 0.25 }
        },
        {
          selector: "edge",
          style: {
            width: 0.7,
            "line-color": "#4f6685",
            opacity: 0.45,
            "curve-style": "bezier"
          }
        },
        {
          selector: 'edge[kind = "protected"]',
          style: { "line-style": "dashed", "line-color": "#b96d7f", width: 1.5 }
        },
        {
          selector: 'edge[kind = "derived"]',
          style: { "line-color": "#6da69f", width: 1.1, opacity: 0.6 }
        },
        {
          selector: ":selected",
          style: { "border-width": 3, "border-color": "#ffffff" }
        }
      ],
      layout: {
        name: "cose",
        animate: false,
        nodeRepulsion: () => 9000,
        idealEdgeLength: () => 90,
        gravity: 0.35,
        numIter: 1200
      }
    });
    cy.on("tap", "node", (event: EventObjectNode) => onSelect(event.target.data()));
    cy.on("tap", (event) => {
      if (event.target === cy) onSelect(null);
    });
    instance.current = cy;
    return () => cy.destroy();
  }, [graph, onSelect]);

  return <div className="star-map" ref={host} aria-label="记忆星图" />;
}
