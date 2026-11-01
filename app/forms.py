"""Form definitions per document type and parsing of submitted values."""

from __future__ import annotations

import datetime as dt
import re

from .numbers import NumberError, normalize_digits, parse_id, parse_money, parse_quantity

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
                ("pay_source", "paysource_credit", True), ("supplier", "text", False)],
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
    "PURCHASE": [("supplier_id", "supplier", True), ("project_id", "project", False), ("items", "items", True),
                 ("paid", "money0", False), ("pay_source", "paysource_opt", False)],
    "SUPPLIER_PAYMENT": [("supplier_id", "supplier", True), ("amount", "money", True),
                         ("pay_source", "paysource", True)],
    "STOCK_ISSUE": [("project_id", "project", True), ("category", "issue_reason", False), ("items", "items", True)],
    "STOCK_RETURN": [("project_id", "project", True), ("items", "items", True)],
    "STOCK_COUNT": [("project_id", "project", False), ("items", "items", True)],
}
MAX_ITEM_ROWS = 40

ATTACHMENT_TYPES = ("EXPENSE", "ASSET_PURCHASE", "INVOICE", "RECEIPT", "CONTRIBUTION", "TRANSFER",
                    "DISTRIBUTION", "ASSET_DISPOSAL", "CAPITAL_TRANSFER", "PURCHASE", "SUPPLIER_PAYMENT")

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
    ("PURCHASE", "paid"): "f_paid_now",
    ("STOCK_ISSUE", "category"): "f_issue_reason",
    ("SUPPLIER_PAYMENT", "amount"): "f_amount_paid",
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
    return parse_id(v)


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
    data["confirm_overpay"] = form.get("confirm_overpay") == "1"
    data["ref_no"] = (form.get("ref_no") or "").strip()[:100]
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
            elif kind == "issue_reason":
                data[field] = "stock_diff" if raw.strip() == "stock_diff" else ""
            elif kind == "category":
                data[field] = raw.strip()
            elif kind == "items":
                rows = []
                priced = doc_type == "PURCHASE"
                for n in range(MAX_ITEM_ROWS):
                    iid, q, pr = (form.get(f"item_id_{n}") or ""), (form.get(f"qty_{n}") or ""), (form.get(f"price_{n}") or "")
                    if not (str(iid).strip() or str(q).strip() or str(pr).strip()):
                        continue
                    if doc_type == "STOCK_COUNT" and not str(q).strip():
                        continue  # item listed but not counted
                    row = {"item_id": _int_or_none(iid),
                           "quantity": parse_quantity(q, allow_zero=(doc_type == "STOCK_COUNT"))}
                    if priced:
                        row["unit_price"] = parse_money(pr)
                    if row["item_id"] is None:
                        raise NumberError("err_item_required")
                    rows.append(row)
                if not rows:
                    raise NumberError("err_items_required")
                data["items"] = rows
            elif kind == "paysource_opt" and not data.get("paid"):
                data["money_account_id"] = data["paid_by_partner_id"] = None
            elif kind == "paysource_credit" and form.get("pay_source") == "credit":
                data["supplier_id"] = _int_or_none(form.get("supplier_id"))
                data["money_account_id"] = data["paid_by_partner_id"] = None
                if data["supplier_id"] is None:
                    raise NumberError("err_supplier_required")
            elif kind in ("paysource", "paysource_opt", "paysource_credit"):
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
