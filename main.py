#!/usr/bin/env python3
"""EPG game notifier — scans Dispatcharr EPG and fires alerts for subscribed events."""

import argparse
import hashlib
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from epg_scanner import DispatcharrClient
from game_thumbs import build_thumb_url
from matcher import Match, build_subscriptions, find_matches, group_matches, consolidate_notifications
from notifiers.base import (
    BaseNotifier, DEFAULT_TITLE_TEMPLATE, DEFAULT_BODY_TEMPLATE,
    build_preview_vars, format_grouped_message,
)
from storage import NotificationStore

log = logging.getLogger(__name__)


# ── Config loading ──────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_notifiers_map(cfg: dict) -> dict[str, BaseNotifier]:
    """Build endpoint_id → notifier for all configured notification endpoints."""
    endpoints = cfg.get("notification_endpoints", [])
    if not endpoints:
        return _build_notifiers_from_legacy(cfg)

    result: dict[str, BaseNotifier] = {}
    for ep in endpoints:
        ep_id = ep.get("id")
        if not ep_id:
            continue
        try:
            t = ep.get("type", "")
            if t == "telegram":
                from notifiers.telegram import TelegramNotifier
                result[ep_id] = TelegramNotifier(ep["bot_token"], str(ep["chat_id"]))
            elif t == "pushover":
                from notifiers.pushover import PushoverNotifier
                result[ep_id] = PushoverNotifier(ep["app_token"], ep["user_key"], ep.get("priority", 0))
            elif t == "ntfy":
                from notifiers.ntfy import NtfyNotifier
                result[ep_id] = NtfyNotifier(ep["url"], ep["topic"], ep.get("token", ""))
            elif t == "discord":
                from notifiers.discord import DiscordNotifier
                result[ep_id] = DiscordNotifier(ep["webhook_url"])
        except KeyError as exc:
            log.warning("Endpoint %r missing field %s — skipped", ep_id, exc)
    return result


def _build_notifiers_from_legacy(cfg: dict) -> dict[str, BaseNotifier]:
    """Backward-compat: read old notifications: block if notification_endpoints absent."""
    n = cfg.get("notifications", {})
    result: dict[str, BaseNotifier] = {}
    if n.get("telegram", {}).get("enabled"):
        from notifiers.telegram import TelegramNotifier
        t = n["telegram"]
        result["telegram"] = TelegramNotifier(t["bot_token"], str(t["chat_id"]))
    if n.get("pushover", {}).get("enabled"):
        from notifiers.pushover import PushoverNotifier
        p = n["pushover"]
        result["pushover"] = PushoverNotifier(p["app_token"], p["user_key"], p.get("priority", 0))
    if n.get("ntfy", {}).get("enabled"):
        from notifiers.ntfy import NtfyNotifier
        nt = n["ntfy"]
        result["ntfy"] = NtfyNotifier(nt["url"], nt["topic"], nt.get("token", ""))
    if n.get("discord", {}).get("enabled"):
        from notifiers.discord import DiscordNotifier
        result["discord"] = DiscordNotifier(n["discord"]["webhook_url"])
    return result


def build_notifiers(cfg: dict) -> list[BaseNotifier]:
    return list(build_notifiers_map(cfg).values())


# ── Core scan logic ─────────────────────────────────────────────────────────────────

def run_scan(cfg: dict, notifiers_map: dict[str, BaseNotifier], store: NotificationStore, dry_run: bool):
    dispatcharr = cfg.get("dispatcharr", {})
    client = DispatcharrClient(
        dispatcharr.get("url", ""),
        dispatcharr.get("token", ""),
        dispatcharr.get("xmltv_url", ""),
    )

    now = datetime.now(timezone.utc)
    lookahead = timedelta(days=dispatcharr.get("lookahead_days", 7))
    window_end = now + lookahead

    default_lead = cfg.get("default_lead_time_minutes", 30)
    subscriptions = build_subscriptions(cfg.get("subscriptions", []), default_lead)
    subscriptions = [s for s in subscriptions if s.enabled]

    if not subscriptions:
        log.warning("No subscriptions configured — nothing to match.")
        return

    desc_dedup = cfg.get("desc_dedup", False)
    log.info("Scanning EPG from %s to %s | espn_verify=%s notify_replays=%s desc_dedup=%s",
             now.strftime("%Y-%m-%d %H:%M"), window_end.strftime("%Y-%m-%d %H:%M"),
             cfg.get("espn_verify", False), cfg.get("espn_notify_replays", False), desc_dedup)
    programmes = client.fetch_programmes(now, window_end)

    matches = find_matches(programmes, subscriptions)
    grace = cfg.get("group_window_minutes", 20)
    grouped = group_matches(matches, grace_window_minutes=grace)
    log.info("Found %d unique events (%d channel slots matched, grace=%dm)", len(grouped), len(matches), grace)

    from espn import filter_replays, get_espn_game_time
    grouped = filter_replays(grouped, cfg)
    log.info("After replay filter: %d events remain", len(grouped))

    # Set ESPN-verified start time; used as the notify_at anchor (reuses cached data)
    for g in grouped:
        t = get_espn_game_time(
            g.title, g.start, g.categories,
            espn_sport=getattr(g.subscription, "espn_sport", None),
            espn_league=getattr(g.subscription, "espn_league", None),
            espn_team=getattr(g.subscription, "espn_team", None),
        )
        if t:
            g.espn_start = t
            log.info("ESPN anchor: '%s' → %s (EPG was %s)",
                     g.title, t.astimezone().strftime("%-I:%M %p"),
                     g.start.astimezone().strftime("%-I:%M %p"))
    consolidated = consolidate_notifications(grouped, grace_window_minutes=grace)
    log.info("After consolidation: %d notification groups", len(consolidated))

    sent_count = 0
    for primary in consolidated:
        all_games = [primary] + primary.extra_games

        # Per-game dedup: skip games already sent
        unsent = [g for g in all_games
                  if not store.already_sent(g.group_uid, g.subscription.label)]
        if not unsent:
            log.info("Already sent: [%s] '%s'", primary.subscription.label, primary.title)
            continue

        # Per-game timing: only include games whose notify window has opened
        # and whose start time has not yet passed (skip games already in progress)
        ready = []
        for g in unsent:
            anchor = g.espn_start if g.espn_start else g.start
            notify_at = anchor - timedelta(minutes=g.subscription.lead_time_minutes)
            if now >= anchor:
                log.info("Game already started, skipping: [%s] '%s' (started %s)",
                         primary.subscription.label, g.subtitle or g.title,
                         anchor.astimezone().strftime("%-I:%M %p"))
                continue
            if now >= notify_at:
                ready.append(g)

        if not ready:
            # Only log "too early" for games that haven't started yet
            pending = [g for g in unsent if now < (g.espn_start if g.espn_start else g.start)]
            if pending:
                earliest_notify = min(
                    (g.espn_start if g.espn_start else g.start)
                    - timedelta(minutes=g.subscription.lead_time_minutes)
                    for g in pending
                )
                mins_until = int((earliest_notify - now).total_seconds() / 60)
                log.info("Too early: [%s] '%s' — notify in %dm (at %s)",
                         primary.subscription.label, primary.title, mins_until,
                         earliest_notify.astimezone().strftime("%-I:%M %p"))
            continue

        # Desc dedup: remove games whose description was already sent
        final_ready = []
        for g in ready:
            if desc_dedup and g.description and g.description.strip():
                desc_hash = hashlib.sha256(g.description.strip().lower().encode()).hexdigest()
                if store.description_already_sent(desc_hash):
                    log.info("Desc dedup: [%s] '%s' — suppressed", primary.subscription.label, g.subtitle or g.title)
                    continue
            final_ready.append(g)

        if not final_ready:
            continue

        notif_tpl = cfg.get("notification_template", {})
        title_tpl = primary.subscription.notif_title_template or notif_tpl.get("title", DEFAULT_TITLE_TEMPLATE)
        body_tpl  = primary.subscription.notif_body_template  or notif_tpl.get("body",  DEFAULT_BODY_TEMPLATE)
        show_nums = notif_tpl.get("show_channel_nums", False)
        thumb_url = build_thumb_url(final_ready[0], cfg)
        title, body = format_grouped_message(final_ready, title_tpl, body_tpl,
                                             show_channel_nums=show_nums, thumb_url=thumb_url)
        if any(g.is_replay for g in final_ready):
            title = f"[REPLAY] {title}"

        if dry_run:
            print(f"\n{'─'*60}")
            print(f"[DRY RUN] {title}")
            print(body)
        else:
            log.info("Sending notification: %s", title)
            _dispatch(notifiers_map, primary.subscription.notify_channels, title, body, thumb_url)
            for g in final_ready:
                desc_hash = None
                if desc_dedup and g.description and g.description.strip():
                    desc_hash = hashlib.sha256(g.description.strip().lower().encode()).hexdigest()
                store.mark_sent(g.group_uid, g.subscription.label, now.isoformat(), desc_hash)
            sent_count += 1

    for primary in consolidated:
        if not primary.subscription.notify_on_start:
            continue
        all_games = [primary] + primary.extra_games

        unsent_start = [g for g in all_games
                        if not store.already_sent(g.group_uid + ":start", g.subscription.label)]
        if not unsent_start:
            continue

        ready_start = []
        for g in unsent_start:
            anchor = g.espn_start if g.espn_start else g.start
            notify_at = anchor - timedelta(minutes=g.subscription.start_lead_time_minutes)
            if now >= anchor:
                log.info("Game already started, skipping start notif: [%s] '%s'",
                         primary.subscription.label, g.subtitle or g.title)
                continue
            if now >= notify_at:
                ready_start.append(g)

        if not ready_start:
            continue

        notif_tpl = cfg.get("notification_template", {})
        title_tpl = primary.subscription.notif_title_template or notif_tpl.get("title", DEFAULT_TITLE_TEMPLATE)
        body_tpl  = primary.subscription.notif_body_template  or notif_tpl.get("body",  DEFAULT_BODY_TEMPLATE)
        show_nums = notif_tpl.get("show_channel_nums", False)
        thumb_url = build_thumb_url(ready_start[0], cfg)
        title, body = format_grouped_message(ready_start, title_tpl, body_tpl,
                                             show_channel_nums=show_nums, thumb_url=thumb_url)
        title = f"Starting soon: {title}"
        if any(g.is_replay for g in ready_start):
            title = f"[REPLAY] {title}"

        if dry_run:
            print(f"\n{'─'*60}")
            print(f"[DRY RUN - START NOTIF] {title}")
            print(body)
        else:
            log.info("Sending start notification: %s", title)
            _dispatch(notifiers_map, primary.subscription.notify_channels, title, body, thumb_url)
            for g in ready_start:
                store.mark_sent(g.group_uid + ":start", g.subscription.label, now.isoformat())
            sent_count += 1

    if not dry_run:
        store.prune_old((now - lookahead).isoformat())
        log.info("Notifications sent: %d", sent_count)


def _dispatch(notifiers_map: dict[str, BaseNotifier], sub_channels: list[str],
              title: str, body: str, thumb_url: str = ""):
    targets = notifiers_map if not sub_channels else {
        k: v for k, v in notifiers_map.items() if k in sub_channels
    }
    for key, notifier in targets.items():
        try:
            notifier.send(title, body, thumb_url=thumb_url)
        except Exception as exc:
            log.error("Notifier %s failed: %s", key, exc)


# ── CLI ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="EPG sports/game notifier for Dispatcharr")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--db", default="alertle.db", help="Path to SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Print matches without sending or storing")
    parser.add_argument("--daemon", action="store_true", help="Run continuously on poll_interval_seconds")
    parser.add_argument("--list", action="store_true", help="List upcoming matched events and exit")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if not Path(args.config).exists():
        log.error("Config file not found: %s", args.config)
        sys.exit(1)

    cfg = load_config(args.config)
    notifiers_map = build_notifiers_map(cfg)

    if not notifiers_map and not args.dry_run and not args.list:
        log.warning("No notification channels enabled. Use --dry-run to test matching.")

    store = NotificationStore(args.db)

    if args.list:
        _cmd_list(cfg)
        return

    if args.daemon:
        log.info("Daemon mode started")
        while True:
            try:
                cfg = load_config(args.config)
                notifiers_map = build_notifiers_map(cfg)
                interval = cfg.get("poll_interval_seconds", 300)
                run_scan(cfg, notifiers_map, store, dry_run=args.dry_run)
            except Exception as exc:
                log.error("Scan error: %s", exc)
                interval = 60
            log.info("Next scan in %ds", interval)
            time.sleep(interval)
    else:
        run_scan(cfg, notifiers_map, store, dry_run=args.dry_run)


def _cmd_list(cfg: dict):
    """Print all matching events in the lookahead window (no notification state)."""
    dispatcharr = cfg["dispatcharr"]
    client = DispatcharrClient(
        dispatcharr["url"],
        dispatcharr.get("token", ""),
        dispatcharr.get("xmltv_url", ""),
    )
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=dispatcharr.get("lookahead_days", 7))
    programmes = client.fetch_programmes(now, window_end)
    default_lead = cfg.get("default_lead_time_minutes", 30)
    subscriptions = build_subscriptions(cfg.get("subscriptions", []), default_lead)
    matches = find_matches(programmes, subscriptions)

    if not matches:
        print("No matching events found in the next", dispatcharr.get("lookahead_days", 7), "days.")
        return

    print(f"\n{'─'*70}")
    print(f"{'START (local)':<22}  {'CHANNEL':<20}  {'SUBSCRIPTION':<22}  TITLE")
    print(f"{'─'*70}")
    for m in sorted(matches, key=lambda x: x.programme.start):
        start_local = m.programme.start.astimezone().strftime("%a %b %-d  %-I:%M %p")
        print(
            f"{start_local:<22}  {m.programme.channel_name:<20}  "
            f"{m.subscription.label:<22}  {m.programme.title}"
        )
    print(f"{'─'*70}")
    print(f"Total: {len(matches)} events")


if __name__ == "__main__":
    main()
