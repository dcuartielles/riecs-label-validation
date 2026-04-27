"""
Import user stories and label taxonomy from spreadsheets into the database.
Run once (or re-run with --reset to clear and reimport).

Usage:
    python -m scripts.import_data
    python -m scripts.import_data --reset
"""
import asyncio
import sys
import random
from pathlib import Path

import openpyxl
from sqlalchemy import delete, select, text

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import init_db, SessionLocal
from app.models import (
    Group, UserStory, StoryLabel, TaxonomyLabel, GroupAssignment
)

DATASET_PATH = Path("input_data/unified_outcomes_v004_labelled_v002_UItest_v001.xlsx")
LABELBOOK_PATH = Path("labelbook/Revised labelbook proposal for Oulu.xlsx")

GROUP_NAMES = ["Group A", "Group B", "Group C"]
# Each group reviews this fraction of all stories; overlap comes from the random draws
SUBSET_FRACTION = 0.6
RANDOM_SEED = 42


def parse_stories(path: Path) -> list[dict]:
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    headers = [cell.value for cell in ws[1]]

    seen_ids = set()
    stories = []

    for row in ws.iter_rows(min_row=2, values_only=True):
        story_id = row[0]
        if not story_id or story_id in seen_ids:
            continue
        seen_ids.add(story_id)

        label_cols = {}
        for i, header in enumerate(headers):
            if header and ("label" in header.lower()) and row[i]:
                label_cols[header] = row[i]

        raw_labels = []
        for header, value in label_cols.items():
            h = header.lower()
            if "human" in h:
                source = "Human"
            elif "ai" in h:
                source = "AI"
            else:
                continue

            # extract index from header, e.g. "Human label 3" → 3
            parts = header.split()
            try:
                idx = int(parts[-1])
            except ValueError:
                idx = 1

            # semicolons separate multiple labels in one cell
            for text_val in str(value).split(";"):
                text_val = text_val.strip()
                if text_val:
                    raw_labels.append({"source": source, "label_text": text_val, "label_index": idx})

        stories.append({
            "story_id": story_id,
            "workshop": row[1],
            "submitted_by": row[2],
            "stakeholder_group": row[3],
            "user_type": row[4],
            "task": row[5],
            "goal": row[6],
            "additional_notes": row[7],
            "labels": raw_labels,
        })

    return stories


def parse_taxonomy(path: Path) -> list[dict]:
    wb = openpyxl.load_workbook(path)
    ws = wb.active

    taxonomy = []
    current_label = None

    # Main taxonomy starts at row 10 (row 10 is the sub-header)
    for row in ws.iter_rows(min_row=11, max_row=ws.max_row,
                             min_col=3, max_col=6, values_only=True):
        label_col, sublabel_col, source_col, desc_col = row

        if label_col:
            current_label = str(label_col).strip()
            # Top-level entry with no sublabel
            taxonomy.append({
                "label": current_label,
                "sublabel": None,
                "source": str(source_col).strip() if source_col else None,
                "description": str(desc_col).strip() if desc_col else None,
            })
        elif sublabel_col and current_label:
            taxonomy.append({
                "label": current_label,
                "sublabel": str(sublabel_col).strip(),
                "source": str(source_col).strip() if source_col else None,
                "description": str(desc_col).strip() if desc_col else None,
            })

    return taxonomy


def assign_subsets(story_ids: list[int], group_count: int,
                   fraction: float, seed: int) -> dict[int, list[int]]:
    """Give each group a random subset of story IDs with partial overlap."""
    rng = random.Random(seed)
    n = len(story_ids)
    subset_size = max(1, int(n * fraction))
    assignments: dict[int, list[int]] = {}
    for g in range(group_count):
        sample = rng.sample(story_ids, subset_size)
        assignments[g] = sample
    return assignments


async def run(reset: bool = False):
    await init_db()

    async with SessionLocal() as db:
        if reset:
            for table in [GroupAssignment, StoryLabel, UserStory,
                          TaxonomyLabel, Group]:
                await db.execute(delete(table))
            await db.commit()
            print("Database cleared.")

        # --- Groups ---
        existing_groups = (await db.execute(select(Group))).scalars().all()
        if not existing_groups:
            for name in GROUP_NAMES:
                db.add(Group(name=name))
            await db.commit()
            print(f"Created {len(GROUP_NAMES)} groups.")

        groups = (await db.execute(select(Group))).scalars().all()
        group_map = {g.name: g.id for g in groups}

        # --- Stories ---
        existing_stories = (await db.execute(select(UserStory))).scalars().all()
        if not existing_stories:
            stories = parse_stories(DATASET_PATH)
            for s in stories:
                story = UserStory(
                    story_id=s["story_id"],
                    workshop=s["workshop"],
                    submitted_by=s["submitted_by"],
                    stakeholder_group=s["stakeholder_group"],
                    user_type=s["user_type"],
                    task=s["task"],
                    goal=s["goal"],
                    additional_notes=s["additional_notes"],
                )
                db.add(story)
                await db.flush()
                for lbl in s["labels"]:
                    db.add(StoryLabel(
                        story_id=story.id,
                        source=lbl["source"],
                        label_text=lbl["label_text"],
                        label_index=lbl["label_index"],
                    ))
            await db.commit()
            print(f"Imported {len(stories)} user stories.")
        else:
            print(f"Stories already in DB ({len(existing_stories)}), skipping import.")

        # --- Taxonomy ---
        existing_tax = (await db.execute(select(TaxonomyLabel))).scalars().all()
        if not existing_tax:
            taxonomy = parse_taxonomy(LABELBOOK_PATH)
            for t in taxonomy:
                db.add(TaxonomyLabel(**t))
            await db.commit()
            print(f"Imported {len(taxonomy)} taxonomy entries.")
        else:
            print(f"Taxonomy already in DB ({len(existing_tax)}), skipping import.")

        # --- Group assignments ---
        existing_assignments = (await db.execute(select(GroupAssignment))).scalars().all()
        if not existing_assignments:
            all_stories = (await db.execute(select(UserStory))).scalars().all()
            story_ids = [s.id for s in all_stories]
            assignments = assign_subsets(story_ids, len(groups), SUBSET_FRACTION, RANDOM_SEED)

            for g_idx, group in enumerate(groups):
                for position, sid in enumerate(assignments[g_idx]):
                    db.add(GroupAssignment(
                        group_id=group.id,
                        story_id=sid,
                        position=position,
                    ))
            await db.commit()
            print("Group subsets assigned.")
        else:
            print("Group assignments already exist, skipping.")

    print("Done.")


if __name__ == "__main__":
    reset = "--reset" in sys.argv
    asyncio.run(run(reset=reset))
