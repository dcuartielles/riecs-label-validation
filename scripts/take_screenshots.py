"""
Automated screenshot capture for the README.
Requires: pip install playwright && python -m playwright install chromium
Run while the app is live on http://localhost:8000

Emails visible on any page are replaced with fakeEmail.com addresses before
the screenshot is taken (via JS text-node injection).
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import Session as ReviewSession, User

OUT = Path("docs/screenshots")
BASE = "http://localhost:8000"


# JS that replaces every email address in the live DOM with a fake one.
# The first email found becomes admin@fakeEmail.com; the rest become user1@…, user2@…
ANONYMISE_JS = """
(adminEmail) => {
    const re = /[a-zA-Z0-9._%+\\-]+@[a-zA-Z0-9.\\-]+\\.[a-zA-Z]{2,}/g;
    const map = {};
    let n = 1;
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walk.nextNode())) {
        if (!re.test(node.textContent)) { re.lastIndex = 0; continue; }
        re.lastIndex = 0;
        node.textContent = node.textContent.replace(re, m => {
            if (!map[m]) {
                map[m] = (m === adminEmail) ? 'admin@fakeEmail.com'
                                            : `user${n++}@fakeEmail.com`;
            }
            return map[m];
        });
    }
}
"""


async def get_admin(db):
    result = await db.execute(select(User).where(User.is_admin == True))
    user = result.scalars().first()
    if not user:
        result = await db.execute(select(User))
        user = result.scalars().first()
    if not user:
        raise RuntimeError("No users in DB. Log in at least once first.")
    return user


async def ensure_active_session(db, admin_id: int) -> int:
    result = await db.execute(
        select(ReviewSession).where(ReviewSession.ended_at.is_(None))
    )
    session = result.scalar_one_or_none()
    if not session:
        session = ReviewSession(started_by=admin_id)
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session.id, True   # (id, created_by_us)
    return session.id, False


async def end_session(db, session_id: int):
    session = await db.get(ReviewSession, session_id)
    if session:
        session.ended_at = datetime.utcnow()
        await db.commit()


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    await init_db()

    async with SessionLocal() as db:
        admin = await get_admin(db)
        admin_id = admin.id
        admin_email = admin.email
        session_id, we_created = await ensure_active_session(db, admin_id)

    print(f"Admin: {admin_email} -> admin@fakeEmail.com")
    print(f"Session {session_id} ({'created' if we_created else 'reused'})")

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # ── 1. Login page (unauthenticated context) ──────────────────────────
        ctx_anon = await browser.new_context(viewport={"width": 1280, "height": 800})
        page_anon = await ctx_anon.new_page()
        await page_anon.goto(f"{BASE}/login")
        await page_anon.wait_for_load_state("networkidle")
        await page_anon.screenshot(path=str(OUT / "01-login.png"), full_page=True)
        await ctx_anon.close()
        print("  OK 01-login.png")

        # ── Authenticated context ─────────────────────────────────────────────
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await ctx.new_page()
        resp = await page.goto(f"{BASE}/_screenshot_auth?uid={admin_id}")
        if resp.status != 200:
            print(f"Auth shim failed ({resp.status}). Is the app running?")
            await browser.close()
            return

        # ── 2. Waiting page (end session temporarily) ─────────────────────────
        async with SessionLocal() as db:
            await end_session(db, session_id)
        await page.goto(f"{BASE}/review")
        await page.wait_for_load_state("networkidle")
        await page.evaluate(ANONYMISE_JS, admin_email)
        await page.screenshot(path=str(OUT / "02-waiting.png"), full_page=True)
        print("  OK 02-waiting.png")

        # Restore session
        async with SessionLocal() as db:
            new_session = ReviewSession(started_by=admin_id)
            db.add(new_session)
            await db.commit()
            await db.refresh(new_session)
            session_id = new_session.id

        # ── 3. Review page ────────────────────────────────────────────────────
        await page.goto(f"{BASE}/review")
        await page.wait_for_load_state("networkidle")
        await page.evaluate(ANONYMISE_JS, admin_email)
        await page.screenshot(path=str(OUT / "03-review.png"), full_page=True)
        print("  OK 03-review.png")

        # ── 4. Statistics page ────────────────────────────────────────────────
        await page.goto(f"{BASE}/stats")
        await page.wait_for_load_state("networkidle")
        await page.evaluate(ANONYMISE_JS, admin_email)
        await page.screenshot(path=str(OUT / "04-stats.png"), full_page=True)
        print("  OK 04-stats.png")

        # ── 5. Infograph ──────────────────────────────────────────────────────
        await page.goto(f"{BASE}/infograph")
        await page.wait_for_timeout(6000)   # wait for D3 simulation to settle
        await page.evaluate(ANONYMISE_JS, admin_email)
        await page.screenshot(path=str(OUT / "05-infograph.png"), full_page=True)
        print("  OK 05-infograph.png")

        # ── 6. Admin panel ────────────────────────────────────────────────────
        await page.goto(f"{BASE}/admin")
        await page.wait_for_load_state("networkidle")
        await page.evaluate(ANONYMISE_JS, admin_email)
        await page.screenshot(path=str(OUT / "06-admin.png"), full_page=True)
        print("  OK 06-admin.png")

        await browser.close()

    # Clean up the session we created (leave it ended so DB is tidy)
    if we_created:
        async with SessionLocal() as db:
            await end_session(db, session_id)

    print("Done — screenshots saved to docs/screenshots/")


asyncio.run(main())
