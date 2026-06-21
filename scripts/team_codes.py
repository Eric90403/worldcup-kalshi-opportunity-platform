"""Kalshi WC team code ↔ full team name mapping.
Auto-derived from KXWCGAME series events. Maps Kalshi 3-letter codes
to the team names used by The Odds API.
"""
TEAM_CODES = {
    "JOR": "Jordan",
    "ARG": "Argentina",
    "DZA": "Algeria",
    "AUT": "Austria",
    "COL": "Colombia",
    "POR": "Portugal",
    "COD": "Congo DR",
    "UZB": "Uzbekistan",
    "PAN": "Panama",
    "ENG": "England",
    "CRO": "Croatia",
    "GHA": "Ghana",
    "NZL": "New Zealand",
    "BEL": "Belgium",
    "EGY": "Egypt",
    "IRI": "Iran",
    "URU": "Uruguay",
    "ESP": "Spain",
    "CPV": "Cape Verde",
    "KSA": "Saudi Arabia",
    "SEN": "Senegal",
    "IRQ": "Iraq",
    "NOR": "Norway",
    "FRA": "France",
    "TUR": "Turkey",
    "USA": "USA",
    "PAR": "Paraguay",
    "AUS": "Australia",
    "TUN": "Tunisia",
    "NED": "Netherlands",
    "JPN": "Japan",
    "SWE": "Sweden",
    "ECU": "Ecuador",
    "GER": "Germany",
    "CUW": "Curacao",
    "CIV": "Ivory Coast",
    "RSA": "South Africa",
    "KOR": "South Korea",
    "CZE": "Czechia",
    "MEX": "Mexico",
    "SCO": "Scotland",
    "BRA": "Brazil",
    "MAR": "Morocco",
    "HTI": "Haiti",
    "SUI": "Switzerland",
    "CAN": "Canada",
    "BIH": "Bosnia and Herzegovina",
    "QAT": "Qatar",
}

# Reverse map: full name -> 3-letter code. The Odds API uses slightly
# different names (e.g. "South Korea" vs Kalshi "Korea Republic"),
# so patch them here.
NAME_TO_CODE = {v: k for k, v in TEAM_CODES.items()}
NAME_TO_CODE["Turkiye"] = "TUR"
NAME_TO_CODE["Korea Republic"] = "KOR"
NAME_TO_CODE["IR Iran"] = "IRI"
NAME_TO_CODE["Congo DR"] = "COD"
NAME_TO_CODE["Curaçao"] = "CUW"
