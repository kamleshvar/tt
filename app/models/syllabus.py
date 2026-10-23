from app.models.user import TimestampMixin, utcnow
from extensions import db


class SyllabusVersion(TimestampMixin, db.Model):
    __tablename__ = "syllabus_versions"
    __table_args__ = (
        db.UniqueConstraint(
            "board", "academic_year", "version_label", name="uq_syllabus_version"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    board = db.Column(db.String(80), nullable=False, index=True)
    academic_year = db.Column(db.String(20), nullable=False, index=True)
    version_label = db.Column(db.String(60), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft", index=True)
    source_file = db.Column(db.String(255))
    checksum = db.Column(db.String(64))
    notes = db.Column(db.Text)
    validated_at = db.Column(db.DateTime)
    imported_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    is_current = db.Column(db.Boolean, nullable=False, default=False, index=True)
    research_status = db.Column(db.String(40), nullable=False, default="unverified")
    research_sources = db.Column(db.JSON, nullable=False, default=list)
    last_researched_at = db.Column(db.Date)

    subjects = db.relationship(
        "SyllabusSubject", back_populates="version", cascade="all, delete-orphan",
        order_by="SyllabusSubject.order_index",
    )
    plans = db.relationship("StudyPlan", back_populates="syllabus_version")

    @property
    def chapter_count(self) -> int:
        return sum(
            1
            for subject in self.subjects
            if not subject.is_removed
            for unit in subject.units
            for chapter in unit.chapters
            if not chapter.is_removed
        )

    @property
    def topic_count(self) -> int:
        return sum(
            1
            for subject in self.subjects
            if not subject.is_removed
            for unit in subject.units
            for chapter in unit.chapters
            if not chapter.is_removed
            for topic in chapter.topics
            if not topic.is_removed
        )

    @property
    def status_label(self) -> str:
        return {"draft": "Draft — unverified", "validated": "Validated", "archived": "Archived"}.get(
            self.status, self.status.title()
        )


class SyllabusSubject(TimestampMixin, db.Model):
    __tablename__ = "syllabus_subjects"
    __table_args__ = (
        db.UniqueConstraint("version_id", "code", name="uq_syllabus_subject_code"),
    )

    id = db.Column(db.Integer, primary_key=True)
    version_id = db.Column(
        db.Integer, db.ForeignKey("syllabus_versions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name = db.Column(db.String(120), nullable=False)
    code = db.Column(db.String(30), nullable=False)
    order_index = db.Column(db.Integer, nullable=False, default=0)
    is_removed = db.Column(db.Boolean, nullable=False, default=False)

    version = db.relationship("SyllabusVersion", back_populates="subjects")
    units = db.relationship(
        "SyllabusUnit", back_populates="subject", cascade="all, delete-orphan",
        order_by="SyllabusUnit.order_index",
    )
    student_links = db.relationship("StudentSubject", back_populates="syllabus_subject")


class SyllabusUnit(TimestampMixin, db.Model):
    __tablename__ = "syllabus_units"
    __table_args__ = (
        db.UniqueConstraint("subject_id", "name", name="uq_syllabus_unit_name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(
        db.Integer, db.ForeignKey("syllabus_subjects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name = db.Column(db.String(180), nullable=False)
    order_index = db.Column(db.Integer, nullable=False, default=0)

    subject = db.relationship("SyllabusSubject", back_populates="units")
    chapters = db.relationship(
        "SyllabusChapter", back_populates="unit", cascade="all, delete-orphan",
        order_by="SyllabusChapter.order_index",
    )


class SyllabusChapter(TimestampMixin, db.Model):
    __tablename__ = "syllabus_chapters"
    __table_args__ = (
        db.UniqueConstraint("unit_id", "canonical_key", name="uq_chapter_canonical"),
        db.Index("ix_chapter_unit_order", "unit_id", "order_index"),
    )

    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(
        db.Integer, db.ForeignKey("syllabus_units.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name = db.Column(db.String(220), nullable=False)
    canonical_key = db.Column(db.String(120), nullable=False)
    order_index = db.Column(db.Integer, nullable=False, default=0)
    estimated_minutes = db.Column(db.Integer, nullable=False, default=180)
    weightage_marks = db.Column(db.Float)
    is_optional = db.Column(db.Boolean, nullable=False, default=False)
    is_removed = db.Column(db.Boolean, nullable=False, default=False, index=True)
    learning_minutes = db.Column(db.Integer, nullable=False, default=0)
    practice_minutes = db.Column(db.Integer, nullable=False, default=0)
    revision_minutes = db.Column(db.Integer, nullable=False, default=0)
    difficulty = db.Column(db.String(20), nullable=False, default="medium")
    estimate_confidence = db.Column(db.String(20), nullable=False, default="medium")
    estimate_method = db.Column(db.String(80))
    research_status = db.Column(db.String(40), nullable=False, default="unverified")
    research_sources = db.Column(db.JSON, nullable=False, default=list)
    research_notes = db.Column(db.Text)
    last_researched_at = db.Column(db.Date)
    estimate_review_note = db.Column(db.Text)
    estimate_updated_at = db.Column(db.DateTime)

    unit = db.relationship("SyllabusUnit", back_populates="chapters")
    student_overrides = db.relationship(
        "StudentChapterPlan", back_populates="chapter", cascade="all, delete-orphan",
    )
    topics = db.relationship(
        "SyllabusTopic", back_populates="chapter", cascade="all, delete-orphan",
        order_by="SyllabusTopic.order_index",
    )
    tasks = db.relationship("StudyTask", back_populates="syllabus_chapter")

    @property
    def subject(self):
        return self.unit.subject if self.unit else None

    @property
    def total_minutes(self) -> int:
        stages = (self.learning_minutes or 0) + (self.practice_minutes or 0)
        stages += self.revision_minutes or 0
        return stages or self.estimated_minutes or 0

    @property
    def total_hours(self) -> float:
        return round(self.total_minutes / 60, 2)

    @property
    def learning_hours(self) -> float:
        return round((self.learning_minutes or 0) / 60, 2)

    @property
    def practice_hours(self) -> float:
        return round((self.practice_minutes or 0) / 60, 2)

    @property
    def revision_hours(self) -> float:
        return round((self.revision_minutes or 0) / 60, 2)

    @property
    def has_stage_workload(self) -> bool:
        return bool(
            (self.learning_minutes or 0)
            + (self.practice_minutes or 0)
            + (self.revision_minutes or 0)
        )

    @property
    def full_label(self) -> str:
        subject = self.subject
        return f"{subject.name} · {self.name}" if subject else self.name


class StudentChapterPlan(TimestampMixin, db.Model):
    __tablename__ = "student_chapter_plans"
    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "syllabus_chapter_id", name="uq_student_chapter_plan"
        ),
        db.CheckConstraint(
            "confidence IS NULL OR (confidence >= 1 AND confidence <= 5)",
            name="ck_student_chapter_confidence",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    syllabus_chapter_id = db.Column(
        db.Integer, db.ForeignKey("syllabus_chapters.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    confidence = db.Column(db.Integer)
    study_mode = db.Column(db.String(20), nullable=False, default="first_time")
    note = db.Column(db.String(255))

    user = db.relationship("User", back_populates="chapter_plans")
    chapter = db.relationship("SyllabusChapter", back_populates="student_overrides")


class SyllabusTopic(TimestampMixin, db.Model):
    __tablename__ = "syllabus_topics"
    __table_args__ = (
        db.UniqueConstraint("chapter_id", "canonical_key", name="uq_topic_canonical"),
    )

    id = db.Column(db.Integer, primary_key=True)
    chapter_id = db.Column(
        db.Integer, db.ForeignKey("syllabus_chapters.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name = db.Column(db.String(220), nullable=False)
    canonical_key = db.Column(db.String(120), nullable=False)
    order_index = db.Column(db.Integer, nullable=False, default=0)
    estimated_minutes = db.Column(db.Integer)
    is_optional = db.Column(db.Boolean, nullable=False, default=False)
    is_removed = db.Column(db.Boolean, nullable=False, default=False, index=True)

    chapter = db.relationship("SyllabusChapter", back_populates="topics")
    tasks = db.relationship("StudyTask", back_populates="syllabus_topic")
