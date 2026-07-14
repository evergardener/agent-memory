import uuid


def stable_uuid(scope: str, value: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"agent-memory:{scope}:{value}")


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
