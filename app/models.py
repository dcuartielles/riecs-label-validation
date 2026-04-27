from datetime import datetime
from sqlalchemy import (
    Integer, String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)

    users: Mapped[list["User"]] = relationship(back_populates="group")
    assignments: Mapped[list["GroupAssignment"]] = relationship(back_populates="group")
    sessions: Mapped[list["Session"]] = relationship(back_populates="group")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255))
    google_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    group: Mapped["Group | None"] = relationship(back_populates="users")
    decisions: Mapped[list["LabelDecision"]] = relationship(back_populates="user")
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
    decisions: Mapped[list["LabelDecision"]] = relationship(back_populates="story")
    added_labels: Mapped[list["AddedLabel"]] = relationship(back_populates="story")


class StoryLabel(Base):
    """A label that was applied to a user story (from the spreadsheet)."""
    __tablename__ = "story_labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("user_stories.id"))
    source: Mapped[str] = mapped_column(String(10))   # 'AI' or 'Human'
    label_text: Mapped[str] = mapped_column(Text, nullable=False)
    label_index: Mapped[int] = mapped_column(Integer)  # 1-5

    story: Mapped["UserStory"] = relationship(back_populates="labels")
    decisions: Mapped[list["LabelDecision"]] = relationship(back_populates="story_label")


class TaxonomyLabel(Base):
    """Label taxonomy loaded from the labelbook spreadsheet."""
    __tablename__ = "taxonomy_labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    sublabel: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    added_labels: Mapped[list["AddedLabel"]] = relationship(back_populates="taxonomy_label")


class GroupAssignment(Base):
    """Which user stories each group is assigned to review."""
    __tablename__ = "group_assignments"
    __table_args__ = (UniqueConstraint("group_id", "story_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    story_id: Mapped[int] = mapped_column(ForeignKey("user_stories.id"))
    position: Mapped[int] = mapped_column(Integer)  # order within the group's subset

    group: Mapped["Group"] = relationship(back_populates="assignments")
    story: Mapped["UserStory"] = relationship(back_populates="assignments")


class Session(Base):
    """A review session for a group."""
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)

    group: Mapped["Group"] = relationship(back_populates="sessions")
    decisions: Mapped[list["LabelDecision"]] = relationship(back_populates="session")
    added_labels: Mapped[list["AddedLabel"]] = relationship(back_populates="session")


class LabelDecision(Base):
    """A researcher's decision on an existing label."""
    __tablename__ = "label_decisions"
    __table_args__ = (UniqueConstraint("session_id", "user_id", "story_label_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    story_id: Mapped[int] = mapped_column(ForeignKey("user_stories.id"))
    story_label_id: Mapped[int] = mapped_column(ForeignKey("story_labels.id"))
    decision: Mapped[str] = mapped_column(String(10))  # 'confirm', 'reject', 'abstain'
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["Session"] = relationship(back_populates="decisions")
    user: Mapped["User"] = relationship(back_populates="decisions")
    story: Mapped["UserStory"] = relationship(back_populates="decisions")
    story_label: Mapped["StoryLabel"] = relationship(back_populates="decisions")


class AddedLabel(Base):
    """A new label added by researchers during review."""
    __tablename__ = "added_labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    story_id: Mapped[int] = mapped_column(ForeignKey("user_stories.id"))
    taxonomy_label_id: Mapped[int | None] = mapped_column(ForeignKey("taxonomy_labels.id"))
    free_text: Mapped[str | None] = mapped_column(Text)  # if not from taxonomy
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["Session"] = relationship(back_populates="added_labels")
    user: Mapped["User"] = relationship(back_populates="added_labels")
    story: Mapped["UserStory"] = relationship(back_populates="added_labels")
    taxonomy_label: Mapped["TaxonomyLabel | None"] = relationship(back_populates="added_labels")
