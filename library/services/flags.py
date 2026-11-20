"""Country name (as MusicBrainz and Metal Archives write it) -> flag image code."""
import re

# ISO 3166-1 alpha-2 (plus the UK nations flagcdn.com also draws) -> names and common spellings
_COUNTRIES = """
af Afghanistan|al Albania|dz Algeria|ad Andorra|ao Angola|ag Antigua and Barbuda|ar Argentina|am Armenia|au Australia
at Austria|az Azerbaijan|bs Bahamas|bh Bahrain|bd Bangladesh|bb Barbados|by Belarus|be Belgium|bz Belize|bj Benin
bt Bhutan|bo Bolivia|ba Bosnia and Herzegovina|bw Botswana|br Brazil|bn Brunei|bg Bulgaria|bf Burkina Faso|bi Burundi
kh Cambodia|cm Cameroon|ca Canada|cv Cape Verde|cf Central African Republic|td Chad|cl Chile|cn China|co Colombia
km Comoros|cg Congo|cd Democratic Republic of the Congo|cr Costa Rica|ci Ivory Coast|Côte d'Ivoire|hr Croatia|cu Cuba
cy Cyprus|cz Czech Republic|Czechia|dk Denmark|dj Djibouti|dm Dominica|do Dominican Republic|ec Ecuador|eg Egypt
sv El Salvador|gq Equatorial Guinea|er Eritrea|ee Estonia|sz Eswatini|et Ethiopia|fj Fiji|fi Finland|fr France|ga Gabon
gm Gambia|ge Georgia|de Germany|gh Ghana|gr Greece|gd Grenada|gt Guatemala|gn Guinea|gw Guinea-Bissau|gy Guyana
ht Haiti|hn Honduras|hk Hong Kong|hu Hungary|is Iceland|in India|id Indonesia|ir Iran|iq Iraq|ie Ireland|il Israel
it Italy|jm Jamaica|jp Japan|jo Jordan|kz Kazakhstan|ke Kenya|ki Kiribati|xk Kosovo|kw Kuwait|kg Kyrgyzstan|la Laos
lv Latvia|lb Lebanon|ls Lesotho|lr Liberia|ly Libya|li Liechtenstein|lt Lithuania|lu Luxembourg|mo Macau|mg Madagascar
mw Malawi|my Malaysia|mv Maldives|ml Mali|mt Malta|mh Marshall Islands|mr Mauritania|mu Mauritius|mx Mexico
fm Micronesia|md Moldova|mc Monaco|mn Mongolia|me Montenegro|ma Morocco|mz Mozambique|mm Myanmar|Burma|na Namibia
nr Nauru|np Nepal|nl Netherlands|The Netherlands|Holland|nz New Zealand|ni Nicaragua|ne Niger|ng Nigeria|kp North Korea
mk North Macedonia|Macedonia|no Norway|om Oman|pk Pakistan|pw Palau|ps Palestine|pa Panama|pg Papua New Guinea
py Paraguay|pe Peru|ph Philippines|pl Poland|pt Portugal|pr Puerto Rico|qa Qatar|ro Romania|ru Russia|Russian Federation
rw Rwanda|kn Saint Kitts and Nevis|lc Saint Lucia|vc Saint Vincent and the Grenadines|ws Samoa|sm San Marino
st Sao Tome and Principe|sa Saudi Arabia|sn Senegal|rs Serbia|sc Seychelles|sl Sierra Leone|sg Singapore|sk Slovakia
si Slovenia|sb Solomon Islands|so Somalia|za South Africa|kr South Korea|Korea, South|ss South Sudan|es Spain|lk Sri Lanka
sd Sudan|sr Suriname|se Sweden|ch Switzerland|sy Syria|tw Taiwan|tj Tajikistan|tz Tanzania|th Thailand|tl Timor-Leste
tg Togo|to Tonga|tt Trinidad and Tobago|tn Tunisia|tr Turkey|Türkiye|tm Turkmenistan|tv Tuvalu|ug Uganda|ua Ukraine
ae United Arab Emirates|gb United Kingdom|UK|Great Britain|us United States|United States of America|USA|US|uy Uruguay
uz Uzbekistan|vu Vanuatu|va Vatican City|ve Venezuela|vn Vietnam|ye Yemen|zm Zambia|zw Zimbabwe
gb-eng England|gb-sct Scotland|gb-wls Wales|gb-nir Northern Ireland
fo Faroe Islands|gl Greenland|gi Gibraltar|je Jersey|gg Guernsey|im Isle of Man|ax Åland Islands|nc New Caledonia
pf French Polynesia|bm Bermuda|ky Cayman Islands|cw Curaçao|aw Aruba|re Réunion|gp Guadeloupe|mq Martinique
cs Serbia and Montenegro|su Soviet Union|dd East Germany|yu Yugoslavia|cz Czechoslovakia
"""


def _build():
    table = {}
    names = {}
    for chunk in _COUNTRIES.replace("\n", "|").split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        head, _, rest = chunk.partition(" ")
        if re.fullmatch(r"[a-z]{2}(-[a-z]{3})?", head) and rest:
            code = head
            table[rest.casefold()] = code
            names.setdefault(code, rest)
        else:  # an alias of the previous code
            table[chunk.casefold()] = code
    return table, names


NAMES, CODE_NAMES = _build()  # CODE_NAMES: "hu" -> "Hungary" (the first name listed for a code)
# Names with no modern flag on flagcdn.com
NO_FLAG = {"cs", "su", "dd", "yu"}


def flag_code(country):
    """"Hungary" -> "hu", "Scotland" -> "gb-sct", "[Worldwide]" -> None."""
    code = NAMES.get((country or "").strip().casefold())
    if code in NO_FLAG:
        return None
    return code
