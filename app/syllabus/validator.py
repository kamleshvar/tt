import re
from collections import Counter


def canonical_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return key[:120]


def validate_syllabus_payload(payload) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return {"errors": ["Syllabus data must be a JSON object."], "warnings": [], "valid": False}

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        errors.append("Missing metadata object.")
        metadata = {}
    for key in ("board", "academic_year", "version_label"):
        if not str(metadata.get(key) or "").strip():
            errors.append(f"Metadata field '{key}' is required.")
    status = str(metadata.get("status") or "draft").lower()
    if status not in {"draft", "validated", "archived"}:
        errors.append("Metadata status must be draft, validated or archived.")
    if status == "validated":
        warnings.append(
            "The importer checks structure, not whether this matches an official board circular. "
            "Keep a source reference with the validated version."
        )

    subjects = payload.get("subjects")
    if not isinstance(subjects, list) or not subjects:
        errors.append("At least one subject is required.")
        subjects = []

    subject_codes = []
    subject_names = []
    all_chapters = []
    chapter_count = 0
    topic_count = 0
    chapters_without_topics = 0

    for s_index, subject in enumerate(subjects, start=1):
        if not isinstance(subject, dict):
            errors.append(f"Subject {s_index} is not an object.")
            continue
        name = str(subject.get("name") or "").strip()
        code = str(subject.get("code") or "").strip()
        if not name:
            errors.append(f"Subject {s_index} has no name.")
        if not code:
            errors.append(f"Subject {s_index} has no code.")
        subject_names.append(name.lower())
        subject_codes.append(code.lower())
        units = subject.get("units")
        if not isinstance(units, list) or not units:
            errors.append(f"Subject '{name or s_index}' has no units or chapters.")
            continue
        unit_names = []
        for u_index, unit in enumerate(units, start=1):
            if not isinstance(unit, dict):
                errors.append(f"Unit {u_index} in '{name}' is not an object.")
                continue
            unit_name = str(unit.get("name") or "").strip()
            if not unit_name:
                errors.append(f"Unit {u_index} in '{name}' has no name.")
            unit_names.append(unit_name.lower())
            chapters = unit.get("chapters")
            if not isinstance(chapters, list) or not chapters:
                errors.append(f"Unit '{unit_name or u_index}' in '{name}' has no chapters.")
                continue
            chapter_keys = []
            for c_index, chapter in enumerate(chapters, start=1):
                if not isinstance(chapter, dict):
                    errors.append(f"Chapter {c_index} in '{unit_name}' is not an object.")
                    continue
                chapter_name = str(chapter.get("name") or "").strip()
                if not chapter_name:
                    errors.append(f"Chapter {c_index} in '{unit_name}' has no name.")
                    continue
                chapter_count += 1
                chapter_keys.append(canonical_key(chapter_name))
                all_chapters.append((name, chapter_name.lower()))
                estimate = chapter.get("estimated_minutes")
                stage_values = {}
                for stage in ("learning_hours", "practice_hours", "revision_hours"):
                    value = chapter.get(stage)
                    if value is None:
                        continue
                    if (
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or value < 0
                    ):
                        errors.append(
                            f"Chapter '{chapter_name}' has an invalid {stage} value."
                        )
                        continue
                    stage_values[stage] = float(value)
                total_hours = chapter.get("total_hours")
                if stage_values:
                    stage_total = round(sum(stage_values.values()), 2)
                    if not isinstance(total_hours, (int, float)):
                        errors.append(
                            f"Chapter '{chapter_name}' has stage hours but no numeric total_hours."
                        )
                    elif abs(float(total_hours) - stage_total) > 0.011:
                        errors.append(
                            f"Chapter '{chapter_name}' total_hours ({total_hours}) does not "
                            f"equal learning + practice + revision ({stage_total})."
                        )
                    if not stage_total:
                        errors.append(
                            f"Chapter '{chapter_name}' has zero hours across all stages."
                        )
                    expected_minutes = stage_total * 60
                    if not isinstance(estimate, int) or estimate <= 0:
                        warnings.append(
                            f"Chapter '{chapter_name}' has stage hours but no positive "
                            "estimated_minutes; the total will be derived from stage hours."
                        )
                    elif abs(estimate - expected_minutes) > 1:
                        warnings.append(
                            f"Chapter '{chapter_name}' estimated_minutes ({estimate}) does not "
                            f"match stage-hour total ({expected_minutes:.0f}). Stage hours take precedence."
                        )
                elif not isinstance(estimate, int) or estimate <= 0:
                    warnings.append(
                        f"Chapter '{chapter_name}' has no positive estimated_minutes; "
                        "the planner will use a conservative default."
                    )
                confidence = chapter.get("estimate_confidence")
                if confidence is not None and str(confidence).lower() not in {
                    "low", "medium", "high"
                }:
                    errors.append(
                        f"Chapter '{chapter_name}' estimate_confidence must be low, medium or high."
                    )
                difficulty = chapter.get("difficulty")
                if difficulty is not None and str(difficulty).lower() not in {
                    "easy", "medium", "hard"
                }:
                    errors.append(
                        f"Chapter '{chapter_name}' difficulty must be easy, medium or hard."
                    )
                sources = chapter.get("research_sources")
                if sources is not None and not isinstance(sources, list):
                    errors.append(
                        f"Chapter '{chapter_name}' research_sources must be a list."
                    )
                weightage = chapter.get("weightage_marks")
                if weightage is not None and (not isinstance(weightage, (int, float)) or weightage < 0):
                    warnings.append(f"Chapter '{chapter_name}' has an invalid weightage_marks value.")
                topics = chapter.get("topics", [])
                if topics is None:
                    topics = []
                if not isinstance(topics, list):
                    errors.append(f"Topics for chapter '{chapter_name}' must be a list.")
                    topics = []
                if not topics:
                    chapters_without_topics += 1
                topic_keys = []
                for topic in topics:
                    if not isinstance(topic, dict) or not str(topic.get("name") or "").strip():
                        errors.append(f"A topic in chapter '{chapter_name}' has no name.")
                        continue
                    topic_count += 1
                    topic_keys.append(canonical_key(topic.get("name")))
                    t_estimate = topic.get("estimated_minutes")
                    if t_estimate is not None and (
                        not isinstance(t_estimate, int) or t_estimate <= 0
                    ):
                        warnings.append(
                            f"Topic '{topic.get('name')}' in '{chapter_name}' has an invalid estimate."
                        )
                duplicates = [key for key, count in Counter(topic_keys).items() if count > 1]
                if duplicates:
                    errors.append(f"Duplicate topics in chapter '{chapter_name}': {', '.join(duplicates)}.")
            duplicates = [key for key, count in Counter(chapter_keys).items() if count > 1]
            if duplicates:
                errors.append(
                    f"Duplicate chapter names in unit '{unit_name}' of '{name}': {', '.join(duplicates)}."
                )
        duplicates = [key for key, count in Counter(unit_names).items() if count > 1]
        if duplicates:
            errors.append(f"Duplicate unit names in subject '{name}': {', '.join(duplicates)}.")

    for label, values in (("subject codes", subject_codes), ("subject names", subject_names)):
        duplicates = [key for key, count in Counter(values).items() if count > 1]
        if duplicates:
            errors.append(f"Duplicate {label}: {', '.join(duplicates)}.")

    duplicates = [
        name for name, count in Counter(all_chapters).items() if count > 1
    ]
    if duplicates:
        warnings.append(
            "The same chapter title appears more than once across the dataset. "
            "This can be legitimate across subjects, so review it rather than assuming an error."
        )

    if chapter_count == 0:
        errors.append("The syllabus contains no chapters.")
    if chapters_without_topics:
        warnings.append(
            f"{chapters_without_topics} chapter(s) have no topic breakdown. "
            "They will be scheduled at chapter level; this is valid for chapter-wise datasets."
        )
    if status == "draft":
        warnings.append(
            "This is a DRAFT syllabus. It must be replaced with a verified official dataset "
            "for real academic planning."
        )
    if metadata.get("board") and str(metadata.get("board")).lower() not in {"cbse"}:
        warnings.append(
            "Only the built-in draft is provided for CBSE. Confirm coverage for this board before validation."
        )

    return {
        "errors": errors,
        "warnings": warnings,
        "valid": not errors,
        "chapter_count": chapter_count,
        "topic_count": topic_count,
        "subject_count": len([s for s in subjects if isinstance(s, dict)]),
    }


def validate_version_structure(version) -> dict:
    errors = []
    warnings = []
    subjects = [subject for subject in version.subjects if not subject.is_removed]
    if not subjects:
        errors.append("The version has no active subjects.")
    chapter_total = 0
    removed_chapters = 0
    topic_total = 0
    chapters_without_topics = 0
    for subject in subjects:
        units = list(subject.units)
        if not units:
            errors.append(f"Subject '{subject.name}' has no units.")
            continue
        active_chapters = 0
        for unit in units:
            for chapter in unit.chapters:
                if chapter.is_removed:
                    removed_chapters += 1
                    continue
                active_chapters += 1
                chapter_total += 1
                topics = [topic for topic in chapter.topics if not topic.is_removed]
                topic_total += len(topics)
                if not topics:
                    chapters_without_topics += 1
                if not chapter.estimated_minutes or chapter.estimated_minutes <= 0:
                    warnings.append(f"Chapter '{chapter.name}' has no positive study estimate.")
        if active_chapters == 0:
            errors.append(f"Subject '{subject.name}' has no active chapters.")
    if chapter_total == 0:
        errors.append("The version contains no active chapters.")
    if chapters_without_topics:
        warnings.append(
            f"{chapters_without_topics} chapter(s) have no topics; those chapters will be "
            "planned at chapter level."
        )
    if removed_chapters:
        warnings.append(
            f"{removed_chapters} retired chapter(s) are retained so existing student "
            "progress is not destroyed."
        )
    if version.status == "draft":
        warnings.append(
            "This version is DRAFT and unverified. Validate it against the official board "
            "syllabus and re-import with status 'validated' when confirmed."
        )
    if version.status == "validated":
        warnings.append(
            "Structural checks only. The administrator remains responsible for confirming "
            "that this dataset matches the official board circular."
        )
    return {
        "errors": errors,
        "warnings": warnings,
        "valid": not errors,
        "subject_count": len(subjects),
        "chapter_count": chapter_total,
        "topic_count": topic_total,
        "removed_chapters": removed_chapters,
        "status": version.status,
    }
