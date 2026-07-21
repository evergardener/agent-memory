#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import subprocess
import sys
from typing import Any


def _docker_json(arguments: list[str]) -> Any:
    completed = subprocess.run(
        ["docker", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout or "[]")


def docker_networks() -> list[dict[str, Any]]:
    completed = subprocess.run(
        ["docker", "network", "ls", "-q"],
        check=True,
        capture_output=True,
        text=True,
    )
    identifiers = [value for value in completed.stdout.splitlines() if value]
    if not identifiers:
        return []
    return list(_docker_json(["network", "inspect", *identifiers]))


def overlapping_networks(
    backend: str,
    edge: str,
    networks: list[dict[str, Any]],
    *,
    ignored_names: set[str] | None = None,
) -> list[str]:
    requested = (ipaddress.ip_network(backend), ipaddress.ip_network(edge))
    ignored_names = ignored_names or set()
    conflicts: list[str] = []
    for network in networks:
        name = str(network.get("Name") or "unknown")
        if name in ignored_names:
            continue
        for config in (network.get("IPAM") or {}).get("Config") or []:
            subnet = config.get("Subnet")
            if not subnet:
                continue
            current = ipaddress.ip_network(subnet)
            if any(candidate.overlaps(current) for candidate in requested):
                conflicts.append(f"{name}={current}")
    return sorted(set(conflicts))


def port_is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        try:
            candidate.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def project_containers(project: str) -> list[str]:
    completed = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.Names}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [value for value in completed.stdout.splitlines() if value]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", required=True)
    parser.add_argument("--edge", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--project", required=True)
    parser.add_argument("--mode", choices=("new", "existing"), required=True)
    arguments = parser.parse_args()

    ignored = set()
    if arguments.mode == "existing":
        ignored = {
            f"{arguments.project}_backend",
            f"{arguments.project}_edge",
        }
    conflicts = overlapping_networks(
        arguments.backend,
        arguments.edge,
        docker_networks(),
        ignored_names=ignored,
    )
    if conflicts:
        raise SystemExit(
            "PREDEPLOY_HOST_CHECK_FAILED: Docker network overlap: "
            + ", ".join(conflicts)
        )

    containers = project_containers(arguments.project)
    if arguments.mode == "new" and containers:
        raise SystemExit(
            "PREDEPLOY_HOST_CHECK_FAILED: Compose project already has containers: "
            + ", ".join(containers)
        )
    if (arguments.mode == "new" or not containers) and not port_is_available(
        arguments.port
    ):
        raise SystemExit(
            f"PREDEPLOY_HOST_CHECK_FAILED: 127.0.0.1:{arguments.port} is unavailable"
        )

    print(
        json.dumps(
            {
                "status": "PASS",
                "check": "production_host",
                "mode": arguments.mode,
                "project": arguments.project,
                "port": arguments.port,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"PREDEPLOY_HOST_CHECK_FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
