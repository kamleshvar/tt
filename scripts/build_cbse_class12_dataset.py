"""Build the CBSE Class 12 2026-27 workload dataset.

The official CBSE curriculum supplies the chapter catalogue and scope. CBSE does
not publish chapter-wise study hours, so every duration in this file is an
explicit planning inference. This script keeps the catalogue reviewable and
reproducible instead of hand-editing a large JSON file.
"""
from __future__ import annotations

import json
from pathlib import Path

ACCESSED = "2026-10-23"
INDEX_URL = "https://cbseacademic.nic.in/curriculum_2027.html"
PDF_ROOT = "https://cbseacademic.nic.in/web_material/CurriculumMain27/SecPart2"
COURSE_NOTE = "Public course page used as a comparative duration reference, not an official CBSE time allocation."

SUBJECTS = [
    {
        "name": "Physics",
        "code": "042",
        "pdf": "Physics_SecP2_2026-27.pdf",
        "course": ("Class 12 Physics full course - Magnet Brains", "https://www.magnetbrains.com/course/class-12th-physics-2-full-video-course"),
        "confidence": "medium",
        "practice_ratio": 0.50,
        "revision_ratio": 0.22,
        "note": "Chapter scope checked against the official CBSE Physics 2026-27 PDF. Public lecture durations were used only as a comparative reference; the listed hours are planning estimates.",
        "units": [
            ("Electrostatics", [("Electric Charges and Fields", 6.0, "medium"), ("Electrostatic Potential and Capacitance", 5.0, "hard")]),
            ("Current Electricity", [("Current Electricity", 5.0, "hard")]),
            ("Magnetic Effects of Current and Magnetism", [("Moving Charges and Magnetism", 5.5, "hard"), ("Magnetism and Matter", 3.5, "medium")]),
            ("Electromagnetic Induction and Alternating Currents", [("Electromagnetic Induction", 4.0, "medium"), ("Alternating Current", 4.5, "hard")]),
            ("Electromagnetic Waves", [("Electromagnetic Waves", 2.0, "easy")]),
            ("Optics", [("Ray Optics and Optical Instruments", 6.5, "hard"), ("Wave Optics", 4.0, "hard")]),
            ("Dual Nature of Radiation and Matter", [("Dual Nature of Radiation and Matter", 3.5, "medium")]),
            ("Atoms and Nuclei", [("Atoms", 3.0, "medium"), ("Nuclei", 3.0, "medium")]),
            ("Semiconductor Electronics: Materials, Devices and Simple Circuits", [("Semiconductor Electronics: Materials, Devices and Simple Circuits", 5.0, "hard")]),
        ],
    },
    {
        "name": "Chemistry",
        "code": "043",
        "pdf": "Chemistry_SecP2_2026-27.pdf",
        "course": ("Class 12 Chemistry full course - Magnet Brains", "https://www.magnetbrains.com/course/iit-jee-chemistry-class-12-full-course"),
        "confidence": "medium",
        "practice_ratio": 0.48,
        "revision_ratio": 0.20,
        "note": "Chapter list checked against the official CBSE Chemistry 2026-27 PDF. Duration is inferred from physical/inorganic/organic chapter breadth and comparable lesson lengths.",
        "units": [
            ("Solutions", [("Solutions", 5.5, "medium")]),
            ("Electrochemistry", [("Electrochemistry", 5.5, "hard")]),
            ("Chemical Kinetics", [("Chemical Kinetics", 5.5, "hard")]),
            ("The d- and f-Block Elements", [("The d- and f-Block Elements", 5.5, "medium")]),
            ("Coordination Compounds", [("Coordination Compounds", 5.0, "hard")]),
            ("Haloalkanes and Haloarenes", [("Haloalkanes and Haloarenes", 4.5, "medium")]),
            ("Alcohols, Phenols and Ethers", [("Alcohols, Phenols and Ethers", 5.0, "medium")]),
            ("Aldehydes, Ketones and Carboxylic Acids", [("Aldehydes, Ketones and Carboxylic Acids", 5.0, "hard")]),
            ("Amines", [("Amines", 4.5, "medium")]),
            ("Biomolecules", [("Biomolecules", 4.0, "easy")]),
        ],
    },
    {
        "name": "Mathematics",
        "code": "041",
        "pdf": "Maths_SecP2_2026-27.pdf",
        "course": ("Class 12 Mathematics updated course - Magnet Brains", "https://www.magnetbrains.com/course/class-12th-maths-ncert-cbse-updated-course"),
        "confidence": "medium",
        "practice_ratio": 0.65,
        "revision_ratio": 0.22,
        "note": "Chapter catalogued from the official CBSE Mathematics 2026-27 PDF. Practice is weighted heavily because problem-solving, not lecture time, dominates this subject.",
        "units": [
            ("Relations and Functions", [("Relations and Functions", 4.0, "medium"), ("Inverse Trigonometric Functions", 3.0, "medium")]),
            ("Algebra", [("Matrices", 5.0, "medium"), ("Determinants", 4.5, "hard")]),
            ("Calculus", [("Continuity and Differentiability", 5.5, "hard"), ("Applications of Derivatives", 5.5, "hard"), ("Integrals", 6.5, "hard"), ("Applications of Integrals", 4.0, "medium"), ("Differential Equations", 5.0, "hard")]),
            ("Vectors and Three-Dimensional Geometry", [("Vector Algebra", 4.5, "medium"), ("Three-Dimensional Geometry", 5.0, "hard")]),
            ("Linear Programming", [("Linear Programming", 3.0, "easy")]),
            ("Probability", [("Probability", 4.5, "medium")]),
        ],
    },
    {
        "name": "Biology",
        "code": "044",
        "pdf": "Biology_SecP2_2026-27.pdf",
        "course": ("Class 12 Biology full course - Magnet Brains", "https://www.magnetbrains.com/course/class-12th-biology-ncert-full-video-course/"),
        "confidence": "medium",
        "practice_ratio": 0.40,
        "revision_ratio": 0.20,
        "note": "Chapter catalogue checked against the official CBSE Biology 2026-27 PDF. Genetics and biotechnology chapters receive more learning time because of diagram and process complexity.",
        "units": [
            ("Reproduction", [("Sexual Reproduction in Flowering Plants", 4.0, "medium"), ("Human Reproduction", 5.0, "hard"), ("Reproductive Health", 3.5, "easy")]),
            ("Genetics and Evolution", [("Principles of Inheritance and Variation", 5.5, "hard"), ("Molecular Basis of Inheritance", 6.0, "hard"), ("Evolution", 4.0, "medium")]),
            ("Biology and Human Welfare", [("Human Health and Disease", 5.0, "medium"), ("Microbes in Human Welfare", 3.5, "easy")]),
            ("Biotechnology", [("Biotechnology: Principles and Processes", 4.0, "hard"), ("Biotechnology and its Applications", 4.0, "medium")]),
            ("Ecology", [("Organisms and Populations", 4.5, "medium"), ("Ecosystem", 4.0, "medium"), ("Biodiversity and Conservation", 3.5, "easy")]),
        ],
    },
    {
        "name": "Computer Science",
        "code": "083",
        "pdf": "Computer_Science_SecP2_2026-27.pdf",
        "course": ("Class 12 Computer Science full course - Magnet Brains", "https://www.magnetbrains.com/course/class-12th-computer-science-book-full-video-course"),
        "confidence": "low",
        "practice_ratio": 0.80,
        "revision_ratio": 0.20,
        "note": "Chapter areas checked against the official CBSE Computer Science 2026-27 PDF. The programming area is deliberately practice-heavy; SQL and networking estimates are inferred from comparable beginner courses.",
        "units": [
            ("Computational Thinking and Programming - II", [("Computational Thinking and Programming - II", 14.0, "hard")]),
            ("Computer Networks", [("Computer Networks", 8.0, "medium")]),
            ("Database Management", [("Database Management", 12.0, "hard")]),
        ],
    },
    {
        "name": "English Core",
        "code": "301",
        "pdf": "English_core_SecP2_2026-27.pdf",
        "course": ("Class 12 English Flamingo full course - Magnet Brains", "https://www.magnetbrains.com/course/class-12th-english-flamingo-book-ncert-full-video-course/"),
        "confidence": "medium",
        "practice_ratio": 0.35,
        "revision_ratio": 0.18,
        "note": "Texts and writing categories follow the supplied CBSE English Core catalogue and the official 2026-27 curriculum index. Writing is separated into schedulable units so practice does not disappear behind literature coverage.",
        "units": [
            ("Flamingo - Prose", [("The Last Lesson", 3.0, "easy"), ("Lost Spring", 3.0, "medium"), ("Deep Water", 3.0, "medium"), ("The Rattrap", 3.5, "medium"), ("Indigo", 3.5, "medium"), ("Poets and Pancakes", 3.5, "medium"), ("The Interview", 3.0, "medium"), ("Going Places", 3.0, "medium")]),
            ("Flamingo - Poetry", [("My Mother at Sixty-Six", 1.5, "easy"), ("Keeping Quiet", 1.5, "medium"), ("A Thing of Beauty", 1.5, "easy"), ("A Roadside Stand", 2.0, "medium"), ("Aunt Jennifer's Tigers", 1.5, "medium")]),
            ("Vistas - Supplementary Reader", [("The Third Level", 2.0, "easy"), ("The Tiger King", 2.5, "medium"), ("Journey to the End of the Earth", 2.5, "medium"), ("The Enemy", 2.5, "medium"), ("On the Face of It", 2.0, "medium"), ("Memories of Childhood", 2.5, "medium")]),
            ("Writing and Language Skills", [("Reading Comprehension", 6.0, "medium"), ("Notice and Formal Invitation Writing", 4.0, "easy"), ("Letters to the Editor and Job Applications", 6.0, "medium"), ("Article and Report Writing", 5.0, "medium"), ("Grammar and Language Skills", 4.0, "medium")]),
        ],
    },
    {
        "name": "Entrepreneurship",
        "code": "066",
        "pdf": "Enterprenuership_SecP2_2026-27.pdf",
        "course": None,
        "confidence": "low",
        "practice_ratio": 0.50,
        "revision_ratio": 0.20,
        "note": "Chapter list checked against the official CBSE Entrepreneurship 2026-27 PDF. No reliable public chapter-duration reference was located; hours are a low-confidence scope-based inference that should be reviewed by the teacher.",
        "units": [
            ("Entrepreneurial Mindset and Planning", [("Entrepreneurial Opportunity", 6.0, "medium"), ("Entrepreneurial Planning", 7.0, "hard")]),
            ("Enterprise Marketing and Growth", [("Enterprise Marketing", 6.5, "hard"), ("Enterprise Growth Strategies", 5.5, "medium")]),
            ("Business Arithmetic and Resource Mobilization", [("Business Arithmetic", 6.0, "hard"), ("Resource Mobilization", 5.5, "medium")]),
        ],
    },
    {
        "name": "Accountancy",
        "code": "055",
        "pdf": "Accountancy_SecP2_2026-27.pdf",
        "course": ("Class 12 Accountancy board-exam crash course - Magnet Brains", "https://www.magnetbrains.com/course/class-12th-accountancy-cbse-board-exam-crash-course"),
        "confidence": "medium",
        "practice_ratio": 0.65,
        "revision_ratio": 0.20,
        "note": "Chapter list checked against the official CBSE Accountancy 2026-27 PDF. Practice time reflects the need to work through journal entries, ledger postings and full financial statements.",
        "units": [
            ("Part A - Accounting for Partnership Firms and Companies", [("Accounting for Partnership Firms - Fundamentals", 8.0, "hard"), ("Goodwill: Nature and Valuation", 5.0, "hard"), ("Change in Profit-Sharing Ratio Among Existing Partners", 4.5, "medium"), ("Admission of a Partner", 6.0, "hard"), ("Retirement and Death of a Partner", 5.5, "hard"), ("Dissolution of a Partnership Firm", 5.0, "hard"), ("Accounting for Share Capital", 7.0, "hard"), ("Issue and Redemption of Debentures", 5.0, "medium")]),
            ("Part B - Financial Statement Analysis", [("Financial Statements of a Company", 5.0, "medium"), ("Financial Statement Analysis", 4.0, "medium"), ("Accounting Ratios", 5.5, "hard"), ("Cash Flow Statement", 5.0, "hard")]),
        ],
    },
    {
        "name": "Economics",
        "code": "030",
        "pdf": "Economics_SecP2_2026-27.pdf",
        "course": ("Class 12 Macroeconomics full course - Magnet Brains", "https://www.magnetbrains.com/course/class-12th-economics-macroeconomics-book-ncert-full-video-course/"),
        "confidence": "medium",
        "practice_ratio": 0.45,
        "revision_ratio": 0.20,
        "note": "Chapters checked against the official CBSE Economics 2026-27 PDF, covering Macro Economics and Indian Economic Development. Numericals and diagram practice are included in practice time.",
        "units": [
            ("Part A - Introductory Macroeconomics", [("National Income and Related Aggregates", 5.0, "hard"), ("Money and Banking", 4.5, "medium"), ("Determination of Income and Employment", 5.0, "hard"), ("Government Budget and the Economy", 4.5, "medium"), ("Balance of Payments", 4.0, "medium")]),
            ("Part B - Indian Economic Development", [("Indian Economy on the Eve of Independence", 4.0, "easy"), ("Indian Economy (1950-1990)", 3.5, "medium"), ("Liberalisation, Privatisation and Globalisation: An Appraisal", 4.0, "medium"), ("Human Capital Formation in India", 3.5, "medium"), ("Rural Development", 3.5, "medium"), ("Employment: Growth, Informalisation and Other Issues", 4.0, "medium"), ("Environment and Sustainable Development", 3.5, "medium"), ("Comparative Development Experiences of India and its Neighbours", 3.0, "easy")]),
        ],
    },
    {
        "name": "Business Studies",
        "code": "054",
        "pdf": "BusinessStudies_SecP2_2026-27.pdf",
        "course": ("Class 12 Business Studies updated course - Magnet Brains", "https://www.magnetbrains.com/course/class-12th-business-studies-ncert-cbse-updated-course/"),
        "confidence": "medium",
        "practice_ratio": 0.45,
        "revision_ratio": 0.20,
        "note": "Chapter list checked against the official CBSE Business Studies 2026-27 PDF. Case-based application and terminology revision are reflected in practice and revision allocations.",
        "units": [
            ("Part A - Principles and Functions of Management", [("Nature and Significance of Management", 4.5, "easy"), ("Principles of Management", 4.0, "medium"), ("Business Environment", 4.0, "medium"), ("Planning", 3.5, "easy"), ("Organising", 4.5, "medium"), ("Staffing", 4.5, "medium"), ("Directing", 5.0, "medium"), ("Controlling", 4.0, "medium")]),
            ("Part B - Business Finance and Marketing", [("Financial Management", 6.0, "hard"), ("Financial Markets", 5.0, "medium"), ("Marketing Management", 6.0, "hard")]),
            ("Consumer Protection", [("Consumer Protection", 4.0, "easy")]),
        ],
    },
    {
        "name": "Physical Education",
        "code": "048",
        "pdf": "PhysicalEducation_SecP2_2026-27.pdf",
        "course": None,
        "confidence": "low",
        "practice_ratio": 0.35,
        "revision_ratio": 0.18,
        "note": "Chapter list checked against the official CBSE Physical Education 2026-27 PDF. No reliable public chapter-duration reference was located; estimates are low-confidence and should be adjusted for the school's practical programme.",
        "units": [
            ("Management and Participation", [("Management of Sporting Events", 4.0, "medium"), ("Children and Women in Sports", 3.5, "medium")]),
            ("Health, Nutrition and Yoga", [("Yoga as a Preventive Measure for Lifestyle Disease", 4.0, "medium"), ("Physical Education and Sports for CWSN", 3.5, "medium"), ("Sports and Nutrition", 4.0, "medium")]),
            ("Measurement and Physiology", [("Test and Measurement in Sports", 4.5, "hard"), ("Physiology and Injuries in Sports", 4.0, "hard"), ("Biomechanics and Sports", 4.0, "hard")]),
            ("Psychology and Training", [("Psychology and Sports", 3.5, "medium"), ("Training in Sports", 4.0, "medium")]),
        ],
    },
]


def round_half(value: float) -> float:
    return round(value * 2) / 2


def source(title: str, url: str, source_type: str, note: str) -> dict:
    return {
        "title": title,
        "url": url,
        "source_type": source_type,
        "accessed": ACCESSED,
        "note": note,
    }


def subject_sources(subject: dict) -> list[dict]:
    sources = [
        source(
            f"CBSE Class 12 {subject['name']} curriculum 2026-27 (official PDF)",
            f"{PDF_ROOT}/{subject['pdf']}",
            "official_syllabus",
            "Primary source for subject code, units and chapter scope. It does not prescribe study hours.",
        ),
        source(
            "CBSE Curriculum for the Academic Year 2026-27",
            INDEX_URL,
            "official_curriculum_index",
            "Official index confirming the subject and syllabus publication.",
        ),
    ]
    if subject.get("course"):
        title, url = subject["course"]
        sources.append(source(title, url, "course_page", COURSE_NOTE))
    return sources


def chapter_entry(subject: dict, name: str, learning: float, difficulty: str) -> dict:
    practice = round_half(learning * subject["practice_ratio"])
    revision = round_half(learning * subject["revision_ratio"])
    total = round_half(learning + practice + revision)
    sources = subject_sources(subject)
    method = "scope_and_comparable_course_duration_inference"
    confidence = subject["confidence"]
    if subject.get("course") is None:
        method = "scope_based_planning_inference"
    return {
        "name": name,
        "estimated_minutes": int(round(total * 60)),
        "weightage_marks": None,
        "learning_hours": learning,
        "practice_hours": practice,
        "revision_hours": revision,
        "total_hours": total,
        "difficulty": difficulty,
        "estimate_confidence": confidence,
        "estimate_method": method,
        "research_status": "inferred",
        "last_researched_at": ACCESSED,
        "research_sources": sources,
        "research_notes": subject["note"],
        "topics": [],
    }


def build_payload() -> dict:
    subjects = []
    for subject in SUBJECTS:
        units = []
        for unit_name, chapters in subject["units"]:
            units.append(
                {
                    "name": unit_name,
                    "chapters": [
                        chapter_entry(subject, name, learning, difficulty)
                        for name, learning, difficulty in chapters
                    ],
                }
            )
        subjects.append(
            {
                "name": subject["name"],
                "code": subject["code"],
                "units": units,
            }
        )
    return {
        "metadata": {
            "board": "CBSE",
            "academic_year": "2026-27",
            "version_label": "draft-2026.1",
            "status": "validated",
            "source": "CBSE Curriculum 2026-27 (official chapter catalogue) plus transparently inferred planning hours.",
            "research_status": "inferred_workload_hours",
            "last_researched_at": ACCESSED,
            "research_sources": [
                source(
                    "CBSE Curriculum for the Academic Year 2026-27",
                    INDEX_URL,
                    "official_curriculum_index",
                    "Official index used to locate every Class 12 subject syllabus.",
                )
            ],
            "research_summary": "The chapter catalogue follows the supplied CBSE Class 12 2026-27 subject lists, and the official curriculum index/PDF links were checked. CBSE does not publish chapter-wise study hours. Every learning, practice and revision figure here is a planning inference, not an official allocation.",
            "workload_methodology": "Learning hours were estimated from chapter scope, then practice and revision were added using subject-specific ratios (practice-heavy for Mathematics, Computer Science and Accountancy; revision-light for literature). Values are rounded to 0.5 hour. The planner divides these totals into user session lengths and treats confidence as a bounded multiplier.",
            "notes": "Validated means the chapter catalogue and source links are structurally verified. Workload hours remain inferred planning estimates and are labelled as such. The version label is kept unchanged so that re-importing updates the existing current version in place and preserves student subject links and completed tasks.",
        },
        "subjects": subjects,
    }


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "data" / "syllabus" / "cbse_class12_2026_27.json"
    payload = build_payload()
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    chapters = sum(
        len(unit["chapters"])
        for subject in payload["subjects"]
        for unit in subject["units"]
    )
    print(f"Wrote {target} with {len(payload['subjects'])} subjects and {chapters} chapters.")


if __name__ == "__main__":
    main()
