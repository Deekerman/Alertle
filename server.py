#!/usr/bin/env python3
"""FastAPI web UI for EPG game notifier."""

from __future__ import annotations

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
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from epg_scanner import DispatcharrClient, Programme
from matcher import build_subscriptions, find_matches, group_matches, group_programmes
from notifiers.base import format_grouped_message
from storage import NotificationStore

CONFIG_PATH = ROOT / "config.yaml"
DB_PATH = ROOT / "epg_notifier.db"


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
app = FastAPI(title="EPG Notifier")
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.filters["tojson"] = lambda v: json.dumps(v)
templates.env.filters["category_color"] = _category_color

# ── Config helpers ─────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.dump(cfg, allow_unicode=True, sort_keys=False))
    tmp.replace(CONFIG_PATH)


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
    return templates.TemplateResponse(request, "dashboard.html", {"page": "dashboard"})


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
    })


# ── Subscription CRUD ──────────────────────────────────────────────────────

@app.post("/partial/subscriptions", response_class=HTMLResponse)
async def add_subscription(
    request: Request,
    label: str = Form(...),
    sport: str = Form(""),
    team: str = Form(""),
    keyword: str = Form(""),
    channel: str = Form(""),
    title_pattern: str = Form(""),
    subtitle_pattern: str = Form(""),
    desc_pattern: str = Form(""),
    exclude: str = Form(""),
    require_sport: str = Form(""),
    lead_time_minutes: str = Form(""),
):
    cfg = load_config()
    subs = cfg.setdefault("subscriptions", [])
    entry: dict = {"label": label.strip()}
    if sport.strip():
        entry["sport"] = sport.strip()
    if team.strip():
        entry["team"] = team.strip()
    if keyword.strip():
        entry["keyword"] = keyword.strip()
    if channel.strip():
        entry["channel"] = channel.strip()
    if title_pattern.strip():
        entry["title_pattern"] = title_pattern.strip()
    if subtitle_pattern.strip():
        entry["subtitle_pattern"] = subtitle_pattern.strip()
    if desc_pattern.strip():
        entry["desc_pattern"] = desc_pattern.strip()
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
    subs.append(entry)
    save_config(cfg)

    resp = templates.TemplateResponse(request, "partials/sub_rows.html", {
        "subscriptions": subs,
    })
    resp.headers["X-Toast"] = f"Added: {label}"
    return resp


@app.delete("/partial/subscriptions/{idx}", response_class=HTMLResponse)
async def delete_subscription(request: Request, idx: int):
    cfg = load_config()
    subs = cfg.get("subscriptions", [])
    label = ""
    if 0 <= idx < len(subs):
        label = subs.pop(idx).get("label", "")
        save_config(cfg)
    resp = templates.TemplateResponse(request, "partials/sub_rows.html", {
        "subscriptions": subs,
    })
    resp.headers["X-Toast"] = f"Removed: {label}"
    return resp


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

        detail = (info.get("snippet") or info.get("error") or "")[:140]

        rows += (
            f'<tr class="border-b border-border">'
            f'<td class="py-1.5 pr-3 font-mono text-xs text-gray-300 whitespace-nowrap">{key}</td>'
            f'<td class="py-1.5 pr-3 text-xs {colour} whitespace-nowrap">{status or "err"}</td>'
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
    d["url"] = form.get("dispatcharr_url", "").strip()
    d["token"] = form.get("dispatcharr_token", "").strip()
    d["xmltv_url"] = form.get("xmltv_url", "").strip()
    try:
        d["lookahead_days"] = int(form.get("lookahead_days", 7))
    except ValueError:
        pass

    try:
        cfg["default_lead_time_minutes"] = int(form.get("default_lead_time_minutes", 30))
    except ValueError:
        pass
    try:
        cfg["poll_interval_seconds"] = int(form.get("poll_interval_seconds", 3600))
    except ValueError:
        pass
    cfg["espn_verify"] = form.get("espn_verify") == "on"

    n = cfg.setdefault("notifications", {})

    # Telegram
    t = n.setdefault("telegram", {})
    t["enabled"] = form.get("telegram_enabled") == "on"
    if form.get("telegram_bot_token"):
        t["bot_token"] = form.get("telegram_bot_token").strip()
    if form.get("telegram_chat_id"):
        t["chat_id"] = form.get("telegram_chat_id").strip()

    # Pushover
    p = n.setdefault("pushover", {})
    p["enabled"] = form.get("pushover_enabled") == "on"
    if form.get("pushover_app_token"):
        p["app_token"] = form.get("pushover_app_token").strip()
    if form.get("pushover_user_key"):
        p["user_key"] = form.get("pushover_user_key").strip()

    # Ntfy
    nt = n.setdefault("ntfy", {})
    nt["enabled"] = form.get("ntfy_enabled") == "on"
    if form.get("ntfy_url"):
        nt["url"] = form.get("ntfy_url").strip()
    if form.get("ntfy_topic"):
        nt["topic"] = form.get("ntfy_topic").strip()
    if form.get("ntfy_token"):
        nt["token"] = form.get("ntfy_token").strip()

    # Discord
    dc = n.setdefault("discord", {})
    dc["enabled"] = form.get("discord_enabled") == "on"
    if form.get("discord_webhook_url"):
        dc["webhook_url"] = form.get("discord_webhook_url").strip()

    # SMTP
    sm = n.setdefault("smtp", {})
    sm["enabled"] = form.get("smtp_enabled") == "on"
    for key, fkey in [("host", "smtp_host"), ("username", "smtp_username"),
                      ("password", "smtp_password"), ("from_addr", "smtp_from_addr")]:
        if form.get(fkey):
            sm[key] = form.get(fkey).strip()
    try:
        sm["port"] = int(form.get("smtp_port", 587))
    except ValueError:
        pass
    sm["use_tls"] = form.get("smtp_use_tls") == "on"
    to_addrs = [a.strip() for a in form.get("smtp_to_addrs", "").split("\n") if a.strip()]
    if to_addrs:
        sm["to_addrs"] = to_addrs

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
    title = "EPG Notifier — Test"
    body = "This is a test notification from EPG Notifier."
    try:
        n = cfg.get("notifications", {})
        if channel == "telegram":
            from notifiers.telegram import TelegramNotifier
            t = n["telegram"]
            TelegramNotifier(t["bot_token"], str(t["chat_id"])).send(title, body)
        elif channel == "pushover":
            from notifiers.pushover import PushoverNotifier
            p = n["pushover"]
            PushoverNotifier(p["app_token"], p["user_key"]).send(title, body)
        elif channel == "ntfy":
            from notifiers.ntfy import NtfyNotifier
            nt = n["ntfy"]
            NtfyNotifier(nt["url"], nt["topic"], nt.get("token", "")).send(title, body)
        elif channel == "discord":
            from notifiers.discord import DiscordNotifier
            DiscordNotifier(n["discord"]["webhook_url"]).send(title, body)
        elif channel == "smtp":
            from notifiers.smtp import SmtpNotifier
            s = n["smtp"]
            SmtpNotifier(
                s["host"], s["port"], s["username"], s["password"],
                s["from_addr"], s["to_addrs"], s.get("use_tls", True),
            ).send(title, body)
        return Response(
            content="Sent",
            headers={"X-Toast": f"Test sent via {channel}"},
        )
    except Exception as exc:
        return Response(
            content=str(exc),
            status_code=400,
            headers={"X-Toast": f"Test failed: {exc}"},
        )


# ── Manual scan ────────────────────────────────────────────────────────────

@app.post("/action/scan", response_class=HTMLResponse)
async def manual_scan(background_tasks: BackgroundTasks):
    background_tasks.add_task(_do_scan)
    return HTMLResponse('<span class="text-green-400 text-sm">Scan started…</span>')


def _do_scan() -> None:
    try:
        from main import build_notifiers, run_scan
        cfg = load_config()
        run_scan(cfg, build_notifiers(cfg), NotificationStore(str(DB_PATH)), dry_run=False)
        bust_cache()
    except Exception as exc:
        log.error("Scan error: %s", exc)


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="EPG Notifier web UI")
    parser.add_argument("--host", default="0.0.0.0")
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
