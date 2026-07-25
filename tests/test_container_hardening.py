from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_container_stages_bind_mounted_secrets_then_drops_privileges() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "scripts/docker-entrypoint.sh").read_text(encoding="utf-8")

    assert "apt-get install -y --no-install-recommends gosu" in dockerfile
    assert 'ENTRYPOINT ["agent-memory-entrypoint"]' in dockerfile
    assert "USER agent-memory" not in dockerfile
    assert 'install -m 0400 -o agent-memory -g agent-memory' in entrypoint
    assert '"/tmp/agent-memory-vault-root-key"' in entrypoint
    assert '"/tmp/agent-memory-model-api-key"' in entrypoint
    assert 'exec gosu agent-memory "$@"' in entrypoint
    assert "cat " not in entrypoint
    assert "echo " not in entrypoint
