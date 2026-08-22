"""Text -> number helpers shared by every adapter.

Japanese exporter sites and Swiss classifieds format the same facts a dozen
different ways (`39,000 Km`, `39'000 km`, `3.9万km`, `US$ 36,000`,
`CHF 33'000.-`). Every adapter funnels through these so a new site is a
selector change, not a new parsing dialect.
"""

from __future__ import annotations

import re
from datetime import date

from .models import PriceTerms, Steering

__all__ = [
    "text_of", "to_int", "to_float", "parse_price", "parse_mileage",
    "parse_year", "parse_month", "parse_engine_cc", "parse_grade",
    "parse_steering", "parse_price_terms", "parse_transmission",
    "parse_repair_history", "parse_chassis", "split_make_model",
]

# Swiss sites use U+2019, Japanese ones use commas, some use thin spaces.
_THOUSANDS = str.maketrans({"'": "", "’": "", " ": "", ",": "", " ": "", "`": ""})
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def text_of(html_or_text: str | None) -> str:
    if not html_or_text:
        return ""
    return _WS.sub(" ", _TAG.sub(" ", html_or_text)).strip()


def to_float(raw: str | float | int | None) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = str(raw).translate(_THOUSANDS)
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def to_int(raw: str | float | int | None) -> int | None:
    v = to_float(raw)
    return int(round(v)) if v is not None else None


def parse_price(raw: str | None) -> float | None:
    """Extract a price. Handles `US$ 36,000`, `CHF 33’000.-`, `$36000`, `¥1,200,000`.

    Japanese yen amounts are returned as-is; the caller is responsible for
    knowing which currency the source quotes in.
    """
    if not raw:
        return None
    text = text_of(raw)
    if re.search(r"\b(ask|inquiry|negotiab|sold|call|-{2,})\b", text, re.I):
        return None
    # A 4-digit year adjacent to the price must not be mistaken for it.
    text = re.sub(r"\b(19|20)\d{2}\s*(year|y|年)\b", " ", text, flags=re.I)
    val = to_float(text)
    if val is None or val <= 0:
        return None
    return val


def parse_mileage(raw: str | None) -> int | None:
    """Kilometres. Understands `39,000 km`, `39’000 km`, `3.9万km`, `12,000 miles`."""
    if not raw:
        return None
    text = text_of(raw)

    man = re.search(r"([\d.,]+)\s*万\s*(?:km|キロ)?", text, re.I)
    if man:
        v = to_float(man.group(1))
        return int(round(v * 10_000)) if v is not None else None

    miles = re.search(r"([\d.,'’ ]+)\s*(?:miles|mi\b)", text, re.I)
    if miles:
        v = to_float(miles.group(1))
        return int(round(v * 1.609344)) if v is not None else None

    km = re.search(r"([\d.,'’ ]+)\s*(?:km|k m|kilomet)", text, re.I)
    v = to_float(km.group(1)) if km else to_float(text)
    if v is None:
        return None
    # Some sites express mileage in thousands ("39k km").
    if km and re.search(r"\d\s*k\s*km", text, re.I) and v < 1000:
        v *= 1000
    return int(round(v)) if 0 <= v <= 2_000_000 else None


def parse_year(raw: str | None) -> int | None:
    if not raw:
        return None
    text = text_of(raw)
    this_year = date.today().year
    for m in re.finditer(r"(19[5-9]\d|20[0-4]\d)", text):
        y = int(m.group())
        if 1950 <= y <= this_year + 1:
            return y
    return None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
)}


def parse_month(raw: str | None) -> int | None:
    """Month of first registration from `2019/06`, `Jun 2019`, `2019年6月`."""
    if not raw:
        return None
    text = text_of(raw).lower()

    m = re.search(r"(19|20)\d{2}\s*[/\-年]\s*(\d{1,2})", text)
    if m:
        month = int(m.group(2))
        return month if 1 <= month <= 12 else None

    m = re.search(r"\b([a-z]{3})[a-z]*\.?\s+(19|20)\d{2}", text)
    if m and m.group(1) in _MONTHS:
        return _MONTHS[m.group(1)]

    m = re.search(r"\b(\d{1,2})\s*/\s*(19|20)\d{2}", text)
    if m:
        month = int(m.group(1))
        return month if 1 <= month <= 12 else None
    return None


def parse_engine_cc(raw: str | None) -> int | None:
    """Displacement in cc. `4700 cc`, `4.7L`, `3,996cc`."""
    if not raw:
        return None
    text = text_of(raw)

    cc = re.search(r"([\d.,'’]+)\s*(?:cc|cm3|cm³|㏄)", text, re.I)
    if cc:
        v = to_int(cc.group(1))
        return v if v and 300 <= v <= 12_000 else None

    litres = re.search(r"(\d(?:\.\d)?)\s*(?:l\b|litre|liter|ℓ)", text, re.I)
    if litres:
        v = to_float(litres.group(1))
        return int(round(v * 1000)) if v else None
    return None


def parse_grade(raw: str | None) -> float | None:
    """Japanese auction grade: 5, 4.5, 4, 3.5, R/RA (accident repaired)."""
    if not raw:
        return None
    text = text_of(raw).upper()
    if re.search(r"\bR\s*A?\b", text) and not re.search(r"\d", text):
        return 0.0  # graded R = repaired; treat as the worst possible grade
    m = re.search(r"\b([0-6](?:\.5)?)\b", text)
    if not m:
        return None
    g = float(m.group(1))
    return g if 0 <= g <= 6 else None


def parse_steering(raw: str | None) -> Steering:
    if not raw:
        return Steering.UNKNOWN
    text = text_of(raw).lower()
    if re.search(r"\b(lhd|left)\b|left[- ]?hand|左ハンドル", text):
        return Steering.LHD
    if re.search(r"\b(rhd|right)\b|right[- ]?hand|右ハンドル", text):
        return Steering.RHD
    return Steering.UNKNOWN


def parse_price_terms(raw: str | None) -> tuple[PriceTerms, str | None]:
    """`(terms, port)` from strings like `C&F Zeebrugge` or `FOB Yokohama`."""
    if not raw:
        return PriceTerms.UNKNOWN, None
    text = text_of(raw)
    upper = text.upper()

    if re.search(r"\bCIF\b", upper):
        terms = PriceTerms.CIF
    elif re.search(r"C\s*&\s*F|\bCNF\b|\bC&F\b|\bCFR\b", upper):
        terms = PriceTerms.CF
    elif re.search(r"\bFOB\b", upper):
        terms = PriceTerms.FOB
    else:
        return PriceTerms.UNKNOWN, None

    port = None
    m = re.search(r"(?:CIF|C\s*&\s*F|CNF|CFR|FOB)\s*[:\-]?\s*([A-Z][A-Za-z\- ]{2,25})", text, re.I)
    if m:
        candidate = m.group(1).strip(" -")
        if candidate and not re.fullmatch(r"(?i)(price|usd|us|total)", candidate):
            port = candidate
    return terms, port


def parse_transmission(raw: str | None) -> str | None:
    if not raw:
        return None
    text = text_of(raw).lower()
    if re.search(r"\bat\b|automatic|オートマ|tiptronic|pdk|dsg|dct", text):
        return "Automatic"
    if re.search(r"\bmt\b|manual|マニュアル|\b\d\s*speed manual\b", text):
        return "Manual"
    return None


def parse_repair_history(raw: str | None) -> bool | None:
    """True when the listing declares accident/repair history."""
    if not raw:
        return None
    text = text_of(raw).lower()
    if re.search(r"no accident|accident[- ]?free|no repair|修復歴なし|none", text):
        return False
    if re.search(r"repair(ed)? history|accident (history|repaired)|修復歴あり|damaged", text):
        return True
    return None


_CHASSIS = re.compile(r"\b([A-Z]{2,5}[- ]?[A-Z0-9]{2,8}[- ]?[0-9X*]{4,10})\b")


def parse_chassis(raw: str | None) -> str | None:
    """Chassis / VIN as printed, keeping redaction asterisks for prefix dedupe."""
    if not raw:
        return None
    text = text_of(raw).upper()
    m = _CHASSIS.search(text)
    if not m:
        return None
    val = m.group(1).strip()
    return val if len(re.sub(r"[^A-Z0-9*]", "", val)) >= 8 else None


_KNOWN_MAKES = [
    "Mercedes-Benz", "Mercedes Benz", "Mercedes", "Porsche", "BMW", "Alfa Romeo",
    "Maserati", "Abarth", "Land Rover", "Range Rover", "Aston Martin", "Rolls-Royce",
    "Volkswagen", "Audi", "Ferrari", "Lamborghini", "Bentley", "Jaguar", "Mini",
    "Toyota", "Nissan", "Honda", "Mazda", "Subaru", "Mitsubishi", "Lexus", "Suzuki",
    "Volvo", "Peugeot", "Renault", "Citroen", "Fiat", "Ford", "Chevrolet", "Jeep",
    "Dodge", "Tesla", "Opel", "Skoda", "Seat", "Hyundai", "Kia", "Isuzu", "Daihatsu",
]


def split_make_model(title: str | None) -> tuple[str, str]:
    """`Maserati Quattroporte Sport GT S` -> `(Maserati, Quattroporte Sport GT S)`.

    Falls back to first-word/rest, which is right far more often than not for
    car listing titles.
    """
    text = text_of(title)
    if not text:
        return "", ""
    lowered = text.lower()
    for make in _KNOWN_MAKES:
        if lowered.startswith(make.lower()):
            canonical = "Mercedes-Benz" if make.lower().startswith("mercedes") else make
            return canonical, text[len(make):].strip(" -,")
    parts = text.split(None, 1)
    return (parts[0], parts[1].strip()) if len(parts) == 2 else (parts[0], "")
