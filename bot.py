"""Discord bot that starts and stops an Aternos Minecraft server.

Flow: a keyword or slash command runs one Playwright browser session, while a
live embed shows the Aternos status until the server is online or stopped.

Only one browser operation is active at a time, each user has a start cooldown,
and /mcstatus reuses a recent result so a chatty channel does not hammer Aternos.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from aternos_runner import AternosController, ServerStatus, controller_from_env

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

PREFIX = "!"
LOG_LINES = 8
MAX_SCREENSHOT_BYTES = 7 * 1024 * 1024

STATE_LABELS = {
    "online": "Online",
    "offline": "Offline",
    "queue": "In queue",
    "starting": "Starting",
    "loading": "Loading",
    "preparing": "Preparing",
    "stopping": "Stopping",
    "error": "Error",
    "unknown": "Unknown",
}

STATE_COLOURS = {
    "online": discord.Colour.green(),
    "queue": discord.Colour.orange(),
    "starting": discord.Colour.blurple(),
    "loading": discord.Colour.blurple(),
    "preparing": discord.Colour.blurple(),
    "stopping": discord.Colour.orange(),
    "offline": discord.Colour.dark_grey(),
    "error": discord.Colour.red(),
}


def _id_set(name: str) -> set[int]:
    raw = os.getenv(name, "")
    return {int(part) for part in re.split(r"[,\s]+", raw) if part.isdigit()}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        log.warning("Ignoring invalid %s=%r", name, raw)
        return default


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
KEYWORDS = tuple(
    item.strip().casefold()
    for item in os.getenv("TRIGGER_KEYWORDS", "start server, start mc").split(",")
    if item.strip()
)
ALLOWED_CHANNELS = _id_set("ALLOWED_CHANNEL_IDS")
ALLOWED_USERS = _id_set("ALLOWED_USER_IDS")
ALLOWED_ROLES = _id_set("ALLOWED_ROLE_IDS")
COOLDOWN_SECONDS = max(0, _env_int("COOLDOWN_SECONDS", 180))
STATUS_CACHE_SECONDS = max(0, _env_int("STATUS_CACHE_SECONDS", 60))
GUILD_ID = _env_int("DISCORD_GUILD_ID", 0)

STATE_PATH = Path(os.getenv("ATERNOS_STATE", "state/aternos_state.json"))
SESSION_COOKIE = os.getenv("ATERNOS_SESSION", "").strip() or None


def find_keyword(content: str) -> str | None:
    haystack = content.casefold()
    for keyword in KEYWORDS:
        pattern = rf"(?<!\w){re.escape(keyword)}(?!\w)"
        if re.search(pattern, haystack):
            return keyword
    return None


def _access_error(member: discord.abc.User) -> str | None:
    if ALLOWED_USERS and member.id not in ALLOWED_USERS:
        return "You're not allowed to start the server."
    if ALLOWED_ROLES:
        roles = getattr(member, "roles", [])
        if not any(role.id in ALLOWED_ROLES for role in roles):
            return "You don't have a role that can start the server."
    return None


def _is_admin(member: discord.abc.User) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    return bool(permissions and permissions.manage_guild)


def _human_state(state: str) -> str:
    return STATE_LABELS.get(state, state.replace("_", " ").title() or "Unknown")


def _queue_text(status: ServerStatus) -> str | None:
    parts = []
    if status.queue_position:
        parts.append(f"Position {status.queue_position}")
    if status.queue_count:
        parts.append(f"{status.queue_count} waiting")
    if status.queue_eta:
        parts.append(status.queue_eta)
    return " · ".join(parts) if parts else None


def status_embed(
    status: ServerStatus, *, cached_age: int | None = None
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Aternos: {_human_state(status.state)}",
        colour=STATE_COLOURS.get(status.state, discord.Colour.blurple()),
    )
    embed.add_field(name="State", value=_human_state(status.state), inline=True)
    if status.address:
        embed.add_field(name="Address", value=f"`{status.address}`", inline=True)

    queue = _queue_text(status)
    if queue:
        embed.add_field(name="Queue", value=queue, inline=False)

    if status.state == "online":
        if status.players:
            embed.add_field(
                name=f"Players ({len(status.players)})",
                value=", ".join(status.players)[:1024],
                inline=False,
            )
        else:
            embed.add_field(name="Players", value="No players online", inline=False)

    if status.raw_text:
        embed.add_field(name="Aternos page", value=status.raw_text[:1024], inline=False)

    footer = "Aternos"
    if cached_age is not None:
        footer += f" · cached {cached_age}s ago"
    embed.set_footer(text=footer)
    return embed


def error_embed(title: str, message: str) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=(message or "Unknown error.")[:4000],
        colour=discord.Colour.red(),
    )


@dataclass
class RunView:
    actor: discord.abc.User
    action: str = "start"
    started: float = field(default_factory=time.monotonic)
    status: str = "Working…"
    ok: bool | None = None
    address: str | None = None
    screenshot: str | None = None
    live: ServerStatus | None = None
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
        live: ServerStatus | None = None,
    ) -> None:
        self.ok = ok
        self.status = status
        self.address = address
        self.screenshot = screenshot
        self.live = live
        self.done_event.set()

    def embed(self) -> discord.Embed:
        noun = "stop" if self.action == "stop" else "start"
        if self.ok is True:
            title = (
                "✅ Aternos server stopped"
                if self.action == "stop"
                else "✅ Aternos server is online"
            )
            colour = discord.Colour.green()
        elif self.ok is False:
            title = f"❌ Aternos {noun} failed"
            colour = discord.Colour.red()
        else:
            title = f"⏳ {'Stopping' if self.action == 'stop' else 'Starting'} Aternos server"
            colour = discord.Colour.blurple()

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
        if self.live is not None:
            queue = _queue_text(self.live)
            if queue:
                embed.add_field(name="Queue", value=queue, inline=False)
            if self.live.state == "online":
                if self.live.players:
                    embed.add_field(
                        name=f"Players ({len(self.live.players)})",
                        value=", ".join(self.live.players)[:1024],
                        inline=False,
                    )
                else:
                    embed.add_field(
                        name="Players", value="No players online", inline=False
                    )
        if self.lines:
            log_text = "\n".join(f"• {line}" for line in self.lines)
            embed.add_field(name="Log", value=log_text[:1020], inline=False)
        embed.set_footer(text="Aternos • Discord")
        return embed


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

ACTIVE_TASK: asyncio.Task | None = None
ACTIVE_VIEW: RunView | None = None
ACTIVE_KIND: str | None = None
RUN_ACTIVE = False
COOLDOWNS: dict[int, float] = {}
STATUS_CACHE: tuple[float, ServerStatus] | None = None

RenderCallback = Callable[[discord.Embed], Awaitable[None]]


def _make_controller(view: RunView | None = None) -> AternosController:
    controller = controller_from_env()
    if view is not None:
        controller.on_event = view.note
    return controller


# --------------------------------------------------------------------- run flow


async def _refresh(view: RunView, render: RenderCallback) -> None:
    while True:
        already_done = view.done_event.is_set()
        try:
            await render(view.embed())
        except discord.NotFound:
            return
        except discord.HTTPException:
            pass

        if already_done:
            # Render the final state once more in case finish() landed mid-edit.
            try:
                await render(view.embed())
            except discord.HTTPException:
                pass
            return

        try:
            await asyncio.wait_for(view.done_event.wait(), timeout=5)
        except TimeoutError:
            pass


async def _drive(view: RunView, status_message: discord.Message, action: str) -> None:
    global RUN_ACTIVE, ACTIVE_VIEW, ACTIVE_KIND, STATUS_CACHE

    async def render(embed: discord.Embed) -> None:
        await status_message.edit(embed=embed)

    updater = asyncio.create_task(_refresh(view, render))
    try:
        controller = _make_controller(view)
        result = await (controller.stop() if action == "stop" else controller.run())
        view.finish(
            result.ok,
            result.message,
            address=result.address,
            screenshot=result.screenshot,
            live=result.status,
        )
        if result.ok and result.status is not None:
            STATUS_CACHE = (time.monotonic(), result.status)
    except asyncio.CancelledError:
        view.finish(False, "Cancelled.")
        raise
    except Exception as exc:
        log.exception("Unhandled error while running %s", action)
        view.finish(False, f"Unhandled error: {exc}")
    finally:
        RUN_ACTIVE = False
        ACTIVE_VIEW = None
        ACTIVE_KIND = None
        await updater

    if view.ok:
        if view.action == "stop":
            text = f"{view.actor.mention} stop finished: {view.status}"
        else:
            text = f"{view.actor.mention} server is up."
            if view.address:
                text += f" Connect with `{view.address}`."
        await _safe_reply(status_message, text)
    else:
        await _safe_reply(
            status_message,
            f"{view.actor.mention} {view.action} failed: {view.status}",
        )
        await _send_screenshot(status_message, view.screenshot)


async def _safe_reply(message: discord.Message, content: str) -> None:
    try:
        await message.reply(content, mention_author=False)
    except discord.HTTPException:
        log.warning("Could not reply to status message", exc_info=True)


def _screenshot_file(path: str | None) -> discord.File | None:
    if not path:
        return None
    file_path = Path(path)
    try:
        if file_path.is_file() and file_path.stat().st_size <= MAX_SCREENSHOT_BYTES:
            return discord.File(file_path)
        log.info("Screenshot not sent (missing or too large): %s", file_path)
    except OSError:
        log.warning("Could not read screenshot %s", file_path, exc_info=True)
    return None


async def _send_screenshot(message: discord.Message, path: str | None) -> None:
    file = _screenshot_file(path)
    if file is None:
        return
    try:
        await message.reply(
            "Debug screenshot of the page:", file=file, mention_author=False
        )
    except discord.HTTPException:
        log.warning("Could not send screenshot %s", path, exc_info=True)


def _reserve_run() -> bool:
    global RUN_ACTIVE
    if RUN_ACTIVE:
        return False
    # Asyncio is single-threaded, so nothing can pass the check between these
    # two lines. Reserving before the next await prevents double browsers.
    RUN_ACTIVE = True
    return True


def _release_run() -> None:
    global RUN_ACTIVE
    RUN_ACTIVE = False


async def _free_run_slot() -> bool:
    """Cancel an in-flight start so /mcstop can take over the browser slot."""
    if not RUN_ACTIVE:
        return True
    task = ACTIVE_TASK
    if ACTIVE_KIND == "start" and task is not None and not task.done():
        task.cancel()
        await asyncio.wait({task})
        return not RUN_ACTIVE
    return False


def _spawn_drive(view: RunView, status_message: discord.Message, action: str) -> None:
    global ACTIVE_TASK, ACTIVE_VIEW, ACTIVE_KIND, STATUS_CACHE
    ACTIVE_VIEW = view
    ACTIVE_KIND = action
    STATUS_CACHE = None
    task = asyncio.create_task(_drive(view, status_message, action))
    ACTIVE_TASK = task
    task.add_done_callback(_log_task_result)


def _log_task_result(task: asyncio.Task) -> None:
    if task.cancelled():
        log.warning("Aternos task was cancelled")
        return
    error = task.exception()
    if error is not None:
        log.error("Aternos task crashed", exc_info=error)


# ----------------------------------------------------------------------- status


async def _read_status() -> tuple[discord.Embed | None, str | None, str | None]:
    """Return (embed, error, screenshot). Uses the cache before a browser."""
    global STATUS_CACHE
    now = time.monotonic()
    if STATUS_CACHE is not None and now - STATUS_CACHE[0] <= STATUS_CACHE_SECONDS:
        age = int(now - STATUS_CACHE[0])
        return status_embed(STATUS_CACHE[1], cached_age=age), None, None

    if not _reserve_run():
        return None, "Another Aternos operation is already running.", None
    try:
        result = await _make_controller().status()
    except Exception as exc:
        log.exception("Status check failed")
        return None, f"Status check failed: {exc}", None
    finally:
        _release_run()

    if not result.ok or result.status is None:
        return None, result.message, result.screenshot

    STATUS_CACHE = (time.monotonic(), result.status)
    return status_embed(result.status), None, None


# --------------------------------------------------------------------- commands


async def trigger(message: discord.Message, source: str) -> None:
    """Handle the keyword and the !mcstart command."""
    if message.guild is None:
        return
    if ALLOWED_CHANNELS and message.channel.id not in ALLOWED_CHANNELS:
        return

    error = _access_error(message.author)
    if error:
        await message.reply(error, mention_author=False)
        return
    if RUN_ACTIVE:
        await message.reply(
            "An Aternos operation is already running — look at the update above.",
            mention_author=False,
        )
        return

    now = time.monotonic()
    remaining = COOLDOWN_SECONDS - (now - COOLDOWNS.get(message.author.id, 0.0))
    if remaining > 0:
        await message.reply(
            f"Cooldown: try again in {int(remaining) + 1}s.", mention_author=False
        )
        return

    if not _reserve_run():
        await message.reply(
            "An Aternos operation is already running.", mention_author=False
        )
        return
    COOLDOWNS[message.author.id] = now

    view = RunView(actor=message.author, action="start")
    view.note(f"Triggered by {source}.")
    try:
        status_message = await message.reply(embed=view.embed(), mention_author=False)
    except discord.HTTPException:
        _release_run()
        raise

    _spawn_drive(view, status_message, "start")


async def _respond(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    file: discord.File | None = None,
    ephemeral: bool = True,
) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                content, embed=embed, file=file, ephemeral=ephemeral
            )
        else:
            await interaction.response.send_message(
                content, embed=embed, file=file, ephemeral=ephemeral
            )
    except discord.HTTPException:
        log.warning("Could not respond to interaction", exc_info=True)


def _help_text() -> str:
    keywords = ", ".join(f"`{keyword}`" for keyword in KEYWORDS) or "(none)"
    return (
        f"**Start:** say {keywords} in chat, or use `/mcstart`.\n"
        "**Status:** `/mcstatus` reads the live state (briefly cached).\n"
        "**Stop:** `/mcstop` stops the server or cancels a pending start "
        "(Manage Server only).\n"
        "Only one browser operation runs at a time."
    )


def _start_checks(member: discord.abc.User) -> tuple[str | None, int]:
    error = _access_error(member)
    if error:
        return error, 0
    if RUN_ACTIVE:
        return (
            "An Aternos operation is already running — look at the update above.",
            0,
        )
    now = time.monotonic()
    remaining = COOLDOWN_SECONDS - (now - COOLDOWNS.get(member.id, 0.0))
    if remaining > 0:
        return f"Cooldown: try again in {int(remaining) + 1}s.", 0
    return None, now


@bot.tree.command(name="mcstart", description="Start the Aternos Minecraft server")
async def slash_mcstart(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await _respond(interaction, "Use this in a server.")
        return

    channel = interaction.channel
    if not isinstance(channel, discord.abc.Messageable):
        await _respond(interaction, "I can't post status updates in this channel.")
        return

    error, now = _start_checks(interaction.user)
    if error:
        await _respond(interaction, error)
        return

    if not _reserve_run():
        await _respond(interaction, "An Aternos operation is already running.")
        return
    COOLDOWNS[interaction.user.id] = now

    view = RunView(actor=interaction.user, action="start")
    view.note("Triggered by `/mcstart`.")
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        status_message = await channel.send(embed=view.embed())
    except discord.HTTPException:
        _release_run()
        await _respond(
            interaction,
            "Could not post the status message — check my Send Messages permission.",
        )
        return

    _spawn_drive(view, status_message, "start")
    try:
        await interaction.edit_original_response(
            content=f"Started — live progress in {channel.mention}."
        )
    except discord.HTTPException:
        pass


@bot.tree.command(name="mcstatus", description="Show the live Aternos server status")
async def slash_mcstatus(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await _respond(interaction, "Use this in a server.")
        return

    if RUN_ACTIVE and ACTIVE_KIND in {"start", "stop"} and ACTIVE_VIEW is not None:
        await interaction.response.send_message(embed=ACTIVE_VIEW.embed())
        return

    await interaction.response.defer(thinking=True)
    embed, error, screenshot = await _read_status()
    if embed is None:
        embed = error_embed("Aternos status failed", error or "Unknown error")
    try:
        await interaction.edit_original_response(embed=embed)
    except discord.HTTPException:
        log.warning("Could not edit status response", exc_info=True)

    if error:
        file = _screenshot_file(screenshot)
        if file is not None:
            await _respond(
                interaction, "Debug screenshot of the page:", file=file, ephemeral=True
            )


@bot.tree.command(
    name="mcstop",
    description="Stop the server, or cancel a start in progress",
)
@discord.app_commands.default_permissions(manage_guild=True)
async def slash_mcstop(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await _respond(interaction, "Use this in a server.")
        return
    if not _is_admin(interaction.user):
        await _respond(interaction, "You need the **Manage Server** permission.")
        return

    channel = interaction.channel
    if not isinstance(channel, discord.abc.Messageable):
        await _respond(interaction, "I can't post status updates in this channel.")
        return

    if not await _free_run_slot():
        await _respond(
            interaction, "Another operation is still running; try again in a moment."
        )
        return
    if not _reserve_run():
        await _respond(interaction, "Another Aternos operation is already running.")
        return

    view = RunView(actor=interaction.user, action="stop")
    view.note("Requested by `/mcstop`.")
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        status_message = await channel.send(embed=view.embed())
    except discord.HTTPException:
        _release_run()
        await _respond(
            interaction,
            "Could not post the status message — check my Send Messages permission.",
        )
        return

    _spawn_drive(view, status_message, "stop")
    try:
        await interaction.edit_original_response(
            content=f"Stopping — live progress in {channel.mention}."
        )
    except discord.HTTPException:
        pass


@bot.tree.command(name="mchelp", description="How to use the Aternos bot")
async def slash_mchelp(interaction: discord.Interaction) -> None:
    await _respond(interaction, _help_text())


# ------------------------------------------------------------------ text commands


@bot.command(name="mcstart", aliases=["startmc"])
@commands.guild_only()
async def mcstart(ctx: commands.Context) -> None:
    """Start the server without typing the keyword."""
    await trigger(ctx.message, "the `!mcstart` command")


@bot.command(name="mcstatus")
@commands.guild_only()
async def mcstatus(ctx: commands.Context) -> None:
    """Show the live server state, players and queue."""
    if RUN_ACTIVE and ACTIVE_KIND in {"start", "stop"} and ACTIVE_VIEW is not None:
        await ctx.reply(embed=ACTIVE_VIEW.embed(), mention_author=False)
        return

    async with ctx.typing():
        embed, error, screenshot = await _read_status()

    if embed is not None:
        await ctx.reply(embed=embed, mention_author=False)
        return

    await ctx.reply(
        embed=error_embed("Aternos status failed", error or "Unknown error"),
        mention_author=False,
    )
    await _send_screenshot(ctx.message, screenshot)


@bot.command(name="mcstop")
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
async def mcstop(ctx: commands.Context) -> None:
    """Stop the server or cancel a pending start (Manage Server only)."""
    if not await _free_run_slot():
        await ctx.reply(
            "Another operation is still running; try again in a moment.",
            mention_author=False,
        )
        return
    if not _reserve_run():
        await ctx.reply(
            "Another Aternos operation is already running.", mention_author=False
        )
        return

    view = RunView(actor=ctx.author, action="stop")
    view.note("Requested by `!mcstop`.")
    try:
        status_message = await ctx.reply(embed=view.embed(), mention_author=False)
    except discord.HTTPException:
        _release_run()
        raise

    _spawn_drive(view, status_message, "stop")


@bot.command(name="mchelp")
async def mchelp(ctx: commands.Context) -> None:
    """List the trigger keywords and commands."""
    await ctx.reply(_help_text(), mention_author=False)


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
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.reply("Use this in a server.", mention_author=False)
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply(
            "You need the **Manage Server** permission for that.",
            mention_author=False,
        )
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.reply("You don't have permission to do that.", mention_author=False)
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply("Missing argument. Try `!mchelp`.", mention_author=False)
        return

    log.error("Command %s failed", ctx.command, exc_info=error)
    await _safe_reply(ctx.message, f"Command failed: {error}")


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
    log.info("Trigger keywords: %s", ", ".join(KEYWORDS) or "(none)")
    if not SESSION_COOKIE and not STATE_PATH.exists():
        log.warning(
            "No Aternos session configured. Set ATERNOS_SESSION or run login.py."
        )

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            scope = f"guild {GUILD_ID}"
        else:
            synced = await bot.tree.sync()
            scope = "globally (Discord may take up to an hour to show them)"
        log.info("Synced %d slash commands %s", len(synced), scope)
    except discord.HTTPException:
        log.warning("Could not sync slash commands", exc_info=True)

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
