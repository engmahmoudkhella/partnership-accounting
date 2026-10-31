"""Form definitions per document type and parsing of submitted values."""

from __future__ import annotations

import datetime as dt
import re

from .numbers import NumberError, normalize_digits, parse_money, parse_quantity

# (field, kind, required)
# kinds: money, money0 (zero allowed), qty, partner, money_account, customer, project, asset, call,
#        category, text, paysource
FORM_FIELDS: dict[str, list[tuple[str, str, bool]]] = {
    "CONTRIBUTION": [("partner_id", "partner", True), ("money_account_id", "money_account", True),
                     ("amount", "money", True), ("capital_call_id", "call", False)],
    "CAPITAL_CALL": [("amount", "money", True), ("project_id", "project", False)],
    "CAPITAL_TRANSFER": [("partner_id", "partner", True), ("partner2_id", "partner", True),
                         ("amount", "money", True)],
    "EXPENSE": [("category", "category", True), ("project_id", "project", False), ("amount", "money", True),
                ("pay_source", "paysource", True), ("supplier", "text", False)],
    "ASSET_PURCHASE": [("asset_name", "text", True), ("project_id", "project", False),
                       ("amount", "money", True), ("pay_source", "paysource", True), ("supplier", "text", False)],
    "ASSET_DISPOSAL": [("asset_id", "asset", True), ("amount", "money0", True),
                       ("money_account_id", "money_account", False)],
    "INVOICE": [("customer_id", "customer", True), ("project_id", "project", True),
                ("quantity", "qty", True), ("unit_price", "money", True)],
    "RECEIPT": [("customer_id", "customer", True), ("money_account_id", "money_account", True),
                ("amount", "money", True), ("fee", "money0", False), ("project_id", "project", False)],
    "TRANSFER": [("money_account_id", "money_account", True), ("money_account2_id", "money_account", True),
                 ("amount", "money", True), ("fee", "money0", False)],
    "DISTRIBUTION": [("money_account_id", "money_account", True), ("amount", "money", True)],
    "PRODUCTION": [("project_id", "project", True), ("quantity", "qty", True)],
}

ATTACHMENT_TYPES = ("EXPENSE", "ASSET_PURCHASE", "INVOICE", "RECEIPT", "CONTRIBUTION", "TRANSFER",
                    "DISTRIBUTION", "ASSET_DISPOSAL", "CAPITAL_TRANSFER")

# label key for a field, per document type when it differs
FIELD_LABELS = {
    ("CAPITAL_TRANSFER", "partner_id"): "f_payer_partner",
    ("CAPITAL_TRANSFER", "partner2_id"): "f_receiver_partner",
    ("TRANSFER", "money_account_id"): "f_from_account",
    ("TRANSFER", "money_account2_id"): "f_to_account",
    ("ASSET_DISPOSAL", "amount"): "f_proceeds",
    ("RECEIPT", "project_id"): "f_fee_project",
    ("CAPITAL_CALL", "amount"): "f_call_total",
    ("TRANSFER", "amount"): "f_amount_sent",
    ("RECEIPT", "amount"): "f_amount_settled",
}


def field_label(doc_type: str, field: str) -> str:
    return FIELD_LABELS.get((doc_type, field), "f_" + field)


def parse_date(text: str) -> dt.date:
    s = normalize_digits((text or "").strip())
    s = s.replace("/", "-")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if not m:
        m2 = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", s)
        if not m2:
            raise NumberError("err_date_format")
        d, mo, y = map(int, m2.groups())
    else:
        y, mo, d = map(int, m.groups())
    try:
        return dt.date(y, mo, d)
    except ValueError:
        raise NumberError("err_date_format") from None


def _int_or_none(v):
    v = normalize_digits((v or "").strip())
    if v == "":
        return None
    if not v.isdigit():
        raise NumberError("err_invalid_choice")
    return int(v)


def parse_document_form(doc_type: str, form) -> tuple[dict, dict, dict]:
    """Return (data for Ledger, extras, errors). errors maps field -> NumberError."""
    data: dict = {}
    extras: dict = {}
    errors: dict = {}
    try:
        extras["date"] = parse_date(form.get("date", ""))
    except NumberError as e:
        errors["date"] = e
    data["description"] = (form.get("description") or "").strip()
    for field, kind, required in FORM_FIELDS[doc_type]:
        raw = form.get(field, "")
        raw = raw if isinstance(raw, str) else ""
        try:
            if kind in ("money", "money0"):
                if raw.strip() == "" and not required:
                    data[field] = 0
                else:
                    data[field] = parse_money(raw, allow_zero=(kind == "money0"))
            elif kind == "qty":
                data[field] = parse_quantity(raw)
            elif kind in ("text",):
                if field == "asset_name":
                    extras["asset_name"] = raw.strip()
                    if required and not raw.strip():
                        raise NumberError("err_asset_name_required")
                else:
                    data[field] = raw.strip()[:200]
            elif kind == "category":
                data[field] = raw.strip()
            elif kind == "paysource":
                src = form.get("pay_source", "account")
                if src == "partner":
                    data["paid_by_partner_id"] = _int_or_none(form.get("paid_by_partner_id"))
                    data["money_account_id"] = None
                    if data["paid_by_partner_id"] is None:
                        raise NumberError("err_partner_required")
                else:
                    data["money_account_id"] = _int_or_none(form.get("money_account_id"))
                    data["paid_by_partner_id"] = None
                    if data["money_account_id"] is None:
                        raise NumberError("err_money_account_required")
            else:
                data[field] = _int_or_none(raw)
        except NumberError as e:
            errors[field] = e
    return data, extras, errors
