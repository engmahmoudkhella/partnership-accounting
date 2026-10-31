"""Number entry: Arabic and English digits, separators, and every way input can go wrong."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.numbers import (
    NumberError,
    allocate,
    format_money,
    format_percent,
    format_quantity,
    line_total,
    money_input_value,
    parse_int,
    parse_money,
    parse_percent,
    parse_quantity,
    percent_input_value,
    qty_input_value,
    round_half_up_div,
)

# ----------------------------------------------------------------- valid money (agorot)

VALID_MONEY = [
    # plain ASCII
    ("0.01", 1), ("1", 100), ("17", 1700), ("17.5", 1750), ("17.50", 1750), ("2000", 200000),
    ("180000", 18000000), ("007", 700), ("0.5", 50), ("999999999.99", 99_999_999_999),
    # extra decimal zeros are exact, therefore accepted
    ("10.500", 1050), ("10.5000000", 1050), ("3.00", 300),
    # thousands separators in correct groups
    ("1,000", 100000), ("1,234.56", 123456), ("12,345,678.90", 1234567890), ("180,000", 18000000),
    # Arabic-Indic digits
    ("١٧", 1700), ("١٧٫٥", 1750), ("٢٠٠٠", 200000), ("١٨٠٬٠٠٠", 18000000), ("١٬٢٣٤٫٥٦", 123456),
    ("٠٫٠١", 1), ("١٨٠،٠٠٠", 18000000),
    # Persian / Extended Arabic-Indic digits
    ("۱۷", 1700), ("۲۰۰۰٫۲۵", 200025), ("۱۲۳۴", 123400),
    # mixed digit systems (people do this)
    ("1٧", 1700), ("١7.5", 1750), ("1,٠٠٠", 100000),
    # Arabic decimal separator with ASCII digits and vice versa
    ("17٫5", 1750), ("١٧.٥", 1750),
    # full-width digits
    ("１７", 1700),
    # spaces as thousands separators (normal, no-break, narrow no-break, thin)
    ("1 000", 100000), ("1 000", 100000), ("1 000.5", 100050), ("1 000", 100000),
    ("12 345 678", 1234567800), ("1'000", 100000),
    # surrounding whitespace and invisible direction marks from copy/paste
    ("  17  ", 1700), ("‏17‏", 1700), ("‎١٧‎", 1700), ("‫17.5‬", 1750),
    ("﻿17", 1700), ("؜17", 1700),
    # currency signs / words
    ("₪17", 1700), ("17₪", 1700), ("17 ₪", 1700), ("ILS 17", 1700), ("17 ils", 1700), ("17 NIS", 1700),
    ("17 شيكل", 1700), ("١٧ شيكل", 1700), ("شيكل 17", 1700), ("17 شيقل", 1700), ("17 شواكل", 1700),
]


@pytest.mark.parametrize("text,expected", VALID_MONEY)
def test_parse_money_valid(text, expected):
    assert parse_money(text) == expected


INVALID_MONEY = [
    ("", "err_number_required"), ("   ", "err_number_required"), (None, "err_number_required"),
    ("‏", "err_number_required"), ("₪", "err_number_required"), ("شيكل", "err_number_required"),
    ("-5", "err_number_negative"), ("−5", "err_number_negative"), ("(5)", "err_number_negative"),
    ("-٥", "err_number_negative"), ("+5", "err_number_format"),
    ("abc", "err_number_format"), ("12abc", "err_number_format"), ("خمسة", "err_number_format"),
    ("1e3", "err_number_format"), ("1E3", "err_number_format"), ("0x10", "err_number_format"),
    (".5", "err_number_format"), ("5.", "err_number_format"), ("1.2.3", "err_number_format"),
    ("1..2", "err_number_format"), ("١٫٢٫٣", "err_number_format"),
    # wrong thousands grouping must NOT be silently accepted as a different number
    ("1,00", "err_number_comma_decimal"), ("12,5", "err_number_comma_decimal"), ("1,5", "err_number_comma_decimal"),
    ("12,34", "err_number_comma_decimal"), ("١٢٬٥", "err_number_comma_decimal"),
    ("1,0000", "err_number_format"), ("10,00,000", "err_number_format"), (",100", "err_number_format"),
    ("100,", "err_number_format"), ("1,,000", "err_number_format"), ("1 0 0", "err_number_format"),
    ("1,000,00", "err_number_format"),
    # more decimals than agorot, with a non-zero digit
    ("17.555", "err_number_too_many_decimals"), ("0.001", "err_number_too_many_decimals"),
    ("١٧٫٥٥٥", "err_number_too_many_decimals"),
    # currency we do not support
    ("$17", "err_number_format"), ("17 USD", "err_number_format"), ("17 دولار", "err_number_format"),
    # zero (not allowed for normal amounts)
    ("0", "err_number_zero"), ("0.00", "err_number_zero"), ("٠", "err_number_zero"), ("000", "err_number_zero"),
    # too large
    ("1000000000", "err_number_too_large"), ("99999999999999999999", "err_number_too_large"),
    # percent sign is not money
    ("17%", "err_number_format"),
    # tab inside
    ("1\t000", "err_number_format"),
]


@pytest.mark.parametrize("text,code", INVALID_MONEY)
def test_parse_money_invalid(text, code):
    with pytest.raises(NumberError) as e:
        parse_money(text)
    assert e.value.code == code


def test_money_zero_allowed_when_requested():
    assert parse_money("0", allow_zero=True) == 0
    assert parse_money("٠٫٠٠", allow_zero=True) == 0


def test_too_many_decimals_reports_limit():
    with pytest.raises(NumberError) as e:
        parse_money("1.234")
    assert e.value.params == {"decimals": 2}


def test_non_string_input():
    assert parse_money(17) == 1700


# ----------------------------------------------------------------- quantities and percent

@pytest.mark.parametrize("text,expected", [
    ("1000", 1_000_000), ("1", 1000), ("2.5", 2500), ("0.001", 1), ("١٠٠٠", 1_000_000), ("١٫٢٥", 1250),
    ("1,500", 1_500_000), ("12.3450", 12345),
])
def test_parse_quantity(text, expected):
    assert parse_quantity(text) == expected


@pytest.mark.parametrize("text,code", [("0.0001", "err_number_too_many_decimals"), ("0", "err_number_zero"),
                                       ("-1", "err_number_negative"), ("x", "err_number_format")])
def test_parse_quantity_invalid(text, code):
    with pytest.raises(NumberError) as e:
        parse_quantity(text)
    assert e.value.code == code


@pytest.mark.parametrize("text,expected", [
    ("50", 500_000), ("16.6667", 166_667), ("16.6666", 166_666), ("100", 1_000_000), ("٥٠", 500_000),
    ("١٦٫٦٦٦٧", 166_667), ("50%", 500_000), ("50 %", 500_000), ("٥٠٪", 500_000), ("٪٥٠", 500_000),
    ("0.0001", 1), ("33.3333", 333_333),
])
def test_parse_percent(text, expected):
    assert parse_percent(text) == expected


@pytest.mark.parametrize("text,code", [("100.0001", "err_percent_over_100"), ("150", "err_percent_over_100"),
                                       ("16.66667", "err_number_too_many_decimals"), ("0", "err_number_zero"),
                                       ("-5", "err_number_negative")])
def test_parse_percent_invalid(text, code):
    with pytest.raises(NumberError) as e:
        parse_percent(text)
    assert e.value.code == code


def test_parse_int():
    assert parse_int("١٢") == 12
    with pytest.raises(NumberError):
        parse_int("1.5")


# ----------------------------------------------------------------- formatting

def test_format_money_latin_and_arabic():
    assert format_money(123456) == "1,234.56"
    assert format_money(123456, "arab") == "١٬٢٣٤٫٥٦"
    assert format_money(0) == "0.00"
    assert format_money(5) == "0.05"
    assert format_money(-123456) == "-1,234.56"
    assert format_money(18000000) == "180,000.00"


def test_format_quantity_and_percent():
    assert format_quantity(1_000_000) == "1,000"
    assert format_quantity(2500) == "2.5"
    assert format_quantity(1) == "0.001"
    assert format_percent(166_667) == "16.6667%"
    assert format_percent(500_000) == "50%"
    assert format_percent(500_000, "arab") == "٥٠%"


@given(st.integers(min_value=0, max_value=99_999_999_999))
def test_money_round_trip_latin(minor):
    assert parse_money(format_money(minor), allow_zero=True) == minor
    assert parse_money(money_input_value(minor), allow_zero=True) == minor


@given(st.integers(min_value=0, max_value=99_999_999_999))
def test_money_round_trip_arabic(minor):
    assert parse_money(format_money(minor, "arab"), allow_zero=True) == minor


@given(st.integers(min_value=1, max_value=999_999_999_999))
def test_qty_round_trip(milli):
    assert parse_quantity(qty_input_value(milli)) == milli
    assert parse_quantity(format_quantity(milli)) == milli


@given(st.integers(min_value=1, max_value=1_000_000))
def test_percent_round_trip(ppm):
    assert parse_percent(percent_input_value(ppm)) == ppm


# ----------------------------------------------------------------- arithmetic

def test_line_total_rounding_half_up():
    assert line_total(1_000_000, 1700) == 1_700_000  # 1000 meals x 17
    assert line_total(1500, 333) == 500  # 1.5 x 3.33 = 4.995 -> 5.00
    assert line_total(1, 1) == 0  # 0.001 x 0.01 -> 0.00001 -> 0.00
    assert line_total(500, 1) == 1  # 0.5 x 0.01 = 0.005 -> 0.01 (half up)
    assert line_total(499, 1) == 0


def test_round_half_up_div():
    assert round_half_up_div(5, 10) == 1
    assert round_half_up_div(4, 10) == 0
    assert round_half_up_div(-5, 10) == -1
    with pytest.raises(ValueError):
        round_half_up_div(1, 0)


def test_allocate_partners_exact():
    w = [(1, 500_000), (2, 166_667), (3, 166_667), (4, 166_666)]
    assert allocate(100, w) == {1: 50, 2: 17, 3: 17, 4: 16}
    parts = allocate(1, w)
    assert sum(parts.values()) == 1 and parts[1] == 1
    assert allocate(0, w) == {1: 0, 2: 0, 3: 0, 4: 0}
    assert allocate(-100, w) == {1: -50, 2: -17, 3: -17, 4: -16}
    assert allocate(18_000_000, w) == {1: 9_000_000, 2: 3_000_006, 3: 3_000_006, 4: 2_999_988}


@given(st.integers(min_value=-10**12, max_value=10**12),
       st.lists(st.integers(min_value=0, max_value=1_000_000), min_size=1, max_size=8).filter(lambda x: sum(x) > 0))
def test_allocate_properties(amount, weights):
    ws = list(enumerate(weights))
    parts = allocate(amount, ws)
    assert sum(parts.values()) == amount
    total = sum(weights)
    for k, wt in ws:
        exact = amount * wt / total
        assert abs(parts[k] - exact) < 1 + 1e-9
        if wt == 0:
            assert parts[k] == 0


def test_allocate_rejects_zero_weights():
    with pytest.raises(ValueError):
        allocate(10, [(1, 0)])
