from app.models.user import TimestampMixin, utcnow
from extensions import db


class StudyPlan(TimestampMixin, db.Model):
    __tablename__ = "study_plans"
    __table_args__ = (
        db.UniqueConstraint("user_id", "version_number", name="uq_plan_user_version"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    syllabus_version_id = db.Column(
        db.Integer, db.ForeignKey("syllabus_versions.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    status = db.Column(db.String(20), nullable=False, default="active", index=True)
    version_number = db.Column(db.Integer, nullable=False, default=1)
    feasible = db.Column(db.Boolean, nullable=False, default=True)
    warnings = db.Column(db.JSON, nullable=False, default=list)
    parameters = db.Column(db.JSON, nullable=False, default=dict)
    horizon_start = db.Column(db.Date, nullable=False)
    horizon_end = db.Column(db.Date, nullable=False)
    generated_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship("User", back_populates="plans")
    syllabus_version = db.relationship("SyllabusVersion", back_populates="plans")
    tasks = db.relationship(
        "StudyTask", back_populates="plan", cascade="all, delete-orphan",
        order_by="StudyTask.scheduled_date, StudyTask.start_time",
    )


class StudyTask(TimestampMixin, db.Model):
    __tablename__ = "study_tasks"
    __table_args__ = (
        db.CheckConstraint("estimated_minutes > 0", name="ck_task_estimated_positive"),
        db.Index("ix_task_user_date_status", "user_id", "scheduled_date", "status"),
        db.Index("ix_task_user_chapter_type", "user_id", "syllabus_chapter_id", "task_type"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    plan_id = db.Column(
        db.Integer, db.ForeignKey("study_plans.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    syllabus_chapter_id = db.Column(
        db.Integer, db.ForeignKey("syllabus_chapters.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    syllabus_topic_id = db.Column(
        db.Integer, db.ForeignKey("syllabus_topics.id", ondelete="SET NULL"),
        nullable=True,
    )
    parent_task_id = db.Column(
        db.Integer, db.ForeignKey("study_tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    task_type = db.Column(db.String(20), nullable=False, default="learning", index=True)
    title = db.Column(db.String(250), nullable=False)
    original_scheduled_date = db.Column(db.Date, nullable=False)
    scheduled_date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.Time)
    end_time = db.Column(db.Time)
    estimated_minutes = db.Column(db.Integer, nullable=False, default=45)
    actual_minutes = db.Column(db.Integer)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    priority = db.Column(db.Integer, nullable=False, default=50)
    revision_cycle = db.Column(db.Integer, nullable=False, default=0)
    notes = db.Column(db.Text)
    completed_at = db.Column(db.DateTime)

    user = db.relationship("User", back_populates="tasks")
    plan = db.relationship("StudyPlan", back_populates="tasks")
    syllabus_chapter = db.relationship("SyllabusChapter", back_populates="tasks")
    syllabus_topic = db.relationship("SyllabusTopic", back_populates="tasks")
    events = db.relationship(
        "TaskEvent", back_populates="task", cascade="all, delete-orphan",
        order_by="TaskEvent.created_at",
    )

    @property
    def is_complete(self) -> bool:
        return self.status == "completed"

    @property
    def is_open(self) -> bool:
        return self.status in {"pending", "in_progress", "postponed", "overdue"}

    @property
    def is_overdue(self) -> bool:
        from datetime import date

        return self.is_open and self.scheduled_date < date.today()

    @property
    def time_label(self) -> str:
        if self.start_time and self.end_time:
            return f"{self.start_time.strftime('%H:%M')}–{self.end_time.strftime('%H:%M')}"
        return f"{self.estimated_minutes} min"


class TaskEvent(db.Model):
    __tablename__ = "task_events"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(
        db.Integer, db.ForeignKey("study_tasks.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    event_type = db.Column(db.String(30), nullable=False, index=True)
    from_status = db.Column(db.String(20))
    to_status = db.Column(db.String(20))
    old_date = db.Column(db.Date)
    new_date = db.Column(db.Date)
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    task = db.relationship("StudyTask", back_populates="events")


class StudySession(db.Model):
    __tablename__ = "study_sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    task_id = db.Column(
        db.Integer, db.ForeignKey("study_tasks.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    started_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    ended_at = db.Column(db.DateTime)
    duration_minutes = db.Column(db.Integer)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class RevisionRecord(TimestampMixin, db.Model):
    __tablename__ = "revision_records"
    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "syllabus_chapter_id", "revision_number",
            name="uq_revision_user_chapter_cycle",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    syllabus_chapter_id = db.Column(
        db.Integer, db.ForeignKey("syllabus_chapters.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    revision_number = db.Column(db.Integer, nullable=False, default=1)
    scheduled_date = db.Column(db.Date, nullable=False)
    completed_date = db.Column(db.Date)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    notes = db.Column(db.Text)


class MockTest(TimestampMixin, db.Model):
    __tablename__ = "mock_tests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    task_id = db.Column(
        db.Integer, db.ForeignKey("study_tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    syllabus_subject_id = db.Column(
        db.Integer, db.ForeignKey("syllabus_subjects.id", ondelete="SET NULL"),
        nullable=True,
    )
    title = db.Column(db.String(180), nullable=False)
    scheduled_date = db.Column(db.Date, nullable=False, index=True)
    completed_date = db.Column(db.Date)
    duration_minutes = db.Column(db.Integer, nullable=False, default=120)
    score = db.Column(db.Float)
    max_score = db.Column(db.Float)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    notes = db.Column(db.Text)

    task = db.relationship("StudyTask")
    syllabus_subject = db.relationship("SyllabusSubject")
