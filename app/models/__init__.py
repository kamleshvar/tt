from app.models.progress import AdminAuditLog, StudentActivityLog, SystemSetting
from app.models.syllabus import (
    StudentChapterPlan,
    SyllabusChapter,
    SyllabusSubject,
    SyllabusTopic,
    SyllabusUnit,
    SyllabusVersion,
)
from app.models.timetable import (
    MockTest,
    RevisionRecord,
    StudyPlan,
    StudySession,
    StudyTask,
    TaskEvent,
)
from app.models.user import (
    ExamDate,
    StudentPreferences,
    StudentProfile,
    StudentSubject,
    User,
    WeeklyCommitment,
    utcnow,
)

TASK_STATUSES = {
    "pending",
    "in_progress",
    "completed",
    "postponed",
    "overdue",
    "cancelled",
}
OPEN_TASK_STATUSES = {"pending", "in_progress", "postponed", "overdue"}
TASK_TYPES = {"learning", "practice", "revision", "mock", "buffer", "other"}

__all__ = [
    "User",
    "StudentProfile",
    "StudentPreferences",
    "StudentSubject",
    "WeeklyCommitment",
    "ExamDate",
    "SyllabusVersion",
    "SyllabusSubject",
    "SyllabusUnit",
    "SyllabusChapter",
    "SyllabusTopic",
    "StudentChapterPlan",
    "StudyPlan",
    "StudyTask",
    "TaskEvent",
    "StudySession",
    "RevisionRecord",
    "MockTest",
    "StudentActivityLog",
    "AdminAuditLog",
    "SystemSetting",
    "TASK_STATUSES",
    "OPEN_TASK_STATUSES",
    "TASK_TYPES",
    "utcnow",
]
