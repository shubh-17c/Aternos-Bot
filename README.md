# Aternos Discord starter

A Discord bot that starts and stops your Aternos Minecraft server from chat.
One command runs a real Chromium browser, clicks **Start**, confirms the queue
when Aternos asks, accepts the Mojang EULA if it appears, and posts a live status
embed until the server reports **Online**. `/mcstatus` reads the live state,
players and queue position; `/mcstop` stops the server or cancels a pending
start.

```
#discord
you: start server
bot: ⏳ Starting Aternos server …
     Aternos: Preparing …
     Queue position 3 of 12, about 2 min.
     Confirmed the queue spot.
     Aternos: Online
bot: ✅ @you server is up. Connect with `yourserver.aternos.me`
```

## Read this first

Aternos has **no public API**, so this drives the website with a browser. That
means:

- It is automation of a free service. Aternos' rules prohibit bots/scripts that
  fake activity or keep servers online 24/7, and their systems look for
  automated requests. An on-demand start is much less abusive than an AFK
  keeper, but it can still get an account restricted. Use your own account and
  accept that risk. Do **not** use this to keep a server up around the clock.
- If you want a supported version of this idea, Aternos' paid sibling
  [exaroton](https://exaroton.com) has autostart-on-join built in.
- Aternos changes its frontend occasionally. The page ids used here are
  `#start`, `#stop`, `#cancel`, `#confirm` and `#status`; if they rename one,
  the bot saves a screenshot of the page and posts it in Discord so you can see
  what broke.

## What's in the box

| File | Purpose |
| --- | --- |
| `bot.py` | Discord side: keyword + slash commands, cooldowns, live embeds, admin-gated stop |
| `aternos_runner.py` | Playwright automation: start, stop/cancel, queue confirm, EULA, live status |
| `login.py` | One-time local login that saves a browser session |
| `Dockerfile`, `docker-compose.yml` | Container with Chromium + Xvfb |
| `.env.example` | All settings, with the safe defaults |

## 1. Create the Discord bot

1. Open <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab → **Reset Token** → copy it. This is `DISCORD_TOKEN`.
3. On the same page, enable **Message Content Intent** (required for keyword
   detection; without it the bot sees empty messages).
4. **OAuth2 → URL Generator**: scope `bot` and `applications.commands`,
   permissions *View Channels, Send Messages, Embed Links, Attach Files, Read
   Message History*. Open the generated URL and add the bot to your server.

The bot prints a ready invite URL to its log on startup too.

## 2. Get an Aternos session

Run the helper on your own computer:

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
python login.py
```

A browser opens. Log in, open the server you want, press Enter in the terminal.
It writes `state/aternos_state.json` and prints:

```
ATERNOS_SESSION=...
ATERNOS_SERVER_ID=...
```

Put those in `.env`. The session file is a live login — never commit it. When
Aternos logs the session out, the bot says so and you repeat this step.

> If you deploy the container elsewhere, either copy `state/aternos_state.json`
> into the server's `state/` folder or just use the `ATERNOS_SESSION` env var.
> The cookie is tied to the account, but Cloudflare clearance is tied to the
> IP + browser, so a fresh challenge on the new host is normal. For the best
> chance of reusing clearance, run `login.py` on the same machine/network as
> the bot.

## 3. Configure

```bash
cp .env.example .env
$EDITOR .env
```

Minimum:

```dotenv
DISCORD_TOKEN=...
ATERNOS_SESSION=...
```

Useful extras:

| Variable | Default | Notes |
| --- | --- | --- |
| `DISCORD_GUILD_ID` | empty (global) | Register slash commands in one server for instant updates |
| `TRIGGER_KEYWORDS` | `start server,start mc` | Comma-separated, whole-word, case-insensitive |
| `ALLOWED_CHANNEL_IDS` | empty (all) | Restrict where the keyword and `!mc*` commands work |
| `ALLOWED_USER_IDS` / `ALLOWED_ROLE_IDS` | empty (everyone) | Lock down who can start the server |
| `COOLDOWN_SECONDS` | `180` | Per-user wait between starts |
| `STATUS_CACHE_SECONDS` | `60` | How long `/mcstatus` reuses a result before launching a browser |
| `ATERNOS_SERVER_ID` | last opened | Choose a server on multi-server accounts |
| `ATERNOS_STATE` | `state/aternos_state.json` | Browser state file written by `login.py` |
| `ATERNOS_SID` | empty | Optional second session cookie from `login.py` |
| `START_TIMEOUT_SECONDS` | `2400` | Includes queue time |
| `HEADLESS` | `true` | `false` runs under Xvfb in the image |
| `PROXY_SERVER` | empty | e.g. `http://host:port` — residential proxy for Cloudflare-blocked hosts |
| `PROXY_USERNAME` / `PROXY_PASSWORD` | empty | Proxy auth, if any |

## 4. Run it

Local:

```bash
python bot.py
```

Docker:

```bash
mkdir -p state
docker compose up -d --build
docker compose logs -f
```

The first build downloads Chromium and its system libraries (roughly 500 MB),
so give it a few minutes.

Test the automation without Discord (handy for selector/Cloudflare problems):

```bash
docker compose run --rm bot python aternos_runner.py start
docker compose run --rm bot python aternos_runner.py status
docker compose run --rm bot python aternos_runner.py stop
```

## Using it

| Command | Who | What |
| --- | --- | --- |
| `start server` / `start mc` | everyone (or allowlist) | Keyword trigger; starts the server |
| `/mcstart` | same as keyword | Slash version of the trigger |
| `/mcstatus` | everyone | Live state, address, players and queue |
| `/mcstop` | **Manage Server** | Stops the server, or cancels a queued/starting one |
| `/mchelp` | everyone | Command summary |
| `!mcstart`, `!mcstatus`, `!mcstop`, `!mchelp` | same rules | Prefix equivalents |

- Only one browser operation runs at a time; duplicate requests get a short
  reply instead of a second Chromium instance.
- `/mcstatus` reuses a result for `STATUS_CACHE_SECONDS` (60 by default) so
  checking status does not launch a browser every time.
- `/mcstop` while a start is queued cancels that start, then loads the server
  page to make sure it is really offline. It requires the **Manage Server**
  permission.
- Slash commands are registered on startup. Set `DISCORD_GUILD_ID` for instant
  per-server registration; global commands can take up to an hour to appear.
- `ALLOWED_CHANNEL_IDS` applies to the keyword and prefix commands. Slash
  commands can be restricted with Discord's own **Integrations** settings.
- The bot never "watches" ads. If Aternos offers an ad versus queue choice, it
  takes the queue path and clicks the green confirm button when it appears. A
  mandatory ad wall cannot be passed headlessly — that shows up as a timeout
  plus a screenshot.

## Hosting notes

Any Docker host works — a small VPS, Railway, Fly.io, a home server, a
Raspberry Pi. Two practical caveats:

1. **Cloudflare.** Aternos sits behind Cloudflare. A bare datacenter IP is very
   likely to get a hard `403 Just a moment...` challenge — I confirmed exactly
   that behaviour while testing this bot from a cloud box, with both Playwright
   headless builds. Cloudflare does not care how good the browser is if the IP
   is already suspect. Your options, best first:
   - run the bot at home (residential IP) — by far the most reliable;
   - point `PROXY_SERVER` at a residential/mobile proxy;
   - try `HEADLESS=false` (the image includes Xvfb) on a host that isn't
     hard-blocked, and solve one challenge by hand;
   - don't bother with free public proxies — they are already flagged.

   The bot detects the challenge and posts a clear error instead of pretending
   to start the server.
2. **Resources.** Chromium wants roughly 1 GB RAM. Give the container
   `shm_size: 512mb` (already set) and don't run it on a 256 MB instance.

For Railway/Fly-style platforms, mount a persistent volume at `/app/state` so
the session survives redeploys, and keep the service always-on (no scale to
zero), otherwise a queue wait will be killed mid-run.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Bot ignores keywords | Message Content Intent is off, or the channel isn't in `ALLOWED_CHANNEL_IDS` |
| Slash commands missing | Restart the bot and check the sync log line; set `DISCORD_GUILD_ID` for instant registration |
| "/mcstop" says no permission | The user needs **Manage Server**; server admins can adjust it under Integrations |
| "Aternos rejected the session" | Re-run `login.py`, update `ATERNOS_SESSION` |
| "Cloudflare challenge did not pass" | Host IP is blocked — use a residential IP/proxy; see hosting note above |
| HTTP 403 / "Just a moment..." | Same as Cloudflare; no code change fixes a flagged IP |
| Stuck at "Preparing" then timeout | Aternos likely changed the UI; check the screenshot the bot posted |
| Queue confirm never happens | `#confirm` was hidden/disabled while polling. Polling every 7 s covers the confirm window; if it still misses, check the screenshot for a renamed button |
| `/mcstop` can't find Stop | Aternos renamed the button; check the screenshot/log |
| "Executable doesn't exist" | Image built without `playwright install` — rebuild with `docker compose build --no-cache` |
| Bot online, nothing happens | Check `docker compose logs`; the embed log is mirrored there |

Screenshots from failed runs land in `state/aternos-*.png` and are posted to
Discord when small enough.
