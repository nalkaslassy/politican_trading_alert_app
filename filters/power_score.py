"""Power/influence scoring for congressional members.

Based on Hall, Karadas & Schlosky (2019) and the 2025 NBER leadership paper:
alpha concentrates in powerful members — leadership roles, committee chairs,
and long-tenured members with institutional influence.

Score 0–35:
  35 = Congressional leadership (Speaker, Majority/Minority Leader)
  30 = Senior leadership (Whip, Conference/Caucus Chair, President Pro Tem)
  25 = Major committee Chair or Ranking Member
  20 = Other committee Chair or Ranking Member
  15 = Senior/long-tenured member on major committee
  10 = Regular member with known committee assignments
   5 = Member with no committee data or very new member
"""

_POWER_SCORES: dict[str, int] = {
    # ── Congressional leadership (119th Congress) ─────────────────────────
    "Mike Johnson":             35,   # Speaker of the House
    "John Thune":               35,   # Senate Majority Leader
    "Chuck Schumer":            35,   # Senate Minority Leader
    "Hakeem Jeffries":          30,   # House Minority Leader
    "Steve Scalise":            30,   # House Majority Leader
    "Tom Emmer":                30,   # House Majority Whip
    "Katherine Clark":          30,   # House Minority Whip
    "John Barrasso":            30,   # Senate Majority Whip
    "Dick Durbin":              30,   # Senate Minority Whip (prev.)
    "Patty Murray":             25,   # President Pro Tempore

    # ── Major committee chairs / ranking members ───────────────────────────
    "French Hill":              25,   # Chair, House Financial Services
    "Roger Wicker":             25,   # Chair, Senate Armed Services
    "John Boozman":             25,   # Chair, Senate Agriculture
    "Tim Scott":                25,   # Chair, Senate Banking
    "Tom Cotton":               25,   # Chair, Senate Intelligence
    "Brett Guthrie":            25,   # Chair, House Energy & Commerce
    "Jason Smith":              25,   # Chair, House Ways & Means
    "Mike Turner":              25,   # Chair, House Intelligence
    "Mark Green":               20,   # Chair, House Homeland Security
    "Jim Jordan":               20,   # Chair, House Judiciary
    "Jodey Arrington":          20,   # Chair, House Budget
    "Sam Graves":               20,   # Chair, House Transportation
    "Brian Babin":              20,   # Chair, House Science, Space & Tech
    "Glenn Grothman":           15,   # House Oversight subcommittee
    "Debbie Wasserman Schultz": 20,   # Ranking, House Appropriations subcommittee

    # ── Highly influential / long-tenured members ─────────────────────────
    "Nancy Pelosi":             25,   # Former Speaker, 30+ year veteran
    "Mitch McConnell":          25,   # Former Senate Majority Leader
    "Kevin McCarthy":           20,   # Former Speaker
    "Patrick McHenry":          20,   # Former Chair, House Financial Services
    "Richard Burr":             20,   # Former Chair, Senate Intelligence
    "Richard Shelby":           20,   # Former Chair, Senate Appropriations

    # ── Active members with mapped committees ─────────────────────────────
    "Ro Khanna":                10,   # Armed Services + Science/Tech
    "Dan Crenshaw":             10,   # Intelligence + Homeland Security
    "Michael McCaul":           15,   # Foreign Affairs + Science; senior member
    "Josh Gottheimer":          10,   # Financial Services + Homeland Security
    "Tommy Tuberville":         10,   # Armed Services + Agriculture
    "Donald Norcross":          10,   # Armed Services + Transportation
    "Terri Sewell":             10,   # Ways and Means
    "Brian Mast":               10,   # Foreign Affairs + Transportation
    "Nick LaLota":              10,   # Financial Services + Homeland Security
    "Marjorie Taylor Greene":   10,   # Budget + Oversight
    "Warren Davidson":          10,   # Financial Services
    "David Rouzer":             10,   # Agriculture + Transportation
    "Rick Scott":               15,   # Banking + Budget + Commerce; senator
    "John Fetterman":           10,   # Agriculture + Banking + Judiciary
    "Gary Peters":              10,   # Armed Services + Homeland Security
    "Steve Cohen":              10,   # Judiciary + Transportation
    "Mike Kelly":               10,   # Ways and Means + Oversight
    "John McGuire":             10,   # Armed Services
    "Nicholas Begich III":      10,   # Armed Services + Transportation
    "Rick Allen":               10,   # Agriculture + Budget
    "Thomas Kean Jr":           10,   # Science Space Technology + Homeland Security
    "Kevin Hern":               10,   # Ways and Means + Budget
    "Andy Barr":                10,   # Financial Services
    "Bill Foster":              10,   # Financial Services + Science
    "Jared Moskowitz":          10,   # Appropriations + Oversight
    "Jonathan Jackson":         10,   # Oversight
    "Chip Roy":                 10,   # Budget + Oversight
    "David Taylor":             10,   # Financial Services + Oversight
    "Tim Moore":                 5,   # Judiciary (limited data)
    "Matt Van Epps":             5,   # No mapped committee
}

_DEFAULT_SCORE = 5   # Unknown member — assume minimal influence


def get_power_score(politician_name: str) -> tuple[int, str]:
    """Return (score 0-35, explanation) for a politician.

    Scores reflect leadership role, committee chair/ranking-member status,
    and seniority — the factors most consistently linked to trading alpha
    in the post-STOCK Act literature.
    """
    score = _POWER_SCORES.get(politician_name, _DEFAULT_SCORE)

    if score >= 35:
        note = f"{politician_name}: Congressional leadership"
    elif score >= 30:
        note = f"{politician_name}: Senior leadership / Whip"
    elif score >= 25:
        note = f"{politician_name}: Major committee Chair/Ranking Member"
    elif score >= 20:
        note = f"{politician_name}: Committee Chair/Ranking Member"
    elif score >= 15:
        note = f"{politician_name}: Senior / long-tenured member"
    elif score >= 10:
        note = f"{politician_name}: Active committee member"
    else:
        note = f"{politician_name}: Limited influence data"

    return score, note
