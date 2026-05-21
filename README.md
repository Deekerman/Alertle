# 🐢 Alertle

<p align="center">
  <img src="alertle_turtle.png" alt="Alertle Turtle" width="300"/>
</p>

### *He's slow. Your alerts aren't.*

Alertle is a self-hosted sports notification app that scans your EPG for the week ahead, and fires off alerts when something you care about is on — before you miss the puck drop.

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
|---|---|
| 💬 **Discord** | Posts to a channel via webhook — great for household or friend group servers |
| ✈️ **Telegram** | Direct messages or group chat via a bot |
| 📲 **Pushover** | Push notifications straight to your phone or desktop |

Configure your preferred channels in the **Notifications** tab of the web UI.

---

## Requirements

- Node.js 18+
- A running [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) instance with EPG sources configured and matched to channels

---

## Quick Start

```bash
git clone https://github.com/Deekerman/alertle.git
cd alertle
python3 server.py
```

Open **http://localhost:8888** — the Alertle Turtle will be waiting.

---

## Setup

### 1. Connect to Dispatcharr

- Enter your Dispatcharr EPG URL

### 2. Configure Notifications

Head to the **Notifications** tab and set up whichever channels you want:

**Discord** — paste a webhook URL from your server's channel settings (`Settings → Integrations → Webhooks`)

**Telegram** — create a bot via [@BotFather](https://t.me/botfather), grab the token, and enter your chat ID

**Pushover** — add your User Key and Application Token from [pushover.net](https://pushover.net)

### 3. Set Up Rules

Rules tell Alertle what to watch for:

| Rule type | Matches against | Example |
|---|---|---|
| **Sport / category** | EPG category + title | `hockey`, `NFL`, `soccer` |
| **Team name** | Program title + description | `Toronto Maple Leafs`, `Blue Jays` |
| **Program title** | Substring match on title | `NHL on CBC`, `Monday Night Football` |

Rules can be toggled on or off without deleting them.

### 4. Browse the EPG

The **EPG** tab shows the next 7 days of programming pulled live from Dispatcharr. Use the **+ Program** and **+ Sport** quick-add buttons to create rules directly from what's listed.

### 5. Scan

Hit **Scan now** to immediately check all programs against your active rules. Matches trigger your configured notifications right away.

---

## Run on Startup

Create `/etc/systemd/system/alertle.service`:

```ini
[Unit]
Description=Alertle — Sports Alerts by Turtle
After=network.target

[Service]
WorkingDirectory=/path/to/alertle
ExecStart=/usr/bin/node server.js
Restart=on-failure
User=your-user
Environment=PORT=8888

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now alertle
```

---

## Data Files

| File | Purpose |
|---|---|
| `config.json` | Connection settings and notification config |
| `rules.json` | Your saved notification rules |
| `sent_notifications.json` | Dedup log — auto-managed, don't stress about it |

Everything is created automatically on first save. No manual setup required.

---

## Troubleshooting

**"Dispatcharr not configured"** — Save your URL and API key on the Connection tab first, then try again.

**No programs returned** — Make sure Dispatcharr has EPG sources configured and matched to channels. You can poke the API directly at `http://your-dispatcharr:9191/swagger/` → `GET /api/epg/programs/` to see what's there.

**Notifications not arriving** — Use the **Send test** button on each channel to isolate the issue. Check that your Discord webhook URL is correct, your Pushover keys are valid, and that your Telegram bot has been started (send it `/start` first).

**Something strange is happening** — Vibe coded. Try restarting the server, or explain the problem to an AI. That's how all of this got built anyway.

---

## Contributing

PRs welcome. The bar is simply: *does it work better than before?* If yes, ship it.

The turtle thanks you. 🐢

---

## License

MIT — do whatever you want. Just don't be slow about it.
