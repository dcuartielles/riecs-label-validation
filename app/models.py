import json
from datetime import datetime
from sqlalchemy import (
    Integer, String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint, Float
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)

    users: Mapped[list["User"]] = relationship(back_populates="group")
    assignments: Mapped[list["GroupAssignment"]] = relationship(back_populates="group")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255))
    google_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    group: Mapped["Group | None"] = relationship(back_populates="users")
    added_labels: Mapped[list["AddedLabel"]] = relationship(back_populates="user")


class UserStory(Base):
    __tablename__ = "user_stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    workshop: Mapped[str | None] = mapped_column(Text)
    submitted_by: Mapped[str | None] = mapped_column(String(255))
    stakeholder_group: Mapped[str | None] = mapped_column(String(255))
    user_type: Mapped[str | None] = mapped_column(String(255))
    task: Mapped[str | None] = mapped_column(Text)
    goal: Mapped[str | None] = mapped_column(Text)
    additional_notes: Mapped[str | None] = mapped_column(Text)

    labels: Mapped[list["StoryLabel"]] = relationship(back_populates="story")
    assignments: Mapped[list["GroupAssignment"]] = relationship(back_populates="story")
    added_labels: Mapped[list["AddedLabel"]] = relationship(back_populates="story")


class StoryLabel(Base):
    """Imported AI/Human labels — kept for reference, not used in v007 review flow."""
    __tablename__ = "story_labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("user_stories.id"))
    source: Mapped[str] = mapped_column(String(10))
    label_text: Mapped[str] = mapped_column(Text, nullable=False)
    label_index: Mapped[int] = mapped_column(Integer)
    label_status: Mapped[str | None] = mapped_column(String(20))

    story: Mapped["UserStory"] = relationship(back_populates="labels")


class TaxonomyLabel(Base):
    __tablename__ = "taxonomy_labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    sublabel: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    is_user_created: Mapped[bool] = mapped_column(Boolean, default=False)

    added_labels: Mapped[list["AddedLabel"]] = relationship(back_populates="taxonomy_label")


class GroupAssignment(Base):
    __tablename__ = "group_assignments"
    __table_args__ = (UniqueConstraint("group_id", "story_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    story_id: Mapped[int] = mapped_column(ForeignKey("user_stories.id"))
    position: Mapped[int] = mapped_column(Integer)

    group: Mapped["Group"] = relationship(back_populates="assignments")
    story: Mapped["UserStory"] = relationship(back_populates="assignments")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    overlap_pct: Mapped[float] = mapped_column(Float, default=0.0)
    stories_per_group: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chart_refresh_secs: Mapped[int] = mapped_column(Integer, default=300)

    started_by_user: Mapped["User | None"] = relationship(foreign_keys=[started_by])
    added_labels: Mapped[list["AddedLabel"]] = relationship(back_populates="session")
    mandatory_classifications: Mapped[list["MandatoryClassification"]] = relationship(back_populates="session")
    story_rejections: Mapped[list["StoryRejection"]] = relationship(back_populates="session")
    story_relevances: Mapped[list["StoryRelevance"]] = relationship(back_populates="session")


class AddedLabel(Base):
    __tablename__ = "added_labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    story_id: Mapped[int] = mapped_column(ForeignKey("user_stories.id"))
    taxonomy_label_id: Mapped[int | None] = mapped_column(ForeignKey("taxonomy_labels.id"))
    free_text: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["Session"] = relationship(back_populates="added_labels")
    user: Mapped["User"] = relationship(back_populates="added_labels")
    story: Mapped["UserStory"] = relationship(back_populates="added_labels")
    taxonomy_label: Mapped["TaxonomyLabel | None"] = relationship(back_populates="added_labels")
    decisions: Mapped[list["AddedLabelDecision"]] = relationship(back_populates="added_label")


class AddedLabelDecision(Base):
    """Confirm or reject a label added by a teammate (same group, same session)."""
    __tablename__ = "added_label_decisions"
    __table_args__ = (UniqueConstraint("session_id", "user_id", "added_label_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    added_label_id: Mapped[int] = mapped_column(ForeignKey("added_labels.id"))
    decision: Mapped[str] = mapped_column(String(10))  # 'confirm' or 'reject'
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    added_label: Mapped["AddedLabel"] = relationship(back_populates="decisions")


class MandatoryClassification(Base):
    """Per-institution mandatory classification: target user (one) + concepts (many)."""
    __tablename__ = "mandatory_classifications"
    __table_args__ = (UniqueConstraint("session_id", "group_id", "story_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    story_id: Mapped[int] = mapped_column(ForeignKey("user_stories.id"))
    target_user: Mapped[str | None] = mapped_column(Text)
    concepts_json: Mapped[str | None] = mapped_column(Text)  # JSON list of strings
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["Session"] = relationship(back_populates="mandatory_classifications")

    @property
    def concepts(self) -> list[str]:
        if not self.concepts_json:
            return []
        return json.loads(self.concepts_json)

    @concepts.setter
    def concepts(self, value: list[str]):
        self.concepts_json = json.dumps(value)


class StoryRejection(Base):
    """Per-institution flag to mark a story as out of scope."""
    __tablename__ = "story_rejections"
    __table_args__ = (UniqueConstraint("session_id", "group_id", "story_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    story_id: Mapped[int] = mapped_column(ForeignKey("user_stories.id"))
    rejected: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["Session"] = relationship(back_populates="story_rejections")


class StoryRelevance(Base):
    """Per-institution relevance rating for a story."""
    __tablename__ = "story_relevances"
    __table_args__ = (UniqueConstraint("session_id", "group_id", "story_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    story_id: Mapped[int] = mapped_column(ForeignKey("user_stories.id"))
    score: Mapped[str] = mapped_column(String(20), default="Normal")  # Normal, High, VeryHigh
    reason: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["Session"] = relationship(back_populates="story_relevances")
