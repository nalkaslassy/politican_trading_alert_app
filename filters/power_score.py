"""Power/influence scoring for congressional members.

Based on Hall, Karadas & Schlosky (2019) and the 2025 NBER leadership paper.
The NBER paper finds alpha concentrates specifically in FORMAL leadership
(Speaker, floor leaders, whips, conference/caucus chairs) — not linearly
across all seniority levels.

Score 0–28 (capped below 30 so freshness and issuer relevance drive the total):
  28 = Speaker of the House
  26 = Senate/House Majority or Minority Leader
  22 = Whip, Conference/Caucus Chair, President Pro Tem
  18 = Major committee Chair (Armed Services, Finance, Intelligence, etc.)
  14 = Other committee Chair or Ranking Member; former leadership
  10 = Senior / long-tenured member (10+ years, major committee)
   7 = Regular member with known committee assignments
   3 = Member with no committee data or very new member
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
    "Dick Durbin":              22,   # Senate Minority Whip (prev.)
    "Patty Murray":             22,   # President Pro Tempore

    # ── Major committee chairs ─────────────────────────────────────────────
    "French Hill":              18,   # Chair, House Financial Services
    "Roger Wicker":             18,   # Chair, Senate Armed Services
    "John Boozman":             18,   # Chair, Senate Agriculture
    "Tim Scott":                18,   # Chair, Senate Banking
    "Tom Cotton":               18,   # Chair, Senate Intelligence
    "Brett Guthrie":            18,   # Chair, House Energy & Commerce
    "Jason Smith":              18,   # Chair, House Ways & Means
    "Mike Turner":              18,   # Chair, House Intelligence

    # ── Other committee chairs / ranking members ───────────────────────────
    "Mark Green":               14,   # Chair, House Homeland Security
    "Jim Jordan":               14,   # Chair, House Judiciary
    "Jodey Arrington":          14,   # Chair, House Budget
    "Sam Graves":               14,   # Chair, House Transportation
    "Brian Babin":              14,   # Chair, House Science, Space & Tech
    "Debbie Wasserman Schultz": 14,   # Ranking, House Appropriations subcommittee
    "Glenn Grothman":           10,   # House Oversight subcommittee

    # ── Former leadership / senior long-tenured members ───────────────────
    "Nancy Pelosi":             18,   # Former Speaker, 30+ year veteran
    "Mitch McConnell":          18,   # Former Senate Majority Leader
    "Kevin McCarthy":           14,   # Former Speaker
    "Patrick McHenry":          14,   # Former Chair, House Financial Services
    "Richard Burr":             14,   # Former Chair, Senate Intelligence
    "Richard Shelby":           14,   # Former Chair, Senate Appropriations
    "Michael McCaul":           10,   # Foreign Affairs + Science; 10+ year senior
    "Rick Scott":               10,   # Banking + Budget + Commerce; senator

    # ── Regular members with known committee assignments ──────────────────
    "Ro Khanna":                 7,   # Armed Services + Science/Tech
    "Dan Crenshaw":              7,   # Intelligence + Homeland Security
    "Josh Gottheimer":           7,   # Financial Services + Homeland Security
    "Tommy Tuberville":          7,   # Armed Services + Agriculture
    "Donald Norcross":           7,   # Armed Services + Transportation
    "Terri Sewell":              7,   # Ways and Means
    "Brian Mast":                7,   # Foreign Affairs + Transportation
    "Nick LaLota":               7,   # Financial Services + Homeland Security
    "Marjorie Taylor Greene":    7,   # Budget + Oversight
    "Warren Davidson":           7,   # Financial Services
    "David Rouzer":              7,   # Agriculture + Transportation
    "John Fetterman":            7,   # Agriculture + Banking + Judiciary
    "Gary Peters":               7,   # Armed Services + Homeland Security
    "Steve Cohen":               7,   # Judiciary + Transportation
    "Mike Kelly":                7,   # Ways and Means + Oversight
    "John McGuire":              7,   # Armed Services
    "Nicholas Begich III":       7,   # Armed Services + Transportation
    "Rick Allen":                7,   # Agriculture + Budget
    "Thomas Kean Jr":            7,   # Science Space Technology + Homeland Security
    "Kevin Hern":                7,   # Ways and Means + Budget
    "Andy Barr":                 7,   # Financial Services
    "Bill Foster":               7,   # Financial Services + Science
    "Jared Moskowitz":           7,   # Appropriations + Oversight
    "Jonathan Jackson":          7,   # Oversight
    "Chip Roy":                  7,   # Budget + Oversight
    "David Taylor":              7,   # Financial Services + Oversight
    "Tim Moore":                 3,   # Judiciary (limited data)
    "Matt Van Epps":             3,   # No mapped committee
}

_DEFAULT_SCORE = 3   # Unknown member — no evidence of informational advantage


def get_power_score(politician_name: str) -> tuple[int, str]:
    """Return (score 0-28, explanation) for a politician.

    Scores reflect formal leadership role and committee chair status.
    Per NBER 2025, alpha concentrates in formal agenda-setting power,
    not seniority alone.
    """
    score = _POWER_SCORES.get(politician_name, _DEFAULT_SCORE)

    if score >= 26:
        note = f"{politician_name}: Formal congressional leadership"
    elif score == 28:
        note = f"{politician_name}: Speaker of the House"
    elif score >= 22:
        note = f"{politician_name}: Senior leadership / Whip"
    elif score >= 18:
        note = f"{politician_name}: Major committee Chair"
    elif score >= 14:
        note = f"{politician_name}: Committee Chair / Ranking Member"
    elif score >= 10:
        note = f"{politician_name}: Senior / long-tenured member"
    elif score >= 7:
        note = f"{politician_name}: Active committee member"
    else:
        note = f"{politician_name}: Limited influence data"

    return score, note
