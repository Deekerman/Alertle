#!/usr/bin/env python3
"""FastAPI web UI for EPG game notifier."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import html
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import uvicorn
import yaml
from fastapi import BackgroundTasks, FastAPI, Form, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from epg_scanner import DispatcharrClient, Programme
from matcher import build_subscriptions, find_matches, group_matches, group_programmes
from notifiers.base import (
    DEFAULT_TITLE_TEMPLATE, DEFAULT_BODY_TEMPLATE, build_preview_vars, format_grouped_message,
)
from storage import NotificationStore

CONFIG_PATH = ROOT / "config.yaml"
DB_PATH = ROOT / "alertle.db"


def _category_color(categories: list[str]) -> str:
    """Map EPG category list to a Tailwind text-color class for the subtitle line."""
    cats = " ".join(categories).lower()
    if any(k in cats for k in ("american football", "nfl")):
        return "text-blue-400"
    if any(k in cats for k in ("soccer", "football")):
        return "text-green-400"
    if "basketball" in cats:
        return "text-orange-400"
    if "baseball" in cats:
        return "text-yellow-400"
    if "hockey" in cats:
        return "text-cyan-400"
    if "golf" in cats:
        return "text-emerald-400"
    if "tennis" in cats:
        return "text-lime-400"
    if any(k in cats for k in ("motor", "racing", "nascar", "formula")):
        return "text-red-400"
    if any(k in cats for k in ("boxing", "mma", "wrestling", "combat")):
        return "text-rose-400"
    if any(k in cats for k in ("rugby", "cricket", "volleyball", "swimming", "athletics")):
        return "text-teal-400"
    if "sport" in cats:
        return "text-indigo-400"
    if any(k in cats for k in ("movie", "film")):
        return "text-purple-400"
    if "news" in cats:
        return "text-yellow-300"
    if any(k in cats for k in ("reality", "game show")):
        return "text-pink-400"
    if any(k in cats for k in ("documentary", "nature")):
        return "text-teal-300"
    return "text-gray-400"

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    asyncio.create_task(_auto_scan_loop())
    yield


app = FastAPI(title="Alertle", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class _SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.tailwindcss.com https://unpkg.com 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline';"
        )
        return response

app.add_middleware(_SecurityHeaders)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.filters["tojson"] = lambda v: json.dumps(v)
templates.env.filters["category_color"] = _category_color


def _preview_vars_filter(g) -> dict:
    cfg = load_config()
    show_nums = cfg.get("notification_template", {}).get("show_channel_nums", False)
    vars_ = build_preview_vars(g, show_channel_nums=show_nums)
    ep_by_id = {ep["id"]: ep.get("name", ep["id"]) for ep in cfg.get("notification_endpoints", [])}
    channel_names = [ep_by_id.get(ch, ch) for ch in g.subscription.notify_channels]
    vars_["notify_channels"] = ", ".join(channel_names)
    vars_["sub_notif_title_template"] = g.subscription.notif_title_template or ""
    vars_["sub_notif_body_template"] = g.subscription.notif_body_template or ""
    vars_["is_replay"] = g.is_replay
    return vars_


templates.env.filters["preview_vars"] = _preview_vars_filter

# ── Config helpers ─────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict) -> None:
    import os, stat
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.dump(cfg, allow_unicode=True, sort_keys=False))
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0o600 — owner read/write only
    tmp.replace(CONFIG_PATH)


def _validate_url(url: str) -> str:
    """Reject non-http(s) schemes to prevent SSRF via file://, ftp://, etc."""
    from urllib.parse import urlparse
    if not url:
        return url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL must use http or https, got: {parsed.scheme!r}")
    return url


def _get_endpoints(cfg: dict) -> list[dict]:
    return cfg.get("notification_endpoints", [])


def _slugify(s: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", s.lower().strip())
    return s.strip("-") or "endpoint"


def _notif_template(cfg: dict) -> dict:
    tpl = cfg.get("notification_template", {})
    return {
        "title": tpl.get("title", DEFAULT_TITLE_TEMPLATE),
        "body": tpl.get("body", DEFAULT_BODY_TEMPLATE),
    }


def _send_to_channels(title: str, body: str, channels: list[str], cfg: dict) -> list[str]:
    """Send to specified endpoint IDs (empty = all). Returns error strings."""
    endpoints = cfg.get("notification_endpoints", [])
    if not endpoints:
        return _send_to_channels_legacy(title, body, channels, cfg)
    targets = endpoints if not channels else [ep for ep in endpoints if ep.get("id") in channels]
    errors = []
    for ep in targets:
        ep_id = ep.get("id", "?")
        try:
            t = ep.get("type", "")
            if t == "telegram":
                from notifiers.telegram import TelegramNotifier
                TelegramNotifier(ep["bot_token"], str(ep["chat_id"])).send(title, body)
            elif t == "pushover":
                from notifiers.pushover import PushoverNotifier
                PushoverNotifier(ep["app_token"], ep["user_key"]).send(title, body)
            elif t == "ntfy":
                from notifiers.ntfy import NtfyNotifier
                NtfyNotifier(ep["url"], ep["topic"], ep.get("token", "")).send(title, body)
            elif t == "discord":
                from notifiers.discord import DiscordNotifier
                DiscordNotifier(ep["webhook_url"]).send(title, body)
        except Exception as exc:
            errors.append(f"{ep_id}: {exc}")
    return errors


def _send_to_channels_legacy(title: str, body: str, channels: list[str], cfg: dict) -> list[str]:
    _LEGACY = [("telegram", "Telegram"), ("pushover", "Pushover"), ("ntfy", "Ntfy"), ("discord", "Discord")]
    n = cfg.get("notifications", {})
    all_enabled = [k for k, _ in _LEGACY if n.get(k, {}).get("enabled")]
    targets = [ch for ch in channels if ch in all_enabled] if channels else all_enabled
    errors = []
    for ch in targets:
        try:
            if ch == "telegram":
                from notifiers.telegram import TelegramNotifier
                t = n["telegram"]
                TelegramNotifier(t["bot_token"], str(t["chat_id"])).send(title, body)
            elif ch == "pushover":
                from notifiers.pushover import PushoverNotifier
                p = n["pushover"]
                PushoverNotifier(p["app_token"], p["user_key"]).send(title, body)
            elif ch == "ntfy":
                from notifiers.ntfy import NtfyNotifier
                nt = n["ntfy"]
                NtfyNotifier(nt["url"], nt["topic"], nt.get("token", "")).send(title, body)
            elif ch == "discord":
                from notifiers.discord import DiscordNotifier
                DiscordNotifier(n["discord"]["webhook_url"]).send(title, body)
        except Exception as exc:
            errors.append(f"{ch}: {exc}")
    return errors


# ── EPG cache (5 min TTL) ──────────────────────────────────────────────────

_epg_cache: Optional[tuple[float, list[Programme]]] = None
EPG_CACHE_TTL = 300


def make_client(cfg: dict) -> DispatcharrClient:
    d = cfg["dispatcharr"]
    return DispatcharrClient(d["url"], d.get("token", ""), d.get("xmltv_url", ""))


def get_programmes(cfg: dict) -> list[Programme]:
    global _epg_cache
    now = time.monotonic()
    if _epg_cache and (now - _epg_cache[0]) < EPG_CACHE_TTL:
        return _epg_cache[1]
    d = cfg["dispatcharr"]
    client = make_client(cfg)
    dt_now = datetime.now(timezone.utc)
    end = dt_now + timedelta(days=d.get("lookahead_days", 7))
    programmes = client.fetch_programmes(dt_now, end)
    _epg_cache = (now, programmes)
    return programmes


def bust_cache() -> None:
    global _epg_cache
    _epg_cache = None


# ── Pages ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    cfg = load_config()
    return templates.TemplateResponse(request, "dashboard.html", {
        "page": "dashboard",
        "notification_template": _notif_template(cfg),
    })


@app.get("/browse", response_class=HTMLResponse)
async def page_browse(request: Request):
    return templates.TemplateResponse(request, "browse.html", {"page": "browse"})


@app.get("/subscriptions", response_class=HTMLResponse)
async def page_subscriptions(request: Request):
    cfg = load_config()
    return templates.TemplateResponse(request, "subscriptions.html", {
        "page": "subscriptions",
        "subscriptions": cfg.get("subscriptions", []),
        "default_lead": cfg.get("default_lead_time_minutes", 30),
        "endpoints": _get_endpoints(cfg),
    })


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    cfg = load_config()
    return templates.TemplateResponse(request, "settings.html", {
        "page": "settings", "cfg": cfg,
    })


# ── Partials ───────────────────────────────────────────────────────────────

@app.get("/partial/matches", response_class=HTMLResponse)
async def partial_matches(request: Request):
    cfg = load_config()
    error: Optional[str] = None
    try:
        programmes = get_programmes(cfg)
        subs = build_subscriptions(
            cfg.get("subscriptions", []),
            cfg.get("default_lead_time_minutes", 30),
        )
        from espn import filter_replays, get_espn_state
        grouped = filter_replays(group_matches(find_matches(programmes, subs)), cfg)
        espn_states = {
            g.group_uid: get_espn_state(g.title, g.start, g.categories)
            for g in grouped
        }
    except Exception as exc:
        error = str(exc)
        grouped = []
        espn_states = {}
    return templates.TemplateResponse(request, "partials/matches.html", {
        "grouped": grouped, "error": error, "espn_states": espn_states,
    })


@app.get("/partial/epg", response_class=HTMLResponse)
async def partial_epg(
    request: Request,
    q: str = "",
    sport: str = "",
    channel: str = "",
):
    cfg = load_config()
    error: Optional[str] = None
    filtered: list[Programme] = []
    channels: list[str] = []
    sports: list[str] = []
    try:
        programmes = get_programmes(cfg)
        channels = sorted({p.channel_name for p in programmes})
        sports = sorted({c for p in programmes for c in p.categories if c})

        sport_l = sport.lower() if sport else ""
        channel_l = channel.lower() if channel else ""
        q_l = q.lower() if q else ""
        filtered = sorted(
            (
                p for p in programmes
                if (not sport_l or any(sport_l in c.lower() for c in p.categories))
                and (not channel_l or channel_l in p.channel_name.lower())
                and (not q_l or q_l in p.title.lower() or q_l in p.description.lower())
            ),
            key=lambda p: p.start,
        )[:600]
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(request, "partials/epg_rows.html", {
        "groups": group_programmes(filtered),
        "channels": channels,
        "sports": sports,
        "error": error,
        "q": q, "sport": sport, "channel": channel,
    })


@app.get("/partial/subscriptions", response_class=HTMLResponse)
async def partial_subscriptions(request: Request):
    cfg = load_config()
    return templates.TemplateResponse(request, "partials/sub_rows.html", {
        "subscriptions": cfg.get("subscriptions", []),
        "endpoints": _get_endpoints(cfg),
    })


# ── Subscription CRUD ──────────────────────────────────────────────────────

_SUB_FORM_PARAMS = dict(
    sport=Form(""), team=Form(""), keyword=Form(""), channel=Form(""),
    title_pattern=Form(""), subtitle_pattern=Form(""), desc_pattern=Form(""),
    exclude=Form(""), require_sport=Form(""), lead_time_minutes=Form(""),
)


def _build_sub_entry(
    label: str, sport: str, team: str, keyword: str, channel: str,
    title_pattern: str, subtitle_pattern: str, desc_pattern: str,
    exclude: str, require_sport: str, lead_time_minutes: str,
    notify_channels: str = "",
    notif_title_tpl: str = "", notif_body_tpl: str = "",
) -> dict:
    entry: dict = {"label": label.strip()}
    for key, val in [("sport", sport), ("team", team), ("keyword", keyword),
                     ("channel", channel), ("title_pattern", title_pattern),
                     ("subtitle_pattern", subtitle_pattern), ("desc_pattern", desc_pattern)]:
        if val.strip():
            entry[key] = val.strip()
    exclude_list = [x.strip() for x in exclude.split(",") if x.strip()]
    if exclude_list:
        entry["exclude"] = exclude_list
    if require_sport == "on":
        entry["require_sport"] = True
    if lead_time_minutes.strip():
        try:
            entry["lead_time_minutes"] = int(lead_time_minutes)
        except ValueError:
            pass
    ch_list = [c.strip() for c in notify_channels.split(",") if c.strip()]
    if ch_list:
        entry["notify_channels"] = ch_list
    if notif_title_tpl.strip():
        entry["notif_title_template"] = notif_title_tpl.strip()
    if notif_body_tpl.strip():
        entry["notif_body_template"] = notif_body_tpl.strip()
    return entry


def _sub_response(request: Request, subs: list, toast: str, cfg: Optional[dict] = None) -> Response:
    if cfg is None:
        cfg = load_config()
    resp = templates.TemplateResponse(request, "partials/sub_rows.html", {
        "subscriptions": subs,
        "endpoints": _get_endpoints(cfg),
    })
    resp.headers["X-Toast"] = toast
    return resp


@app.post("/partial/subscriptions", response_class=HTMLResponse)
async def add_subscription(
    request: Request,
    label: str = Form(...),
    sport: str = Form(""), team: str = Form(""), keyword: str = Form(""),
    channel: str = Form(""), title_pattern: str = Form(""),
    subtitle_pattern: str = Form(""), desc_pattern: str = Form(""),
    exclude: str = Form(""), require_sport: str = Form(""),
    lead_time_minutes: str = Form(""), notify_channels: str = Form(""),
    notif_title_tpl: str = Form(""), notif_body_tpl: str = Form(""),
):
    cfg = load_config()
    subs = cfg.setdefault("subscriptions", [])
    subs.append(_build_sub_entry(label, sport, team, keyword, channel,
                                 title_pattern, subtitle_pattern, desc_pattern,
                                 exclude, require_sport, lead_time_minutes, notify_channels,
                                 notif_title_tpl, notif_body_tpl))
    save_config(cfg)
    return _sub_response(request, subs, f"Added: {label}", cfg)


@app.post("/partial/subscriptions/bulk", response_class=HTMLResponse)
async def bulk_delete_subscriptions(request: Request, indices: str = Form("")):
    cfg = load_config()
    subs = cfg.get("subscriptions", [])
    to_remove = sorted({int(i) for i in indices.split(",") if i.strip().isdigit()}, reverse=True)
    count = 0
    for i in to_remove:
        if 0 <= i < len(subs):
            subs.pop(i)
            count += 1
    if count:
        save_config(cfg)
    noun = "subscription" if count == 1 else "subscriptions"
    return _sub_response(request, subs, f"Removed {count} {noun}", cfg)


@app.post("/partial/subscriptions/edit", response_class=HTMLResponse)
async def update_subscription_edit(
    request: Request,
    idx: int = Form(...),
    label: str = Form(...),
    sport: str = Form(""), team: str = Form(""), keyword: str = Form(""),
    channel: str = Form(""), title_pattern: str = Form(""),
    subtitle_pattern: str = Form(""), desc_pattern: str = Form(""),
    exclude: str = Form(""), require_sport: str = Form(""),
    lead_time_minutes: str = Form(""), notify_channels: str = Form(""),
    notif_title_tpl: str = Form(""), notif_body_tpl: str = Form(""),
):
    cfg = load_config()
    subs = cfg.get("subscriptions", [])
    if 0 <= idx < len(subs):
        subs[idx] = _build_sub_entry(label, sport, team, keyword, channel,
                                     title_pattern, subtitle_pattern, desc_pattern,
                                     exclude, require_sport, lead_time_minutes, notify_channels,
                                     notif_title_tpl, notif_body_tpl)
        save_config(cfg)
    return _sub_response(request, subs, f"Saved: {label}", cfg)


@app.post("/partial/subscriptions/{idx}", response_class=HTMLResponse)
async def update_subscription(
    request: Request, idx: int,
    label: str = Form(...),
    sport: str = Form(""), team: str = Form(""), keyword: str = Form(""),
    channel: str = Form(""), title_pattern: str = Form(""),
    subtitle_pattern: str = Form(""), desc_pattern: str = Form(""),
    exclude: str = Form(""), require_sport: str = Form(""),
    lead_time_minutes: str = Form(""), notify_channels: str = Form(""),
):
    cfg = load_config()
    subs = cfg.get("subscriptions", [])
    if 0 <= idx < len(subs):
        subs[idx] = _build_sub_entry(label, sport, team, keyword, channel,
                                     title_pattern, subtitle_pattern, desc_pattern,
                                     exclude, require_sport, lead_time_minutes, notify_channels)
        save_config(cfg)
    return _sub_response(request, subs, f"Saved: {label}", cfg)


@app.delete("/partial/subscriptions/{idx}", response_class=HTMLResponse)
async def delete_subscription(request: Request, idx: int):
    cfg = load_config()
    subs = cfg.get("subscriptions", [])
    label = ""
    if 0 <= idx < len(subs):
        label = subs.pop(idx).get("label", "")
        save_config(cfg)
    return _sub_response(request, subs, f"Removed: {label}", cfg)


# ── API probe ─────────────────────────────────────────────────────────────

@app.get("/action/probe", response_class=HTMLResponse)
async def probe_dispatcharr(request: Request):
    cfg = load_config()
    d = cfg.get("dispatcharr", {})
    if not d.get("url"):
        return HTMLResponse('<p class="text-yellow-400 text-sm">No URL configured yet.</p>')
    try:
        client = make_client(cfg)
        results = client.probe_api()
    except Exception as exc:
        return HTMLResponse(f'<p class="text-red-400 text-sm">Probe failed: {exc}</p>')

    rows = ""
    for key, info in results.items():
        status = info.get("status")
        if status == 200:
            colour = "text-green-400"
        elif status is None:
            colour = "text-red-400"
        elif status in (401, 403):
            colour = "text-yellow-400"
        else:
            colour = "text-gray-500"

        detail = html.escape((info.get("snippet") or info.get("error") or "")[:140])

        rows += (
            f'<tr class="border-b border-border">'
            f'<td class="py-1.5 pr-3 font-mono text-xs text-gray-300 whitespace-nowrap">{html.escape(key)}</td>'
            f'<td class="py-1.5 pr-3 text-xs {colour} whitespace-nowrap">{html.escape(str(status or "err"))}</td>'
            f'<td class="py-1.5 text-xs text-gray-500 break-all">{detail}</td>'
            f'</tr>'
        )

    return HTMLResponse(
        f'<div class="overflow-x-auto"><table class="w-full mt-3"><tbody>{rows}</tbody></table></div>'
    )


# ── Settings ───────────────────────────────────────────────────────────────

@app.post("/action/settings")
async def save_settings(request: Request):
    form = await request.form()
    cfg = load_config()

    d = cfg.setdefault("dispatcharr", {})
    try:
        d["url"] = _validate_url(form.get("dispatcharr_url", "").strip())
        d["xmltv_url"] = _validate_url(form.get("xmltv_url", "").strip())
    except ValueError as exc:
        return Response(status_code=400, headers={"X-Toast": str(exc)})
    d["token"] = form.get("dispatcharr_token", "").strip()
    try:
        d["lookahead_days"] = int(form.get("lookahead_days", 7))
    except ValueError:
        pass

    try:
        cfg["default_lead_time_minutes"] = int(form.get("default_lead_time_minutes", 30))
    except ValueError:
        pass
    try:
        cfg["poll_interval_seconds"] = max(60, int(form.get("poll_interval_seconds", 300)))
    except ValueError:
        pass
    cfg["espn_verify"] = form.get("espn_verify") == "on"
    cfg["espn_notify_replays"] = form.get("espn_notify_replays") == "on"

    tpl = cfg.setdefault("notification_template", {})
    tpl_title = form.get("notif_title_tpl", "").strip()
    tpl_body = form.get("notif_body_tpl", "").strip()
    if tpl_title:
        tpl["title"] = tpl_title
    if tpl_body:
        tpl["body"] = tpl_body
    tpl["show_channel_nums"] = form.get("notif_show_channel_nums") == "on"

    save_config(cfg)
    bust_cache()

    return Response(
        content="Saved",
        headers={"X-Toast": "Settings saved"},
    )


# ── Test notification ──────────────────────────────────────────────────────

@app.post("/action/test/{channel}")
async def test_notification(channel: str):
    cfg = load_config()
    errors = _send_to_channels(
        "Alertle — Test",
        "This is a test notification from Alertle.",
        [channel], cfg,
    )
    if errors:
        msg = errors[0].split(": ", 1)[-1]
        return HTMLResponse(
            content=f'<span class="text-red-400 text-xs" title="{html.escape(msg)}">✕ {html.escape(msg[:60])}{"…" if len(msg) > 60 else ""}</span>',
            status_code=400,
        )
    return HTMLResponse(content='<span class="text-green-400 text-xs font-medium">✓ Sent</span>')


@app.get("/partial/endpoints", response_class=HTMLResponse)
async def partial_endpoints(request: Request):
    cfg = load_config()
    return templates.TemplateResponse(request, "partials/endpoints.html", {
        "endpoints": _get_endpoints(cfg),
    })


@app.post("/partial/endpoints", response_class=HTMLResponse)
async def add_endpoint(
    request: Request,
    ep_name: str = Form(...),
    ep_type: str = Form(...),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    discord_webhook_url: str = Form(""),
    ntfy_url: str = Form(""),
    ntfy_topic: str = Form(""),
    ntfy_token: str = Form(""),
    pushover_app_token: str = Form(""),
    pushover_user_key: str = Form(""),
):
    if ep_type not in ("telegram", "discord", "ntfy", "pushover"):
        return Response(status_code=400, headers={"X-Toast": "Invalid endpoint type"})
    cfg = load_config()
    endpoints = cfg.setdefault("notification_endpoints", [])
    base_id = _slugify(ep_name)
    existing = {ep.get("id") for ep in endpoints}
    ep_id, n = base_id, 2
    while ep_id in existing:
        ep_id = f"{base_id}-{n}"; n += 1
    entry: dict = {"id": ep_id, "name": ep_name.strip(), "type": ep_type}
    try:
        if ep_type == "telegram":
            entry["bot_token"] = telegram_bot_token.strip()
            entry["chat_id"] = telegram_chat_id.strip()
        elif ep_type == "discord":
            entry["webhook_url"] = _validate_url(discord_webhook_url.strip())
        elif ep_type == "ntfy":
            entry["url"] = _validate_url(ntfy_url.strip() or "https://ntfy.sh")
            entry["topic"] = ntfy_topic.strip()
            if ntfy_token.strip():
                entry["token"] = ntfy_token.strip()
        elif ep_type == "pushover":
            entry["app_token"] = pushover_app_token.strip()
            entry["user_key"] = pushover_user_key.strip()
    except ValueError as exc:
        return Response(status_code=400, headers={"X-Toast": str(exc)})
    endpoints.append(entry)
    save_config(cfg)
    resp = templates.TemplateResponse(request, "partials/endpoints.html", {"endpoints": endpoints})
    resp.headers["X-Toast"] = f"Added: {ep_name}"
    return resp


@app.delete("/partial/endpoints/{idx}", response_class=HTMLResponse)
async def delete_endpoint(request: Request, idx: int):
    cfg = load_config()
    endpoints = cfg.get("notification_endpoints", [])
    name = ""
    if 0 <= idx < len(endpoints):
        name = endpoints.pop(idx).get("name", "")
        save_config(cfg)
    resp = templates.TemplateResponse(request, "partials/endpoints.html", {"endpoints": endpoints})
    resp.headers["X-Toast"] = f"Removed: {name}" if name else "Removed"
    return resp


@app.post("/action/preview-send")
async def preview_send(
    notif_title: str = Form(...),
    notif_body: str = Form(""),
    sub_label: str = Form(""),
):
    # Enforce reasonable content limits
    notif_title = notif_title[:300]
    notif_body = notif_body[:2000]

    cfg = load_config()
    sub_channels: list[str] = []
    for s in cfg.get("subscriptions", []):
        if s.get("label") == sub_label:
            sub_channels = s.get("notify_channels", [])
            break
    errors = _send_to_channels(notif_title, notif_body, sub_channels, cfg)
    if errors:
        return Response(status_code=400, headers={"X-Toast": f"Send failed: {errors[0]}"})
    via = ", ".join(sub_channels) if sub_channels else "all enabled channels"
    return Response(headers={"X-Toast": f"Sent via {via}"})


# ── Manual scan ────────────────────────────────────────────────────────────

@app.post("/action/scan", response_class=HTMLResponse)
async def manual_scan(background_tasks: BackgroundTasks):
    background_tasks.add_task(_do_scan)
    return HTMLResponse('<span class="text-green-400 text-sm">Scan started…</span>')


def _do_scan() -> None:
    try:
        from main import build_notifiers_map, run_scan
        cfg = load_config()
        run_scan(cfg, build_notifiers_map(cfg), NotificationStore(str(DB_PATH)), dry_run=False)
        bust_cache()
    except Exception as exc:
        log.error("Scan error: %s", exc, exc_info=True)


# ── Background auto-scanner ───────────────────────────────────────────────

async def _auto_scan_loop():
    await asyncio.sleep(15)  # brief delay to let the server finish starting up
    while True:
        cfg = load_config()
        interval = cfg.get("poll_interval_seconds", 300)
        log.info("Auto-scan triggered (interval: %ds)", interval)
        _do_scan()
        await asyncio.sleep(interval)


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Alertle web UI")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (use 0.0.0.0 for LAN access, 127.0.0.1 for localhost only)")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    CONFIG_PATH = Path(args.config)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    uvicorn.run(app, host=args.host, port=args.port)
