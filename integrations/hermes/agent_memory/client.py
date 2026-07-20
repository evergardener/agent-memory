import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class ApiUnavailable(RuntimeError):
    """The local Agent Memory API could not complete a request."""


class ApiResponseError(RuntimeError):
    def __init__(self, status: int, code: str):
        super().__init__(f"Agent Memory API rejected request: {code}")
        self.status = status
        self.code = code


@dataclass(frozen=True)
class AgentMemoryHttpClient:
    base_url: str
    service_token: str
    timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        try:
            is_loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            is_loopback = (parsed.hostname or "").casefold() == "localhost"
        if (
            parsed.scheme != "http"
            or not is_loopback
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError(
                "Agent Memory Provider API URL must be a loopback HTTP origin"
            )

    def _execute(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code >= 500:
                raise ApiUnavailable("Agent Memory API unavailable") from error
            try:
                body = json.load(error)
                code = str(body.get("detail") or body.get("error") or "API_ERROR")
            except (json.JSONDecodeError, AttributeError):
                code = "API_ERROR"
            raise ApiResponseError(error.code, code) from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise ApiUnavailable("Agent Memory API unavailable") from error

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url.rstrip("/") + path,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={
                "Authorization": f"Bearer {self.service_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return self._execute(request)

    def get(self, path: str, query: dict[str, str]) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.service_token}"},
            method="GET",
        )
        return self._execute(request)
