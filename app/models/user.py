from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TimestampMixin:
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class User(UserMixin, TimestampMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    auth_version = db.Column(db.Integer, nullable=False, default=0)
    last_login_at = db.Column(db.DateTime)

    profile = db.relationship(
        "StudentProfile", back_populates="user", uselist=False,
        cascade="all, delete-orphan",
    )
    preferences = db.relationship(
        "StudentPreferences", back_populates="user", uselist=False,
        cascade="all, delete-orphan",
    )
    subjects = db.relationship(
        "StudentSubject", back_populates="user", cascade="all, delete-orphan",
    )
    commitments = db.relationship(
        "WeeklyCommitment", back_populates="user", cascade="all, delete-orphan",
    )
    plans = db.relationship("StudyPlan", back_populates="user")
    tasks = db.relationship("StudyTask", back_populates="user")
    chapter_plans = db.relationship(
        "StudentChapterPlan", back_populates="user", cascade="all, delete-orphan",
    )
    activity_logs = db.relationship("StudentActivityLog", back_populates="user")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password, method="scrypt")

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def onboarding_complete(self) -> bool:
        return bool(self.profile and self.profile.onboarding_completed)

    @property
    def board_exam_date(self):
        return self.profile.exam_date if self.profile else None

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class StudentProfile(TimestampMixin, db.Model):
    __tablename__ = "student_profiles"
    __table_args__ = (
        db.CheckConstraint("class_level = 'Class 12'", name="ck_profile_class_12"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    class_level = db.Column(db.String(20), nullable=False, default="Class 12")
    board = db.Column(db.String(80), nullable=False, default="")
    academic_year = db.Column(db.String(20), nullable=False, default="")
    exam_date = db.Column(db.Date)
    onboarding_completed = db.Column(db.Boolean, nullable=False, default=False)
    onboarding_step = db.Column(db.Integer, nullable=False, default=1)
    plan_generated_at = db.Column(db.DateTime)

    user = db.relationship("User", back_populates="profile")


class StudentPreferences(TimestampMixin, db.Model):
    __tablename__ = "student_preferences"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    wake_time = db.Column(db.Time, nullable=False, default=lambda: datetime.strptime("06:30", "%H:%M").time())
    sleep_time = db.Column(db.Time, nullable=False, default=lambda: datetime.strptime("22:30", "%H:%M").time())
    preferred_study_start = db.Column(
        db.Time, nullable=False, default=lambda: datetime.strptime("17:00", "%H:%M").time()
    )
    preferred_study_end = db.Column(
        db.Time, nullable=False, default=lambda: datetime.strptime("22:00", "%H:%M").time()
    )
    session_duration_minutes = db.Column(db.Integer, nullable=False, default=45)
    break_duration_minutes = db.Column(db.Integer, nullable=False, default=10)
    break_frequency_sessions = db.Column(db.Integer, nullable=False, default=1)
    daily_cap_minutes = db.Column(db.Integer, nullable=False, default=240)
    revision_frequency = db.Column(db.Integer, nullable=False, default=2)
    lighter_days = db.Column(db.JSON, nullable=False, default=list)
    struggle_notes = db.Column(db.Text)

    user = db.relationship("User", back_populates="preferences")


class WeeklyCommitment(TimestampMixin, db.Model):
    __tablename__ = "weekly_commitments"
    __table_args__ = (
        db.CheckConstraint("day_of_week >= 0 AND day_of_week <= 6", name="ck_commitment_day"),
        db.CheckConstraint("end_time > start_time", name="ck_commitment_time"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    day_of_week = db.Column(db.Integer, nullable=False, index=True)
    commitment_type = db.Column(db.String(20), nullable=False, default="other")
    title = db.Column(db.String(120), nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    travel_before_minutes = db.Column(db.Integer, nullable=False, default=0)
    travel_after_minutes = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)

    user = db.relationship("User", back_populates="commitments")


class StudentSubject(TimestampMixin, db.Model):
    __tablename__ = "student_subjects"
    __table_args__ = (
        db.UniqueConstraint("user_id", "syllabus_subject_id", name="uq_student_subject"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    syllabus_subject_id = db.Column(
        db.Integer, db.ForeignKey("syllabus_subjects.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    confidence = db.Column(db.Integer, nullable=False, default=3)
    recent_performance = db.Column(db.String(40))
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    user = db.relationship("User", back_populates="subjects")
    syllabus_subject = db.relationship("SyllabusSubject", back_populates="student_links")


class ExamDate(TimestampMixin, db.Model):
    __tablename__ = "exam_dates"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    title = db.Column(db.String(120), nullable=False)
    exam_type = db.Column(db.String(30), nullable=False, default="board")
    exam_date = db.Column(db.Date, nullable=False, index=True)
    is_primary = db.Column(db.Boolean, nullable=False, default=False)
