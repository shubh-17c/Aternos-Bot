"""Discord bot that starts an Aternos Minecraft server when someone says a keyword.

Flow: message keyword -> one Playwright run -> live status embed -> ping when up.

Only one run can be active at a time and each user has a cooldown, because every
run opens a real browser and Aternos dislikes hammering.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from aternos_runner import AternosStarter

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

PREFIX = "!"
LOG_LINES = 8
MAX_SCREENSHOT_BYTES = 7 * 1024 * 1024


def _id_set(name: str) -> set[int]:
    raw = os.getenv(name, "")
    return {int(part) for part in re.split(r"[,\s]+", raw) if part.isdigit()}


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
KEYWORDS = tuple(
    item.strip().casefold()
    for item in os.getenv("TRIGGER_KEYWORDS", "start server, start mc").split(",")
    if item.strip()
)
ALLOWED_CHANNELS = _id_set("ALLOWED_CHANNEL_IDS")
ALLOWED_USERS = _id_set("ALLOWED_USER_IDS")
ALLOWED_ROLES = _id_set("ALLOWED_ROLE_IDS")
COOLDOWN_SECONDS = max(0, int(os.getenv("COOLDOWN_SECONDS", "180")))

HEADLESS = os.getenv("HEADLESS", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
STATE_PATH = Path(os.getenv("ATERNOS_STATE", "state/aternos_state.json"))
SESSION_COOKIE = os.getenv("ATERNOS_SESSION", "").strip() or None
SID_COOKIE = os.getenv("ATERNOS_SID", "").strip() or None
SERVER_ID = os.getenv("ATERNOS_SERVER_ID", "").strip() or None
START_TIMEOUT = int(os.getenv("START_TIMEOUT_SECONDS", "2400"))
POLL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "7"))
TIMEZONE = os.getenv("TZ_NAME", "Asia/Kolkata")
PROXY_SERVER = os.getenv("PROXY_SERVER", "").strip() or None
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "").strip() or None
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD") or None


def find_keyword(content: str) -> str | None:
    haystack = content.casefold()
    for keyword in KEYWORDS:
        pattern = rf"(?<!\w){re.escape(keyword)}(?!\w)"
        if re.search(pattern, haystack):
            return keyword
    return None


@dataclass
class RunView:
    actor: discord.abc.User
    started: float = field(default_factory=time.monotonic)
    status: str = "Preparing…"
    ok: bool | None = None
    address: str | None = None
    screenshot: str | None = None
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    done_event: asyncio.Event = field(default_factory=asyncio.Event)

    def note(self, text: str) -> None:
        self.lines.append(text)

    def finish(
        self,
        ok: bool,
        status: str,
        address: str | None = None,
        screenshot: str | None = None,
    ) -> None:
        self.ok = ok
        self.status = status
        self.address = address
        self.screenshot = screenshot
        self.done_event.set()

    def embed(self) -> discord.Embed:
        if self.ok is True:
            title, colour = "✅ Aternos server is online", discord.Colour.green()
        elif self.ok is False:
            title, colour = "❌ Aternos start failed", discord.Colour.red()
        else:
            title, colour = "⏳ Starting Aternos server", discord.Colour.blurple()

        embed = discord.Embed(
            title=title, colour=colour, timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Triggered by", value=self.actor.mention, inline=True)
        embed.add_field(name="Status", value=(self.status or "—")[:1000], inline=True)
        embed.add_field(
            name="Elapsed",
            value=f"{int(time.monotonic() - self.started)}s",
            inline=True,
        )
        if self.address:
            embed.add_field(name="Address", value=f"`{self.address}`", inline=False)
        if self.lines:
            log_text = "\n".join(f"• {line}" for line in self.lines)
            embed.add_field(name="Log", value=log_text[:1020], inline=False)
        embed.set_footer(text="Aternos • Discord")
        return embed


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

ACTIVE_TASK: asyncio.Task | None = None
CURRENT_VIEW: RunView | None = None
RUN_ACTIVE = False
COOLDOWNS: dict[int, float] = {}


# --------------------------------------------------------------------- run flow


async def _refresh(view: RunView, message: discord.Message) -> None:
    while True:
        already_done = view.done_event.is_set()
        try:
            await message.edit(embed=view.embed())
        except discord.NotFound:
            return
        except discord.HTTPException:
            pass

        if already_done:
            # Render the final state once more in case finish() landed mid-edit.
            try:
                await message.edit(embed=view.embed())
            except discord.HTTPException:
                pass
            return

        try:
            await asyncio.wait_for(view.done_event.wait(), timeout=5)
        except TimeoutError:
            pass


async def _drive(view: RunView, status_message: discord.Message) -> None:
    global RUN_ACTIVE
    updater = asyncio.create_task(_refresh(view, status_message))
    try:
        starter = AternosStarter(
            state_path=STATE_PATH,
            session_cookie=SESSION_COOKIE,
            sid_cookie=SID_COOKIE,
            server_id=SERVER_ID,
            headless=HEADLESS,
            timeout=START_TIMEOUT,
            poll_interval=POLL_SECONDS,
            screenshot_dir=STATE_PATH.parent,
            timezone=TIMEZONE,
            proxy_server=PROXY_SERVER,
            proxy_username=PROXY_USERNAME,
            proxy_password=PROXY_PASSWORD,
            on_event=view.note,
        )
        result = await starter.run()
        view.finish(
            result.ok,
            result.message,
            address=result.address,
            screenshot=result.screenshot,
        )
    except asyncio.CancelledError:
        view.finish(False, "Cancelled.")
        raise
    except Exception as exc:
        log.exception("Unhandled error while starting the server")
        view.finish(False, f"Unhandled error: {exc}")
    finally:
        RUN_ACTIVE = False
        await updater

    if view.ok:
        text = f"{view.actor.mention} server is up."
        if view.address:
            text += f" Connect with `{view.address}`."
        await _safe_reply(status_message, text)
    else:
        await _safe_reply(
            status_message, f"{view.actor.mention} start failed: {view.status}"
        )
        await _send_screenshot(status_message, view.screenshot)


async def _safe_reply(message: discord.Message, content: str) -> None:
    try:
        await message.reply(content, mention_author=False)
    except discord.HTTPException:
        log.warning("Could not reply to status message", exc_info=True)


async def _send_screenshot(message: discord.Message, path: str | None) -> None:
    if not path:
        return
    file_path = Path(path)
    try:
        if file_path.is_file() and file_path.stat().st_size <= MAX_SCREENSHOT_BYTES:
            await message.reply(
                "Debug screenshot of the page:",
                file=discord.File(file_path),
                mention_author=False,
            )
        else:
            log.info("Screenshot not sent (missing or too large): %s", file_path)
    except (OSError, discord.HTTPException):
        log.warning("Could not send screenshot %s", file_path, exc_info=True)


async def trigger(message: discord.Message, source: str) -> None:
    global ACTIVE_TASK, CURRENT_VIEW, RUN_ACTIVE

    if message.guild is None:
        return
    if ALLOWED_CHANNELS and message.channel.id not in ALLOWED_CHANNELS:
        return
    if ALLOWED_USERS and message.author.id not in ALLOWED_USERS:
        await message.reply("You're not allowed to start the server.")
        return
    if ALLOWED_ROLES:
        roles = getattr(message.author, "roles", [])
        if not any(role.id in ALLOWED_ROLES for role in roles):
            await message.reply("You don't have a role that can start the server.")
            return

    if RUN_ACTIVE:
        await message.reply("A start is already running — look at the update above.")
        return

    now = time.monotonic()
    remaining = COOLDOWN_SECONDS - (now - COOLDOWNS.get(message.author.id, 0.0))
    if remaining > 0:
        await message.reply(f"Cooldown: try again in {int(remaining) + 1}s.")
        return
    COOLDOWNS[message.author.id] = now

    # Reserve before awaiting Discord, otherwise two messages that arrive in the
    # same tick could both pass the check and launch two browsers.
    RUN_ACTIVE = True

    view = RunView(actor=message.author)
    view.note(f"Triggered by {source}.")
    try:
        status_message = await message.reply(embed=view.embed(), mention_author=False)
    except discord.HTTPException:
        RUN_ACTIVE = False
        raise

    CURRENT_VIEW = view
    ACTIVE_TASK = asyncio.create_task(_drive(view, status_message))
    ACTIVE_TASK.add_done_callback(_log_task_result)


def _log_task_result(task: asyncio.Task) -> None:
    if task.cancelled():
        log.warning("Start task was cancelled")
        return
    error = task.exception()
    if error is not None:
        log.error("Start task crashed", exc_info=error)


# --------------------------------------------------------------------- commands


@bot.command(name="mcstart", aliases=["startmc"])
async def mcstart(ctx: commands.Context) -> None:
    """Start the server without typing the keyword."""
    await trigger(ctx.message, "the `!mcstart` command")


@bot.command(name="mcstatus")
async def mcstatus(ctx: commands.Context) -> None:
    """Show the latest run and its live log."""
    view = CURRENT_VIEW
    if view is None:
        await ctx.reply("No start has run since I came online.", mention_author=False)
        return
    await ctx.reply(embed=view.embed(), mention_author=False)


@bot.command(name="mchelp")
async def mchelp(ctx: commands.Context) -> None:
    keywords = ", ".join(f"`{keyword}`" for keyword in KEYWORDS)
    await ctx.reply(
        f"Say {keywords} in chat, or use `!mcstart`.\n"
        "`!mcstatus` shows the latest run.",
        mention_author=False,
    )


# ----------------------------------------------------------------------- events


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.guild is None:
        return

    await bot.process_commands(message)

    if message.content.startswith(PREFIX):
        return

    keyword = find_keyword(message.content)
    if keyword is not None:
        await trigger(message, f"the `{keyword}` keyword")


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
    log.info("Trigger keywords: %s", ", ".join(KEYWORDS) or "(none)")
    if not SESSION_COOKIE and not STATE_PATH.exists():
        log.warning(
            "No Aternos session configured. Set ATERNOS_SESSION or run login.py."
        )

    permissions = discord.Permissions(
        view_channel=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
        read_message_history=True,
        add_reactions=True,
    )
    log.info(
        "Invite URL: %s", discord.utils.oauth_url(bot.user.id, permissions=permissions)
    )
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching, name="the Aternos queue"
        )
    )


def main() -> None:
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set — copy .env.example to .env first.")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
