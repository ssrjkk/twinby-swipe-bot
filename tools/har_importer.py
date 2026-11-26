import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.adapters.declarative_client import DeclarativeAPIProfile, EndpointSpec
from core.safe_names import safe_filename


def _guess_role(method: str, path: str) -> str:
    p = path.lower()
    if any(k in p for k in ("refresh", "login", "auth", "token", "session")):
        return "auth"
    if any(k in p for k in ("pass", "skip", "dislike", "nope")):
        return "pass"
    if any(k in p for k in ("swipe", "like", "vote", "reaction", "rate")):
        return "like"
    if any(k in p for k in ("message", "chat", "dialog")):
        return "messages"
    if any(k in p for k in ("match", "mutual", "pair")):
        return "matches"
    if method == "GET" and any(k in p for k in ("feed", "recommend", "encounter", "search", "cards", "users", "people")):
        return "feed"
    return "other"


def _detect_items_path(body_json: Any) -> str:
    if isinstance(body_json, list):
        return ""
    if isinstance(body_json, dict):
        for candidate in ("items", "data", "results", "users", "profiles", "cards", "messages"):
            if isinstance(body_json.get(candidate), list):
                return candidate
        for k, v in body_json.items():
            if isinstance(v, dict):
                for sub in ("items", "results", "users", "list"):
                    if isinstance(v.get(sub), list):
                        return f"{k}.{sub}"
    return "items"


def inspect_har(har_path: str, host_filter: str = "") -> dict[str, Any]:
    raw = json.loads(Path(har_path).read_text(encoding="utf-8"))
    entries = raw.get("log", {}).get("entries", [])
    seen: dict[tuple[str, str], dict[str, Any]] = {}

    for entry in entries:
        req = entry.get("request", {})
        resp = entry.get("response", {})
        url = req.get("url", "")
        parsed = urlparse(url)
        if host_filter and host_filter.lower() not in parsed.netloc.lower():
            continue

        mime = resp.get("content", {}).get("mimeType", "")
        if "json" not in mime.lower():
            continue

        method = req.get("method", "GET")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        endpoint = f"{origin}{parsed.path}"
        key = (method, endpoint)
        auth_headers = [
            h["name"]
            for h in req.get("headers", [])
            if any(tok in h["name"].lower() for tok in ("auth", "token", "sign", "device", "x-"))
        ]

        sample_keys: list[str] = []
        items_path = "items"
        body_text = resp.get("content", {}).get("text", "")
        if body_text:
            try:
                body_json = json.loads(body_text)
                items_path = _detect_items_path(body_json)
                if isinstance(body_json, dict):
                    sample_keys = list(body_json.keys())[:10]
            except Exception:
                pass

        seen[key] = {
            "role": _guess_role(method, parsed.path),
            "method": method,
            "origin": origin,
            "path": parsed.path,
            "endpoint": endpoint,
            "status": resp.get("status", 0),
            "auth_headers": sorted(set(auth_headers)),
            "items_path": items_path,
            "response_keys": sample_keys,
        }

    endpoints = list(seen.values())
    by_role: dict[str, list[dict[str, Any]]] = {}
    for ep in endpoints:
        by_role.setdefault(ep["role"], []).append(ep)

    return {
        "total_json_endpoints": len(endpoints),
        "by_role": by_role,
        "endpoints": endpoints,
    }


def export_api_profile_from_har(
    har_path: str,
    platform: str,
    host_filter: str = "",
    output_dir: str = "./data/api_profiles",
) -> Path:
    """Превращает реальный .har дамп в готовый декларативный профиль `./data/api_profiles/<platform>.json`,
    который сразу подхватывается `create_dating_client()` в режиме MODE=api.
    """
    platform_name = safe_filename(platform.strip())
    report = inspect_har(har_path, host_filter=host_filter or platform_name)
    by_role = report["by_role"]
    all_eps = report["endpoints"]
    if not all_eps:
        raise ValueError(f"В HAR не найдено JSON-эндпоинтов (фильтр: {host_filter or platform!r}).")

    base_url = all_eps[0]["origin"]
    auth_header = "Authorization"
    for ep in all_eps:
        for h in ep["auth_headers"]:
            if h.lower() in ("authorization", "x-auth-token", "x-device-token", "x-vk-sign"):
                auth_header = h
                break

    feed_ep = (by_role.get("feed") or [all_eps[0]])[0]
    like_ep = (by_role.get("like") or [{"method": "POST", "path": "/swipes/like"}])[0]
    pass_ep = (by_role.get("pass") or [{"method": "POST", "path": "/swipes/pass"}])[0]
    matches_ep = (by_role.get("matches") or [None])[0]
    msg_eps = by_role.get("messages") or []
    msg_get = next((e for e in msg_eps if e["method"] == "GET"), None)
    msg_send = next((e for e in msg_eps if e["method"] == "POST"), None)

    profile = DeclarativeAPIProfile(
        platform=platform_name,
        base_url=base_url,
        auth_header=auth_header,
        auth_prefix="Bearer " if auth_header.lower() == "authorization" else "",
        feed=EndpointSpec(method=feed_ep["method"], path=feed_ep["path"], items_path=feed_ep.get("items_path", "items")),
        like=EndpointSpec(method=like_ep["method"], path=like_ep["path"], body_template={"target_user_id": "{target_user_id}"}),
        pass_action=EndpointSpec(method=pass_ep["method"], path=pass_ep["path"], body_template={"target_user_id": "{target_user_id}"}),
        matches=EndpointSpec(method=matches_ep["method"], path=matches_ep["path"], items_path=matches_ep.get("items_path", "items")) if matches_ep else None,
        messages_get=EndpointSpec(method=msg_get["method"], path=msg_get["path"], items_path=msg_get.get("items_path", "items")) if msg_get else None,
        messages_send=EndpointSpec(method=msg_send["method"], path=msg_send["path"], body_template={"text": "{text}"}) if msg_send else None,
    )

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{platform_name}.json"
    out_file.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    return out_file
