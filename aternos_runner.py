"""Drive the Aternos website with a real Chromium browser.

Aternos has no public API. This module does what a human does in the browser:
open the server page, start/stop the server, confirm the queue spot when asked,
accept the Mojang EULA if it appears, and read the live server status.

The UI ids used here are the ones the current Aternos frontend ships:
    #start, #stop, #cancel, #confirm, #status
If Aternos redesigns the page, the failure screenshot is the quickest way to
see what changed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError

log = logging.getLogger("aternos")

SERVER_URL = "https://aternos.org/server/"

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]

ADDRESS_RE = re.compile(r"\b([a-z0-9][a-z0-9-]{1,62}\.aternos\.me)\b", re.IGNORECASE)
EULA_TEXT_RE = re.compile(r"eula|end[- ]?user[- ]?licen[cs]e", re.IGNORECASE)
ACCEPT_TEXT_RE = re.compile(r"accept|agree|continue|got it|^ok$", re.IGNORECASE)
STOP_CONFIRM_TEXT_RE = re.compile(r"stop|shut ?down|yes|confirm|ok", re.IGNORECASE)

NUMERIC_STATES = {
    0: "offline",
    1: "online",
    2: "starting",
    3: "stopping",
    6: "loading",
    7: "error",
    10: "preparing",
}

EventCallback = Callable[[str], None]


def chrome_user_agent(browser: Browser) -> str:
    """Normal Chrome UA, without the "HeadlessChrome" token.

    Kept in one place so login.py and the bot present the same UA — that makes
    Cloudflare clearance cookies reusable between them on the same IP.
    """
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{browser.version} Safari/537.36"
    )


@dataclass
class ServerStatus:
    state: str
    address: str | None = None
    players: list[str] = field(default_factory=list)
    queue_position: int | None = None
    queue_count: int | None = None
    queue_eta: str | None = None
    raw_text: str = ""


@dataclass
class ActionResult:
    ok: bool
    message: str
    address: str | None = None
    screenshot: str | None = None
    status: ServerStatus | None = None


class AternosError(RuntimeError):
    """Aternos could not be driven (bad session, Cloudflare, changed UI)."""


class AternosController:
    def __init__(
        self,
        state_path: str | Path = "state/aternos_state.json",
        session_cookie: str | None = None,
        sid_cookie: str | None = None,
        server_id: str | None = None,
        headless: bool = True,
        timeout: int = 2400,
        poll_interval: float = 7.0,
        screenshot_dir: str | Path = "state",
        timezone: str = "Asia/Kolkata",
        proxy_server: str | None = None,
        proxy_username: str | None = None,
        proxy_password: str | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.session_cookie = (session_cookie or "").strip() or None
        self.sid_cookie = (sid_cookie or "").strip() or None
        self.server_id = (server_id or "").strip() or None
        self.headless = headless
        self.timeout = max(60, int(timeout))
        self.poll_interval = max(2.0, float(poll_interval))
        self.screenshot_dir = Path(screenshot_dir)
        self.timezone = timezone
        self.proxy_server = (proxy_server or "").strip() or None
        self.proxy_username = (proxy_username or "").strip() or None
        self.proxy_password = proxy_password or None
        self.on_event = on_event

    # ------------------------------------------------------------------ public

    async def run(self) -> ActionResult:
        """Open the server page and keep at it until online, timeout, or error."""

        async def action(page: Page) -> ActionResult:
            await self._accept_eula(page)
            return await self._wait_until_online(page)

        return await self._perform(action)

    async def stop(self) -> ActionResult:
        """Cancel a pending start, or stop a running server."""

        async def action(page: Page) -> ActionResult:
            return await self._stop_action(page)

        return await self._perform(action)

    async def status(self) -> ActionResult:
        """Read the current server state, players and queue information."""

        async def action(page: Page) -> ActionResult:
            current = await self._server_status(page, include_address=True)
            return ActionResult(
                True,
                current.raw_text or current.state,
                address=current.address,
                status=current,
            )

        return await self._perform(action)

    # --------------------------------------------------------------- lifecycle

    async def _perform(
        self, action: Callable[[Page], Awaitable[ActionResult]]
    ) -> ActionResult:
        if not self.session_cookie and not self.state_path.exists():
            return ActionResult(
                False,
                "No Aternos session. Set ATERNOS_SESSION (or run login.py to "
                f"create {self.state_path}).",
            )

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

        page: Page | None = None
        context: BrowserContext | None = None

        async with async_playwright() as pw:
            browser: Browser | None = None
            try:
                browser = await self._launch(pw)
                context = await self._new_context(browser)
                page = await context.new_page()
                page.set_default_timeout(15_000)

                self._emit("Opening Aternos…")
                await page.goto(
                    SERVER_URL, wait_until="domcontentloaded", timeout=60_000
                )
                await self._wait_for_cloudflare(page)

                if await self._is_login_page(page):
                    raise AternosError(
                        "Aternos rejected the session (login page shown). "
                        "Grab a fresh ATERNOS_SESSION cookie and update the config."
                    )

                self._emit("Logged in.")
                return await action(page)

            except AternosError as exc:
                self._emit(str(exc))
                return ActionResult(
                    False, str(exc), screenshot=await self._screenshot(page, "failed")
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Unexpected automation error")
                return ActionResult(
                    False,
                    f"Automation error: {exc}",
                    screenshot=await self._screenshot(page, "error"),
                )
            finally:
                if context is not None:
                    try:
                        await context.storage_state(path=str(self.state_path))
                    except Exception:
                        log.warning("Could not persist browser state", exc_info=True)
                if browser is not None:
                    await browser.close()

    # ----------------------------------------------------------------- context

    async def _launch(self, pw) -> Browser:
        """Prefer full Chromium (new headless); fall back to the headless shell."""
        try:
            return await pw.chromium.launch(
                headless=self.headless, channel="chromium", args=CHROMIUM_ARGS
            )
        except PlaywrightError as exc:
            message = str(exc).casefold()
            if "distribution" not in message and "channel" not in message:
                raise
            self._emit("Full Chromium channel unavailable; using headless shell.")
            return await pw.chromium.launch(headless=self.headless, args=CHROMIUM_ARGS)

    async def _new_context(self, browser: Browser) -> BrowserContext:
        kwargs: dict = {
            "viewport": {"width": 1366, "height": 900},
            "user_agent": chrome_user_agent(browser),
            "locale": "en-US",
            "timezone_id": self.timezone,
            "color_scheme": "light",
        }
        if self.state_path.exists():
            kwargs["storage_state"] = str(self.state_path)
        if self.proxy_server:
            proxy: dict = {"server": self.proxy_server}
            if self.proxy_username:
                proxy["username"] = self.proxy_username
            if self.proxy_password:
                proxy["password"] = self.proxy_password
            kwargs["proxy"] = proxy

        context = await browser.new_context(**kwargs)

        cookies = [
            {
                "name": "ATERNOS_LANGUAGE",
                "value": "en",
                "domain": ".aternos.org",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        ]
        if self.session_cookie:
            cookies.append(
                {
                    "name": "ATERNOS_SESSION",
                    "value": self.session_cookie,
                    "domain": ".aternos.org",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
        if self.sid_cookie:
            cookies.append(
                {
                    "name": "ATERNOS_SID",
                    "value": self.sid_cookie,
                    "domain": ".aternos.org",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
        if self.server_id:
            cookies.append(
                {
                    "name": "ATERNOS_SERVER",
                    "value": self.server_id,
                    "domain": ".aternos.org",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
        await context.add_cookies(cookies)
        return context

    # -------------------------------------------------------------------- page

    async def _wait_for_cloudflare(self, page: Page, seconds: int = 90) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            title = (await page.title()).casefold()
            if (
                "access denied" in title
                or "error 1020" in title
                or "you have been blocked" in title
            ):
                raise AternosError(
                    "Cloudflare is blocking this host outright (403). A normal "
                    "browser cannot pass this; use a residential IP or proxy."
                )
            stuck = (
                "just a moment" in title
                or "attention required" in title
                or await self._visible(page, "#challenge-form")
                or await self._visible(page, "#challenge-running")
            )
            if not stuck:
                return
            self._emit("Waiting for Cloudflare…")
            await asyncio.sleep(4)
        raise AternosError(
            "Cloudflare challenge did not pass. Datacenter IPs are often blocked — "
            "run this on a home connection/VPS with a residential IP, or set "
            "HEADLESS=false and solve it once by hand."
        )

    async def _is_login_page(self, page: Page) -> bool:
        if "/go" in page.url or "/login" in page.url:
            return True
        return await self._visible(page, "#user") and await self._visible(
            page, "#password"
        )

    async def _wait_until_online(self, page: Page) -> ActionResult:
        deadline = time.monotonic() + self.timeout
        last_status = ""
        last_start_click = 0.0
        start_clicks = 0
        last_confirm = 0.0
        last_eula_check = 0.0

        while time.monotonic() < deadline:
            now = time.monotonic()
            await self._close_popups(page)

            if now - last_eula_check > 15:
                last_eula_check = now
                if await self._accept_eula(page):
                    await asyncio.sleep(self.poll_interval)
                    continue

            current = await self._server_status(page)
            if current.raw_text and current.raw_text != last_status:
                last_status = current.raw_text
                self._emit(f"Aternos: {current.raw_text}")
                if current.queue_position:
                    eta = f", about {current.queue_eta}" if current.queue_eta else ""
                    count = f" of {current.queue_count}" if current.queue_count else ""
                    self._emit(f"Queue position {current.queue_position}{count}{eta}.")

            if current.state == "online":
                full = await self._server_status(page, include_address=True)
                return ActionResult(
                    True, "Server is online.", address=full.address, status=full
                )

            confirm_visible = await self._clickable(page, "#confirm")
            if confirm_visible and now - last_confirm > 15:
                await self._click(page, "#confirm")
                last_confirm = now
                self._emit("Confirmed the queue spot.")

            # Aternos only renders #start while the server is off; if it is
            # still there a few seconds later the click did not take.
            if (
                not confirm_visible
                and start_clicks < 5
                and now - last_start_click > 12
                and current.state
                not in {"starting", "loading", "preparing", "online", "stopping"}
                and await self._clickable(page, "#start")
            ):
                await self._click(page, "#start")
                last_start_click = now
                start_clicks += 1
                self._emit("Clicked Start.")

            await asyncio.sleep(self.poll_interval)

        raise AternosError(
            f"Timed out after {self.timeout}s waiting for the server. "
            f"Last status: {last_status or 'unknown'}."
        )

    async def _stop_action(self, page: Page) -> ActionResult:
        current = await self._server_status(page, include_address=True)
        self._emit(f"Aternos: {current.raw_text or current.state}")

        if current.state == "offline":
            return ActionResult(True, "Server is already offline.", status=current)

        if current.state in {"queue", "starting", "loading", "preparing"}:
            for selector in (
                "#cancel",
                "button:has-text('Cancel')",
                "a:has-text('Cancel')",
            ):
                if await self._clickable(page, selector):
                    await self._click(page, selector)
                    self._emit("Cancelled the pending start.")
                    return ActionResult(
                        True, "Cancelled the pending start.", status=current
                    )

        stop_selector = None
        for selector in (
            "#stop",
            "button:has-text('Stop')",
            "a:has-text('Stop')",
            "[data-action='stop']",
        ):
            if await self._clickable(page, selector):
                stop_selector = selector
                break

        if stop_selector is None:
            raise AternosError(
                "Could not find the Stop button; the Aternos UI may have changed."
            )

        await self._click(page, stop_selector)
        self._emit("Clicked Stop. Waiting for the server to shut down…")
        await asyncio.sleep(1.5)
        await self._confirm_stop_dialog(page)

        deadline = time.monotonic() + min(self.timeout, 300)
        last = ""
        while time.monotonic() < deadline:
            current = await self._server_status(page)
            if current.raw_text and current.raw_text != last:
                last = current.raw_text
                self._emit(f"Aternos: {last}")
            if current.state == "offline":
                return ActionResult(True, "Server is offline.", status=current)
            await asyncio.sleep(self.poll_interval)

        raise AternosError("Timed out waiting for the server to stop.")

    async def _confirm_stop_dialog(self, page: Page) -> bool:
        """Click through a 'do you really want to stop?' dialog, if shown."""
        dialogs = page.locator(".modal, .dialog, [role='dialog']")
        try:
            count = await dialogs.count()
        except PlaywrightError:
            return False

        for index in range(min(count, 5)):
            box = dialogs.nth(index)
            try:
                if not await box.is_visible():
                    continue
                text = (await box.inner_text()).casefold()
                if not any(
                    word in text for word in ("stop", "shut down", "disconnect")
                ):
                    continue
                button = (
                    box.locator("button, a").filter(has_text=STOP_CONFIRM_TEXT_RE).first
                )
                if await button.count() and await button.is_visible():
                    await button.click(timeout=5_000)
                    self._emit("Confirmed the stop dialog.")
                    return True
            except PlaywrightError:
                continue
        return False

    # ------------------------------------------------------------------- state

    async def _server_status(
        self, page: Page, *, include_address: bool = False
    ) -> ServerStatus:
        text, live = await self._snapshot(page)
        lowered = text.casefold()
        queue = live.get("queue") or {}
        position = queue.get("position")

        state = NUMERIC_STATES.get(live.get("status"), "")
        if not state:
            state = str(live.get("cls") or live.get("lang") or "").casefold()

        if (
            "queue" in lowered
            or "confirm" in lowered
            or (isinstance(position, int) and position > 0)
        ):
            state = "queue"
        elif "online" in lowered:
            state = "online"
        elif "offline" in lowered:
            state = "offline"
        elif "stopping" in lowered:
            state = "stopping"
        elif "starting" in lowered:
            state = "starting"
        elif "loading" in lowered or "preparing" in lowered:
            state = "loading"
        elif state not in NUMERIC_STATES.values():
            state = "unknown"

        raw_players = live.get("playerlist") or []
        players = [str(player) for player in raw_players if str(player).strip()]

        address = live.get("ip") or None
        if include_address and not address:
            address = await self._read_address(page)

        return ServerStatus(
            state=state,
            address=address,
            players=players,
            queue_position=position
            if isinstance(position, int) and position > 0
            else None,
            queue_count=queue.get("count")
            if isinstance(queue.get("count"), int)
            else None,
            queue_eta=queue.get("time") or None,
            raw_text=text,
        )

    async def _close_popups(self, page: Page) -> None:
        """Close extra tabs (Aternos ads sometimes open one on Start)."""
        for extra in page.context.pages[1:]:
            if extra is page:
                continue
            try:
                await extra.close()
                self._emit("Closed an extra browser tab.")
            except PlaywrightError:
                continue

    async def _snapshot(self, page: Page) -> tuple[str, dict]:
        """Return (visible status text, live `lastStatus` object if present).

        Aternos embeds `var lastStatus = {...}` and its frontend keeps that
        object around; `class` is one of offline/loading/preparing/online and
        `ip`/`playerlist`/`queue` come from the same payload. Reading it is far
        less brittle than scraping labels, and the label stays as a fallback.
        """
        text = await self._status_text(page)
        live: dict = {}
        try:
            live = (
                await page.evaluate(
                    """() => {
                        const s = window.lastStatus;
                        if (!s) return {};
                        return {
                            status: s.status, cls: s.class, lang: s.lang,
                            ip: s.ip, port: s.port, playerlist: s.playerlist,
                            queue: s.queue ? {
                                position: s.queue.position,
                                count: s.queue.count,
                                time: s.queue.time,
                                minutes: s.queue.minutes,
                                status: s.queue.status
                            } : null
                        };
                    }"""
                )
                or {}
            )
        except PlaywrightError:
            live = {}
        return text, live

    async def _status_text(self, page: Page) -> str:
        for selector in ("#status", "#server-status", ".status"):
            try:
                locator = page.locator(selector).first
                if await locator.count():
                    text = (await locator.inner_text()).strip()
                    if text:
                        return " ".join(text.split())
            except PlaywrightError:
                continue
        return ""

    async def _read_address(self, page: Page) -> str | None:
        for selector in ("#server-address", "#address", ".server-address", "#ip"):
            try:
                locator = page.locator(selector).first
                if await locator.count():
                    match = ADDRESS_RE.search(await locator.inner_text())
                    if match:
                        return match.group(1)
            except PlaywrightError:
                continue
        try:
            match = ADDRESS_RE.search(await page.locator("body").inner_text())
            return match.group(1) if match else None
        except PlaywrightError:
            return None

    async def _accept_eula(self, page: Page) -> bool:
        """Click through a visible EULA prompt, if one is on screen."""
        containers = page.locator(".modal, .dialog, #eula, .alert, [role='dialog']")
        try:
            count = await containers.count()
        except PlaywrightError:
            return False

        for index in range(min(count, 5)):
            box = containers.nth(index)
            try:
                if not await box.is_visible():
                    continue
                text = await box.inner_text()
                if not EULA_TEXT_RE.search(text):
                    continue
                button = box.locator("button, a").filter(has_text=ACCEPT_TEXT_RE).first
                if await button.count() and await button.is_visible():
                    await button.click(timeout=5_000)
                    self._emit("Accepted the Mojang EULA.")
                    return True
            except PlaywrightError:
                continue

        for selector in ("#accept-eula", "#eula-accept", "a[href*='accept-eula']"):
            if await self._clickable(page, selector):
                await self._click(page, selector)
                self._emit("Accepted the Mojang EULA.")
                return True
        return False

    # ----------------------------------------------------------------- helpers

    async def _visible(self, page: Page, selector: str) -> bool:
        try:
            locator = page.locator(selector).first
            return bool(await locator.count()) and await locator.is_visible()
        except PlaywrightError:
            return False

    async def _clickable(self, page: Page, selector: str) -> bool:
        try:
            locator = page.locator(selector).first
            if not await locator.count():
                return False
            return await locator.is_visible() and await locator.is_enabled()
        except PlaywrightError:
            return False

    async def _click(self, page: Page, selector: str) -> None:
        await page.locator(selector).first.click(timeout=5_000)

    async def _screenshot(self, page: Page | None, tag: str) -> str | None:
        if page is None:
            return None
        path = self.screenshot_dir / f"aternos-{tag}-{int(time.time())}.png"
        try:
            await page.screenshot(path=str(path), full_page=True)
            return str(path)
        except PlaywrightError:
            log.warning("Could not take a screenshot", exc_info=True)
            return None

    def _emit(self, message: str) -> None:
        log.info("%s", message)
        if self.on_event is not None:
            try:
                self.on_event(message)
            except Exception:
                log.warning("Event callback failed", exc_info=True)


def controller_from_env() -> AternosController:
    return AternosController(
        state_path=os.getenv("ATERNOS_STATE", "state/aternos_state.json"),
        session_cookie=os.getenv("ATERNOS_SESSION"),
        sid_cookie=os.getenv("ATERNOS_SID"),
        server_id=os.getenv("ATERNOS_SERVER_ID"),
        headless=os.getenv("HEADLESS", "true").strip().lower()
        not in {"0", "false", "no", "off"},
        timeout=int(os.getenv("START_TIMEOUT_SECONDS", "2400")),
        poll_interval=float(os.getenv("POLL_INTERVAL_SECONDS", "7")),
        timezone=os.getenv("TZ_NAME", "Asia/Kolkata"),
        proxy_server=os.getenv("PROXY_SERVER"),
        proxy_username=os.getenv("PROXY_USERNAME"),
        proxy_password=os.getenv("PROXY_PASSWORD"),
    )


async def _cli(action: str) -> int:
    controller = controller_from_env()
    if action == "start":
        result = await controller.run()
    elif action == "stop":
        result = await controller.stop()
    elif action == "status":
        result = await controller.status()
    else:
        raise SystemExit(f"Unknown action {action!r}; use start, stop or status.")
    print(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    chosen = sys.argv[1] if len(sys.argv) > 1 else "start"
    raise SystemExit(asyncio.run(_cli(chosen)))
