#!/usr/bin/env python3
"""
simulate_session.py — RIECS workshop simulation script
=======================================================
See docs/evaluation-procedure.md for full documentation.

Usage
-----
  python scripts/simulate_session.py --save-users   # backup real users
  python scripts/simulate_session.py --phase 1      # fake users + session + 50%
  python scripts/simulate_session.py --phase 2      # remaining 50%
  python scripts/simulate_session.py --reset        # restore real users, clear data
  python scripts/simulate_session.py --status       # show DB state summary
"""

import argparse
import asyncio
import json
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, delete, func
from app.database import SessionLocal
from app.models import (
    AddedLabel, AddedLabelDecision, Group, GroupAssignment,
    MandatoryClassification, Session as ReviewSession,
    StoryRejection, StoryRelevance, TaxonomyLabel, User, UserStory,
)

# ── Constants ─────────────────────────────────────────────────────────────────
BACKUP_FILE        = Path("scripts/real_users_backup.json")
SIM_MARKER         = "@simulation.fake"
RANDOM_SEED        = 42      # must match admin.py RANDOM_SEED (story assignment)
SIM_RNG_SEED       = 777     # seed for label choices
OVERLAP_PCT        = 0.10
CHART_REFRESH_SECS = 60

SIM_RNG = random.Random(SIM_RNG_SEED)

# ── Fake users: 3 per group (group_id 1–13) ───────────────────────────────────
FAKE_USERS_BY_GROUP: dict[int, list[tuple[str, str]]] = {
    1:  [("María García Pérez",   "m.garcia@simulation.fake"),
         ("Carlos López Moreno",  "c.lopez@simulation.fake"),
         ("Ana Martínez Ruiz",    "a.martinez@simulation.fake")],
    2:  [("Hans Müller",          "h.muller@simulation.fake"),
         ("Laura Schmidt",        "l.schmidt@simulation.fake"),
         ("Klaus Weber",          "k.weber@simulation.fake")],
    3:  [("Pablo Fernández",      "p.fernandez@simulation.fake"),
         ("Isabel Rodríguez",     "i.rodriguez@simulation.fake"),
         ("Miguel Sánchez",       "m.sanchez@simulation.fake")],
    4:  [("Johann Bauer",         "j.bauer@simulation.fake"),
         ("Sophia Huber",         "s.huber@simulation.fake"),
         ("Michael Wagner",       "m.wagner@simulation.fake")],
    5:  [("Franz Gruber",         "f.gruber@simulation.fake"),
         ("Anna Steiner",         "a.steiner@simulation.fake"),
         ("Thomas Berger",        "t.berger@simulation.fake")],
    6:  [("Eva Hofer",            "e.hofer@simulation.fake"),
         ("Peter Maier",          "p.maier@simulation.fake"),
         ("Clara Schwarz",        "c.schwarz@simulation.fake")],
    7:  [("Reinhard Koch",        "r.koch@simulation.fake"),
         ("Maria Schneider",      "m.schneider@simulation.fake"),
         ("Stefan Fischer",       "s.fischer@simulation.fake")],
    8:  [("Erik Eriksson",        "e.eriksson@simulation.fake"),
         ("Anna Johansson",       "a.johansson@simulation.fake"),
         ("Lars Lindqvist",       "l.lindqvist@simulation.fake")],
    9:  [("Marco Rossi",          "m.rossi@simulation.fake"),
         ("Giulia Bianchi",       "g.bianchi@simulation.fake"),
         ("Luca Ferrari",         "l.ferrari@simulation.fake")],
    10: [("Nikola Jovanović",     "n.jovanovic@simulation.fake"),
         ("Milica Petrović",      "m.petrovic@simulation.fake"),
         ("Stefan Nikolić",       "s.nikolic@simulation.fake")],
    11: [("Jonas Kazlauskas",     "j.kazlauskas@simulation.fake"),
         ("Ruta Petronyte",       "r.petronyte@simulation.fake"),
         ("Tomas Balciunas",      "t.balciunas@simulation.fake")],
    12: [("Sophie Müller",        "s.muller.csgp@simulation.fake"),
         ("Jean-Pierre Dubois",   "jp.dubois@simulation.fake"),
         ("Maria Gonzalez",       "m.gonzalez@simulation.fake")],
    13: [("Daniel Keller",        "d.keller@simulation.fake"),
         ("Sandra Moser",         "s.moser@simulation.fake"),
         ("Tobias Richter",       "t.richter@simulation.fake")],
}

# ── Story-assignment replication (identical to admin.py) ──────────────────────
def _assign_stories(story_ids: list[int], group_count: int,
                    overlap_pct: float, seed: int) -> dict[int, list[int]]:
    """Pairwise overlap: each overlap story is reviewed by exactly two
    adjacent groups (ring topology).  Must stay identical to admin.py."""
    rng = random.Random(seed)
    ids = story_ids[:]
    rng.shuffle(ids)
    n = len(ids)

    n_overlap    = round(n * overlap_pct)
    overlap_pool = ids[:n_overlap]
    unique_pool  = ids[n_overlap:]

    pair_slices: list[list[int]] = [[] for _ in range(group_count)]
    for i, sid in enumerate(overlap_pool):
        pair_slices[i % group_count].append(sid)

    unique_per_group = max(1, (len(unique_pool) + group_count - 1) // group_count)

    assignments: dict[int, list[int]] = {}
    for g in range(group_count):
        u_start = g * unique_per_group
        u_end   = min(u_start + unique_per_group, len(unique_pool))
        unique  = unique_pool[u_start:u_end]
        left_slice  = pair_slices[(g - 1) % group_count]
        right_slice = pair_slices[g]
        assignments[g] = unique + left_slice + right_slice
    return assignments


# ── Label taxonomy keyword mapping ────────────────────────────────────────────
TARGET_USER_RULES: list[tuple[list[str], str]] = [
    (["researcher", "scientist", "phd", "academic", "principal investigator",
      "research group", "study", "analysis"], "Researchers & scientists"),
    (["citizen scientist", "volunteer", "hobbyist", "birdwatch", "amateur",
      "lay person", "everyday", "public"], "Citizen (scientist)"),
    (["coordinator", "facilitator", "organiser", "organizer", "cs practitioner",
      "project leader", "project manager", "community manager"], "Citizen science practitioner"),
    (["school", "teacher", "pupil", "secondary", "high school", "k-12",
      "primary school", "gymnasium"], "Primary and Secondary Education"),
    (["university", "college", "student", "undergraduate", "postgraduate",
      "master", "bachelor", "campus"], "Post-secondary Education"),
    (["policy", "government", "minister", "regulator", "policymaker",
      "authority", "parliament", "legislation"], "Policy Makers"),
    (["ngo", "non-profit", "nonprofit", "foundation", "charity",
      "civil society"], "NGOs"),
    (["company", "enterprise", "industry", "business", "sme", "startup",
      "corporation", "firm", "vendor"], "Companies"),
    (["network", "consortium", "association", "federation",
      "platform network"], "CS Networks"),
    (["technology provider", "it provider", "software developer",
      "developer", "tech company"], "Technology providers"),
    (["infrastructure", "eu ri", "research infrastructure", "observatory",
      "institute", "institution"], "Other EU RIs"),
]

CONCEPT_RULES: list[tuple[list[str], str]] = [
    (["problem", "challenge", "difficult", "barrier", "obstacle",
      "struggle", "issue", "gap", "lack of"], "Challenge"),
    (["technical requirement", "technical need", "system must", "it must",
      "needs to support", "should be able"], "Technical requirement"),
    (["resource", "dataset", "data source", "access to data",
      "database", "repository"], "Resource"),
    (["service", "provide", "offer a", "enable users", "offer users",
      "as a service"], "Service"),
    (["feature", "functionality", "function", "capability",
      "option", "feature set"], "Funcionality"),
    # Solution is default for "I want to" stories
    (["i want to", "i need to", "i would like", "i wish", "i'd like",
      "solution", "approach", "method", "way to"], "Solution"),
]

# keyword fragments → list of sublabel names
TECH_RULES: list[tuple[list[str], list[str]]] = [
    # Hardware & sensors
    (["sensor", "sensors", "sensing device", "measurement device"],
     ["Sensors", "Sensor accuracy, reliability and calibration"]),
    (["iot", "internet of things", "connected device"],
     ["Hardware", "Network connectivity"]),
    (["hardware", "equipment", "physical device"],
     ["Hardware"]),
    (["installation", "deploy sensor", "setup device"],
     ["Installation difficulties"]),
    (["waterproof", "weather resistant", "outdoor", "environmental durability"],
     ["Environmental durability"]),
    (["smartphone", "mobile phone", "android", "ios", "tablet"],
     ["Device compatibility", "Cross-platform development"]),

    # Software & apps
    (["mobile app", "web app", "application", "software tool"],
     ["Cross-platform development", "Application stability"]),
    (["platform", "web platform", "online platform", "cs platform"],
     ["Platform integration"]),
    (["api", "rest api", "web service", "endpoint", "integration"],
     ["API and service integration"]),
    (["legacy system", "existing system", "old database"],
     ["Legacy system integration"]),
    (["feature limitation", "missing feature", "not supported"],
     ["Feature limitations"]),

    # Data quality
    (["data quality", "quality control", "quality assurance"],
     ["Data quality control"]),
    (["missing data", "incomplete data", "data gap", "completeness"],
     ["Data completeness"]),
    (["data processing", "process data", "analyse data", "data analysis"],
     ["Data processing"]),
    (["metadata", "catalogue", "catalog", "data catalogue"],
     ["Metadata management", "Data & Metadata standards"]),
    (["validation", "calibration", "verify data", "ground truth"],
     ["validation and callibration procedures"]),

    # Infrastructure
    (["cloud", "aws", "azure", "gcp", "cloud computing", "cloud storage"],
     ["Computing & Cloud infrastructures"]),
    (["server", "database server", "data storage", "repository server"],
     ["Data & Server infrastructure"]),
    (["network connectivity", "internet access", "bandwidth", "latency"],
     ["Network connectivity"]),
    (["digital infrastructure", "digital platform", "e-infrastructure"],
     ["Digital Infrastructures"]),
    (["physical infrastructure", "lab", "field station", "observatory"],
     ["Physical Infrastructures"]),
    (["citizen equipment", "volunteer equipment", "provided device"],
     ["Citizens' resources"]),

    # Architecture
    (["interoperab", "interoperable", "compatible system", "modular", "flexible system"],
     ["Interoperability, Flexibility & Modular Design"]),
    (["distributed", "federated", "decentralised", "decentralized", "peer-to-peer"],
     ["Distributed & Federated Design"]),
    (["middleware", "integration layer", "bus", "message queue"],
     ["Architecture interfaces & Middleware"]),
    (["security", "secure", "authentication", "authorisation", "authorization", "cybersecurity"],
     ["Security"]),
    (["reliability", "uptime", "availability", "fault tolerant", "resilient"],
     ["Reliability"]),

    # Scalability
    (["scalab", "scale up", "growing number of users", "large number"],
     ["User Scalability", "Data Scalability"]),
    (["performance", "speed", "fast response", "efficient", "optimize"],
     ["Performance Optimization"]),
    (["computational", "processing power", "hpc", "high performance computing"],
     ["Computational Scalability"]),
    (["geographic", "global scale", "international", "multi-country", "eu-wide"],
     ["Geographic Scalability"]),

    # Standards
    (["standard", "protocol", "norm", "specification", "common format"],
     ["Community standards", "Data standardization"]),
    (["open source", "open-source", "github", "foss", "free software"],
     ["Open-source software"]),
    (["data standard", "metadata standard", "dublin core", "darwin core"],
     ["Data & Metadata standards"]),

    # Human capacity
    (["training", "train users", "workshop", "tutorial", "guide", "manual"],
     ["Training & Documentation"]),
    (["technical skill", "digital skill", "literacy", "capacity building"],
     ["Technical skills requirements"]),
    (["user support", "help desk", "helpdesk", "support service"],
     ["User support needs"]),
    (["interactive learning", "gamif", "engagement activity", "e-learning"],
     ["Enhanced learning / interactive engagement"]),

    # Workflows & UX
    (["user interface", "ui design", "ux", "usability", "user-friendly", "intuitive"],
     ["User interface & usability"]),
    (["collaboration", "collaborative", "teamwork", "co-create", "shared workspace"],
     ["Collaboration"]),
    (["discover project", "find project", "search project", "browse"],
     ["Project discovery"]),
    (["data collection", "field collection", "onsite collection", "in situ"],
     ["Onsite data collection"]),
    (["crowdsourc", "crowd intelligence", "collective data"],
     ["Crowdsourcing"]),
    (["visuali", "visualization", "dashboard", "map view", "charting"],
     ["Online data analysis and visualization"]),
    (["network people", "connect researchers", "community building", "social network"],
     ["Networking"]),
    (["decision support", "recommendation", "suggest action", "guide decision"],
     ["decision support"]),
    (["workflow", "process flow", "pipeline", "automated workflow"],
     ["Workflow management"]),
    (["aggregate resource", "compile data", "resource collection"],
     ["Resource collection"]),

    # Governance
    (["fund", "funding", "grant", "financial", "budget", "cost model"],
     ["funding models", "financial sustainability"]),
    (["legal", "regulation", "compliance", "legislation", "legal framework"],
     ["legal frameworks"]),
    (["long-term", "long term sustainability", "institutional support"],
     ["long-term institutional support"]),
    (["transparent", "transparency", "open governance", "accountability"],
     ["tools for transparency"]),
    (["platform misuse", "abuse", "policy violation", "terms of service"],
     ["Platform misuse policy"]),
    (["digital sovereignty", "data sovereignty"],
     ["Digital sovereignity"]),
    (["representation", "participatory governance", "governance structure"],
     ["representation in decision making"]),
    (["public resource", "public good", "common resource"],
     ["public resource"]),
    (["fragmentation", "redundancy", "duplication", "siloed"],
     ["Fragmentation, superfluous redundancy"]),

    # Data governance
    (["privacy", "gdpr", "personal data", "pii", "data protection"],
     ["Data Privacy", "Data Consent"]),
    (["consent", "opt-in", "permission", "data agreement"],
     ["Data Consent"]),
    (["data ownership", "ownership rights", "intellectual property"],
     ["Data ownership rights"]),
    (["sensitive data", "confidential data", "anonymi", "pseudonymous"],
     ["Sensitive Data"]),
    (["data provenance", "data lineage", "data origin", "data traceability"],
     ["Data provenance"]),

    # Inclusivity
    (["accessible", "accessibility", "disability", "impairment", "ada"],
     ["accessibility"]),
    (["multilingual", "multi-language", "translation", "localisation", "localization"],
     ["multi-language support"]),
    (["outreach", "awareness campaign", "public engagement"],
     ["outreach programs"]),
    (["scientific literacy", "science communication", "communicate science"],
     ["scientific literacy"]),
    (["digital divide", "inequality", "underserved", "rural access", "remote area"],
     ["digital divide"]),
    (["participation barrier", "barrier to entry", "exclude", "excluded"],
     ["barriers to entry to CS"]),

    # Scientific principles
    (["citizen science value", "cs methodology", "cs best practice", "cs protocol"],
     ["Valuations of citizen science"]),
    (["interdisciplinary", "transdisciplinary", "cross-sector", "multi-disciplinary"],
     ["Transdisciplinarity and interdisciplinarity"]),
    (["dissemination", "publish result", "report finding", "science communication"],
     ["Communication"]),
    (["open science", "open access", "open knowledge", "open publication"],
     ["Open Science", "FAIR"]),
    (["recognition", "credit volunteer", "reward participant", "acknowledge"],
     ["recognition mechanisms"]),
    (["fair data", "findable", "accessible interoperable", "reusable"],
     ["FAIR"]),
    (["care principle", "collective authority", "indigenous data"],
     ["CARE"]),
    (["trust", "trustworthy", "credibility", "reliable source"],
     ["TRUST"]),

    # Domain focus
    (["health", "medical", "patient", "clinical", "disease", "epidemiology", "cancer"],
     ["Health data"]),
    (["climate change", "global warming", "carbon", "greenhouse"],
     ["Climate Change"]),
    (["environment", "ecological", "ecosystem", "pollution", "air quality"],
     ["Environment"]),
    (["biodiversity", "species", "wildlife", "habitat", "flora", "fauna", "bird"],
     ["Biodiversity"]),
    (["earth observation", "satellite", "remote sensing", "eo data", "copernicus"],
     ["Earth Observation"]),
    (["social science", "humanities", "sociology", "anthropology", "social research"],
     ["Social Sciences and Humanities"]),
    (["agriculture", "farming", "crop", "food production", "agricultural"],
     ["Agriculture & Food Systems"]),

    # AI & analytics
    (["machine learning", "deep learning", "neural network", "ml model"],
     ["Machine learning implementation"]),
    (["automat", "automation", "automated process", "robotic process"],
     ["Process automation"]),
    (["ai validation", "automated validation", "ai quality check"],
     ["AI data validation support"]),
    (["species identification", "image recognition", "object detection", "photo id"],
     ["AI assisted species or object identification"]),
    (["decision support system", "dss", "recommendation engine", "smart suggestion"],
     ["Automated Decision support system"]),
    (["human in the loop", "human review", "expert review", "manual check"],
     ["Human in the loop review"]),
    (["ai training", "ai literacy", "ai capacity", "learn ai"],
     ["Capacity building for AI use"]),

    # Ethics
    (["ethics", "ethical ai", "bias", "fairness", "ai ethics"],
     ["AI ethics framework"]),
    (["ethics committee", "irb", "institutional review board"],
     ["Ethics Committee Support"]),
    (["ethical sandbox", "pilot test", "sandboxed", "test environment"],
     ["Ethical Sandbox"]),

    # To be classified
    (["insight platform", "knowledge hub", "cs insight"],
     ["CS Insight platform"]),
    (["impact assessment", "societal impact", "policy impact", "uptake"],
     ["Impact"]),
    (["policy ready", "policy relevant", "evidence-based policy"],
     ["Policy readiness"]),
]

# ── Label assigner ────────────────────────────────────────────────────────────
class LabelAssigner:
    """Assigns taxonomy labels to a story based on text keyword analysis."""

    def __init__(self, all_taxonomy: list[TaxonomyLabel]):
        # sublabel name → TaxonomyLabel object
        self.by_sublabel: dict[str, TaxonomyLabel] = {}
        # main category name → list of TaxonomyLabel
        self.by_main: dict[str, list[TaxonomyLabel]] = {}
        self.target_user_labels: list[TaxonomyLabel] = []
        self.concept_labels: list[TaxonomyLabel] = []
        self.tech_labels: list[TaxonomyLabel] = []

        MANDATORY_MAINS = {"Target user in story", "User Story Concept"}

        for t in all_taxonomy:
            if not t.sublabel:
                continue
            self.by_sublabel[t.sublabel.strip()] = t
            self.by_main.setdefault(t.label, []).append(t)
            if t.label == "Target user in story":
                self.target_user_labels.append(t)
            elif t.label == "User Story Concept":
                self.concept_labels.append(t)
            elif t.label not in MANDATORY_MAINS:
                self.tech_labels.append(t)

    def _story_text(self, story: UserStory) -> str:
        parts = [
            story.user_type or "",
            story.task or "",
            story.goal or "",
            story.additional_notes or "",
            story.story_id or "",
        ]
        return " ".join(parts).lower()

    def _match(self, text: str, rules: list[tuple[list[str], str | list[str]]],
               default=None):
        """Return first match from rules or default."""
        for keywords, label in rules:
            if any(kw in text for kw in keywords):
                return label
        return default

    def _match_all(self, text: str,
                   rules: list[tuple[list[str], list[str]]]) -> list[str]:
        """Return all matching sublabels from tech rules, deduplicated."""
        found: list[str] = []
        seen: set[str] = set()
        for keywords, sublabels in rules:
            if any(kw in text for kw in keywords):
                for s in sublabels:
                    if s not in seen:
                        seen.add(s)
                        found.append(s)
        return found

    def assign(self, story: UserStory) -> dict:
        text = self._story_text(story)

        # ── Target user ───────────────────────────────────────────────────
        tu_name = self._match(text, TARGET_USER_RULES, "Researchers & scientists")
        tu_obj  = self.by_sublabel.get(tu_name) or SIM_RNG.choice(self.target_user_labels)
        target_user = tu_obj.sublabel

        # ── Story concepts ────────────────────────────────────────────────
        con_name = self._match(text, CONCEPT_RULES, "Solution")
        con_obj  = self.by_sublabel.get(con_name) or SIM_RNG.choice(self.concept_labels)
        concepts = [con_obj.sublabel]
        # Add a second concept 50% of the time
        if SIM_RNG.random() < 0.50:
            extra = SIM_RNG.choice([c for c in self.concept_labels if c.sublabel != con_obj.sublabel])
            concepts.append(extra.sublabel)

        # ── Technical labels ──────────────────────────────────────────────
        matched_subs = self._match_all(text, TECH_RULES)
        matched_objs = [self.by_sublabel[s] for s in matched_subs if s in self.by_sublabel]

        # Keep 2–5 matched labels; supplement with random picks if needed
        target_n = SIM_RNG.randint(2, 5)
        SIM_RNG.shuffle(matched_objs)
        chosen = matched_objs[:target_n]

        if len(chosen) < 2:
            pool = [t for t in self.tech_labels if t not in chosen]
            chosen += SIM_RNG.sample(pool, min(2 - len(chosen), len(pool)))

        tech_label_ids = [t.id for t in chosen]

        # ── Rejection (7%) ────────────────────────────────────────────────
        rejected = SIM_RNG.random() < 0.07
        rejection_reason = None
        if rejected:
            rejection_reason = SIM_RNG.choice([
                "Story is too vague to classify meaningfully.",
                "This appears to be a duplicate of another submission.",
                "The story describes an internal institutional process, not a CS scenario.",
                "Insufficient detail to assign meaningful labels.",
                "Out of scope for this project's focus area.",
            ])

        # ── Relevance (12% High, 3% VeryHigh) ────────────────────────────
        rv = SIM_RNG.random()
        if rv < 0.03:
            relevance_score = "VeryHigh"
            relevance_reason = SIM_RNG.choice([
                "Directly addresses a critical challenge identified across multiple partner institutions.",
                "This story represents a high-priority use case with broad applicability.",
                "Exceptional relevance to the project's core research questions.",
            ])
        elif rv < 0.15:
            relevance_score = "High"
            relevance_reason = SIM_RNG.choice([
                "Good representative example of a recurring challenge.",
                "Strong alignment with the technical infrastructure workstream.",
                "Useful benchmark story for cross-partner comparison.",
                None,
            ])
        else:
            relevance_score = "Normal"
            relevance_reason = None

        return {
            "target_user":      target_user,
            "concepts":         concepts,
            "tech_label_ids":   tech_label_ids,
            "rejected":         rejected,
            "rejection_reason": rejection_reason,
            "relevance_score":  relevance_score,
            "relevance_reason": relevance_reason,
        }


# ── DB helpers ────────────────────────────────────────────────────────────────
async def save_real_users():
    async with SessionLocal() as db:
        users = (await db.execute(
            select(User).where(~User.email.like(f"%{SIM_MARKER}"))
        )).scalars().all()
        data = [
            {"id": u.id, "name": u.name, "email": u.email,
             "group_id": u.group_id, "is_admin": u.is_admin}
            for u in users
        ]
    BACKUP_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(data)} real users to {BACKUP_FILE}")


MASK_NAMES = [
    ("Alicia Vega Montero",    "a.vega@example.org"),
    ("Brendan O'Sullivan",     "b.osullivan@example.org"),
    ("Chiara Lombardi",        "c.lombardi@example.org"),
    ("Dimitri Papadakis",      "d.papadakis@example.org"),
    ("Elena Kovacevic",        "e.kovacevic@example.org"),
    ("Fabio Marchetti",        "f.marchetti@example.org"),
    ("Greta Lindberg",         "g.lindberg@example.org"),
    ("Henrik Svensson",        "h.svensson@example.org"),
    ("Ines Carvalho",          "i.carvalho@example.org"),
    ("Jakub Novotny",          "j.novotny@example.org"),
    ("Katarzyna Wisniewska",   "k.wisniewska@example.org"),
    ("Lukas Braun",            "l.braun@example.org"),
    ("Miriam Gutierrez",       "m.gutierrez@example.org"),
    ("Niko Virtanen",          "n.virtanen@example.org"),
    ("Olivia Fontaine",        "o.fontaine@example.org"),
    ("Petra Blazevic",         "p.blazevic@example.org"),
    ("Quentin Renard",         "q.renard@example.org"),
    ("Raluca Ionescu",         "r.ionescu@example.org"),
]


async def mask_real_users():
    """Replace real user names and emails with realistic fake ones for screenshots."""
    if not BACKUP_FILE.exists():
        print("ERROR: no backup file found — run --save-users first")
        return

    async with SessionLocal() as db:
        real_users = (await db.execute(
            select(User).where(~User.email.like(f"%{SIM_MARKER}"))
            .order_by(User.id)
        )).scalars().all()

        for i, u in enumerate(real_users):
            name, email = MASK_NAMES[i % len(MASK_NAMES)]
            u.name  = name
            u.email = email

        await db.commit()
    print(f"Masked {len(real_users)} real users with fake names/emails")


async def create_fake_users() -> dict[int, list[int]]:
    """Insert fake users; return {group_id: [user_id, ...]}."""
    group_to_uids: dict[int, list[int]] = {}
    async with SessionLocal() as db:
        for gid, people in FAKE_USERS_BY_GROUP.items():
            uids = []
            for name, email in people:
                existing = (await db.execute(
                    select(User).where(User.email == email)
                )).scalar_one_or_none()
                if existing:
                    uids.append(existing.id)
                else:
                    u = User(name=name, email=email, group_id=gid, is_admin=False)
                    db.add(u)
                    await db.flush()
                    uids.append(u.id)
            group_to_uids[gid] = uids
        await db.commit()
    total = sum(len(v) for v in group_to_uids.values())
    print(f"Created {total} fake users across {len(group_to_uids)} groups")
    return group_to_uids


async def create_session() -> int:
    """Create a session with overlap 10% and 60 s refresh; return session id."""
    async with SessionLocal() as db:
        # Check for existing active session
        existing = (await db.execute(
            select(ReviewSession).where(ReviewSession.ended_at.is_(None))
        )).scalar_one_or_none()
        if existing:
            print(f"Active session already exists (id={existing.id}), reusing")
            return existing.id

        groups   = (await db.execute(select(Group))).scalars().all()
        stories  = (await db.execute(select(UserStory))).scalars().all()
        story_ids = [s.id for s in stories]
        n, g = len(story_ids), len(groups)
        stories_per_group = round(n / g) + round(n * OVERLAP_PCT / g) if g else 0

        await db.execute(delete(GroupAssignment))
        assignments = _assign_stories(story_ids, g, OVERLAP_PCT, RANDOM_SEED)
        for g_idx, group in enumerate(groups):
            for pos, sid in enumerate(assignments[g_idx]):
                db.add(GroupAssignment(group_id=group.id, story_id=sid, position=pos))

        # Use first admin user as session owner
        admin = (await db.execute(
            select(User).where(User.is_admin == True)
        )).scalar_one_or_none()

        sess = ReviewSession(
            started_by=admin.id if admin else None,
            overlap_pct=OVERLAP_PCT,
            stories_per_group=stories_per_group,
            chart_refresh_secs=CHART_REFRESH_SECS,
        )
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        print(f"Created session id={sess.id} | overlap={OVERLAP_PCT} | "
              f"refresh={CHART_REFRESH_SECS}s | {n} stories -> {g} groups "
              f"(~{stories_per_group} each)")
        return sess.id


async def run_labels(session_id: int, phase: int):
    """Generate labels for 50% (phase=1) or remaining 50% (phase=2)."""
    async with SessionLocal() as db:
        groups     = (await db.execute(select(Group))).scalars().all()
        taxonomy   = (await db.execute(select(TaxonomyLabel))).scalars().all()
        all_stories = {s.id: s for s in (await db.execute(select(UserStory))).scalars().all()}

        assigner   = LabelAssigner(list(taxonomy))
        total_added = 0

        for group in groups:
            gid = group.id
            # Stories assigned to this group, in position order
            assigned = (await db.execute(
                select(GroupAssignment.story_id)
                .where(GroupAssignment.group_id == gid)
                .order_by(GroupAssignment.position)
            )).scalars().all()

            # Which stories already have labels from this group?
            labelled_sids = set((await db.execute(
                select(AddedLabel.story_id)
                .join(User, AddedLabel.user_id == User.id)
                .where(User.group_id == gid, AddedLabel.session_id == session_id)
            )).scalars().all())

            # Phase 1: first half; Phase 2: second half
            half = len(assigned) // 2
            if phase == 1:
                target_sids = [s for s in assigned[:half] if s not in labelled_sids]
            else:
                target_sids = [s for s in assigned[half:] if s not in labelled_sids]

            # Fake user IDs for this group
            fake_uids = (await db.execute(
                select(User.id)
                .where(User.group_id == gid, User.email.like("%@simulation.fake"))
            )).scalars().all()
            if not fake_uids:
                print(f"  WARNING: no fake users for group {gid}, skipping")
                continue

            for story_id in target_sids:
                story = all_stories.get(story_id)
                if not story:
                    continue

                result = assigner.assign(story)
                actor_uid = SIM_RNG.choice(fake_uids)

                # Mandatory classification
                existing_mc = (await db.execute(
                    select(MandatoryClassification).where(
                        MandatoryClassification.session_id == session_id,
                        MandatoryClassification.group_id  == gid,
                        MandatoryClassification.story_id  == story_id,
                    )
                )).scalar_one_or_none()
                if not existing_mc:
                    db.add(MandatoryClassification(
                        session_id  = session_id,
                        group_id    = gid,
                        story_id    = story_id,
                        target_user = result["target_user"],
                        concepts    = result["concepts"],
                        user_id     = actor_uid,
                    ))

                # Technical labels (each added by potentially a different user)
                for tax_id in result["tech_label_ids"]:
                    labeller = SIM_RNG.choice(fake_uids)
                    db.add(AddedLabel(
                        session_id      = session_id,
                        story_id        = story_id,
                        user_id         = labeller,
                        taxonomy_label_id = tax_id,
                    ))

                # Rejection
                if result["rejected"]:
                    existing_rej = (await db.execute(
                        select(StoryRejection).where(
                            StoryRejection.session_id == session_id,
                            StoryRejection.group_id   == gid,
                            StoryRejection.story_id   == story_id,
                        )
                    )).scalar_one_or_none()
                    if not existing_rej:
                        db.add(StoryRejection(
                            session_id = session_id,
                            group_id   = gid,
                            story_id   = story_id,
                            rejected   = True,
                            reason     = result["rejection_reason"],
                            user_id    = actor_uid,
                        ))

                # Relevance
                if result["relevance_score"] != "Normal":
                    existing_rel = (await db.execute(
                        select(StoryRelevance).where(
                            StoryRelevance.session_id == session_id,
                            StoryRelevance.group_id   == gid,
                            StoryRelevance.story_id   == story_id,
                        )
                    )).scalar_one_or_none()
                    if not existing_rel:
                        db.add(StoryRelevance(
                            session_id = session_id,
                            group_id   = gid,
                            story_id   = story_id,
                            score      = result["relevance_score"],
                            reason     = result["relevance_reason"],
                            user_id    = actor_uid,
                        ))

                total_added += len(result["tech_label_ids"])

            await db.commit()
            phase_done = len(target_sids)
            print(f"  Group {gid:2d} ({group.name[:30]:<30}): "
                  f"{phase_done} stories labelled this phase")

    print(f"\nPhase {phase} complete — {total_added} label records added")


async def add_peer_reviews(session_id: int, pct: float = 0.05):
    """
    For each group, take pct% of stories that already have AddedLabel records
    from phase 1, then have a different fake user in the same group confirm/reject
    each label on those stories (80% confirm, 20% reject).
    """
    async with SessionLocal() as db:
        groups = (await db.execute(select(Group))).scalars().all()
        total_decisions = 0

        for group in groups:
            gid = group.id

            # Fake users in this group
            fake_uids = (await db.execute(
                select(User.id)
                .where(User.group_id == gid, User.email.like("%@simulation.fake"))
            )).scalars().all()
            if len(fake_uids) < 2:
                continue

            # Stories with labels from this group in this session
            story_ids = list(dict.fromkeys((await db.execute(
                select(AddedLabel.story_id)
                .join(User, AddedLabel.user_id == User.id)
                .where(User.group_id == gid, AddedLabel.session_id == session_id)
                .order_by(AddedLabel.story_id)
            )).scalars().all()))

            n_review = max(1, round(len(story_ids) * pct))
            review_sids = SIM_RNG.sample(story_ids, min(n_review, len(story_ids)))

            for sid in review_sids:
                # Labels added by any fake user in this group for this story
                al_rows = (await db.execute(
                    select(AddedLabel)
                    .join(User, AddedLabel.user_id == User.id)
                    .where(
                        User.group_id == gid,
                        AddedLabel.session_id == session_id,
                        AddedLabel.story_id == sid,
                    )
                )).scalars().all()

                for al in al_rows:
                    # Reviewer must be different from the author
                    reviewer_pool = [u for u in fake_uids if u != al.user_id]
                    if not reviewer_pool:
                        continue
                    reviewer_uid = SIM_RNG.choice(reviewer_pool)
                    decision = "confirm" if SIM_RNG.random() < 0.80 else "reject"

                    existing = (await db.execute(
                        select(AddedLabelDecision).where(
                            AddedLabelDecision.session_id == session_id,
                            AddedLabelDecision.user_id == reviewer_uid,
                            AddedLabelDecision.added_label_id == al.id,
                        )
                    )).scalar_one_or_none()
                    if not existing:
                        db.add(AddedLabelDecision(
                            session_id=session_id,
                            user_id=reviewer_uid,
                            added_label_id=al.id,
                            decision=decision,
                        ))
                        total_decisions += 1

            await db.commit()
            print(f"  Group {gid:2d} ({group.name[:30]:<30}): "
                  f"reviewed {len(review_sids)} stories")

    print(f"\nPeer reviews complete — {total_decisions} decision records added")


async def reset_db():
    """Clear all session data and restore real users from backup."""
    async with SessionLocal() as db:
        for model in [AddedLabelDecision, AddedLabel, MandatoryClassification,
                      StoryRejection, StoryRelevance, ReviewSession]:
            await db.execute(delete(model))

        # Remove fake users
        fake = (await db.execute(
            select(User).where(User.email.like("%@simulation.fake"))
        )).scalars().all()
        for u in fake:
            await db.delete(u)
        await db.commit()
        print(f"Removed {len(fake)} fake users and all session data")

    if not BACKUP_FILE.exists():
        print("No backup file found — real users not restored")
        return

    data = json.loads(BACKUP_FILE.read_text())
    async with SessionLocal() as db:
        restored = 0
        for u in data:
            existing = (await db.execute(
                select(User).where(User.email == u["email"])
            )).scalar_one_or_none()
            if not existing:
                db.add(User(
                    name=u["name"], email=u["email"],
                    group_id=u["group_id"], is_admin=u["is_admin"],
                ))
                restored += 1
            else:
                existing.group_id = u["group_id"]
                existing.is_admin = u["is_admin"]
        await db.commit()
    print(f"Restored {restored} real users from {BACKUP_FILE}")


async def status():
    async with SessionLocal() as db:
        users    = (await db.execute(select(func.count(User.id)))).scalar()
        fake     = (await db.execute(
            select(func.count(User.id)).where(User.email.like("%@simulation.fake"))
        )).scalar()
        sessions = (await db.execute(select(ReviewSession))).scalars().all()
        labels   = (await db.execute(select(func.count(AddedLabel.id)))).scalar()
        mcs      = (await db.execute(select(func.count(MandatoryClassification.id)))).scalar()
        rejs     = (await db.execute(select(func.count(StoryRejection.id)))).scalar()
        decs     = (await db.execute(select(func.count(AddedLabelDecision.id)))).scalar()

    print(f"Users:          {users} total ({fake} fake)")
    print(f"Sessions:       {len(sessions)}")
    for s in sessions:
        print(f"  id={s.id} started={s.started_at} ended={s.ended_at}")
    print(f"AddedLabels:    {labels}")
    print(f"MandatoryClass: {mcs}")
    print(f"Rejections:     {rejs}")
    print(f"PeerReviews:    {decs}")


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser(description="RIECS session simulation")
    parser.add_argument("--save-users", action="store_true", help="Backup real users")
    parser.add_argument("--phase", type=int, choices=[1, 2],
                        help="Run labelling phase 1 (50%%) or phase 2 (remaining 50%%)")
    parser.add_argument("--peer-reviews", action="store_true",
                        help="Add peer review decisions (5%% of stories per group)")
    parser.add_argument("--mask-users", action="store_true",
                        help="Replace real user names/emails with placeholders (for screenshots)")
    parser.add_argument("--reset", action="store_true", help="Clear simulation data")
    parser.add_argument("--status", action="store_true", help="Show DB state")
    args = parser.parse_args()

    if args.save_users:
        await save_real_users()

    elif args.phase == 1:
        await save_real_users()
        await create_fake_users()
        session_id = await create_session()
        await run_labels(session_id, phase=1)

    elif args.phase == 2:
        async with SessionLocal() as db:
            sess = (await db.execute(
                select(ReviewSession).order_by(ReviewSession.started_at.desc())
            )).scalar_one_or_none()
        if not sess:
            print("No session found — run --phase 1 first")
            sys.exit(1)
        await run_labels(sess.id, phase=2)

    elif args.peer_reviews:
        async with SessionLocal() as db:
            sess = (await db.execute(
                select(ReviewSession).order_by(ReviewSession.started_at.desc())
            )).scalar_one_or_none()
        if not sess:
            print("No session found — run --phase 1 first")
            sys.exit(1)
        await add_peer_reviews(sess.id)

    elif args.mask_users:
        await mask_real_users()

    elif args.reset:
        await reset_db()

    elif args.status:
        await status()

    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
