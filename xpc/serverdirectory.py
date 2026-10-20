"""公共服务器目录；只接收 Mumble 和明文 FSD 端点。"""
from dataclasses import dataclass
import json
import logging
import urllib.request

log = logging.getLogger("serverdirectory")
DEFAULT_URL = "https://ceruleanavi.net/api/v1/servers"


@dataclass(frozen=True)
class Server:
    name: str
    host: str
    port: int
    location: str = ""

    @property
    def label(self):
        place = f" · {self.location}" if self.location else ""
        return f"{self.name}{place} ({self.host}:{self.port})"


def parse(payload):
    if (not isinstance(payload, dict) or payload.get("status") != 200
            or not isinstance(payload.get("data"), dict)):
        raise ValueError("Invalid server directory response")
    result = {"audio": [], "fsd": []}
    for kind, servers in result.items():
        rows = payload["data"].get(kind, [])
        if not isinstance(rows, list):
            continue
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or row.get("kind") != kind:
                continue
            name, host, port = row.get("name"), row.get("host"), row.get("port")
            if (not isinstance(name, str) or not name.strip()
                    or not isinstance(host, str) or not host.strip()
                    or any(c.isspace() for c in host) or "://" in host
                    or type(port) is not int or not 1 <= port <= 65535):
                continue
            endpoint = (host, port)
            if endpoint in seen:
                continue
            seen.add(endpoint)
            location = row.get("location")
            servers.append(Server(name.strip(), host.strip(), port,
                                  location if isinstance(location, str) else ""))
    return result


def fetch():
    try:
        request = urllib.request.Request(DEFAULT_URL, headers={
            "User-Agent": "Mozilla/5.0 (compatible; CanClient/1.0)",
            "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=8) as response:
            return parse(json.loads(response.read(1024 * 1024)))
    except Exception as exc:
        log.warning("could not load server directory: %s", exc)
        return None
