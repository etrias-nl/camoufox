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
import uuid
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from camoufox.async_api import AsyncCamoufox
from camoufox.exceptions import InvalidIP
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
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
        await page.evaluate(f"window.scrollBy(0, {random.randint(100, 300)})")
        await human_delay(0.8, 0.4)


async def google_search_warmup(
    session: dict[str, Any],
    target_url: str,
    session_id: str,
) -> bool:
    """Build a real search history on google.nl before the first target
    navigation. Goto google.nl, (optionally) clear the consent
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

    try:
        try:
            await page.goto(
                "https://www.google.nl/",
                wait_until="load",
                timeout=30000,
            )
        except PlaywrightTimeoutError as exc:
            logger.warning(
                f"Session {session_id}: warmup:google_load_timeout ({exc})"
            )

        await simulate_page_arrival(page, session["behavior"])

        # Consent interstitial (Dutch "Alles accepteren"). Short wait —
        # NL residential IPs usually skip it entirely.
        try:
            consent = page.locator("#L2AGLb")
            await consent.wait_for(state="visible", timeout=3000)
            await consent.click()
            logger.info(f"Session {session_id}: warmup:consent_clicked")
            await human_delay(0.6, 0.3)
        except Exception:
            pass

        search_box = page.locator('textarea[name="q"], input[name="q"]').first
        await search_box.wait_for(state="visible", timeout=8000)
        await search_box.scroll_into_view_if_needed()
        await search_box.click()

        # Hands-to-keyboard pause, same as /type
        await asyncio.sleep(random.uniform(0.4, 0.9))
        await _humanized_type_text(page, session["behavior"], query)
        await page.keyboard.press("Enter")
        logger.info(f"Session {session_id}: warmup:query_submitted")

        try:
            await page.wait_for_load_state("load", timeout=30000)
        except PlaywrightTimeoutError as exc:
            logger.warning(
                f"Session {session_id}: warmup:serp_load_timeout ({exc})"
            )
        await human_delay(0.8, 0.4)
        logger.info(
            f"Session {session_id}: warmup:serp_loaded url={page.url!r}"
        )
        await _log_state_snapshot(page, session_id, "post_warmup")
        return True
    except Exception as exc:
        logger.warning(
            f"Session {session_id}: warmup:fallthrough reason={exc!r}"
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
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "active_sessions": len(sessions)}


@app.post("/session")
async def create_session(req: CreateSessionRequest):
    session_id = str(uuid.uuid4())

    proxy_url = req.proxy_url or PROXY_URL or None
    proxy_config = None
    if proxy_url:
        # Randomize the session ID in the proxy username for a fresh residential IP
        random_session = ''.join(random.choices(string.ascii_letters + string.digits, k=9))
        proxy_url = re.sub(r'(session-)[A-Za-z0-9]+([-:])', rf'\g<1>{random_session}\2', proxy_url)
        parsed = urlparse(proxy_url)
        proxy_config = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        }
        if parsed.username:
            proxy_config["username"] = parsed.username
        if parsed.password:
            proxy_config["password"] = parsed.password
        logger.info(f"Session {session_id} proxy: server={proxy_config['server']}, username={proxy_config.get('username', 'N/A')}, scheme={parsed.scheme}")

    locale = req.locale or DEFAULT_LOCALE
    camoufox_kwargs: dict[str, Any] = {
        "headless": not DEBUG_MODE,
        "humanize": True,
        "locale": locale,
    }
    if proxy_config:
        camoufox_kwargs["proxy"] = proxy_config
        camoufox_kwargs["geoip"] = True
        # WebRTC can leak the real client IP past the HTTP proxy; disable it
        # whenever we're proxying so navigator-reported IP and RTC-reported
        # IP can't diverge.
        camoufox_kwargs["block_webrtc"] = True

    try:
        browser_cm = AsyncCamoufox(**camoufox_kwargs)
        browser = await browser_cm.__aenter__()
    except InvalidIP:
        logger.warning(f"Session {session_id} geoip lookup failed, retrying without geoip")
        camoufox_kwargs["geoip"] = False
        browser_cm = AsyncCamoufox(**camoufox_kwargs)
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
        warmup_ok = False
        if session["google_warmup"] and not already_google:
            warmup_ok = await google_search_warmup(session, req.url, session_id)
        if not warmup_ok:
            # No real search history — fall back to the cheap fake
            # referrer so the target doesn't see a bare direct load.
            goto_kwargs["referer"] = "https://www.google.com/"
        session["first_navigation"] = False

    # Playwright's page.goto does NOT auto-inherit the current page's URL
    # as Referer — it's address-bar-equivalent. Set it explicitly so the
    # in-session Referer chain matches what was on screen: after a
    # successful warmup this is the google.nl SERP URL, and on subsequent
    # navigations it's whatever the previous page was.
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

    if req.page_arrival:
        await simulate_page_arrival(page, session["behavior"])

    await _log_state_snapshot(page, session_id, "post_navigate")

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
    await locator.scroll_into_view_if_needed()
    await locator.click()
    return {"clicked": True}


@app.post("/session/{session_id}/type")
async def type_text(session_id: str, req: TypeRequest):
    session = get_session(session_id)
    page = session["page"]
    behavior = session["behavior"]
    await _wait_for_load_best_effort(page, session_id, "type")
    locator = page.locator(req.selector)
    await locator.scroll_into_view_if_needed()
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
    await page.evaluate("window.scrollBy(0, window.innerHeight * 0.6)")
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
