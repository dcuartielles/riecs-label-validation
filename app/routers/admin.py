import json
import random
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from app.templates import templates

from fastapi import APIRouter, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select, func, delete
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.database import SessionLocal
from app.models import (
    AddedLabel, AddedLabelDecision, Group, GroupAssignment,
    MandatoryClassification, Session as ReviewSession,
    StoryRejection, StoryRelevance, User, UserStory
)

router = APIRouter()

RANDOM_SEED  = 42
PROJECT_ROOT = Path(__file__).parent.parent
GITHUB_REPO  = "dcuartielles/riecs-label-validation"


async def require_admin(request: Request):
    user = await get_current_user(request)
    if not user or not user.is_admin:
        return None
    return user


async def get_active_session(db) -> ReviewSession | None:
    result = await db.execute(
        select(ReviewSession)
        .where(ReviewSession.ended_at.is_(None))
        .order_by(ReviewSession.started_at.desc())
    )
    return result.scalar_one_or_none()


def _assign_stories(story_ids: list[int], group_count: int,
                    overlap_pct: float, seed: int) -> dict[int, list[int]]:
    """Assign stories to groups with pairwise overlap.

    Overlap stories are split into group_count slices, one per adjacent pair
    (ring topology: 0-1, 1-2, ..., (G-1)-0).  Each group receives its unique
    slice plus the two overlap slices from its left and right neighbours, so
    every overlap story is reviewed by exactly two groups.
    """
    rng = random.Random(seed)
    ids = story_ids[:]
    rng.shuffle(ids)
    n = len(ids)

    n_overlap = round(n * overlap_pct)
    overlap_pool = ids[:n_overlap]
    unique_pool  = ids[n_overlap:]

    # Distribute overlap stories round-robin across the group_count pairs
    pair_slices: list[list[int]] = [[] for _ in range(group_count)]
    for i, sid in enumerate(overlap_pool):
        pair_slices[i % group_count].append(sid)

    # Unique stories divided equally among groups
    unique_per_group = max(1, (len(unique_pool) + group_count - 1) // group_count)

    assignments: dict[int, list[int]] = {}
    for g in range(group_count):
        u_start = g * unique_per_group
        u_end   = min(u_start + unique_per_group, len(unique_pool))
        unique  = unique_pool[u_start:u_end]
        # Pair to the left: (g-1, g);  pair to the right: (g, g+1)
        left_slice  = pair_slices[(g - 1) % group_count]
        right_slice = pair_slices[g]
        assignments[g] = unique + left_slice + right_slice
    return assignments


@router.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        users = (await db.execute(select(User).order_by(User.email))).scalars().all()
        groups = (await db.execute(select(Group))).scalars().all()
        active_session = await get_active_session(db)

        # Progress per group — stories with at least one added label in the active session
        progress = {}
        for group in groups:
            total_q = await db.execute(
                select(func.count(GroupAssignment.id))
                .where(GroupAssignment.group_id == group.id)
            )
            if active_session:
                labelled_q = await db.execute(
                    select(func.count(func.distinct(AddedLabel.story_id)))
                    .join(User, AddedLabel.user_id == User.id)
                    .where(
                        User.group_id == group.id,
                        AddedLabel.session_id == active_session.id,
                    )
                )
                labelled = labelled_q.scalar()
            else:
                labelled = 0
            progress[group.id] = {
                "total": total_q.scalar(),
                "labelled": labelled,
            }

        # Cross-group comparison — stories labelled by 2+ groups
        comparison = []
        if active_session:
            overlap_result = await db.execute(
                select(
                    UserStory.id,
                    UserStory.story_id,
                    func.count(func.distinct(User.group_id)).label("group_count"),
                )
                .join(AddedLabel, AddedLabel.story_id == UserStory.id)
                .join(User, AddedLabel.user_id == User.id)
                .where(
                    User.group_id.isnot(None),
                    AddedLabel.session_id == active_session.id,
                )
                .group_by(UserStory.id, UserStory.story_id)
                .having(func.count(func.distinct(User.group_id)) > 1)
                .order_by(UserStory.story_id)
            )
            overlapping = overlap_result.all()

            for row in overlapping:
                story_detail = []
                for group in groups:
                    count_q = await db.execute(
                        select(func.count(AddedLabel.id))
                        .join(User, AddedLabel.user_id == User.id)
                        .where(
                            AddedLabel.story_id == row.id,
                            AddedLabel.session_id == active_session.id,
                            User.group_id == group.id,
                        )
                    )
                    n = count_q.scalar()
                    if n:
                        story_detail.append({"group": group.name, "labels": n})
                if story_detail:
                    comparison.append({
                        "story_id": row.story_id,
                        "groups": story_detail,
                    })

    return templates.TemplateResponse(request, "admin.html", {
        "user": user,
        "users": users,
        "groups": groups,
        "progress": progress,
        "comparison": comparison,
        "active_session": active_session,
        "add_user_error": None,
    })


@router.post("/session/start")
async def start_session(
    request: Request,
    overlap_pct: float = Form(0.0),
    chart_refresh_secs: int = Form(300),
):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    overlap_pct = max(0.0, min(1.0, overlap_pct))
    chart_refresh_secs = max(60, chart_refresh_secs)

    async with SessionLocal() as db:
        existing = await get_active_session(db)
        if existing:
            return RedirectResponse(url="/admin", status_code=302)

        groups = (await db.execute(select(Group))).scalars().all()
        all_stories = (await db.execute(select(UserStory))).scalars().all()
        story_ids = [s.id for s in all_stories]
        n = len(story_ids)
        g = len(groups)
        stories_per_group = round(n / g) + round(n * overlap_pct / g) if g else 0

        # Regenerate group assignments
        await db.execute(delete(GroupAssignment))
        assignments = _assign_stories(story_ids, g, overlap_pct, RANDOM_SEED)
        for g_idx, group in enumerate(groups):
            for position, sid in enumerate(assignments[g_idx]):
                db.add(GroupAssignment(
                    group_id=group.id,
                    story_id=sid,
                    position=position,
                ))

        db.add(ReviewSession(
            started_by=user.id,
            overlap_pct=overlap_pct,
            stories_per_group=stories_per_group,
            chart_refresh_secs=chart_refresh_secs,
        ))
        await db.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/session/end")
async def end_session(request: Request):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        session = await get_active_session(db)
        if session:
            # Generate a final export snapshot before closing the session
            from app.routers.export import generate_workbook
            session_dir = Path("output") / f"session_{session.id}"
            session_dir.mkdir(parents=True, exist_ok=True)
            wb, _ = await generate_workbook(db, session.id)
            stamp = datetime.utcnow().strftime("%Y%m%d")
            wb.save(session_dir / f"{stamp}_label_results_session_{session.id}_final.xlsx")

            session.ended_at = datetime.utcnow()
            await db.commit()

            # Zip all daily exports for this session, then remove the folder
            zip_path = Path("output") / f"session_{session.id}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in sorted(session_dir.rglob("*")):
                    if f.is_file():
                        zf.write(f, f.relative_to(session_dir.parent))
            shutil.rmtree(session_dir)

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/add-user")
async def add_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    group_id: int = Form(0),
):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    email = email.strip().lower()
    async with SessionLocal() as db:
        existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if not existing:
            db.add(User(
                name=name.strip(),
                email=email,
                group_id=group_id if group_id != 0 else None,
            ))
            await db.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/remove-user")
async def remove_user(
    request: Request,
    user_id: int = Form(...),
):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        target = await db.get(User, user_id)
        if target and target.id != user.id:
            await db.delete(target)
            await db.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/assign-group")
async def assign_group(
    request: Request,
    user_id: int = Form(...),
    group_id: int = Form(...),
):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        target = await db.get(User, user_id)
        if target:
            target.group_id = group_id if group_id != 0 else None
            await db.commit()

    return RedirectResponse(url="/admin", status_code=302)


@router.post("/admin/set-admin")
async def set_admin(
    request: Request,
    user_id: int = Form(...),
    is_admin: bool = Form(False),
):
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    async with SessionLocal() as db:
        target = await db.get(User, user_id)
        if target:
            target.is_admin = is_admin
            await db.commit()

    return RedirectResponse(url="/admin", status_code=302)


def _fetch_latest_release(json_mod) -> dict | None:
    import urllib.request
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "riecs-admin"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json_mod.loads(resp.read())
    except Exception:
        return None


# Files/dirs never overwritten during a zip-based update
_PROTECTED = {"labelling.db", ".env", "output", "server.log", "__pycache__"}


@router.get("/admin/check-update")
async def check_update(request: Request):
    import subprocess, json as _json
    user = await require_admin(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    has_git = (PROJECT_ROOT / ".git").exists()
    latest_release = _fetch_latest_release(_json)
    rel_info = {
        "tag":          latest_release["tag_name"],
        "name":         latest_release["name"],
        "url":          latest_release["html_url"],
        "published_at": latest_release["published_at"],
    } if latest_release else None

    if not has_git:
        return JSONResponse({
            "has_git":        False,
            "current_version": "zip deployment (version unknown)",
            "current_commit":  None,
            "up_to_date":      False,
            "commits_behind":  None,
            "latest_release":  rel_info,
        })

    def git(*args):
        return subprocess.run(
            ["git"] + list(args),
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT),
        )

    git("fetch", "origin", "--quiet")
    local  = git("rev-parse", "HEAD").stdout.strip()
    remote = git("rev-parse", "origin/master").stdout.strip()
    tag_res   = git("describe", "--tags", "--exact-match", "HEAD")
    tag_local = tag_res.stdout.strip() if tag_res.returncode == 0 else local[:8]
    behind    = git("log", "HEAD..origin/master", "--oneline").stdout.strip()

    return JSONResponse({
        "has_git":        True,
        "current_version": tag_local,
        "current_commit":  local[:8],
        "up_to_date":      local == remote,
        "commits_behind":  len(behind.splitlines()) if behind else 0,
        "latest_release":  rel_info,
    })


@router.post("/admin/do-update")
async def do_update(request: Request):
    import subprocess, urllib.request, zipfile, io, shutil, json as _json
    user = await require_admin(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    has_git = (PROJECT_ROOT / ".git").exists()

    if has_git:
        try:
            result = subprocess.run(
                ["git", "pull", "origin", "master"],
                capture_output=True, text=True, timeout=60,
                cwd=str(PROJECT_ROOT),
            )
            success = result.returncode == 0
            output  = (result.stdout + result.stderr).strip()
            return JSONResponse({
                "success": success,
                "output":  output,
                "message": "Update applied — server is reloading." if success else "Update failed.",
            })
        except subprocess.TimeoutExpired:
            return JSONResponse({"success": False, "output": "", "message": "git pull timed out (60 s)."})
        except Exception as e:
            return JSONResponse({"success": False, "output": str(e), "message": "Unexpected error."})

    # ── Zip-based update (no git) ──────────────────────────────────────────
    try:
        release = _fetch_latest_release(_json)
        if not release:
            return JSONResponse({"success": False, "output": "", "message": "Could not reach GitHub API."})

        zipball_url = release["zipball_url"]
        log = [f"Downloading {release['name']} ({release['tag_name']}) from GitHub…"]

        req = urllib.request.Request(zipball_url, headers={"User-Agent": "riecs-admin"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            zip_bytes = resp.read()

        log.append(f"Downloaded {len(zip_bytes) // 1024} KB. Extracting…")

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names  = zf.namelist()
            prefix = names[0].split("/")[0] + "/"   # e.g. "owner-repo-abc123/"
            copied = skipped = 0

            for name in names:
                rel = name[len(prefix):]             # strip the GitHub top-level folder
                if not rel:
                    continue
                top = rel.split("/")[0]
                if top in _PROTECTED:
                    skipped += 1
                    continue
                target = PROJECT_ROOT / rel
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, open(target, "wb") as dst:
                        dst.write(src.read())
                    copied += 1

        log.append(f"Done — {copied} files updated, {skipped} protected files preserved.")
        log.append("Server is reloading with the new code.")
        return JSONResponse({
            "success": True,
            "output":  "\n".join(log),
            "message": "Update applied — server is reloading.",
        })

    except Exception as e:
        return JSONResponse({"success": False, "output": str(e), "message": "Update failed."})



@router.get("/admin/download-db")
async def download_db(request: Request):
    import sqlite3, tempfile, os
    from fastapi.responses import StreamingResponse
    user = await require_admin(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    # Safe consistent snapshot via SQLite backup API (works even under active writes)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    src = sqlite3.connect("labelling.db")
    dst = sqlite3.connect(tmp.name)
    src.backup(dst)
    src.close()
    dst.close()
    def _stream():
        with open(tmp.name, "rb") as f:
            yield from iter(lambda: f.read(65536), b"")
        os.unlink(tmp.name)
    return StreamingResponse(
        _stream(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=labelling.db"},
    )
