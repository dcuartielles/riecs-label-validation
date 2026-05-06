"""
Automated screenshot capture for the README.
Requires: pip install playwright && python -m playwright install chromium
Run while the app is live on http://localhost:8000

Emails and real names visible on any page are replaced with fictional
equivalents before the screenshot is taken (via JS text-node injection).
"""
import asyncio
import json
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

FAKE_NAMES = [
    "Alice Müller",
    "Bob Eriksson",
    "Carol Dupont",
    "David Rossi",
    "Eva Kowalski",
    "Frank Andersen",
    "Grace Nakamura",
    "Hugo Ferreira",
]

# Replaces every occurrence of the real strings with their fake counterparts.
# replacements = [[real, fake], ...]  — applied in order, longest-first.
ANONYMISE_JS = """
(replacements) => {
    replacements.sort((a, b) => b[0].length - a[0].length);
    const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walk.nextNode())) {
        let t = node.textContent;
        for (const [real, fake] of replacements) {
            if (t.includes(real)) t = t.split(real).join(fake);
        }
        node.textContent = t;
    }
}
"""


async def build_replacements(db) -> tuple[list[list[str]], int, str]:
    """Return (replacements, admin_id, admin_email)."""
    result = await db.execute(select(User).order_by(User.id))
    users = result.scalars().all()

    admin = next((u for u in users if u.is_admin), users[0] if users else None)
    if not admin:
        raise RuntimeError("No users in DB. Log in at least once first.")

    replacements = []
    fake_idx = 0
    for u in users:
        is_admin = u.id == admin.id
        fake_name = "Admin User" if is_admin else FAKE_NAMES[fake_idx % len(FAKE_NAMES)]
        if not is_admin:
            fake_idx += 1
        fake_email = "admin@fakemail.com" if is_admin else f"user{fake_idx}@fakemail.com"

        if u.name:
            replacements.append([u.name, fake_name])
        if u.email:
            replacements.append([u.email, fake_email])

    return replacements, admin.id, admin.email


async def ensure_active_session(db, admin_id: int):
    result = await db.execute(
        select(ReviewSession).where(ReviewSession.ended_at.is_(None))
    )
    session = result.scalar_one_or_none()
    if not session:
        session = ReviewSession(started_by=admin_id)
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session.id, True
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
        replacements, admin_id, admin_email = await build_replacements(db)
        session_id, we_created = await ensure_active_session(db, admin_id)

    print(f"Admin: {admin_email} -> admin@fakemail.com")
    print(f"Session {session_id} ({'created' if we_created else 'reused'})")
    repl_json = json.dumps(replacements)

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # ── 1. Login page (unauthenticated) ──────────────────────────────────
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

        async def anon(pg):
            await pg.evaluate(f"({ANONYMISE_JS})({repl_json})")

        # ── 2. Waiting page ───────────────────────────────────────────────────
        async with SessionLocal() as db:
            await end_session(db, session_id)
        await page.goto(f"{BASE}/review")
        await page.wait_for_load_state("networkidle")
        await anon(page)
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
        await anon(page)
        await page.screenshot(path=str(OUT / "03-review.png"), full_page=True)
        print("  OK 03-review.png")

        # ── 4. Statistics page ────────────────────────────────────────────────
        await page.goto(f"{BASE}/stats")
        await page.wait_for_load_state("networkidle")
        await anon(page)
        await page.screenshot(path=str(OUT / "04-stats.png"), full_page=True)
        print("  OK 04-stats.png")

        # ── 5. Infograph ──────────────────────────────────────────────────────
        await page.goto(f"{BASE}/infograph")
        await page.wait_for_timeout(6000)
        await anon(page)
        await page.screenshot(path=str(OUT / "05-infograph.png"), full_page=True)
        print("  OK 05-infograph.png")

        # ── 6. Admin panel ────────────────────────────────────────────────────
        await page.goto(f"{BASE}/admin")
        await page.wait_for_load_state("networkidle")
        await anon(page)
        await page.screenshot(path=str(OUT / "06-admin.png"), full_page=True)
        print("  OK 06-admin.png")

        # ── 7. Labels tab ─────────────────────────────────────────────────────
        await page.goto(f"{BASE}/labels")
        await page.wait_for_load_state("networkidle")
        await anon(page)
        await page.screenshot(path=str(OUT / "07-labels.png"), full_page=True)
        print("  OK 07-labels.png")

        await browser.close()

    if we_created:
        async with SessionLocal() as db:
            await end_session(db, session_id)

    print("Done — screenshots saved to docs/screenshots/")


asyncio.run(main())
