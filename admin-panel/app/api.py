"""REST API exposed by the admin panel."""
from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp
from aiohttp import web

from .config import Config
from .database import AdminDB
from .docker_ctl import DockerControl
from .security import Principal

log = logging.getLogger("orion.admin.api")


def _principal(request: web.Request) -> Principal:
    return request["principal"]


def _config(request: web.Request) -> Config:
    return request.app["config"]


def _db(request: web.Request) -> AdminDB:
    return request.app["db"]


def _docker(request: web.Request) -> DockerControl:
    return request.app["docker"]


def _require(request: web.Request, permission: str) -> None:
    if not _principal(request).has(permission):
        raise web.HTTPForbidden(reason=f"Permission {permission} required")


def _json(data: Any, *, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _error(message: str, status: int = 400) -> web.Response:
    return _json({"error": message}, status=status)


async def health(request: web.Request) -> web.Response:
    return _json({"status": "ok", "service": "orion-admin"})


async def me(request: web.Request) -> web.Response:
    principal = _principal(request)
    return _json(
        {
            "user_id": principal.user_id,
            "username": principal.username,
            "permissions": sorted(principal.permissions),
            "via": principal.via,
            "csrf": request.get("csrf", ""),
            "writes_enabled": _config(request).allow_writes,
        }
    )


def _docker_error(exc: Exception) -> web.Response:
    log.warning("Docker control unavailable: %s", exc)
    return _error("Docker control is unavailable", 503)


async def overview(request: web.Request) -> web.Response:
    _require(request, "read")
    data = await _db(request).overview()
    try:
        data["containers"] = await _docker(request).containers()
    except (RuntimeError, OSError, aiohttp.ClientError) as exc:
        data["containers"] = []
        data["containers_error"] = str(exc)
    return _json(data)


async def tables(request: web.Request) -> web.Response:
    _require(request, "read")
    return _json({"tables": await _db(request).tables()})


async def table_rows(request: web.Request) -> web.Response:
    _require(request, "read")
    name = request.match_info["name"]
    try:
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
    except ValueError:
        return _error("limit and offset must be integers")
    try:
        data = await _db(request).table_rows(
            name,
            limit=limit,
            offset=offset,
            sort=request.query.get("sort", ""),
            desc=request.query.get("direction", "asc").lower() == "desc",
        )
    except LookupError as exc:
        return _error(str(exc), 404)
    return _json(data)


async def insert_row(request: web.Request) -> web.Response:
    _require(request, "write")
    if not _config(request).allow_writes:
        return _error("Writes are disabled", 403)
    body = await request.json()
    try:
        row = await _db(request).insert_row(request.match_info["name"], body)
    except PermissionError as exc:
        return _error(str(exc), 403)
    except (ValueError, LookupError) as exc:
        return _error(str(exc))
    log.info("AUDIT user=%s insert table=%s", _principal(request).user_id, request.match_info["name"])
    return _json({"row": row}, status=201)


async def update_row(request: web.Request) -> web.Response:
    _require(request, "write")
    if not _config(request).allow_writes:
        return _error("Writes are disabled", 403)
    body = await request.json()
    try:
        row = await _db(request).update_row(request.match_info["name"], body.get("key", {}), body.get("values", {}))
    except PermissionError as exc:
        return _error(str(exc), 403)
    except LookupError as exc:
        return _error(str(exc), 404)
    except (ValueError, TypeError) as exc:
        return _error(str(exc))
    log.info("AUDIT user=%s update table=%s", _principal(request).user_id, request.match_info["name"])
    return _json({"row": row})


async def delete_row(request: web.Request) -> web.Response:
    _require(request, "write")
    if not _config(request).allow_writes:
        return _error("Writes are disabled", 403)
    body = await request.json()
    try:
        deleted = await _db(request).delete_row(request.match_info["name"], body.get("key", {}))
    except PermissionError as exc:
        return _error(str(exc), 403)
    except LookupError as exc:
        return _error(str(exc), 404)
    except (ValueError, TypeError) as exc:
        return _error(str(exc))
    log.info("AUDIT user=%s delete table=%s", _principal(request).user_id, request.match_info["name"])
    return _json({"deleted": deleted})


async def config_all(request: web.Request) -> web.Response:
    _require(request, "read")
    return _json({"config": await _db(request).config_all()})


async def config_set(request: web.Request) -> web.Response:
    _require(request, "write")
    body = await request.json()
    key = request.match_info["key"]
    if not key or len(key) > 200:
        return _error("Invalid config key")
    await _db(request).config_set(key, body.get("value"))
    log.info(
        "AUDIT user=%s config_set key=%s (bot requires restart to reload)",
        _principal(request).user_id,
        key,
    )
    return _json({"key": key, "value": body.get("value"), "restart_required": True})


async def migrations(request: web.Request) -> web.Response:
    _require(request, "read")
    return _json({"migrations": await _db(request).migrations()})


async def containers(request: web.Request) -> web.Response:
    _require(request, "read")
    try:
        return _json({"containers": await _docker(request).containers()})
    except (RuntimeError, OSError, aiohttp.ClientError) as exc:
        return _docker_error(exc)


async def container_logs(request: web.Request) -> web.Response:
    _require(request, "read")
    try:
        tail = int(request.query.get("tail", "200"))
    except ValueError:
        return _error("tail must be an integer")
    try:
        text = await _docker(request).logs(request.match_info["cid"], tail=min(max(tail, 1), 2000))
    except (RuntimeError, OSError, aiohttp.ClientError) as exc:
        return _docker_error(exc)
    return _json({"logs": text})


async def container_restart(request: web.Request) -> web.Response:
    _require(request, "control")
    cid = request.match_info["cid"]
    try:
        containers_ = await _docker(request).containers()
        target = next((item for item in containers_ if item["id"] == cid or item["name"] == cid), None)
        if target is None:
            return _error("Container not found", 404)
        if target["name"] in {"orion-db", "orion-updater"}:
            return _error("Database and updater cannot be restarted from the panel", 403)
        await _docker(request).restart(cid)
    except (RuntimeError, OSError, aiohttp.ClientError) as exc:
        return _docker_error(exc)
    log.info("AUDIT user=%s restart container=%s", _principal(request).user_id, target["name"])
    return _json({"restarted": target["name"]})


async def backups(request: web.Request) -> web.Response:
    _require(request, "read")
    directory = _config(request).backups_dir
    if not os.path.isdir(directory):
        return _json({"backups": [], "directory": directory})
    names = sorted(
        (name for name in os.listdir(directory) if name.endswith(".sql.gz")),
        reverse=True,
    )
    return _json(
        {
            "backups": [
                {"name": name, "size": os.path.getsize(os.path.join(directory, name))} for name in names
            ],
            "directory": directory,
        }
    )


async def backup_download(request: web.Request) -> web.Response:
    _require(request, "read")
    name = os.path.basename(request.match_info["name"])
    if not name.endswith(".sql.gz"):
        return _error("Invalid backup name")
    path = os.path.join(_config(request).backups_dir, name)
    if not os.path.isfile(path):
        return _error("Backup not found", 404)
    return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{name}"'})


def setup_api(app: web.Application) -> None:
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/me", me)
    app.router.add_get("/api/overview", overview)
    app.router.add_get("/api/tables", tables)
    app.router.add_get("/api/tables/{name}", table_rows)
    app.router.add_post("/api/tables/{name}", insert_row)
    app.router.add_patch("/api/tables/{name}", update_row)
    app.router.add_delete("/api/tables/{name}", delete_row)
    app.router.add_get("/api/config", config_all)
    app.router.add_put("/api/config/{key}", config_set)
    app.router.add_get("/api/migrations", migrations)
    app.router.add_get("/api/containers", containers)
    app.router.add_get("/api/containers/{cid}/logs", container_logs)
    app.router.add_post("/api/containers/{cid}/restart", container_restart)
    app.router.add_get("/api/backups", backups)
    app.router.add_get("/api/backups/{name}", backup_download)
