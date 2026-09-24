"""backend/faculty_map.py — Faculty definitions, palette, and mapping table.

7 Faculties + UNMAPPED fallback:
1. SCIENCE
2. ENGINEERING
3. MANAGEMENT
4. SOCIAL_SCI
5. DESIGN
6. INTERDISCIPLINARY
7. PERFORMING_ARTS
8. UNMAPPED
"""
from __future__ import annotations

from enum import Enum
import re
from typing import Mapping, Optional


class Faculty(str, Enum):
    SCIENCE = "SCIENCE"
    ENGINEERING = "ENGINEERING"
    MANAGEMENT = "MANAGEMENT"
    SOCIAL_SCI = "SOCIAL_SCI"
    DESIGN = "DESIGN"
    INTERDISCIPLINARY = "INTERDISCIPLINARY"
    PERFORMING_ARTS = "PERFORMING_ARTS"
    UNMAPPED = "UNMAPPED"


# Palette: strong / light hex pairs as specified
PALETTE: dict[str, dict[str, str]] = {
    Faculty.SCIENCE.value: {
        "strong": "#278844",
        "light": "#9DD29C",
        "name": "Science",
    },
    Faculty.ENGINEERING.value: {
        "strong": "#134B90",
        "light": "#9EC8E9",
        "name": "Engineering",
    },
    Faculty.MANAGEMENT.value: {
        "strong": "#CB3127",
        "light": "#F8B0AC",
        "name": "Management",
    },
    Faculty.SOCIAL_SCI.value: {
        "strong": "#FAA61A",
        "light": "#FDF8B0",
        "name": "Social Sci",
    },
    Faculty.DESIGN.value: {
        "strong": "#082138",
        "light": "#56BAD8",
        "name": "Design",
    },
    Faculty.INTERDISCIPLINARY.value: {
        "strong": "#683285",
        "light": "#D8BBDA",
        "name": "Interdisciplinary",
    },
    Faculty.PERFORMING_ARTS.value: {
        "strong": "#AA1F5E",
        "light": "#EBA9C8",
        "name": "Performing Arts",
    },
    Faculty.UNMAPPED.value: {
        "strong": "#58595B",
        "light": "#E6E7E8",
        "name": "Fallback / Unmapped",
    },
}


def normalize_name(s: Optional[str]) -> str:
    """Normalize a name by stripping leading/trailing whitespace, collapsing multiple spaces, lowercasing, and stripping '(see Note N)'."""
    if not s:
        return ""
    cleaned = re.sub(r"\(see\s+note\s+\d+\)", "", str(s), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


# ERP school / department in DB is the primary source of truth
ERP_SCHOOL_TO_FACULTY: dict[str, str] = {
    normalize_name("Basic and Applied Sciences"): Faculty.SCIENCE.value,
    normalize_name("School of Basic and Applied Sciences"): Faculty.SCIENCE.value,
    normalize_name("Engineering & Technology"): Faculty.ENGINEERING.value,
    normalize_name("Faculty of Engineering and Technology"): Faculty.ENGINEERING.value,
    normalize_name("School of Engineering and Technology"): Faculty.ENGINEERING.value,
    normalize_name("Management and Commerce"): Faculty.MANAGEMENT.value,
    normalize_name("Institute of Management and Research"): Faculty.MANAGEMENT.value,
    normalize_name("Social Sciences and Humanities"): Faculty.SOCIAL_SCI.value,
    normalize_name("School of Social Sciences and Humanities"): Faculty.SOCIAL_SCI.value,
    normalize_name("Faculty of Design"): Faculty.DESIGN.value,
    normalize_name("Institute of Design"): Faculty.DESIGN.value,
    normalize_name("Faculty of Interdisciplinary Studies"): Faculty.INTERDISCIPLINARY.value,
    normalize_name("Faculty of Performing Arts"): Faculty.PERFORMING_ARTS.value,
    normalize_name("Mahagami Gurukul"): Faculty.PERFORMING_ARTS.value,
}


# Baseline mapping of all 108 distinct programme names in the database
# Used as fallback when school is unmapped / missing, and to seed the `programme_faculty` DB table.
DEFAULT_PROGRAMME_MAP: dict[str, str] = {
    # ─── Science ─────────────────────────────────────────────────────────────
    normalize_name("B Sc. (Hons) Computer Science"): Faculty.SCIENCE.value,
    normalize_name("B. Sc. (Hons) Food Nutrition and Dietetics"): Faculty.SCIENCE.value,
    normalize_name("B. Sc. (Hons.) Information Technology"): Faculty.SCIENCE.value,
    normalize_name("B.Sc. (Hon) Geology"): Faculty.SCIENCE.value,
    normalize_name("B.Sc. Forensic Science"): Faculty.SCIENCE.value,
    normalize_name("B.Sc.(Computer Science)"): Faculty.SCIENCE.value,
    normalize_name("B.Sc.(Hons) Biotechnology"): Faculty.SCIENCE.value,
    normalize_name("BCA (Hons.) Digital Marketing"): Faculty.SCIENCE.value,
    normalize_name("BCA (Hons.) Science"): Faculty.SCIENCE.value,
    normalize_name("Bachelor of Science (Honours) Forensic Science"): Faculty.SCIENCE.value,
    normalize_name("Bachelor of Science (Hons.) Bioinformatics"): Faculty.SCIENCE.value,
    normalize_name("Bachelor of Science (Hons.) Food Technology & Processing"): Faculty.SCIENCE.value,
    normalize_name("Bachelor of Science (Hons.) Microbiology"): Faculty.SCIENCE.value,
    normalize_name("Integrated Bachelor of Science - Master of Science (Data Science)"): Faculty.SCIENCE.value,
    normalize_name("M. Sc. Chemistry"): Faculty.SCIENCE.value,
    normalize_name("M. Sc. Forensic Science"): Faculty.SCIENCE.value,
    normalize_name("M.Sc. Biotechnology"): Faculty.SCIENCE.value,
    normalize_name("M.Sc. Data Science"): Faculty.SCIENCE.value,
    normalize_name("M.Sc. Food Technology"): Faculty.SCIENCE.value,
    normalize_name("M.Sc. Microbiology/Virology"): Faculty.SCIENCE.value,
    normalize_name("M.Sc. Physics"): Faculty.SCIENCE.value,
    normalize_name("M.Sc. Plant Breeding & Molecular Genetics"): Faculty.SCIENCE.value,
    normalize_name("M.Sc.(Computer Science)"): Faculty.SCIENCE.value,
    normalize_name("PhD Chemistry"): Faculty.SCIENCE.value,
    normalize_name("PhD Physics"): Faculty.SCIENCE.value,
    normalize_name("PhD(Computer Science and Information Technology)"): Faculty.SCIENCE.value,

    # ─── Engineering ─────────────────────────────────────────────────────────
    normalize_name("B. Tech Artificial Intelligence and Machine Learning"): Faculty.ENGINEERING.value,
    normalize_name("B. Tech. Biomedical Engineering"): Faculty.ENGINEERING.value,
    normalize_name("B. Tech. Biotechnology"): Faculty.ENGINEERING.value,
    normalize_name("B. Tech. Computer Science and Engineering (IoT, Cyber Security Including Block Chain Technology)"): Faculty.ENGINEERING.value,
    normalize_name("B. Tech. Food Processing Technology"): Faculty.ENGINEERING.value,
    normalize_name("B. Tech. Information Technology IICT"): Faculty.ENGINEERING.value,
    normalize_name("B.Tech Electrical and Computer Engineering"): Faculty.ENGINEERING.value,
    normalize_name("B.Tech Electronics and Computer Engineering"): Faculty.ENGINEERING.value,
    normalize_name("B.Tech. (Chemical Engineering)"): Faculty.ENGINEERING.value,
    normalize_name("B.Tech. (Computer Science and Engineering)"): Faculty.ENGINEERING.value,
    normalize_name("B.Tech. (Mechanical Engineering)"): Faculty.ENGINEERING.value,
    normalize_name("B.Tech. Artificial Intelligence (AI) and Data Science"): Faculty.ENGINEERING.value,
    normalize_name("B.Tech. Data Science"): Faculty.ENGINEERING.value,
    normalize_name("B.Tech. Electrical and Instrumentation Engineering"): Faculty.ENGINEERING.value,
    normalize_name("B.Tech. Mechanical and Mechatronics Engineering (Additive Manufacturing)"): Faculty.ENGINEERING.value,
    normalize_name("B.Tech. Robotics and Artificial Intelligence"): Faculty.ENGINEERING.value,
    normalize_name("Bachelor of Architecture"): Faculty.ENGINEERING.value,
    normalize_name("Bachelor of Computer Application (Hons.) - Cloud Technology and Information Security"): Faculty.ENGINEERING.value,
    normalize_name("Bachelor of Technology(Civil Engineering)"): Faculty.ENGINEERING.value,
    normalize_name("Diploma in Pharmacy"): Faculty.ENGINEERING.value,
    normalize_name("M.Arch Environmental Architecture"): Faculty.ENGINEERING.value,
    normalize_name("M.Tech Data Science"): Faculty.ENGINEERING.value,
    normalize_name("MCA"): Faculty.ENGINEERING.value,
    normalize_name("PhD(Computer Science Engineering)"): Faculty.ENGINEERING.value,

    # ─── Management ──────────────────────────────────────────────────────────
    normalize_name("B. Com (Hons.)"): Faculty.MANAGEMENT.value,
    normalize_name("B.B.A."): Faculty.MANAGEMENT.value,
    normalize_name("B.B.A. (Hons.) in Aviation, Hospitality and Travel & Tourism Studies"): Faculty.MANAGEMENT.value,
    normalize_name("B.Com"): Faculty.MANAGEMENT.value,
    normalize_name("B.Sc (Hons.) Culinary Arts"): Faculty.MANAGEMENT.value,
    normalize_name("B.Sc. (Hotel Operations And Catering Services)- MGMU"): Faculty.MANAGEMENT.value,
    normalize_name("BBA (Hons.)"): Faculty.MANAGEMENT.value,
    normalize_name("Bachelor of Management Studies(Hons.)"): Faculty.MANAGEMENT.value,
    normalize_name("Bachelor of Science Hons. (Hotel Operations and Catering Services)"): Faculty.MANAGEMENT.value,
    normalize_name("Diploma Program in Hotel Operations"): Faculty.MANAGEMENT.value,
    normalize_name("Diploma in Bakery and Patisseries"): Faculty.MANAGEMENT.value,
    normalize_name("M.B.A"): Faculty.MANAGEMENT.value,
    normalize_name("M.Com"): Faculty.MANAGEMENT.value,
    normalize_name("Master of Management Studies"): Faculty.MANAGEMENT.value,
    normalize_name("Post Degree Diploma Program in Hotel Operations"): Faculty.MANAGEMENT.value,

    # ─── Social Sciences ─────────────────────────────────────────────────────
    normalize_name("B.A. (Hons.) in Filmmaking-NEP"): Faculty.SOCIAL_SCI.value,
    normalize_name("B.A. (International Journalism & Mass Communication)"): Faculty.SOCIAL_SCI.value,
    normalize_name("BA (Hons.) Mass Communication and Journalism (MCJ)"): Faculty.SOCIAL_SCI.value,
    normalize_name("BA (Hons.) Photography-NEP"): Faculty.SOCIAL_SCI.value,
    normalize_name("BA( International Journalism and Electronic Media)"): Faculty.SOCIAL_SCI.value,
    normalize_name("Diploma in Photography"): Faculty.SOCIAL_SCI.value,
    normalize_name("LL.B."): Faculty.SOCIAL_SCI.value,
    normalize_name("M. A. Marathi"): Faculty.SOCIAL_SCI.value,
    normalize_name("M.A Clinical Psychology"): Faculty.SOCIAL_SCI.value,
    normalize_name("M.A Political Science and Governance"): Faculty.SOCIAL_SCI.value,
    normalize_name("M.A Urdu"): Faculty.SOCIAL_SCI.value,
    normalize_name("M.A. (Mass Communication & Journalism) with Specialization"): Faculty.SOCIAL_SCI.value,
    normalize_name("M.A.Economics-Sustainable Entrepreneurship"): Faculty.SOCIAL_SCI.value,
    normalize_name("MA Broadcast Communication"): Faculty.SOCIAL_SCI.value,
    normalize_name("MA English"): Faculty.SOCIAL_SCI.value,
    normalize_name("Master of Arts (Film Direction)"): Faculty.SOCIAL_SCI.value,
    normalize_name("Master of Arts (History and Archaeology)"): Faculty.SOCIAL_SCI.value,
    normalize_name("Master of Arts (Sound Designing and Music Production)"): Faculty.SOCIAL_SCI.value,
    normalize_name("Master of Public Health"): Faculty.SOCIAL_SCI.value,
    normalize_name("Master of Social Work"): Faculty.SOCIAL_SCI.value,
    normalize_name("PG Diploma in Guidance and Counseling"): Faculty.SOCIAL_SCI.value,
    normalize_name("PG Diploma in Taxation Law."): Faculty.SOCIAL_SCI.value,
    normalize_name("PhD(Hindi)"): Faculty.SOCIAL_SCI.value,
    normalize_name("PhD(Marathi)"): Faculty.SOCIAL_SCI.value,

    # ─── Design ──────────────────────────────────────────────────────────────
    normalize_name("Advance Diploma in Fashion Design & Boutique Management (Faculty of Design)"): Faculty.DESIGN.value,
    normalize_name("B. Sc. (Hons.) Animation"): Faculty.DESIGN.value,
    normalize_name("BFA (Applied Art)"): Faculty.DESIGN.value,
    normalize_name("BFA Painting"): Faculty.DESIGN.value,
    normalize_name("Bachelor of Design (Fashion Design)"): Faculty.DESIGN.value,
    normalize_name("Bachelor of Design (Interior Design)"): Faculty.DESIGN.value,
    normalize_name("Bachelor of Design (Textile Design)"): Faculty.DESIGN.value,
    normalize_name("M.F.M. Master of Fashion Management"): Faculty.DESIGN.value,
    normalize_name("Master of Arts (VFX & Animation)"): Faculty.DESIGN.value,
    normalize_name("Master of Design (Textile Design) Faculty of Design"): Faculty.DESIGN.value,

    # ─── Interdisciplinary ───────────────────────────────────────────────────
    normalize_name("M.A. Women and Gender Studies"): Faculty.INTERDISCIPLINARY.value,
    normalize_name("MA (Education)"): Faculty.INTERDISCIPLINARY.value,
    normalize_name("PhD Education"): Faculty.INTERDISCIPLINARY.value,
    normalize_name("PhD Library & Information Sci."): Faculty.INTERDISCIPLINARY.value,
    normalize_name("PhD Physical Education"): Faculty.INTERDISCIPLINARY.value,

    # ─── Performing Arts ─────────────────────────────────────────────────────
    normalize_name("Bachelor of Performing Arts (BPA) in Odissi Dance (kala Vid Odissi)"): Faculty.PERFORMING_ARTS.value,
    normalize_name("Foundation Certificate in Kathak Dance (Kala Adhar Kathak)"): Faculty.PERFORMING_ARTS.value,
    normalize_name("Master of Arts( M. A.) Musicology"): Faculty.PERFORMING_ARTS.value,
    normalize_name("Master of Performing Arts( MPA) Odissi"): Faculty.PERFORMING_ARTS.value,
}


def derive_faculty(
    school: Optional[str] = None,
    programme: Optional[str] = None,
    custom_map: Optional[Mapping[str, str]] = None,
    overrides: Optional[set[str]] = None,
) -> str:
    """Derive faculty for a student.
    
    Priority:
    1. Admin override (is_override=True in programme_faculty).
    2. ERP school / department.
    3. Programme mapping table (custom DB map, then baseline map).
    4. Default: UNMAPPED.
    """
    norm_prog = normalize_name(programme)
    if overrides and norm_prog in overrides and custom_map and norm_prog in custom_map:
        val = custom_map[norm_prog]
        if val in PALETTE:
            return val

    norm_school = normalize_name(school)
    if norm_school in ERP_SCHOOL_TO_FACULTY:
        return ERP_SCHOOL_TO_FACULTY[norm_school]

    if custom_map and norm_prog in custom_map:
        val = custom_map[norm_prog]
        if val in PALETTE:
            return val

    if norm_prog in DEFAULT_PROGRAMME_MAP:
        return DEFAULT_PROGRAMME_MAP[norm_prog]

    return Faculty.UNMAPPED.value


def get_faculty_palette(faculty: Optional[str]) -> dict[str, str]:
    """Return the strong and light hex colors for a given faculty name."""
    if not faculty or faculty not in PALETTE:
        return PALETTE[Faculty.UNMAPPED.value]
    return PALETTE[faculty]
