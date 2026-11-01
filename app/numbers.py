"""Strict parsing and formatting of numbers typed in Arabic or English.

Design rules (see README, section "Numbers"):

* Money is stored as an integer number of agorot (1/100 shekel). Shares are
  stored as parts-per-million of 100% (1,000,000 = 100%). Quantities are stored
  in thousandths (milli-units). No floats anywhere.
* Accepted digits: ASCII 0-9, Arabic-Indic (U+0660-0669), Extended
  Arabic-Indic / Persian (U+06F0-06F9) and full-width digits. They may be mixed.
* The decimal separator is "." or the Arabic decimal separator "٫" (U+066B).
* Thousands separators: "," "٬" (U+066C) "،" (U+060C) "'" and spaces
  (normal, no-break, narrow no-break, thin). They are only accepted in correct
  groups of three digits, so "1,5" or "12 5" are rejected instead of being
  silently read as 15.
* Invisible direction marks that come with copy/paste from Arabic text are
  removed. A shekel sign or the words "ILS", "شيكل", "شيقل", "شواكل" around the
  number are removed. Any other text is an error.
* Negative numbers, exponents, ".5", "5." and more decimals than allowed are
  errors. For money any third decimal is refused, even "10.500", because
  "1.500" is how many people write 1,500 and we never guess. For quantities
  and percentages extra trailing zeros are accepted.
* Values are never rounded silently.
"""

from __future__ import annotations

import re

MONEY_DECIMALS = 2
QTY_DECIMALS = 3
PERCENT_DECIMALS = 4

MAX_MONEY_MINOR = 99_999_999_999  # 999,999,999.99
MAX_QTY_MILLI = 999_999_999_999  # 999,999,999.999
PPM_TOTAL = 1_000_000  # 100%


class NumberError(ValueError):
    """Raised for any input that is not an exact, valid number.

    ``code`` is a translation key (see i18n.py) and ``params`` its arguments.
    """

    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = params


_DIGIT_MAP = {}
for _base in (0x0660, 0x06F0, 0xFF10):
    for _i in range(10):
        _DIGIT_MAP[chr(_base + _i)] = str(_i)

_INVISIBLE = dict.fromkeys(
    map(ord, "‎‏​‌‍⁦⁧⁨⁩‪‫‬‭‮﻿؜"),
    None,
)

_THOUSANDS_CHARS = ",٬،'    "
_DECIMAL_CHARS = ".٫"
_CURRENCY_WORDS = ("₪", "ils", "nis", "شيكل", "شيقل", "شواكل", "شيكلات", "ش.ج")

_PLAIN_RE = re.compile(r"^(\d+)(?:\.(\d+))?$")
_GROUPED_RE = re.compile(r"^(\d{1,3}(?:,\d{3})+)(?:\.(\d+))?$")


def normalize_digits(text: str) -> str:
    """Convert Arabic-Indic / Persian / full-width digits to ASCII."""
    return "".join(_DIGIT_MAP.get(ch, ch) for ch in text)


def _clean(text) -> str:
    if text is None:
        raise NumberError("err_number_required")
    if not isinstance(text, str):
        text = str(text)
    s = text.translate(_INVISIBLE).strip()
    s = normalize_digits(s)
    low = s.lower()
    changed = True
    while changed:
        changed = False
        for word in _CURRENCY_WORDS:
            if low.startswith(word):
                s, low = s[len(word):].strip(), low[len(word):].strip()
                changed = True
            if low.endswith(word):
                s, low = s[: -len(word)].strip(), low[: -len(word)].strip()
                changed = True
    if s == "":
        raise NumberError("err_number_required")
    return s


def _parse_decimal(text, decimals: int, strict_decimals: bool = False) -> int:
    """Return the value multiplied by 10**decimals as an exact int."""
    s = _clean(text)
    if s.startswith("-") or s.startswith("−") or (s.startswith("(") and s.endswith(")")):
        raise NumberError("err_number_negative")
    if s.startswith("+"):
        raise NumberError("err_number_format")
    # unify separators
    out = []
    for ch in s:
        if ch in _DECIMAL_CHARS:
            out.append(".")
        elif ch in _THOUSANDS_CHARS:
            out.append(",")
        else:
            out.append(ch)
    s = "".join(out)
    if s.count(".") > 1:
        raise NumberError("err_number_format")
    m = _PLAIN_RE.match(s) or _GROUPED_RE.match(s)
    if not m:
        if re.fullmatch(r"\d+,\d{1,2}", s):
            # very likely a comma used as a decimal separator: "12,5"
            raise NumberError("err_number_comma_decimal")
        raise NumberError("err_number_format")
    int_part = m.group(1).replace(",", "")
    frac = m.group(2) or ""
    if strict_decimals and len(frac) > decimals:
        # "1.500" is how many people write one thousand five hundred. For money we never guess:
        # more decimals than agorot is an error, with a hint when it looks like a thousands dot.
        if len(frac) == 3 and "," not in m.group(1):
            raise NumberError("err_number_dot_thousands", value=int_part + frac)
        raise NumberError("err_number_too_many_decimals", decimals=decimals)
    if len(frac) > decimals:
        extra = frac[decimals:]
        if set(extra) != {"0"}:
            raise NumberError("err_number_too_many_decimals", decimals=decimals)
        frac = frac[:decimals]
    frac = frac.ljust(decimals, "0")
    return int(int_part) * (10 ** decimals) + (int(frac) if frac else 0)


def parse_money(text, *, allow_zero: bool = False) -> int:
    """Parse a shekel amount and return agorot (int)."""
    value = _parse_decimal(text, MONEY_DECIMALS, strict_decimals=True)
    if value == 0 and not allow_zero:
        raise NumberError("err_number_zero")
    if value > MAX_MONEY_MINOR:
        raise NumberError("err_number_too_large")
    return value


def parse_quantity(text, *, allow_zero: bool = False) -> int:
    """Parse a quantity and return thousandths (int)."""
    value = _parse_decimal(text, QTY_DECIMALS)
    if value == 0 and not allow_zero:
        raise NumberError("err_number_zero")
    if value > MAX_QTY_MILLI:
        raise NumberError("err_number_too_large")
    return value


def parse_percent(text, *, allow_zero: bool = False) -> int:
    """Parse a percentage (e.g. "16.6667") and return parts per million."""
    s = _clean(text)
    if s.endswith("%") or s.endswith("٪"):
        s = s[:-1].strip()
    elif s.startswith("%") or s.startswith("٪"):
        s = s[1:].strip()
    value = _parse_decimal(s, PERCENT_DECIMALS)
    if value == 0 and not allow_zero:
        raise NumberError("err_number_zero")
    if value > PPM_TOTAL:
        raise NumberError("err_percent_over_100")
    return value


def parse_int(text, *, allow_zero: bool = True) -> int:
    value = _parse_decimal(text, 0)
    if value == 0 and not allow_zero:
        raise NumberError("err_number_zero")
    return value


# ---------------------------------------------------------------- formatting

_AR_DIGITS = str.maketrans("0123456789,.", "٠١٢٣٤٥٦٧٨٩٬٫")


def _group(n: int) -> str:
    return f"{n:,}"


def _fmt_fixed(value: int, decimals: int, digits: str, strip_zeros: bool = False) -> str:
    neg = value < 0
    value = abs(value)
    base = 10 ** decimals
    whole, frac = divmod(value, base)
    s = _group(whole)
    if decimals:
        f = str(frac).rjust(decimals, "0")
        if strip_zeros:
            f = f.rstrip("0")
        if f:
            s += "." + f
    if neg:
        s = "-" + s
    if digits == "arab":
        s = s.translate(_AR_DIGITS)
    return s


def format_money(minor: int, digits: str = "latn") -> str:
    return _fmt_fixed(int(minor), MONEY_DECIMALS, digits)


def format_quantity(milli: int, digits: str = "latn") -> str:
    return _fmt_fixed(int(milli), QTY_DECIMALS, digits, strip_zeros=True)


def format_percent(ppm: int, digits: str = "latn") -> str:
    return _fmt_fixed(int(ppm), PERCENT_DECIMALS, digits, strip_zeros=True) + "%"


def money_input_value(minor: int | None) -> str:
    """Value placed back into an <input> when editing (plain, no grouping)."""
    if minor is None:
        return ""
    whole, frac = divmod(int(minor), 100)
    return f"{whole}.{frac:02d}"


def qty_input_value(milli: int | None) -> str:
    if milli is None:
        return ""
    whole, frac = divmod(int(milli), 1000)
    f = f"{frac:03d}".rstrip("0")
    return f"{whole}.{f}" if f else str(whole)


def percent_input_value(ppm: int | None) -> str:
    if ppm is None:
        return ""
    whole, frac = divmod(int(ppm), 10000)
    f = f"{frac:04d}".rstrip("0")
    return f"{whole}.{f}" if f else str(whole)


# ---------------------------------------------------------------- arithmetic

def round_half_up_div(numerator: int, denominator: int) -> int:
    """Integer division rounding half away from zero (for non-negative inputs)."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator < 0:
        return -round_half_up_div(-numerator, denominator)
    q, r = divmod(numerator, denominator)
    return q + (1 if 2 * r >= denominator else 0)


def line_total(qty_milli: int, unit_price_minor: int) -> int:
    """quantity x unit price, rounded half-up to the agora."""
    return round_half_up_div(qty_milli * unit_price_minor, 1000)


def parse_id(text) -> int | None:
    """An id from a form/query value. Empty -> None. Anything but ASCII/Arabic digits -> NumberError."""
    s = normalize_digits(str(text or "").strip())
    if s == "":
        return None
    if not re.fullmatch(r"[0-9]{1,18}", s):
        raise NumberError("err_invalid_choice")
    return int(s)


def allocate(amount: int, weights: list[tuple[object, int]], tie_offset: int = 0) -> dict:
    """Split ``amount`` (int, may be negative) by integer ``weights``.

    Largest-remainder method: every part is floor/ceil of its exact share and
    the parts always add up to exactly ``amount``. Ties are broken by the order
    of ``weights`` rotated by ``tie_offset`` (deterministic, but callers rotate it
    so the same partner does not always receive the extra agora).
    """
    total_w = sum(w for _, w in weights)
    if total_w <= 0:
        raise ValueError("weights must sum to a positive number")
    sign = -1 if amount < 0 else 1
    a = abs(amount)
    parts = []
    for idx, (key, w) in enumerate(weights):
        q, r = divmod(a * w, total_w)
        parts.append([key, q, r, idx])
    left = a - sum(p[1] for p in parts)
    n = len(parts)
    for p in sorted(parts, key=lambda p: (-p[2], (p[3] - tie_offset) % n))[:left]:
        p[1] += 1
    return {p[0]: sign * p[1] for p in parts}


# ------------------------------------------------------------------ amount in words (تفقيط)

_AR_ONES = ["", "واحد", "اثنان", "ثلاثة", "أربعة", "خمسة", "ستة", "سبعة", "ثمانية", "تسعة"]
_AR_TEENS = ["عشرة", "أحد عشر", "اثنا عشر", "ثلاثة عشر", "أربعة عشر", "خمسة عشر", "ستة عشر", "سبعة عشر",
             "ثمانية عشر", "تسعة عشر"]
_AR_TENS = ["", "", "عشرون", "ثلاثون", "أربعون", "خمسون", "ستون", "سبعون", "ثمانون", "تسعون"]
_AR_HUNDREDS = ["", "مئة", "مئتان", "ثلاثمئة", "أربعمئة", "خمسمئة", "ستمئة", "سبعمئة", "ثمانمئة", "تسعمئة"]
# (one, two, 3-10 plural, 11+ singular)
_AR_SCALES = [("ألف", "ألفان", "آلاف", "ألف"), ("مليون", "مليونان", "ملايين", "مليون"),
              ("مليار", "ملياران", "مليارات", "مليار")]


def _ar_below_1000(n: int) -> str:
    h, r = divmod(n, 100)
    parts = []
    if h:
        parts.append(_AR_HUNDREDS[h])
    if r:
        if r < 10:
            parts.append(_AR_ONES[r])
        elif r < 20:
            parts.append(_AR_TEENS[r - 10])
        else:
            t, o = divmod(r, 10)
            parts.append((_AR_ONES[o] + " و" if o else "") + _AR_TENS[t])
    return " و".join(parts)


def number_to_arabic_words(n: int) -> str:
    if n == 0:
        return "صفر"
    groups = []
    while n:
        n, g = divmod(n, 1000)
        groups.append(g)
    parts = []
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if not g:
            continue
        if i == 0:
            parts.append(_ar_below_1000(g))
            continue
        one, two, plural, single = _AR_SCALES[i - 1]
        if g == 1:
            parts.append(one)
        elif g == 2:
            parts.append(two)
        elif 3 <= g <= 10:
            parts.append(f"{_ar_below_1000(g)} {plural}")
        else:
            parts.append(f"{_ar_below_1000(g)} {single}")
    return " و".join(parts)


_EN_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
            "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def number_to_english_words(n: int) -> str:
    if n == 0:
        return "zero"

    def below_1000(x):
        h, r = divmod(x, 100)
        out = []
        if h:
            out.append(_EN_ONES[h] + " hundred")
        if r:
            out.append(_EN_ONES[r] if r < 20 else _EN_TENS[r // 10] + ("-" + _EN_ONES[r % 10] if r % 10 else ""))
        return " ".join(out)

    parts, scales = [], ["", " thousand", " million", " billion"]
    i = 0
    while n:
        n, g = divmod(n, 1000)
        if g:
            parts.insert(0, below_1000(g) + scales[i])
        i += 1
    return " ".join(parts)


def amount_in_words(minor: int, lang: str = "ar") -> str:
    """Shekels and agorot in words, the way receipts are written: «فقط ... شيكل و... أغورة لا غير»."""
    minor = abs(int(minor or 0))
    shekels, agorot = divmod(minor, 100)
    if lang == "ar":
        s = f"فقط {number_to_arabic_words(shekels)} شيكل"
        if agorot:
            s += f" و{number_to_arabic_words(agorot)} أغورة"
        return s + " لا غير"
    s = f"{number_to_english_words(shekels)} shekels"
    if agorot:
        s += f" and {number_to_english_words(agorot)} agorot"
    return s[0].upper() + s[1:] + " only"
