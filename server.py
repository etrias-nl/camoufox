"""
Camoufox Stealth Browser Sidecar

A FastAPI microservice that wraps Camoufox (anti-detect Firefox browser) and exposes
a simple HTTP API for PHP crawlers. All human-like behavior (mouse movement, typing,
scrolling, delays) is handled automatically by this sidecar.
"""

import asyncio
import base64
import logging
import os
import random
import re
import string
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from camoufox.async_api import AsyncCamoufox
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("camoufox-sidecar")

# ---------------------------------------------------------------------------
# Session storage
# ---------------------------------------------------------------------------
sessions: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROXY_URL = os.getenv("TWO_CAPTCHA_PROXY_URL", "")
DEBUG_MODE = os.getenv("CAMOUFOX_DEBUG", "0") == "1"

# NL-residential-proxy default. Camoufox's handle_locales splits on comma
# and uses the first entry as the primary navigator.language; the Accept-
# Language header is generated downstream from the full list.
DEFAULT_LOCALE = "nl-NL, nl, en"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class CreateSessionRequest(BaseModel):
    proxy_url: str | None = None
    behavior: str = "cautious"  # "cautious" or "fast"
    viewport_width: int = 1200
    viewport_height: int = 1100
    locale: str | None = None
    google_warmup: bool = True
    google_query: str | None = None


class NavigateRequest(BaseModel):
    url: str
    page_arrival: bool = True


class ScriptRequest(BaseModel):
    script: str


class WaitRequest(BaseModel):
    selector: str
    timeout: int = 10


class ClickRequest(BaseModel):
    selector: str


class TypeRequest(BaseModel):
    selector: str
    text: str


class KeyRequest(BaseModel):
    key: str


# ---------------------------------------------------------------------------
# Human behavior helpers
# ---------------------------------------------------------------------------
# Mouse movement, click humanization, and typing cadence are handled natively
# by Camoufox (humanize=True). We only add light page-level delays here.
# ---------------------------------------------------------------------------

async def human_delay(mean: float = 2.0, std: float = 1.0):
    delay = max(0.3, min(random.gauss(mean, std), mean * 3))
    await asyncio.sleep(delay)


async def _humanized_type_text(page, behavior: str, text: str) -> None:
    """Per-character typing cadence. Used by /type and the Google warmup
    so both look identical on the wire."""
    for char in text:
        await page.keyboard.press(char)
        if behavior == "fast":
            await asyncio.sleep(random.uniform(0.01, 0.05))
        else:
            await asyncio.sleep(random.uniform(0.05, 0.20))
            # 3% chance of longer pause (as if thinking)
            if random.random() < 0.03:
                await asyncio.sleep(random.uniform(0.3, 0.8))


# Two-part public suffixes we hit in practice. tldextract would be
# comprehensive but we don't need the extra dep for portal hostnames.
_TWO_PART_TLDS = frozenset({
    "co.uk", "co.nz", "com.au", "co.jp", "co.za", "com.br",
    "com.mx", "com.sg", "com.hk", "com.tr",
})


def _hostname_sld(url: str) -> str:
    """Extract the second-level label of a URL's hostname, e.g.
    https://partners.timberland.com/x -> 'timberland',
    https://foo.co.uk/ -> 'foo'. Falls back to 'site' if no hostname."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "site"
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in _TWO_PART_TLDS:
        return labels[-3]
    if len(labels) >= 2:
        return labels[-2]
    return labels[0]


async def _human_scroll_by(page, delta_y: int) -> None:
    """Scroll the page by delta_y pixels using real mouse wheel events in
    several small chunks with small random delays. Fires wheel/scroll
    handlers the same way a real trackpad or wheel would, instead of the
    instant window.scrollBy jump."""
    if delta_y == 0:
        return
    direction = 1 if delta_y > 0 else -1
    remaining = abs(delta_y)
    while remaining > 0:
        step = min(remaining, random.randint(60, 140))
        await page.mouse.wheel(0, step * direction)
        remaining -= step
        if remaining > 0:
            await asyncio.sleep(random.uniform(0.04, 0.12))


async def _human_scroll_to_locator(page, locator, target_ratio: float = 0.4) -> None:
    """Scroll via mouse wheel until the locator sits near target_ratio of
    the viewport height (0.0=top, 0.5=center, 1.0=bottom). No-op if
    already in view. Falls back to Playwright's scroll_into_view_if_needed
    if wheel scrolling stalls (e.g. element inside a nested scroll
    container where page.mouse.wheel wouldn't help)."""
    prev_y: float | None = None
    for _ in range(15):
        try:
            box = await locator.bounding_box()
        except Exception:
            break
        if not box:
            break
        viewport = page.viewport_size or {"width": 1200, "height": 900}
        vh = viewport["height"]
        elem_center = box["y"] + box["height"] / 2
        target_y = vh * target_ratio
        delta = elem_center - target_y
        if abs(delta) < 40:
            return
        if prev_y is not None and abs(box["y"] - prev_y) < 1:
            break  # stuck — nested scroll container or page boundary
        prev_y = box["y"]
        step = int(max(-280, min(280, delta)))
        await page.mouse.wheel(0, step)
        await asyncio.sleep(random.uniform(0.05, 0.14))

    # Fallback for nested scroll containers or edge cases.
    try:
        await locator.scroll_into_view_if_needed()
    except Exception:
        pass


async def simulate_page_arrival(page, behavior: str):
    """Simulate a user settling onto a freshly loaded page: small glance
    with the mouse and a short scroll. Camoufox's humanize=True already
    curves the mouse path from the current cursor position to each
    destination, so we just pick the destinations."""
    if behavior == "fast":
        await asyncio.sleep(random.uniform(0.3, 0.8))
        return

    await human_delay(1.0, 0.5)

    viewport = page.viewport_size or {"width": 1200, "height": 1100}
    max_x = max(101, viewport["width"] - 100)
    max_y = max(101, viewport["height"] - 100)
    for _ in range(random.randint(1, 3)):
        await page.mouse.move(random.randint(100, max_x), random.randint(100, max_y))
        await asyncio.sleep(random.uniform(0.2, 0.7))

    # 70% chance: scroll a bit to look natural
    if random.random() < 0.7:
        await _human_scroll_by(page, random.randint(100, 300))
        await human_delay(0.8, 0.4)


async def google_search_warmup(
    session: dict[str, Any],
    target_url: str,
    session_id: str,
) -> bool:
    """Build a real search history on google.com before the first target
    navigation. Goto google.com, (optionally) clear the consent
    interstitial, type a derived query, submit, and leave the browser
    on the SERP — the subsequent page.goto(target_url) then sets Referer
    from the SERP URL naturally, and the session has real Google cookies
    / TLS history.

    We deliberately do not click a SERP anchor: brand queries often
    surface the wrong page and the anchor selector is fragile. The
    caller's next navigate does the real load.

    Returns True if we reached the SERP, False otherwise."""
    page = session["page"]
    query = session["google_query"] or f"{_hostname_sld(target_url)} portal"
    logger.info(f"Session {session_id}: warmup:start query={query!r}")

    step = "goto_google"
    try:
        try:
            await page.goto(
                "https://www.google.com/",
                wait_until="load",
                timeout=30000,
            )
        except PlaywrightTimeoutError as exc:
            logger.warning(
                f"Session {session_id}: warmup:google_load_timeout ({exc})"
            )

        # Google doesn't fingerprint mouse trajectories or per-character
        # typing cadence the way the portal anti-bot does — its defenses
        # are IP/TLS/UA-based, which residential proxy + Camoufox already
        # cover. Skip the humanize ritual here to shave seconds off every
        # session startup.

        step = "consent"
        # Consent interstitial ("Alles accepteren" / "Accept all"). Short
        # wait — residential IPs with prior google.com cookies usually
        # skip it entirely.
        try:
            consent = page.locator("#L2AGLb")
            await consent.wait_for(state="visible", timeout=3000)
            await consent.click()
            logger.info(f"Session {session_id}: warmup:consent_clicked")
        except Exception:
            logger.info(f"Session {session_id}: warmup:consent_skipped (not shown)")

        step = "search_box_wait"
        search_box = page.locator('textarea[name="q"], input[name="q"]').first
        await search_box.wait_for(state="visible", timeout=8000)

        step = "search_box_type"
        # Use keyboard.type rather than fill: fill just sets
        # element.value via JS, skipping keydown/keypress events.
        # Google's form handler reads those events to track query
        # state — without them, Enter becomes a no-op and we stay
        # on the home page.
        await search_box.click()
        await page.keyboard.type(query, delay=0)

        step = "submit_enter"
        await page.keyboard.press("Enter")
        logger.info(f"Session {session_id}: warmup:query_submitted")

        step = "serp_nav"
        # wait_for_url actually proves the SERP loaded; wait_for_load_state
        # would return immediately if we never navigated off the homepage.
        try:
            await page.wait_for_url(
                lambda u: "/search" in u, timeout=30000
            )
        except PlaywrightTimeoutError as exc:
            logger.warning(
                f"Session {session_id}: warmup:no_serp_nav "
                f"url={page.url!r} ({exc})"
            )
            return False

        logger.info(
            f"Session {session_id}: warmup:serp_loaded url={page.url!r}"
        )
        await _log_state_snapshot(page, session_id, "post_warmup")
        return True
    except Exception as exc:
        logger.warning(
            f"Session {session_id}: warmup:fallthrough step={step} "
            f"url={page.url!r} reason={exc!r}"
        )
        return False


async def _wait_for_load_best_effort(page, session_id: str, action: str) -> None:
    """Wait for the page to reach the `load` state before interacting with
    the DOM, so anti-bot bootstraps that mutate the target element have
    had a chance to finish. If the page never reaches `load` (long-poll
    beacons, challenge iframes), log and proceed — the caller explicitly
    asked us to act on this element."""
    try:
        await page.wait_for_load_state("load", timeout=30000)
    except PlaywrightTimeoutError as exc:
        logger.warning(
            f"Session {session_id}: {action} waited on load but didn't reach "
            f"it within 30s, proceeding anyway ({exc})"
        )


# ---------------------------------------------------------------------------
# Debug logging
# ---------------------------------------------------------------------------
# When CAMOUFOX_DEBUG=1, log a one-line state snapshot after key events
# (post-warmup, post-first-navigate) so we can verify locale, referrer,
# cookies, etc. from `docker compose logs` without new HTTP endpoints.

async def _log_state_snapshot(page, session_id: str, label: str) -> None:
    if not DEBUG_MODE:
        return
    try:
        state = await page.evaluate(
            """() => ({
                url: document.URL,
                referrer: document.referrer,
                language: navigator.language,
                languages: Array.from(navigator.languages || []),
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            })"""
        )
    except Exception as exc:
        logger.info(f"Session {session_id}: {label} state_snapshot_failed ({exc})")
        return

    try:
        cookies = await page.context.cookies()
        domain_counts: dict[str, int] = {}
        for c in cookies:
            domain_counts[c.get("domain") or "?"] = (
                domain_counts.get(c.get("domain") or "?", 0) + 1
            )
    except Exception:
        domain_counts = {}

    logger.info(
        f"Session {session_id}: {label} url={state['url']!r} "
        f"referrer={state['referrer']!r} lang={state['language']!r} "
        f"langs={state['languages']} tz={state['timezone']!r} "
        f"cookies={domain_counts}"
    )


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    logger.info("Camoufox sidecar starting up")
    yield
    # Cleanup all sessions on shutdown
    for session_id in list(sessions.keys()):
        try:
            session = sessions[session_id]
            await session["page"].context.close()
            await session["browser"].__aexit__(None, None, None)
        except Exception:
            pass
    sessions.clear()
    logger.info("Camoufox sidecar shut down")


app = FastAPI(title="Camoufox Stealth Browser Sidecar", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Optional per-op trace logging (STEALTH_TRACE=1)
# ---------------------------------------------------------------------------
_SESSION_OP_RE = re.compile(r"^/session/([^/]+)/(.+)$")
_STEALTH_TRACE = os.getenv("STEALTH_TRACE") == "1"
if _STEALTH_TRACE:
    logger.setLevel(logging.DEBUG)


@app.middleware("http")
async def log_session_ops(request, call_next):
    if not _STEALTH_TRACE:
        return await call_next(request)
    match = _SESSION_OP_RE.match(request.url.path)
    if not match:
        return await call_next(request)
    session_id, op = match.group(1), match.group(2)
    start = time.monotonic()
    logger.debug(f"Session {session_id}: {op}:start")
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.exception(f"Session {session_id}: {op}:fail ({elapsed_ms}ms)")
        raise
    elapsed_ms = int((time.monotonic() - start) * 1000)
    if response.status_code < 400:
        logger.debug(f"Session {session_id}: {op}:ok status={response.status_code} ({elapsed_ms}ms)")
    else:
        logger.warning(f"Session {session_id}: {op}:fail status={response.status_code} ({elapsed_ms}ms)")
    return response


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "active_sessions": len(sessions)}


def _build_proxy_config(raw_proxy_url: str) -> tuple[dict, str]:
    """Randomize the proxy session token and return (playwright_config,
    redacted_server_str_for_logging)."""
    random_session = ''.join(random.choices(string.ascii_letters + string.digits, k=9))
    rotated = re.sub(
        r'(session-)[A-Za-z0-9]+([-:])', rf'\g<1>{random_session}\2', raw_proxy_url
    )
    parsed = urlparse(rotated)
    config: dict[str, Any] = {
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
    }
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config, f"{config['server']} (session=...{random_session[-4:]})"


MAX_PROXY_ATTEMPTS = 3


@app.post("/session")
async def create_session(req: CreateSessionRequest):
    session_id = str(uuid.uuid4())

    raw_proxy_url = req.proxy_url or PROXY_URL or None

    locale = req.locale or DEFAULT_LOCALE
    base_kwargs: dict[str, Any] = {
        "headless": not DEBUG_MODE,
        "humanize": True,
        "locale": locale,
    }

    browser_cm = None
    browser = None

    if raw_proxy_url:
        # Retry proxy-backed session creation up to N times with fresh
        # session tokens. We never fall back to a direct connection —
        # the server's own egress IP (datacenter) is blocked on sight
        # by every portal anti-bot. If all attempts fail, surface 503
        # so the caller can retry / route around.
        last_exc: Exception | None = None
        for attempt in range(1, MAX_PROXY_ATTEMPTS + 1):
            proxy_config, redacted = _build_proxy_config(raw_proxy_url)
            attempt_kwargs = {
                **base_kwargs,
                "proxy": proxy_config,
                "geoip": True,
                # WebRTC can leak the real client IP past the HTTP proxy;
                # block so navigator-reported IP and RTC-reported IP can't
                # diverge.
                "block_webrtc": True,
            }
            logger.info(
                f"Session {session_id}: proxy attempt "
                f"{attempt}/{MAX_PROXY_ATTEMPTS} via {redacted}"
            )
            try:
                browser_cm = AsyncCamoufox(**attempt_kwargs)
                browser = await browser_cm.__aenter__()
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    f"Session {session_id}: proxy attempt {attempt}/"
                    f"{MAX_PROXY_ATTEMPTS} failed "
                    f"({type(exc).__name__}: {exc})"
                )
                if browser_cm is not None:
                    try:
                        await browser_cm.__aexit__(None, None, None)
                    except Exception:
                        pass
                browser_cm = None
                browser = None

        if browser is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Failed to acquire residential proxy after "
                    f"{MAX_PROXY_ATTEMPTS} attempts: "
                    f"{type(last_exc).__name__}: {last_exc}"
                ),
            )
    else:
        # No proxy configured — caller explicitly wants a direct
        # connection. This is only safe for local dev / testing.
        logger.info(f"Session {session_id}: no proxy configured, direct connection")
        browser_cm = AsyncCamoufox(**base_kwargs)
        browser = await browser_cm.__aenter__()

    page = await browser.new_page()
    await page.set_viewport_size(
        {"width": req.viewport_width, "height": req.viewport_height}
    )

    sessions[session_id] = {
        "browser": browser_cm,
        "page": page,
        "behavior": req.behavior,
        "first_navigation": True,
        "locale": locale,
        "google_warmup": req.google_warmup,
        "google_query": req.google_query,
    }

    logger.info(
        f"Session {session_id} created (behavior={req.behavior}, locale={locale}, "
        f"google_warmup={req.google_warmup})"
    )
    return {"session_id": session_id}


def get_session(session_id: str) -> dict[str, Any]:
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    return sessions[session_id]


@app.post("/session/{session_id}/navigate")
async def navigate(session_id: str, req: NavigateRequest):
    session = get_session(session_id)
    page = session["page"]

    goto_kwargs: dict[str, Any] = {"wait_until": "load", "timeout": 60000}
    if session["first_navigation"]:
        target_host = (urlparse(req.url).hostname or "").lower()
        already_google = target_host.endswith("google.com") or target_host.endswith("google.nl")
        if already_google:
            # Caller is pre-navigating to google themselves (intentionally
            # or not). Don't consume first_navigation — let the warmup run
            # when they eventually navigate to a non-google URL. This
            # call becomes a bare goto with whatever Referer logic below
            # derives from page.url, which is about:blank on a cold
            # session so we send no Referer (matches a URL-bar visit).
            logger.info(
                f"Session {session_id}: deferring first_navigation warmup — "
                f"target is google ({target_host})"
            )
        else:
            warmup_ok = False
            if session["google_warmup"]:
                warmup_ok = await google_search_warmup(session, req.url, session_id)
            if not warmup_ok:
                # No real search history — fall back to the cheap fake
                # referrer so the target doesn't see a bare direct load.
                goto_kwargs["referer"] = "https://www.google.com/"
            session["first_navigation"] = False

    # Playwright's page.goto does NOT auto-inherit the current page's URL
    # as Referer — it's address-bar-equivalent. Set it explicitly so the
    # in-session Referer chain matches what was on screen: after a
    # successful warmup this is the google.com SERP URL, and on
    # subsequent navigations it's whatever the previous page was.
    if "referer" not in goto_kwargs:
        current = page.url or ""
        if current and not current.startswith("about:"):
            goto_kwargs["referer"] = current

    logger.info(
        f"Session {session_id}: navigating to {req.url} "
        f"(referer={goto_kwargs.get('referer')!r})"
    )
    try:
        await page.goto(req.url, **goto_kwargs)
    except PlaywrightTimeoutError as exc:
        # Many anti-bot pages never fire `load` because of long-polling
        # beacons — proceed with whatever DOM state we got. The navigation
        # itself has already been kicked off at this point.
        logger.warning(
            f"Session {session_id}: goto {req.url} did not reach load within "
            f"{goto_kwargs['timeout']}ms, continuing ({exc})"
        )
    except PlaywrightError as exc:
        # NS_ERROR_NET_INTERRUPT / NS_ERROR_ABORT: the page started
        # loading, then a second same-frame navigation (JS
        # location.replace, meta-refresh) interrupted ours before load
        # fired. The browser is on the right page, just not the URL we
        # asked for. DNS / connection errors are different classes and
        # should NOT be swallowed — re-raise anything else.
        msg = str(exc)
        if "NS_ERROR_NET_INTERRUPT" in msg or "NS_ERROR_ABORT" in msg:
            logger.warning(
                f"Session {session_id}: goto {req.url} interrupted by "
                f"sub-navigation — waiting for settle ({exc})"
            )
            try:
                await page.wait_for_load_state("load", timeout=30000)
            except PlaywrightTimeoutError:
                pass
            logger.info(
                f"Session {session_id}: settled at {page.url!r}"
            )
        else:
            raise

    if req.page_arrival:
        await simulate_page_arrival(page, session["behavior"])

    await _log_state_snapshot(page, session_id, "post_navigate")

    try:
        html = await page.content()
    except PlaywrightError as exc:
        # Page still in flux (redirect chain not done) — give it one
        # more chance to settle, then try again. If it still fails,
        # surface the error.
        logger.warning(
            f"Session {session_id}: content() failed, retrying after settle ({exc})"
        )
        try:
            await page.wait_for_load_state("load", timeout=15000)
        except PlaywrightTimeoutError:
            pass
        html = await page.content()
    return {"html": html, "url": page.url}


@app.get("/session/{session_id}/html")
async def get_html(session_id: str):
    session = get_session(session_id)
    html = await session["page"].content()
    return {"html": html, "url": session["page"].url}


@app.get("/session/{session_id}/url")
async def get_url(session_id: str):
    session = get_session(session_id)
    return {"url": session["page"].url}


@app.post("/session/{session_id}/script")
async def execute_script(session_id: str, req: ScriptRequest):
    session = get_session(session_id)
    result = await session["page"].evaluate(req.script)
    return {"result": result}


@app.post("/session/{session_id}/wait")
async def wait_for(session_id: str, req: WaitRequest):
    session = get_session(session_id)
    page = session["page"]

    try:
        await page.wait_for_selector(req.selector, timeout=req.timeout * 1000)
    except Exception as e:
        raise HTTPException(
            status_code=408,
            detail=f"Timeout waiting for selector '{req.selector}': {e}",
        )

    return {"found": True}


@app.post("/session/{session_id}/click")
async def click(session_id: str, req: ClickRequest):
    session = get_session(session_id)
    page = session["page"]
    await _wait_for_load_best_effort(page, session_id, "click")
    locator = page.locator(req.selector)
    await _human_scroll_to_locator(page, locator)
    await locator.click()
    return {"clicked": True}


@app.post("/session/{session_id}/type")
async def type_text(session_id: str, req: TypeRequest):
    session = get_session(session_id)
    page = session["page"]
    behavior = session["behavior"]
    await _wait_for_load_best_effort(page, session_id, "type")
    locator = page.locator(req.selector)
    await _human_scroll_to_locator(page, locator)
    await locator.click()

    # Simulate hands moving from mouse back to keyboard
    await asyncio.sleep(random.uniform(0.4, 0.9))
    await _humanized_type_text(page, behavior, req.text)
    return {"typed": True}


@app.post("/session/{session_id}/key")
async def press_key(session_id: str, req: KeyRequest):
    session = get_session(session_id)
    page = session["page"]
    await page.keyboard.press(req.key)
    return {"pressed": True}


@app.post("/session/{session_id}/scroll")
async def scroll(session_id: str):
    session = get_session(session_id)
    page = session["page"]
    viewport = page.viewport_size or {"width": 1200, "height": 900}
    await _human_scroll_by(page, int(viewport["height"] * 0.6))
    return {"scrolled": True}


@app.post("/session/{session_id}/page_arrived")
async def page_arrived(session_id: str):
    session = get_session(session_id)
    await simulate_page_arrival(session["page"], session["behavior"])
    return {"arrived": True}


@app.get("/session/{session_id}/ip")
async def get_ip(session_id: str):
    session = get_session(session_id)
    page = session["page"]
    try:
        ip = await page.evaluate(
            "fetch('https://api.ipify.org').then(r => r.text())"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch IP: {e}")
    return {"ip": ip}


@app.post("/session/{session_id}/screenshot")
async def screenshot(session_id: str):
    session = get_session(session_id)
    screenshot_bytes = await session["page"].screenshot()
    return {"base64": base64.b64encode(screenshot_bytes).decode()}


@app.get("/debug/{session_id}", response_class=HTMLResponse)
async def debug_viewer(session_id: str):
    """Live debug viewer — auto-refreshing screenshot stream for a session."""
    get_session(session_id)  # validate session exists
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Debug — {session_id[:8]}</title>
    <style>
        body {{ margin: 0; background: #1a1a1a; display: flex; flex-direction: column; align-items: center; font-family: monospace; color: #ccc; }}
        #status {{ padding: 8px 16px; font-size: 14px; }}
        #screen {{ max-width: 100vw; max-height: calc(100vh - 40px); }}
        .error {{ color: #f66; }}
    </style>
</head>
<body>
    <div id="status">Connecting...</div>
    <img id="screen" />
    <script>
        const img = document.getElementById('screen');
        const status = document.getElementById('status');
        let frame = 0;
        async function refresh() {{
            try {{
                const res = await fetch('/session/{session_id}/screenshot', {{ method: 'POST' }});
                if (!res.ok) {{
                    status.innerHTML = '<span class="error">Session lost (HTTP ' + res.status + ')</span>';
                    return;
                }}
                const data = await res.json();
                img.src = 'data:image/png;base64,' + data.base64;
                frame++;
                status.textContent = 'Frame ' + frame + ' — ' + new Date().toLocaleTimeString();
            }} catch (e) {{
                status.innerHTML = '<span class="error">Connection error</span>';
            }}
            setTimeout(refresh, 500);
        }}
        refresh();
    </script>
</body>
</html>"""


@app.get("/debug", response_class=HTMLResponse)
async def debug_index():
    """List all active sessions with links to their debug viewers."""
    rows = ""
    for sid in sessions:
        rows += f'<li><a href="/debug/{sid}">{sid}</a></li>'
    if not rows:
        rows = "<li>No active sessions</li>"
    return f"""<!DOCTYPE html>
<html>
<head><title>Camoufox Debug</title>
<style>body {{ font-family: monospace; background: #1a1a1a; color: #ccc; padding: 20px; }} a {{ color: #6cf; }}</style>
</head>
<body><h1>Active Sessions</h1><ul>{rows}</ul></body></html>"""


@app.delete("/session/{session_id}")
async def close_session(session_id: str):
    session = get_session(session_id)
    try:
        await session["page"].context.close()
    except Exception:
        pass
    try:
        await session["browser"].__aexit__(None, None, None)
    except Exception:
        pass
    del sessions[session_id]
    logger.info(f"Session {session_id} closed")
    return {"closed": True}
