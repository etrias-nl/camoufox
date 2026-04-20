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
from collections import deque
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
DEBUG_EVENT_CAP = 500
_SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie"}


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


def _clamp(value: float, low: float, high: float) -> int:
    return int(max(low, min(high, value)))


async def _move_and_track(page, session: dict[str, Any], x: int, y: int) -> None:
    await page.mouse.move(x, y)
    session["last_mouse"] = (x, y)


async def _update_last_mouse_from_locator(session: dict[str, Any], locator) -> None:
    """Set session last_mouse to the on-screen center of a locator. No-op if
    the element is gone (navigated away, detached) or offscreen."""
    try:
        box = await locator.bounding_box()
    except Exception:
        return
    if not box:
        return
    session["last_mouse"] = (
        int(box["x"] + box["width"] / 2),
        int(box["y"] + box["height"] / 2),
    )


async def simulate_page_arrival(session: dict[str, Any]):
    """Simulate a user settling onto a freshly loaded page: small glance
    with the mouse and a short scroll. Continues from the last known
    cursor position so the pointer doesn't teleport across navigations —
    a real OS cursor stays where the user left it.

    Camoufox's humanize=True curves the mouse path between points; we
    pick the points."""
    page = session["page"]
    behavior = session["behavior"]
    if behavior == "fast":
        await asyncio.sleep(random.uniform(0.3, 0.8))
        return

    await human_delay(1.0, 0.5)

    viewport = page.viewport_size or {"width": 1200, "height": 1100}
    margin = 20
    max_x = viewport["width"] - margin
    max_y = viewport["height"] - margin
    sx, sy = session["last_mouse"]

    # First move: short drift from wherever the cursor already is, so the
    # new-page settle looks continuous with whatever the user was doing
    # before. ~150px gaussian, clamped inside the viewport.
    nearby_x = _clamp(sx + random.gauss(0, 150), margin, max_x)
    nearby_y = _clamp(sy + random.gauss(0, 150), margin, max_y)
    await _move_and_track(page, session, nearby_x, nearby_y)
    await asyncio.sleep(random.uniform(0.2, 0.7))

    # Then 1-3 more "glance around" moves. These can be broader but we
    # still anchor them to the current cursor rather than a fresh random.
    for _ in range(random.randint(1, 3)):
        cx, cy = session["last_mouse"]
        gx = _clamp(cx + random.gauss(0, 220), margin, max_x)
        gy = _clamp(cy + random.gauss(0, 220), margin, max_y)
        await _move_and_track(page, session, gx, gy)
        await asyncio.sleep(random.uniform(0.2, 0.7))

    # 70% chance: scroll a bit to look natural
    if random.random() < 0.7:
        await page.evaluate(f"window.scrollBy(0, {random.randint(100, 300)})")
        await human_delay(0.8, 0.4)


# ---------------------------------------------------------------------------
# Debug instrumentation
# ---------------------------------------------------------------------------

def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in _SENSITIVE_HEADERS:
            out[k] = f"<redacted, {len(v)} chars>"
        else:
            out[k] = v[:512]
    return out


def _attach_debug_listeners(page, events: deque) -> None:
    def on_request(request):
        try:
            events.append({
                "ts": time.time(),
                "type": "request",
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
                "headers": _safe_headers(request.headers),
            })
        except Exception as exc:  # never let listener errors kill the page
            logger.debug(f"debug request listener failed: {exc}")

    def on_response(response):
        try:
            events.append({
                "ts": time.time(),
                "type": "response",
                "status": response.status,
                "url": response.url,
                "headers": _safe_headers(response.headers),
            })
        except Exception as exc:
            logger.debug(f"debug response listener failed: {exc}")

    page.on("request", on_request)
    page.on("response", on_response)


def _ensure_debug_enabled() -> None:
    if not DEBUG_MODE:
        raise HTTPException(status_code=404, detail="Debug mode not enabled")


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

    session = {
        "browser": browser_cm,
        "page": page,
        "behavior": req.behavior,
        "first_navigation": True,
        "locale": locale,
        "google_warmup": req.google_warmup,
        "google_query": req.google_query,
        # Initial cursor position — used by Phase 2 (cursor continuity).
        # Keeping it populated now so the debug snapshot is consistent.
        "last_mouse": (
            random.randint(100, max(101, req.viewport_width - 100)),
            random.randint(50, max(51, req.viewport_height // 3)),
        ),
        "debug_events": deque(maxlen=DEBUG_EVENT_CAP) if DEBUG_MODE else None,
    }

    if DEBUG_MODE:
        _attach_debug_listeners(page, session["debug_events"])

    sessions[session_id] = session

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
        # Fresh session has no referrer chain; arrive as if from a Google
        # search so the target doesn't see a direct-load / no-referrer hit.
        # Phase 4 replaces this with a real google.nl warmup.
        goto_kwargs["referer"] = "https://www.google.com/"
        session["first_navigation"] = False

    logger.info(f"Session {session_id}: navigating to {req.url}")
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
        await simulate_page_arrival(session)

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
    locator = page.locator(req.selector)
    await locator.scroll_into_view_if_needed()
    await locator.click()
    await _update_last_mouse_from_locator(session, locator)
    return {"clicked": True}


@app.post("/session/{session_id}/type")
async def type_text(session_id: str, req: TypeRequest):
    session = get_session(session_id)
    page = session["page"]
    behavior = session["behavior"]
    locator = page.locator(req.selector)
    await locator.scroll_into_view_if_needed()
    await locator.click()
    await _update_last_mouse_from_locator(session, locator)

    # Simulate hands moving from mouse back to keyboard
    await asyncio.sleep(random.uniform(0.4, 0.9))

    for char in req.text:
        await page.keyboard.press(char)
        if behavior == "fast":
            await asyncio.sleep(random.uniform(0.01, 0.05))
        else:
            await asyncio.sleep(random.uniform(0.05, 0.20))
            # 3% chance of longer pause (as if thinking)
            if random.random() < 0.03:
                await asyncio.sleep(random.uniform(0.3, 0.8))
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
    await simulate_page_arrival(session)
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


@app.get("/session/{session_id}/debug/state")
async def debug_state(session_id: str):
    _ensure_debug_enabled()
    session = get_session(session_id)
    page = session["page"]
    try:
        browser_state = await page.evaluate(
            """() => {
                const safeStorage = (s) => { try { return s.length; } catch (e) { return -1; } };
                return {
                    language: navigator.language,
                    languages: Array.from(navigator.languages || []),
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                    referrer: document.referrer,
                    url: document.URL,
                    userAgent: navigator.userAgent,
                    viewport: { w: window.innerWidth, h: window.innerHeight },
                    storageCounts: {
                        local: safeStorage(localStorage),
                        session: safeStorage(sessionStorage),
                    },
                };
            }"""
        )
    except Exception as exc:
        browser_state = {"error": str(exc)}

    return {
        "browser": browser_state,
        "session": {
            "last_mouse": session["last_mouse"],
            "first_navigation": session["first_navigation"],
            "behavior": session["behavior"],
            "locale": session["locale"],
            "google_warmup": session["google_warmup"],
            "google_query": session["google_query"],
        },
    }


@app.get("/session/{session_id}/debug/cookies")
async def debug_cookies(session_id: str):
    _ensure_debug_enabled()
    session = get_session(session_id)
    cookies = await session["page"].context.cookies()
    by_domain: dict[str, dict[str, Any]] = {}
    for c in cookies:
        domain = c.get("domain") or "?"
        entry = by_domain.setdefault(domain, {
            "count": 0,
            "earliest_expiry": None,
            "latest_expiry": None,
            "names": [],
        })
        entry["count"] += 1
        entry["names"].append(c.get("name"))
        expiry = c.get("expires")
        if expiry and expiry > 0:
            if entry["earliest_expiry"] is None or expiry < entry["earliest_expiry"]:
                entry["earliest_expiry"] = expiry
            if entry["latest_expiry"] is None or expiry > entry["latest_expiry"]:
                entry["latest_expiry"] = expiry
    return {"total": len(cookies), "by_domain": by_domain}


@app.get("/session/{session_id}/debug/storage")
async def debug_storage(session_id: str):
    _ensure_debug_enabled()
    session = get_session(session_id)
    page = session["page"]
    try:
        storage = await page.evaluate(
            """() => {
                const keys = (s) => {
                    try {
                        return Array.from({ length: s.length }, (_, i) => s.key(i));
                    } catch (e) {
                        return { error: e.message };
                    }
                };
                return {
                    origin: window.location.origin,
                    localStorage: keys(localStorage),
                    sessionStorage: keys(sessionStorage),
                };
            }"""
        )
    except Exception as exc:
        storage = {"error": str(exc)}
    return storage


@app.get("/session/{session_id}/debug/events")
async def debug_events(session_id: str, since: float = 0.0, limit: int = 200):
    _ensure_debug_enabled()
    session = get_session(session_id)
    events = list(session["debug_events"] or [])
    if since:
        events = [e for e in events if e["ts"] > since]
    if limit and len(events) > limit:
        events = events[-limit:]
    return {"count": len(events), "events": events}


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
        inspect = (
            f' — <a href="/session/{sid}/debug/state">state</a>'
            f' | <a href="/session/{sid}/debug/cookies">cookies</a>'
            f' | <a href="/session/{sid}/debug/storage">storage</a>'
            f' | <a href="/session/{sid}/debug/events">events</a>'
        ) if DEBUG_MODE else ""
        rows += f'<li><a href="/debug/{sid}">{sid}</a>{inspect}</li>'
    if not rows:
        rows = "<li>No active sessions</li>"
    note = "" if DEBUG_MODE else "<p>CAMOUFOX_DEBUG is not enabled — inspector endpoints return 404.</p>"
    return f"""<!DOCTYPE html>
<html>
<head><title>Camoufox Debug</title>
<style>body {{ font-family: monospace; background: #1a1a1a; color: #ccc; padding: 20px; }} a {{ color: #6cf; }}</style>
</head>
<body><h1>Active Sessions</h1><ul>{rows}</ul>{note}</body></html>"""


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
