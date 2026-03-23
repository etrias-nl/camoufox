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
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
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
DEBUG_MODE = os.getenv("DEBUG", "0") == "1"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class CreateSessionRequest(BaseModel):
    proxy_url: str | None = None
    behavior: str = "cautious"  # "cautious" or "fast"
    viewport_width: int = 1200
    viewport_height: int = 1100


class NavigateRequest(BaseModel):
    url: str
    warm_up: bool = True


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


async def simulate_page_arrival(page, behavior: str):
    """Brief pause after page load — Camoufox handles the rest."""
    if behavior == "fast":
        await asyncio.sleep(random.uniform(0.3, 0.8))
        return

    await human_delay(1.0, 0.5)

    # 70% chance: scroll a bit to look natural
    if random.random() < 0.7:
        await page.evaluate(f"window.scrollBy(0, {random.randint(100, 300)})")
        await human_delay(0.8, 0.4)


def extract_base_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


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

    camoufox_kwargs: dict[str, Any] = {
        "headless": not DEBUG_MODE,
        "humanize": True,
    }
    if proxy_config:
        camoufox_kwargs["proxy"] = proxy_config
        camoufox_kwargs["geoip"] = True

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
        "visited_homepage": False,
    }

    logger.info(f"Session {session_id} created (behavior={req.behavior})")
    return {"session_id": session_id}


def get_session(session_id: str) -> dict[str, Any]:
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    return sessions[session_id]


@app.post("/session/{session_id}/navigate")
async def navigate(session_id: str, req: NavigateRequest):
    session = get_session(session_id)
    page = session["page"]
    behavior = session["behavior"]

    # Homepage warmup
    base_url = extract_base_url(req.url)
    if req.warm_up and not session["visited_homepage"]:
        if base_url != req.url:
            logger.info(f"Session {session_id}: warming up at {base_url}")
            await page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
            await human_delay(1.5, 0.5)
            session["visited_homepage"] = True
        else:
            session["visited_homepage"] = True

    logger.info(f"Session {session_id}: navigating to {req.url}")
    await page.goto(req.url, wait_until="domcontentloaded", timeout=60000)

    await simulate_page_arrival(page, behavior)

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
    return {"clicked": True}


@app.post("/session/{session_id}/type")
async def type_text(session_id: str, req: TypeRequest):
    session = get_session(session_id)
    page = session["page"]
    behavior = session["behavior"]
    locator = page.locator(req.selector)
    await locator.scroll_into_view_if_needed()
    await locator.click()

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
