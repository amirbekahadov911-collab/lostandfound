# School Lost & Found Bot

A Telegram bot for a school Lost & Found group:

- **Anyone in the group** can DM the bot with `/report` to post a found item. The bot asks for a photo, description, date found, where it was found, and where to claim it, then posts it to the group.
- **Every school morning** the bot reposts each unclaimed item with its photo and caption, and deletes the previous day's reposts so the chat stays tidy.
- **Only the person who posted an item** (or a group admin) can close it, using the "✅ Owner found" button or `/mine`. Closed items leave the daily loop immediately.
- **Moderation** works with strikes. 1st: friendly warning, 2nd: 1-hour mute, 3rd: 1-day mute, 4th: 1-week mute, 5th: ban. Flooding, repeated messages and links get a strike automatically. Admins use `/warn` for trolling or larping. Strikes don't reset, so each new offence is punished harder than the last.

## 1. Create the bot

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the steps. Copy the **token**.
2. Add the bot to your group and **make it an admin** with these permissions: *Delete messages* and *Ban users*. As an admin it can also see all messages, which it needs for spam detection.
3. Find the group ID: add **@RawDataBot** to the group for a moment. It replies with `"chat": {"id": -100...}`. Copy that number, then remove it.

## 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in `BOT_TOKEN`, `GROUP_ID`, your `TIMEZONE` (e.g. `Asia/Tashkent`, `Europe/London`), the reminder time and days, and your school's claim points.

## 3. Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```

To test right away, send `/remindnow` to the bot as a group admin.

## 4. Keep it running 24/7

The bot has to run all the time on a computer that's always on. Options, cheapest first:

| Option | Cost | Notes |
|---|---|---|
| **Old laptop / Raspberry Pi at school or home** | Free | Must stay on and online. |
| **Small VPS** (Hetzner, DigitalOcean, Oracle Cloud free tier) | €0–5/month | Most reliable. Setup below. |
| **Railway / Render** | ~$5/month | No server admin, but add a persistent volume for `lostfound.db` or you'll lose items on redeploy. |

**On a Linux VPS** (Ubuntu), after copying this folder to `/opt/lost-and-found-bot` and setting up `.env` and `.venv` as above:

```bash
sudo tee /etc/systemd/system/lostfound.service >/dev/null <<'EOF'
[Unit]
Description=School Lost & Found bot
After=network-online.target

[Service]
WorkingDirectory=/opt/lost-and-found-bot
ExecStart=/opt/lost-and-found-bot/.venv/bin/python bot.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now lostfound
journalctl -u lostfound -f   # view logs
```

Back up `lostfound.db` now and then. It holds all items and strikes.

## Commands

**Everyone (in a private chat with the bot)**
- `/report`: post a found item
- `/list`: see all unclaimed items
- `/mine`: mark your items as returned or remove them
- `/strikes`: check your own strikes
- `/cancel`: stop the current step

**Group admins**
- `/warn [reason]` (reply to a message): give a strike and apply the next punishment
- `/unwarn`: remove one strike
- `/strikes` (reply): see someone's strikes
- `/ban [reason]` (reply): ban right away
- `/unmute`, `/unban`: reply to the person, or pass their user ID
- `/remove <item number>`: remove any item from the loop
- `/remindnow`: send the daily reminder now

To change the punishment ladder, edit `LADDER` in `moderation.py`.
