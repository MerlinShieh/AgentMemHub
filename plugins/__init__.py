from __future__ import annotations

import importlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app_paths import CONFIG_PATH
from repair_sources import atomic_write_config

PLUGIN_ROOT = Path(__file__).resolve().parent
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def require_local_url(url: str) -> None:
    parsed = urlsplit(url)
    host = str(parsed.hostname or "").casefold()
    if parsed.username or parsed.password:
        raise ValueError("插件地址不能带账号密码")
    if parsed.scheme not in {"http", "https"} or host not in LOCAL_HOSTS:
        raise ValueError("插件只能连接本机 127.0.0.1 / localhost")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise RuntimeError("插件网关不允许跳转到其它地址")


def local_http_json(
    method: str,
    url: str,
    *,
    api_key: str = "",
    payload: dict[str, Any] | None = None,
    timeout: float = 2.5,
) -> Any:
    require_local_url(url)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("网关返回了非 JSON") from exc


class HubPlugin:
    """Optional sidecar. Default off. Must not write vendor conversation stores."""

    id = ""
    title = ""
    summary = ""
    docs_url = ""
    capabilities: tuple[str, ...] = ()
    secret_fields: tuple[str, ...] = ()

    def settings(self) -> dict[str, Any]:
        return {"enabled": False}

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.settings()

    def health(self) -> dict[str, Any]:
        return {"ok": False, "status": "disabled", "detail": ""}

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return []


def _read_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def plugin_settings(plugin_id: str) -> dict[str, Any]:
    block = _read_config().get("plugins")
    if not isinstance(block, dict):
        return {}
    raw = block.get(plugin_id)
    return raw if isinstance(raw, dict) else {}


def write_plugin_settings(plugin_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    data = _read_config()
    plugins = data.get("plugins")
    plugins = plugins if isinstance(plugins, dict) else {}
    plugins[plugin_id] = settings
    data["plugins"] = plugins
    atomic_write_config(data)
    return settings


def discover_plugins() -> list[HubPlugin]:
    found: list[HubPlugin] = []
    if not PLUGIN_ROOT.is_dir():
        return found
    for child in sorted(PLUGIN_ROOT.iterdir()):
        manifest = child / "plugin.json"
        if not child.is_dir() or not manifest.is_file():
            continue
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
            module_name = f"plugins.{child.name}.{meta.get('module') or 'plugin'}"
            class_name = str(meta.get("class") or "Plugin")
            module = importlib.import_module(module_name)
            plugin = getattr(module, class_name)()
            if getattr(plugin, "id", ""):
                found.append(plugin)
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not take down the hub
            found.append(_BrokenPlugin(child.name, str(exc)))
    return found


class _BrokenPlugin(HubPlugin):
    def __init__(self, plugin_id: str, detail: str) -> None:
        self.id = plugin_id
        self.title = plugin_id
        self.summary = "插件加载失败"
        self._detail = detail

    def health(self) -> dict[str, Any]:
        return {"ok": False, "status": "error", "detail": self._detail}


def plugin_public_view(plugin: HubPlugin) -> dict[str, Any]:
    settings = plugin.settings()
    secrets = {name for name in getattr(plugin, "secret_fields", ()) if name}
    secrets.add("api_key")
    return {
        "id": plugin.id,
        "title": plugin.title,
        "summary": plugin.summary,
        "docs_url": plugin.docs_url,
        "capabilities": list(getattr(plugin, "capabilities", ()) or ()),
        "enabled": bool(settings.get("enabled")),
        "settings": {key: value for key, value in settings.items() if key not in secrets},
        "has_api_key": bool(str(settings.get("api_key") or "").strip()),
        "secret_fields": sorted(name for name in secrets if name in settings or name == "api_key"),
    }


def get_plugin(plugin_id: str) -> HubPlugin | None:
    for plugin in discover_plugins():
        if plugin.id == plugin_id:
            return plugin
    return None
