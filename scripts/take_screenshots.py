"""
Automated screenshot capture for the README.
Requires: pip install playwright && python -m playwright install chromium
Run while the app is live on http://localhost:8000
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import User

OUT = Path("docs/screenshots")
BASE = "http://localhost:8000"

PAGES = [
    ("01-login.png",      "/login",      None,     "full"),
    ("02-review.png",     "/review",     None,     "full"),
    ("03-stats.png",      "/stats",      None,     "full"),
    ("04-infograph.png",  "/infograph",  6000,     "full"),   # wait for D3
    ("05-admin.png",      "/admin",      None,     "full"),
]


async def get_admin_id() -> int:
    await init_db()
    async with SessionLocal() as db:
        result = await db.execute(select(User).where(User.is_admin == True))
        user = result.scalars().first()
        if not user:
            result = await db.execute(select(User))
            user = result.scalars().first()
        if not user:
            raise RuntimeError("No users in the database. Log in at least once first.")
        return user.id


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    admin_id = await get_admin_id()

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={"width": 1280, "height": 800})

        # Inject session cookie so we're logged in
        await context.add_cookies([{
            "name": "session",
            "value": "",          # placeholder — we'll use the API instead
            "domain": "localhost",
            "path": "/",
        }])

        # Use the app's own session mechanism via a direct API call approach:
        # navigate to a special temp endpoint we'll add inline below
        page = await context.new_page()

        # Hit the temp auth shim
        resp = await page.goto(f"{BASE}/_screenshot_auth?uid={admin_id}")
        if resp.status != 200:
            print(f"Auth shim failed: {resp.status}. Is the app running with the shim route?")
            await browser.close()
            return

        for filename, path, wait_ms, _ in PAGES:
            if path == "/login":
                # Need a fresh context without session for the login page
                ctx2 = await browser.new_context(viewport={"width": 1280, "height": 800})
                p2 = await ctx2.new_page()
                await p2.goto(f"{BASE}/login")
                await p2.wait_for_load_state("networkidle")
                await p2.screenshot(path=str(OUT / filename), full_page=True)
                await ctx2.close()
                print(f"  OK {filename}")
                continue

            await page.goto(f"{BASE}{path}")
            if wait_ms:
                await page.wait_for_timeout(wait_ms)
            else:
                await page.wait_for_load_state("networkidle")
            await page.screenshot(path=str(OUT / filename), full_page=True)
            print(f"  OK {filename}")

        await browser.close()
    print("Done — screenshots saved to docs/screenshots/")


asyncio.run(main())
