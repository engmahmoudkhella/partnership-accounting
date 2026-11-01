"""What each user may see and do: sections x actions, chosen per user (the role only gives the defaults).

Stored on the user as {"s": {section: [actions]}, "own_only": bool}. An admin can always do everything
(and alone manages users and settings). A user without stored permissions gets his role's defaults.
"""

from __future__ import annotations

VIEW, ADD, EDIT, DELETE, MANAGE = "view", "add", "edit", "delete", "manage"
CRUD = (VIEW, ADD, EDIT, DELETE)

# (section, actions it has), in the order shown on the users page
SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("partners_money", CRUD),   # partner payments, capital calls, transfers between partners, distributions
    ("sales", CRUD),            # invoices, payments received, quotations, financial claims
    ("expenses", CRUD),         # expenses and supplier payments
    ("purchases_stock", CRUD),  # purchases, stock issues / returns / counts, inventory
    ("production", CRUD),       # daily production and recipes
    ("cash", CRUD),             # transfers between accounts, currency rates, exchange differences
    ("assets", CRUD),
    ("contracts", CRUD),        # monthly rents
    ("master", CRUD),           # projects, accounts, customers, suppliers, items
    ("partners", CRUD),         # partners, shares, project shares
    ("opening", (VIEW, EDIT, DELETE)),  # opening balances (delete = void them)
    ("reports", (VIEW,)),       # reports, dashboard figures, statements, export
    ("months", (VIEW, MANAGE)),  # month locks
    ("audit", (VIEW,)),
]
SECTION_ACTIONS = dict(SECTIONS)

DOC_SECTION = {
    "OPENING": "opening",
    "CONTRIBUTION": "partners_money", "CAPITAL_CALL": "partners_money", "CAPITAL_TRANSFER": "partners_money",
    "DISTRIBUTION": "partners_money",
    "INVOICE": "sales", "RECEIPT": "sales",
    "EXPENSE": "expenses", "SUPPLIER_PAYMENT": "expenses",
    "PURCHASE": "purchases_stock", "STOCK_ISSUE": "purchases_stock", "STOCK_RETURN": "purchases_stock",
    "STOCK_COUNT": "purchases_stock",
    "PRODUCTION": "production",
    "TRANSFER": "cash", "FX_REVALUATION": "cash",
    "ASSET_PURCHASE": "assets", "ASSET_DISPOSAL": "assets",
}


def section_of(doc_type: str) -> str:
    return DOC_SECTION.get(doc_type, "partners")


def _all() -> dict[str, set[str]]:
    return {s: set(a) for s, a in SECTIONS}


def _view_all() -> dict[str, set[str]]:
    return {s: {VIEW} for s, _ in SECTIONS}


def preset(role: str) -> dict:
    """The defaults of a role."""
    if role == "admin":
        return {"s": _all(), "own_only": False}
    if role == "accountant":  # everything except managing partners / shares and locking months
        s = _all()
        s["partners"] = {VIEW}
        s["months"] = {VIEW}
        return {"s": s, "own_only": False}
    if role == "partner":
        s = _view_all()
        s["expenses"] |= {ADD, EDIT, DELETE}
        s["production"] |= {ADD, EDIT, DELETE}
        return {"s": s, "own_only": True}
    return {"s": _view_all(), "own_only": False}  # viewer


def effective(user) -> dict:
    """{"s": {section: set(actions)}, "own_only": bool} for a user."""
    if user is None:
        return {"s": {}, "own_only": True}
    if user.role == "admin":
        return preset("admin")
    stored = getattr(user, "permissions", None)
    if not stored:
        return preset(user.role)
    s = {sec: set(acts) & set(SECTION_ACTIONS[sec]) for sec, acts in (stored.get("s") or {}).items()
         if sec in SECTION_ACTIONS}
    return {"s": s, "own_only": bool(stored.get("own_only"))}


def can(user, section: str, action: str = VIEW) -> bool:
    return action in effective(user)["s"].get(section, set())


def clean(raw: dict | None, role: str) -> dict | None:
    """Validate permissions submitted for a user. Anything not a known section/action is dropped.
    Doing anything in a section implies seeing it."""
    if raw is None:
        return None
    out = {}
    for sec, acts in (raw.get("s") or {}).items():
        if sec not in SECTION_ACTIONS:
            continue
        a = [x for x in SECTION_ACTIONS[sec] if x in set(acts)]
        if a and VIEW not in a:
            a.insert(0, VIEW)
        if a:
            out[sec] = a
    return {"s": out, "own_only": bool(raw.get("own_only"))}


def to_json(p: dict) -> dict:
    return {"s": {k: sorted(v) for k, v in p["s"].items()}, "own_only": p["own_only"]}
