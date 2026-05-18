#!/usr/bin/env python3
"""EPG game notifier — scans Dispatcharr EPG and fires alerts for subscribed events."""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from epg_scanner import DispatcharrClient
from matcher import Match, build_subscriptions, find_matches, group_matches
from notifiers.base import (
    BaseNotifier, DEFAULT_TITLE_TEMPLATE, DEFAULT_BODY_TEMPLATE,
    build_preview_vars, format_grouped_message,
)
from storage import NotificationStore

log = logging.getLogger(__name__)


# ── Config loading ─────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_notifiers_map(cfg: dict) -> dict[str, BaseNotifier]:
    """Build a dict of channel_key → notifier for all enabled channels."""
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


# ── Core scan logic ────────────────────────────────────────────────────────

def run_scan(cfg: dict, notifiers_map: dict[str, BaseNotifier], store: NotificationStore, dry_run: bool):
    dispatcharr = cfg["dispatcharr"]
    client = DispatcharrClient(
        dispatcharr["url"],
        dispatcharr.get("token", ""),
        dispatcharr.get("xmltv_url", ""),
    )

    now = datetime.now(timezone.utc)
    lookahead = timedelta(days=dispatcharr.get("lookahead_days", 7))
    window_end = now + lookahead

    default_lead = cfg.get("default_lead_time_minutes", 30)
    subscriptions = build_subscriptions(cfg.get("subscriptions", []), default_lead)

    if not subscriptions:
        log.warning("No subscriptions configured — nothing to match.")
        return

    log.info("Scanning EPG from %s to %s", now.strftime("%Y-%m-%d %H:%M"), window_end.strftime("%Y-%m-%d %H:%M"))
    programmes = client.fetch_programmes(now, window_end)

    matches = find_matches(programmes, subscriptions)
    grouped = group_matches(matches)
    log.info("Found %d unique events (%d channel slots matched)", len(grouped), len(matches))

    from espn import filter_replays
    grouped = filter_replays(grouped, cfg)

    sent_count = 0
    for g in grouped:
        notify_at = g.start - timedelta(minutes=g.subscription.lead_time_minutes)
        if now < notify_at:
            log.debug("Skipping '%s' — notify at %s UTC", g.title, notify_at.strftime("%Y-%m-%d %H:%M"))
            continue

        if store.already_sent(g.group_uid, g.subscription.label):
            log.debug("Already notified: %s / %s", g.subscription.label, g.title)
            continue

        notif_tpl = cfg.get("notification_template", {})
        title_tpl = g.subscription.notif_title_template or notif_tpl.get("title", DEFAULT_TITLE_TEMPLATE)
        body_tpl = g.subscription.notif_body_template or notif_tpl.get("body", DEFAULT_BODY_TEMPLATE)
        show_nums = notif_tpl.get("show_channel_nums", False)
        title, body = format_grouped_message(g, title_tpl, body_tpl, show_channel_nums=show_nums)

        if dry_run:
            print(f"\n{'─'*60}")
            print(f"[DRY RUN] {title}")
            print(body)
        else:
            _dispatch(notifiers_map, g.subscription.notify_channels, title, body)
            store.mark_sent(g.group_uid, g.subscription.label, now.isoformat())
            sent_count += 1

    if not dry_run:
        store.prune_old((now - lookahead).isoformat())
        log.info("Notifications sent: %d", sent_count)


def _dispatch(notifiers_map: dict[str, BaseNotifier], sub_channels: list[str], title: str, body: str):
    targets = notifiers_map if not sub_channels else {
        k: v for k, v in notifiers_map.items() if k in sub_channels
    }
    for key, notifier in targets.items():
        try:
            notifier.send(title, body)
        except Exception as exc:
            log.error("Notifier %s failed: %s", key, exc)


# ── CLI ────────────────────────────────────────────────────────────────────

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
