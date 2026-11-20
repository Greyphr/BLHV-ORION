"""Cultivation maths: 10 realms x 9 sub-ranks (pure, no Discord imports, easy to test).

A member's *level* is their rank index, 0..MAX_LEVEL:
    0 = Earth King 1, 8 = Earth King 9, 9 = Planetary 1, ... 89 = Eternal True God 9.
Only the realm (e.g. "Earth King") is a Discord role; the sub-rank lives in the database.
XP thresholds are configurable: call `configure(step_xp)` with 89 per-step costs (see /admin -> Cultivation),
or leave the default curve.
"""
import bisect

REALMS = [
    "Earth King", "Planetary", "Stellar", "Universe", "Domain Lord",
    "World Lord", "Immortal", "Universe Venerable", "True God", "Eternal True God",
]
SUBRANKS = 9
MAX_LEVEL = len(REALMS) * SUBRANKS - 1          # 89

# (first level of the realm, role name). Roles REPLACE each other, they never stack.
LEVEL_ROLES = [(i * SUBRANKS, name) for i, name in enumerate(REALMS)]
LEVEL_ROLE_NAMES = {name for _, name in LEVEL_ROLES}

# Celestial blues -> gold at the top
ROLE_COLORS = {
    "Earth King": 0x9CA3AF, "Planetary": 0x7DD3FC, "Stellar": 0x38BDF8, "Universe": 0x3B82F6,
    "Domain Lord": 0x2563EB, "World Lord": 0x4F46E5, "Immortal": 0x7C3AED,
    "Universe Venerable": 0xA855F7, "True God": 0xF59E0B, "Eternal True God": 0xFACC15,
}


def default_step(level: int) -> int:
    """Default XP needed to go from `level` to `level + 1` (a tuning draft; override with configure())."""
    return int(0.6 * level * level + 12 * level + 100)


_CUM: list[int] = []


def configure(step_xp: list[int] | None = None) -> None:
    """Install custom per-step costs (exactly MAX_LEVEL positive ints), or None to restore the default curve."""
    global _STEPS, _CUM
    if step_xp is not None:
        if len(step_xp) != MAX_LEVEL or any((not isinstance(x, int)) or x <= 0 for x in step_xp):
            raise ValueError(f"need {MAX_LEVEL} positive integer step costs")
        _STEPS = list(step_xp)
    else:
        _STEPS = [default_step(l) for l in range(MAX_LEVEL)]
    _CUM = [0]
    for s in _STEPS:
        _CUM.append(_CUM[-1] + s)


configure()


def xp_to_next(level: int) -> int:
    """XP needed to go from `level` to `level + 1` (0 at max level)."""
    return _STEPS[level] if 0 <= level < MAX_LEVEL else 0


def total_xp_for_level(level: int) -> int:
    return _CUM[min(max(level, 0), MAX_LEVEL)]


def level_from_xp(xp: int) -> int:
    return min(MAX_LEVEL, bisect.bisect_right(_CUM, max(xp, 0)) - 1)


def progress(xp: int) -> tuple[int, int, int]:
    """Returns (level, xp_into_level, xp_needed_for_next). needed == 0 at max level."""
    lvl = level_from_xp(xp)
    return lvl, xp - _CUM[lvl], xp_to_next(lvl)


def realm_of(level: int) -> str:
    return REALMS[min(max(level, 0), MAX_LEVEL) // SUBRANKS]


def rank_name(level: int) -> str:
    """'Earth King 5': realm plus sub-rank 1..9."""
    level = min(max(level, 0), MAX_LEVEL)
    return f"{REALMS[level // SUBRANKS]} {level % SUBRANKS + 1}"


def is_realm_change(old_level: int, new_level: int) -> bool:
    return realm_of(old_level) != realm_of(new_level)


def role_name_for_level(level: int) -> str:
    return realm_of(level)


def next_milestone(level: int):
    """The next realm as (first level, name), or None in the top realm."""
    for lv, n in LEVEL_ROLES:
        if lv > level:
            return lv, n
    return None
