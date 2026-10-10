# Notifications

**Settings → Notifications.** Shrinkerr can tell you when something
happens — through Discord, Telegram, email, a webhook, ntfy, Gotify, or any
of the 100+ services Apprise supports. Set up as many as you like; every
event goes to all of them. **Test Notifications** sends one to each.
**Notification language** sets the language they're written in,
whatever language each browser shows the interface in.

## Channels

| Channel | What to enter |
|---|---|
| Discord | A channel webhook URL |
| Telegram | A bot token and the chat ID |
| Email | SMTP host, port, username, password and the address to send to |
| Webhook | Any URL; Shrinkerr POSTs JSON with the event, title, message and fields |
| ntfy | The topic URL (e.g. `https://ntfy.sh/my-shrinkerr`), and an access token for a protected topic |
| Gotify | The server URL and an application token |
| Apprise | Service URLs, one per line — Pushover, Slack, Matrix, Pushbullet, Teams and more; see the [Apprise wiki](https://github.com/caronc/apprise/wiki) for each service's URL |

The email password, the Telegram bot token and the ntfy and Gotify tokens
are shown masked after saving. Settings exports leave out the password, the
tokens, and the Discord, webhook, ntfy and Apprise URLs.

## Events

| Event | When |
|---|---|
| Queue complete | The queue has run out of work |
| Job failed | A conversion or cleanup failed |
| Disk space low | A media folder's free space falls below the disk threshold (GB) set here |
| VMAF rejected | A conversion scored below your minimum VMAF and was discarded (the original kept) |
| Worker offline | A remote worker stopped answering — once per outage |
| Weekly summary | Once a week: files converted, space saved, failures, what's waiting, and the total saved |

Turn each one on or off above the channel list. Failures, low disk
space, rejections and offline workers are sent as urgent where the
channel has priorities (ntfy, Gotify).

## Monitoring instead

For dashboards and alerting rules, scrape Shrinkerr's Prometheus metrics —
see [Monitoring](monitoring.md).
