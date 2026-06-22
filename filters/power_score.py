"""Power/influence scoring for congressional members.

Based on the 2025 NBER leadership paper. Evidence is concentrated among the
~20 lawmakers who held formal leadership (Speaker, floor leaders, whips,
conference/caucus chairs) — NOT generalized across all committee members
or senior members.

Score 0–28:
  28 = Speaker of the House
  26 = Senate/House Majority or Minority Leader
  22 = Whip, Conference/Caucus Chair, President Pro Tem
  16 = Major committee Chair (Armed Services, Finance, Intelligence, etc.)
  12 = Other committee Chair
  10 = Ranking Member; former Speaker or floor leader
   8 = Former committee Chair
   0 = Regular member, senior member, or unknown — seniority without a
       formal agenda-setting role has no validated alpha per the paper
"""

_POWER_SCORES: dict[str, int] = {
    # ── Formal congressional leadership (119th Congress) ──────────────────
    "Mike Johnson":             28,   # Speaker of the House
    "John Thune":               26,   # Senate Majority Leader
    "Chuck Schumer":            26,   # Senate Minority Leader
    "Hakeem Jeffries":          26,   # House Minority Leader
    "Steve Scalise":            26,   # House Majority Leader
    "Tom Emmer":                22,   # House Majority Whip
    "Katherine Clark":          22,   # House Minority Whip
    "John Barrasso":            22,   # Senate Majority Whip
    "Dick Durbin":              22,   # Senate Minority Whip (ret. 2025)
    "Patty Murray":             22,   # President Pro Tempore

    # ── Major committee chairs ─────────────────────────────────────────────
    "French Hill":              16,   # Chair, House Financial Services
    "Roger Wicker":             16,   # Chair, Senate Armed Services
    "John Boozman":             16,   # Chair, Senate Agriculture
    "Tim Scott":                16,   # Chair, Senate Banking
    "Tom Cotton":               16,   # Chair, Senate Intelligence
    "Brett Guthrie":            16,   # Chair, House Energy & Commerce
    "Jason Smith":              16,   # Chair, House Ways & Means
    "Mike Turner":              16,   # Chair, House Intelligence

    # ── Other committee chairs ─────────────────────────────────────────────
    "Mark Green":               12,   # Chair, House Homeland Security
    "Jim Jordan":               12,   # Chair, House Judiciary
    "Jodey Arrington":          12,   # Chair, House Budget
    "Sam Graves":               12,   # Chair, House Transportation
    "Brian Babin":              12,   # Chair, House Science, Space & Tech

    # ── Ranking members ────────────────────────────────────────────────────
    "Debbie Wasserman Schultz": 10,   # Ranking, House Appropriations subcommittee

    # ── Still serving, former formal leadership ───────────────────────────
    # NBER 2025 paper is about *current* agenda-setting power. Experimental
    # 3 pts for network/institutional influence that may persist post-role.
    "Nancy Pelosi":              3,   # Former Speaker, still serving
    "Mitch McConnell":           3,   # Former Senate Majority Leader, still serving

    # ── No longer serving — score zero ────────────────────────────────────
    # Kevin McCarthy resigned Dec 2023; former chairs retired.
    "Kevin McCarthy":            0,
    "Patrick McHenry":           0,
    "Richard Burr":              0,
    "Richard Shelby":            0,

    # NOTE: All regular committee members and unknown roles score 0.
    # Seniority without formal agenda-setting power has no validated alpha.
}

_DEFAULT_SCORE = 0   # Unknown or regular member — no evidence of informational advantage


def get_power_score(politician_name: str) -> tuple[int, str]:
    """Return (score 0-28, explanation) for a politician.

    Only formal agenda-setting roles earn points. Regular committee
    membership and seniority are explicitly excluded per NBER 2025.
    """
    score = _POWER_SCORES.get(politician_name, _DEFAULT_SCORE)

    if score >= 26:
        note = f"{politician_name}: Formal congressional leadership"
    elif score == 28:
        note = f"{politician_name}: Speaker of the House"
    elif score >= 22:
        note = f"{politician_name}: Senior leadership / Whip"
    elif score >= 16:
        note = f"{politician_name}: Major committee Chair"
    elif score >= 12:
        note = f"{politician_name}: Committee Chair"
    elif score >= 10:
        note = f"{politician_name}: Ranking Member / Former leadership"
    elif score >= 8:
        note = f"{politician_name}: Former committee Chair"
    else:
        note = f"{politician_name}: No formal leadership role"

    return score, note
