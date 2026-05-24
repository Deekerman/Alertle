# 🐢 Alertle

<p align="center">
  <img src="alertle_turtle.png" alt="Alertle Turtle" width="300"/>
</p>


### *He's slow. Your alerts aren't.*

Alertle is a self-hosted EPG monitoring and alert system. Built for sports fans, but flexible enough to notify you about any programming you care about.

---

## Meet the Alertle Turtle 🐢

The Alertle Turtle is our mascot, our spirit animal, and our greatest irony. He is notoriously slow. He does not rush for anyone. And yet somehow, he always makes sure your game alerts arrive on time.

*Don't miss a game. The Alertle Turtle's got you.*

---

## ⚡ Vibe Coded

This entire project was built through conversation with Claude AI — no code was written by hand. No documentation was read. No Stack Overflow tabs were opened. Just vibes, prompts, and one very good turtle idea.

If it works, great. If something breaks, that's also the vibes. PRs are welcome. So is asking an AI about it.

---

## Features

- 📅 **7-day EPG scanning** — always looking ahead so you don't have to
- 🏒 **Flexible rules** — get notified by sport, team name, or specific program title
- 🔔 **Multiple notification channels** — Discord, Telegram, and Pushover
- 🖥️ **Web UI** — clean setup interface, no config files to hand-edit
- 🔁 **Deduplication** — one alert per game, not ten

---

## Notification Channels

| Channel | What it does |
| --------------- | ---------------------------------------------------------------------------- |
| 💬 **Discord** | Posts to a channel via webhook — great for household or friend group servers |
| ✈️ **Telegram** | Direct messages or group chat via a bot |
| 📲 **Pushover** | Push notifications straight to your phone or desktop |

Configure your preferred channels in the **Notifications** tab of the web UI.

---

## Requirements

- Python 3.9+
- A running [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) instance with EPG sources configured and matched to channels

---

## Quick Start

```bash
git clone https://github.com/Deekerman/Alertle.git
cd Alertle
sudo bash install.sh
```

Open **http://localhost:8888** — the Alertle Turtle will be waiting.

> The installer handles everything: Python dependencies, a dedicated service user, and a systemd service that starts automatically on boot.

---

## Setup

### 1. Connect to Dispatcharr

- Enter your Dispatcharr EPG URL in the **Settings** tab

### 2. Configure Notifications

Head to the **Notifications** tab and set up whichever channels you want:

**Discord** — paste a webhook URL from your server's channel settings (`Settings → Integrations → Webhooks`)

**Telegram** — create a bot via [@BotFather](https://t.me/botfather), grab the token, and enter your chat ID

**Pushover** — add your User Key and Application Token from [pushover.net](https://pushover.net)

### 3. Set Up Rules

Rules tell Alertle what to watch for:

| Rule type | Matches against | Example |
| -------------------- | --------------------------- | ------------------------------------- |
| **Sport / category** | EPG category + title | `hockey`, `NFL`, `soccer` |
| **Team name** | Program title + description | `Toronto Maple Leafs`, `Blue Jays` |
| **Program title** | Substring match on title | `NHL on CBC`, `Monday Night Football` |

Rules can be toggled on or off without deleting them.

### 4. Browse the EPG

The **EPG** tab shows the next 7 days of programming pulled live from Dispatcharr. Use the **+ Program** and **+ Sport** quick-add buttons to create rules directly from what's listed.

### 5. Scan

Hit **Scan now** to immediately check all programs against your active rules. Matches trigger your configured notifications right away.

---

## Data Files

| File | Purpose |
| ------------- | ----------------------------------------------- |
| `config.yaml` | Connection settings and notification config |
| `alertle.db` | Rules and sent notification history — auto-managed |

Everything is created automatically on first run. No manual setup required.

---

## Useful Commands

```bash
# View live logs
journalctl -u alertle -f

# Restart the service
systemctl restart alertle

# Stop the service
systemctl stop alertle
```

---

## Troubleshooting

**Something strange is happening** — Vibe coded. Try restarting the service, or explain the problem to an AI. That's how all of this got built anyway.

---

## Contributing

PRs welcome. The bar is simply: *does it work better than before?* If yes, ship it.

The turtle thanks you. 🐢

---

## License

MIT — do whatever you want. Just don't be slow about it.
