"""Geographic scope tests for HIPAA Safe Harbor identifier category (B).

45 CFR 164.514(b)(2)(i)(B) requires removal of "all geographic subdivisions
smaller than a State, including street address, city, county, precinct, ZIP
code, and their equivalent geocodes". The rule is scoped deliberately: a State
is not itself a subdivision smaller than a State, and neither is a country.
The only sub-state geography Safe Harbor permits is the first three digits of
a ZIP code, subject to the 20,000-person population test in
164.514(b)(2)(i)(B)(1)-(2).

Presidio's NER emits a single LOCATION entity for every place name, so
"Fresno", "CA" and "Mexico" all arrive carrying category B. This module is
where that flat label is resolved into a regulatory scope, so callers can
tell a place the rule requires them to remove from a place the rule leaves
alone.
"""

from __future__ import annotations

from functools import lru_cache

# Two-letter USPS abbreviations and full names for the 50 States, the
# District of Columbia, and the US territories that carry their own postal
# abbreviation. DC is included because 164.514(b)(2)(i)(B) treats the
# District as a State for this purpose (see the definition of "State" at
# 45 CFR 160.103).
_US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
    "AS": "American Samoa", "GU": "Guam",
    "MP": "Northern Mariana Islands", "PR": "Puerto Rico",
    "VI": "Virgin Islands",
}


@lru_cache(maxsize=1)
def _state_or_larger_us() -> frozenset[str]:
    """Casefolded place names that are a US State or larger.

    Country names come from pycountry's ISO 3166-1 tables (name, official
    name, and common name) so the set tracks the standard instead of a
    hand-kept literal that silently goes stale.
    """
    names = set()
    for abbreviation, state in _US_STATES.items():
        names.add(abbreviation.casefold())
        names.add(state.casefold())
    try:
        import pycountry
    except ImportError:  # pragma: no cover - dependency is pinned
        return frozenset(names)
    for country in pycountry.countries:
        for attribute in ("name", "official_name", "common_name"):
            value = getattr(country, attribute, None)
            if value:
                names.add(value.casefold())
    # Everyday forms of the host country that ISO 3166 does not carry.
    names.update({"usa", "u.s.", "u.s.a.", "us", "america"})
    return frozenset(names)


def is_state_or_larger(place: str, jurisdiction: str = "us") -> bool:
    """True when `place` names a State, territory, or country.

    Such a place is outside HIPAA Safe Harbor category (B), which reaches
    only geographic subdivisions *smaller* than a State. Only the US
    jurisdiction is answered today; every other jurisdiction returns False
    so its own pack must make the call rather than inherit HIPAA's.
    """
    if (jurisdiction or "us").lower() != "us":
        return False
    token = place.strip().strip(".,;:'\"()[]").casefold()
    if not token:
        return False
    return token in _state_or_larger_us()
