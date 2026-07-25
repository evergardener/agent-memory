#!/usr/bin/env python3
import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_NAME = "agent_memory"
MARKER = ".agent-memory-managed"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "integrations" / "hermes" / PLUGIN_NAME
PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def target_for(hermes_home: Path, profile: str | None = None) -> Path:
    if profile is None:
        return hermes_home / "plugins" / PLUGIN_NAME
    if not PROFILE_NAME.fullmatch(profile):
        raise RuntimeError(f"invalid Hermes profile name: {profile!r}")
    return hermes_home / "profiles" / profile / "plugins" / PLUGIN_NAME


def install(hermes_home: Path, profile: str | None = None) -> None:
    target = target_for(hermes_home, profile)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not (target / MARKER).is_file():
        raise RuntimeError(f"refusing to replace unmanaged directory: {target}")
    with tempfile.TemporaryDirectory(prefix="agent-memory-install-", dir=target.parent) as temp:
        staged = Path(temp) / PLUGIN_NAME
        shutil.copytree(
            SOURCE,
            staged,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (staged / MARKER).write_text("managed by agent-memory\n", encoding="utf-8")
        backup = target.with_name(f".{PLUGIN_NAME}.previous-{os.getpid()}")
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staged, target)
        except Exception:
            if backup.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    print(f"installed {PLUGIN_NAME} at {target}")
    if profile is None:
        print("next: run `hermes memory setup agent_memory` and restart Hermes")
    else:
        print(
            f"next: run `hermes -p {profile} memory setup agent_memory` when "
            "configuring it for the first time"
        )
        print(f"then restart Hermes or open a new `{profile}` session")


def uninstall(hermes_home: Path, profile: str | None = None) -> None:
    target = target_for(hermes_home, profile)
    if not target.exists():
        print(f"not installed: {target}")
        return
    if not (target / MARKER).is_file():
        raise RuntimeError(f"refusing to remove unmanaged directory: {target}")
    shutil.rmtree(target)
    print(f"removed {target}; Hermes configuration was left unchanged")


def status(hermes_home: Path, profile: str | None = None) -> None:
    target = target_for(hermes_home, profile)
    if (target / MARKER).is_file():
        state = "managed"
    else:
        state = "unmanaged" if target.exists() else "absent"
    print(f"{state}: {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the Hermes Agent Memory provider")
    parser.add_argument("action", choices=("install", "upgrade", "uninstall", "status"))
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")),
    )
    parser.add_argument(
        "--profile",
        help=(
            "install into HERMES_HOME/profiles/<profile>/plugins instead of "
            "the global plugin directory"
        ),
    )
    args = parser.parse_args()
    try:
        action = install if args.action == "upgrade" else {
            "install": install,
            "uninstall": uninstall,
            "status": status,
        }[args.action]
        action(args.hermes_home.expanduser().resolve(), args.profile)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
