"""Docker Engine control for the project's containers.

Uses the local Unix socket. Access is protected by the panel's own auth layer;
the socket is never exposed to the browser.
"""
from __future__ import annotations

import json
import struct
from typing import Any
from urllib.parse import quote as urlquote

import aiohttp


def demux_docker_stream(raw: bytes) -> str:
    """Strip Docker's 8-byte multiplexing headers from attached log output."""
    output: list[str] = []
    offset = 0
    while offset + 8 <= len(raw):
        _stream, _pad1, _pad2, _pad3, length = struct.unpack(">BBBBI", raw[offset : offset + 8])
        offset += 8
        chunk = raw[offset : offset + length]
        offset += length
        if chunk:
            output.append(chunk.decode("utf-8", errors="replace"))
    if offset < len(raw):
        output.append(raw[offset:].decode("utf-8", errors="replace"))
    return "".join(output)


class DockerControl:
    def __init__(self, socket_path: str, project: str, client: aiohttp.ClientSession) -> None:
        self.socket_path = socket_path
        self.project = project
        self.client = client

    def _url(self, path: str) -> str:
        return f"http://docker{path}"

    async def containers(self) -> list[dict[str, Any]]:
        filters = json.dumps({"label": [f"com.docker.compose.project={self.project}"]})
        url = self._url(f"/containers/json?all=1&filters={urlquote(filters, safe='')}")
        async with self.client.get(url) as response:
            if response.status != 200:
                raise RuntimeError(f"Docker list failed ({response.status})")
            payload = await response.json()
        result = []
        for item in payload:
            result.append(
                {
                    "id": item.get("Id", "")[:12],
                    "name": (item.get("Names") or [""])[0].lstrip("/"),
                    "image": item.get("Image"),
                    "state": item.get("State"),
                    "status": item.get("Status"),
                    "health": (item.get("Health") or {}).get("Status"),
                }
            )
        return sorted(result, key=lambda row: row["name"])

    async def logs(self, container_id: str, tail: int = 200) -> str:
        url = self._url(
            f"/containers/{urlquote(container_id, safe='')}/logs?stdout=1&stderr=1&tail={tail}"
        )
        async with self.client.get(url) as response:
            if response.status != 200:
                raise RuntimeError(f"Docker logs failed ({response.status})")
            raw = await response.read()
        return demux_docker_stream(raw)

    async def restart(self, container_id: str) -> None:
        url = self._url(f"/containers/{urlquote(container_id, safe='')}/restart")
        async with self.client.post(url) as response:
            if response.status not in (204, 200):
                text = await response.text()
                raise RuntimeError(f"Docker restart failed ({response.status}): {text[:200]}")
