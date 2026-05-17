"""Match EPG programmes against user subscriptions."""

import logging
from dataclasses import dataclass
from typing import Optional

from epg_scanner import Programme

log = logging.getLogger(__name__)


@dataclass
class Subscription:
    label: str
    sport: Optional[str] = None      # substring match against categories
    team: Optional[str] = None       # substring match against title + description
    keyword: Optional[str] = None    # substring match against title + description
    channel: Optional[str] = None    # substring match against channel name or id
    lead_time_minutes: int = 30      # minutes before start to notify

    def matches(self, prog: Programme) -> bool:
        """Return True if *prog* satisfies all non-None fields of this subscription."""
        if self.sport:
            if not any(self.sport.lower() in cat.lower() for cat in prog.categories):
                return False

        text = f"{prog.title} {prog.description}".lower()

        if self.team:
            if self.team.lower() not in text:
                return False

        if self.keyword:
            if self.keyword.lower() not in text:
                return False

        if self.channel:
            ch = self.channel.lower()
            if ch not in prog.channel_name.lower() and ch not in prog.channel_id.lower():
                return False

        return True


@dataclass
class Match:
    programme: Programme
    subscription: Subscription


def build_subscriptions(raw: list[dict], default_lead_time: int) -> list[Subscription]:
    subs: list[Subscription] = []
    for entry in raw:
        subs.append(
            Subscription(
                label=entry.get("label", "Unnamed"),
                sport=entry.get("sport"),
                team=entry.get("team"),
                keyword=entry.get("keyword"),
                channel=entry.get("channel"),
                lead_time_minutes=entry.get("lead_time_minutes", default_lead_time),
            )
        )
    return subs


def find_matches(programmes: list[Programme], subscriptions: list[Subscription]) -> list[Match]:
    matches: list[Match] = []
    for prog in programmes:
        for sub in subscriptions:
            if sub.matches(prog):
                matches.append(Match(programme=prog, subscription=sub))
                log.debug("Matched '%s' → subscription '%s'", prog.title, sub.label)
                break  # one notification per programme per run
    return matches
