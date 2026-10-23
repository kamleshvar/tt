from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import func

from app.models import StudyTask, WeeklyCommitment
from extensions import db

DAY_MINUTES = 24 * 60
MIN_SESSION_MINUTES = 20
MIN_POOL_SLOT_MINUTES = 15


@dataclass(frozen=True)
class Interval:
    start: int
    end: int

    @property
    def minutes(self) -> int:
        return max(0, self.end - self.start)


@dataclass(frozen=True)
class Slot:
    day: date
    start: int
    end: int
    date_override: date | None = None

    @property
    def minutes(self) -> int:
        return max(0, self.end - self.start)

    @property
    def start_time(self) -> time:
        return minutes_to_time(self.start)

    @property
    def end_time(self) -> time:
        return minutes_to_time(self.end)


def time_to_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def minutes_to_time(value: int) -> time:
    value = max(0, min(DAY_MINUTES - 1, value))
    return time(hour=value // 60, minute=value % 60)


def parse_clock(value: str | None) -> time | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M")
    except ValueError:
        return None
    return parsed.time()


def _subtract(base: list[Interval], blocked: list[Interval]) -> list[Interval]:
    result = list(base)
    for block in blocked:
        if block.end <= block.start:
            continue
        next_result = []
        for current in result:
            if block.end <= current.start or block.start >= current.end:
                next_result.append(current)
                continue
            if block.start > current.start:
                next_result.append(Interval(current.start, min(block.start, current.end)))
            if block.end < current.end:
                next_result.append(Interval(max(block.end, current.start), current.end))
        result = next_result
    return [item for item in result if item.minutes >= MIN_SESSION_MINUTES]


def _intersect(base: list[Interval], other: list[Interval]) -> list[Interval]:
    result = []
    for left in base:
        for right in other:
            start = max(left.start, right.start)
            end = min(left.end, right.end)
            if end - start >= MIN_SESSION_MINUTES:
                result.append(Interval(start, end))
    return sorted(result, key=lambda item: item.start)


def _awake_intervals(wake: time, sleep: time) -> list[Interval]:
    wake_min = time_to_minutes(wake)
    sleep_min = time_to_minutes(sleep)
    if sleep_min > wake_min:
        return [Interval(wake_min, sleep_min)]
    if sleep_min == wake_min:
        return []
    return [Interval(wake_min, DAY_MINUTES), Interval(0, sleep_min)]


def _preferred_intervals(start: time, end: time) -> list[Interval]:
    start_min = time_to_minutes(start)
    end_min = time_to_minutes(end)
    if end_min > start_min:
        return [Interval(start_min, end_min)]
    if end_min == start_min:
        return []
    return [Interval(start_min, DAY_MINUTES), Interval(0, end_min)]


def _commitment_intervals(commitments: list[WeeklyCommitment]) -> list[Interval]:
    blocks = []
    for item in commitments:
        start = time_to_minutes(item.start_time) - max(0, item.travel_before_minutes or 0)
        end = time_to_minutes(item.end_time) + max(0, item.travel_after_minutes or 0)
        blocks.append(Interval(max(0, start), min(DAY_MINUTES, end)))
    return blocks


def sleep_minutes(wake: time, sleep: time) -> int:
    wake_min = time_to_minutes(wake)
    sleep_min = time_to_minutes(sleep)
    if sleep_min >= wake_min:
        return DAY_MINUTES - (sleep_min - wake_min)
    return wake_min - sleep_min


def validate_time_settings(
    *,
    wake: time,
    sleep: time,
    preferred_start: time,
    preferred_end: time,
    session_minutes: int,
    break_minutes: int,
    daily_cap: int,
    revision_frequency: int,
    lighter_days: list[int] | None,
) -> list[str]:
    errors = []
    if sleep_minutes(wake, sleep) < 300:
        errors.append("Sleep must be at least 5 hours. Adjust wake or sleep time.")
    if not _awake_intervals(wake, sleep):
        errors.append("Wake and sleep times cannot be identical.")
    if time_to_minutes(preferred_start) >= time_to_minutes(preferred_end):
        errors.append("Preferred study period must start before it ends.")
    preferred = _preferred_intervals(preferred_start, preferred_end)
    awake = _awake_intervals(wake, sleep)
    if awake and preferred and not _intersect(awake, preferred):
        errors.append("Preferred study period must overlap your waking hours.")
    if not 20 <= session_minutes <= 120:
        errors.append("Study session duration must be between 20 and 120 minutes.")
    if not 5 <= break_minutes <= 60:
        errors.append("Break duration must be between 5 and 60 minutes.")
    if not 30 <= daily_cap <= 600:
        errors.append("Daily study cap must be between 30 and 600 minutes.")
    if not 1 <= revision_frequency <= 3:
        errors.append("Revision frequency must be between 1 and 3 cycles.")
    if lighter_days:
        if any(not isinstance(day, int) or day < 0 or day > 6 for day in lighter_days):
            errors.append("Lighter days must contain valid weekdays.")
    return errors


def _task_blocks(user_id: int, day: date, exclude_task_id: int | None = None) -> list[Interval]:
    query = StudyTask.query.filter(
        StudyTask.user_id == user_id,
        StudyTask.scheduled_date == day,
        StudyTask.status != "cancelled",
        StudyTask.start_time.isnot(None),
        StudyTask.end_time.isnot(None),
    )
    if exclude_task_id is not None:
        query = query.filter(StudyTask.id != exclude_task_id)
    blocks = []
    for task in query.all():
        blocks.append(
            Interval(time_to_minutes(task.start_time), time_to_minutes(task.end_time))
        )
    return blocks


def _scheduled_minutes(user_id: int, day: date, exclude_task_id: int | None = None) -> int:
    query = db.session.query(
        func.coalesce(func.sum(StudyTask.estimated_minutes), 0)
    ).filter(
        StudyTask.user_id == user_id,
        StudyTask.scheduled_date == day,
        StudyTask.status != "cancelled",
    )
    if exclude_task_id is not None:
        query = query.filter(StudyTask.id != exclude_task_id)
    return int(query.scalar() or 0)


def _commitment_blocks(user_id: int, day: date) -> tuple[list[Interval], list[WeeklyCommitment]]:
    commitments = (
        WeeklyCommitment.query.filter_by(
            user_id=user_id, day_of_week=day.weekday(), is_active=True
        )
        .order_by(WeeklyCommitment.start_time)
        .all()
    )
    return _commitment_intervals(commitments), commitments


def day_availability(
    preferences,
    commitments: list[WeeklyCommitment],
    day: date,
    task_blocks: list[Interval] | None = None,
    *,
    now: datetime | None = None,
    min_start_minutes: int | None = None,
) -> list[Interval]:
    awake = _awake_intervals(preferences.wake_time, preferences.sleep_time)
    preferred = _preferred_intervals(
        preferences.preferred_study_start, preferences.preferred_study_end
    )
    windows = _intersect(awake, preferred)
    blocked = _commitment_intervals(commitments)
    blocked.extend(task_blocks or [])
    windows = _subtract(windows, blocked)
    effective_start = min_start_minutes
    reference_today = now.date() if now else date.today()
    if effective_start is None and now is not None and day == reference_today:
        effective_start = now.hour * 60 + now.minute + 1
    if effective_start is not None and day == reference_today:
        windows = [
            Interval(max(window.start, effective_start), window.end)
            for window in windows
            if window.end - max(window.start, effective_start) >= MIN_SESSION_MINUTES
        ]
    return sorted(windows, key=lambda item: item.start)


def get_study_slots(
    user,
    day: date,
    *,
    exclude_task_id: int | None = None,
    now: datetime | None = None,
) -> list[Slot]:
    now = now or datetime.now()
    prefs = user.preferences
    if prefs is None:
        return []
    _, commitments = _commitment_blocks(user.id, day)
    raw = day_availability(
        prefs,
        commitments,
        day,
        _task_blocks(user.id, day, exclude_task_id=exclude_task_id),
        now=now,
    )
    cap = prefs.daily_cap_minutes
    lighter = set(prefs.lighter_days or [])
    if day.weekday() in lighter:
        cap = min(cap, 120)
    used = _scheduled_minutes(user.id, day, exclude_task_id=exclude_task_id)
    remaining_cap = max(0, cap - used)
    slots: list[Slot] = []
    break_minutes = max(0, prefs.break_duration_minutes)
    break_every = max(1, prefs.break_frequency_sessions or 1)
    sessions_since_break = 0
    for window in raw:
        cursor = window.start
        while cursor < window.end and remaining_cap >= MIN_SESSION_MINUTES:
            length = min(prefs.session_duration_minutes, window.end - cursor, remaining_cap)
            if length < MIN_SESSION_MINUTES:
                break
            slots.append(Slot(day=day, start=cursor, end=cursor + length))
            remaining_cap -= length
            sessions_since_break += 1
            gap = break_minutes if sessions_since_break >= break_every else 0
            if gap:
                sessions_since_break = 0
            cursor += length + gap
    return slots


def find_slot(
    user,
    duration: int,
    start_date: date,
    end_date: date,
    *,
    exclude_task_id: int | None = None,
    now: datetime | None = None,
) -> Slot | None:
    if duration < 1:
        return None
    if start_date < date.today():
        start_date = date.today()
    current = start_date
    while current <= end_date:
        for slot in get_study_slots(
            user, current, exclude_task_id=exclude_task_id, now=now
        ):
            if slot.minutes >= duration:
                return Slot(day=current, start=slot.start, end=slot.start + duration)
        current += timedelta(days=1)
    return None


def find_contiguous_slot(
    user,
    duration: int,
    start_date: date,
    end_date: date,
    *,
    exclude_task_id: int | None = None,
    now: datetime | None = None,
) -> Slot | None:
    prefs = user.preferences
    if prefs is None:
        return None
    current = max(start_date, date.today())
    while current <= end_date:
        _, commitments = _commitment_blocks(user.id, current)
        raw = day_availability(
            prefs,
            commitments,
            current,
            _task_blocks(user.id, current, exclude_task_id=exclude_task_id),
            now=now,
        )
        used = _scheduled_minutes(user.id, current, exclude_task_id=exclude_task_id)
        cap = min(prefs.daily_cap_minutes, 120) if current.weekday() in set(
            prefs.lighter_days or []
        ) else prefs.daily_cap_minutes
        room = max(0, cap - used)
        wanted = min(duration, room)
        if wanted >= min(duration, 60):
            for window in raw:
                if window.minutes >= wanted:
                    return Slot(day=current, start=window.start, end=window.start + wanted)
        current += timedelta(days=1)
    return None


def available_minutes(user, start_date: date, end_date: date) -> int:
    total = 0
    current = start_date
    while current <= end_date:
        total += sum(slot.minutes for slot in get_study_slots(user, current))
        current += timedelta(days=1)
    return total


def commitments_for_day(user_id: int, day: date) -> list[WeeklyCommitment]:
    return (
        WeeklyCommitment.query.filter_by(
            user_id=user_id, day_of_week=day.weekday(), is_active=True
        )
        .order_by(WeeklyCommitment.start_time)
        .all()
    )


def detect_commitment_overlap(commitments: list[WeeklyCommitment]) -> list[str]:
    grouped: dict[int, list[WeeklyCommitment]] = {}
    for item in commitments:
        grouped.setdefault(item.day_of_week, []).append(item)
    errors = []
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    for day, items in grouped.items():
        ordered = sorted(items, key=lambda item: item.start_time)
        for index in range(1, len(ordered)):
            previous = ordered[index - 1]
            current = ordered[index]
            if current.start_time < previous.end_time:
                errors.append(
                    f"{day_names[day]}: '{previous.title}' and '{current.title}' overlap."
                )
    return errors


def task_overlaps(user_id: int, day: date, start: time, end: time, exclude_task_id=None) -> bool:
    start_min = time_to_minutes(start)
    end_min = time_to_minutes(end)
    for block in _task_blocks(user_id, day, exclude_task_id=exclude_task_id):
        if start_min < block.end and end_min > block.start:
            return True
    return False


@dataclass
class _PoolSlot:
    start: int
    end: int
    used: bool = False

    @property
    def minutes(self) -> int:
        return self.end - self.start


@dataclass
class _DayState:
    day: date
    cap: int
    used_minutes: int
    windows: list[Interval]
    preference: object
    slots: list[_PoolSlot] = field(default_factory=list)

    @property
    def remaining_cap(self) -> int:
        return max(0, self.cap - self.used_minutes)

    def reserve_contiguous(self, duration: int) -> Slot | None:
        if duration > self.remaining_cap:
            return None
        for index, window in enumerate(self.windows):
            if window.minutes >= duration:
                start = window.start
                end = start + duration
                replacement = []
                if start > window.start:
                    replacement.append(Interval(window.start, start))
                if end < window.end:
                    replacement.append(Interval(end, window.end))
                self.windows = self.windows[:index] + replacement + self.windows[index + 1:]
                self.used_minutes += duration
                return Slot(day=self.day, start=start, end=end)
        return None

    def build_slots(self) -> None:
        remaining = self.remaining_cap
        session = self.preference.session_duration_minutes
        break_minutes = max(0, self.preference.break_duration_minutes)
        break_every = max(1, self.preference.break_frequency_sessions or 1)
        sessions_since_break = 0
        for window in self.windows:
            cursor = window.start
            while cursor < window.end and remaining >= MIN_POOL_SLOT_MINUTES:
                length = min(session, window.end - cursor, remaining)
                if length < MIN_POOL_SLOT_MINUTES:
                    break
                self.slots.append(_PoolSlot(start=cursor, end=cursor + length))
                remaining -= length
                sessions_since_break += 1
                gap = break_minutes if sessions_since_break >= break_every else 0
                if gap:
                    sessions_since_break = 0
                cursor += length + gap

    def reserve_slot(self, duration: int) -> Slot | None:
        if duration > self.remaining_cap:
            return None
        for slot in self.slots:
            if not slot.used and slot.minutes >= duration:
                slot.used = True
                self.used_minutes += duration
                return Slot(day=self.day, start=slot.start, end=slot.start + duration)
        return None


class SchedulePool:
    """In-memory slot pool used for deterministic, conflict-free plan generation."""

    def __init__(self, user, start_date: date, end_date: date, now: datetime | None = None):
        self.user = user
        self.start_date = start_date
        self.end_date = end_date
        self.now = now or datetime.now()
        self.days: dict[date, _DayState] = {}
        self._build()

    def _build(self) -> None:
        prefs = self.user.preferences
        tasks = (
            StudyTask.query.filter(
                StudyTask.user_id == self.user.id,
                StudyTask.scheduled_date >= self.start_date,
                StudyTask.scheduled_date <= self.end_date,
                StudyTask.status != "cancelled",
            )
            .all()
        )
        tasks_by_day: dict[date, list[StudyTask]] = {}
        for task in tasks:
            tasks_by_day.setdefault(task.scheduled_date, []).append(task)
        commitments = WeeklyCommitment.query.filter_by(
            user_id=self.user.id, is_active=True
        ).all()
        by_weekday: dict[int, list[WeeklyCommitment]] = {}
        for item in commitments:
            by_weekday.setdefault(item.day_of_week, []).append(item)
        lighter = set(prefs.lighter_days or [])
        current = self.start_date
        while current <= self.end_date:
            day_tasks = tasks_by_day.get(current, [])
            task_blocks = [
                Interval(time_to_minutes(task.start_time), time_to_minutes(task.end_time))
                for task in day_tasks
                if task.start_time and task.end_time
            ]
            windows = day_availability(
                prefs,
                by_weekday.get(current.weekday(), []),
                current,
                task_blocks,
                now=self.now,
            )
            cap = prefs.daily_cap_minutes
            if current.weekday() in lighter:
                cap = min(cap, 120)
            used = sum(task.estimated_minutes for task in day_tasks)
            self.days[current] = _DayState(
                day=current,
                cap=cap,
                used_minutes=used,
                windows=windows,
                preference=prefs,
            )
            current += timedelta(days=1)

    def reserve_contiguous(self, duration: int, start_date: date, end_date: date) -> Slot | None:
        current = max(start_date, self.start_date)
        final = min(end_date, self.end_date)
        while current <= final:
            state = self.days.get(current)
            if state is not None:
                slot = state.reserve_contiguous(duration)
                if slot is not None:
                    return slot
            current += timedelta(days=1)
        return None

    def prepare_slots(self) -> None:
        for state in self.days.values():
            state.build_slots()

    def schedule(self, duration: int, earliest: date) -> Slot | None:
        current = max(earliest, self.start_date)
        while current <= self.end_date:
            state = self.days.get(current)
            if state is not None:
                if not state.slots:
                    state.build_slots()
                slot = state.reserve_slot(duration)
                if slot is not None:
                    return slot
            current += timedelta(days=1)
        return None

    def capacity_minutes(self) -> int:
        return sum(
            min(max(0, state.cap - state.used_minutes), sum(w.minutes for w in state.windows))
            for state in self.days.values()
        )

    def slot_capacity_minutes(self) -> int:
        return sum(
            sum(slot.minutes for slot in state.slots if not slot.used)
            for state in self.days.values()
        )
