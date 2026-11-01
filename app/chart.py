"""Numbered chart of accounts (فهرس الحسابات) in the style accountants know from Al-Aseel.

The books keep a few control accounts (cash, receivables, capital...) with a sub-ledger key on each
journal line (which cash box, which partner...). Here every (control account, key) pair gets a
5-digit leaf code, and the leaves roll up into a fixed tree:

    10000 الأصول
      11000 الأصول المتداولة
        11100 النقدية والبنوك        111NN = one per cash box / bank account
        11200 ذمم الجهات المستفيدة    112NN = one per customer
      12000 الأصول الثابتة           120NN = one per asset
    30000 حقوق الملكية
      31000 رأس مال الشركاء          310NN = one per partner
      32000 أرباح موزعة على الشركاء  320NN = one per partner
    40000 الإيرادات                  41001 إيرادات الخدمات · 49001 أرباح بيع أصول
    50000 المصروفات
      51000 المصاريف التشغيلية       510NN = one per expense category
      59001 خسائر بيع / شطب أصول

Codes are derived, never stored: they stay the same as long as the ids do. Nothing here can change
a balance; it only presents the existing journal.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select

from .models import EXPENSE_CATEGORIES, Asset, Customer, Document, JournalLine, MoneyAccount, Partner

CASH, AR, FA, CAP, DIST, REV, GAIN, EXP, LOSS = (
    "1100", "1200", "1500", "3100", "3200", "4100", "4900", "5100", "5900",
)

# code, parent, nature (d = normally debit, c = normally credit)
GROUPS = [
    ("10000", None, "d"),
    ("11000", "10000", "d"),
    ("11100", "11000", "d"),
    ("11200", "11000", "d"),
    ("12000", "10000", "d"),
    ("30000", None, "c"),
    ("31000", "30000", "c"),
    ("32000", "30000", "d"),
    ("40000", None, "c"),
    ("50000", None, "d"),
    ("51000", "50000", "d"),
]
GROUP_PARENT = {c: p for c, p, _ in GROUPS}
GROUP_NATURE = {c: n for c, _, n in GROUPS}

# control account -> (leaf prefix, parent group, nature)
LEAF_RULES = {
    CASH: ("111", "11100", "d"),
    AR: ("112", "11200", "d"),
    FA: ("120", "12000", "d"),
    CAP: ("310", "31000", "c"),
    DIST: ("320", "32000", "d"),
    EXP: ("510", "51000", "d"),
}
FIXED_LEAVES = {REV: ("41001", "40000", "c"), GAIN: ("49001", "40000", "c"), LOSS: ("59001", "50000", "d")}


def _key(prefix: str, n: int | None) -> str:
    return f"{prefix}{(n or 0):02d}"


def line_code(account: str, *, money_account_id=None, customer_id=None, asset_id=None, partner_id=None,
              category: str = "") -> str:
    if account in FIXED_LEAVES:
        return FIXED_LEAVES[account][0]
    prefix = LEAF_RULES[account][0]
    if account == CASH:
        return _key(prefix, money_account_id)
    if account == AR:
        return _key(prefix, customer_id)
    if account == FA:
        return _key(prefix, asset_id)
    if account in (CAP, DIST):
        return _key(prefix, partner_id)
    # expenses: one leaf per category, in the fixed order of EXPENSE_CATEGORIES
    idx = EXPENSE_CATEGORIES.index(category) + 1 if category in EXPENSE_CATEGORIES else 99
    return _key(prefix, idx)


def code_of(ln: JournalLine) -> str:
    return line_code(ln.account, money_account_id=ln.money_account_id, customer_id=ln.customer_id,
                     asset_id=ln.asset_id, partner_id=ln.partner_id, category=ln.category or "")


def is_group(code: str) -> bool:
    return code in GROUP_PARENT


def matches(code: str, target: str) -> bool:
    """Does leaf ``code`` belong to ``target`` (itself, or a group above it)?"""
    if not is_group(target):
        return code == target
    p = parent_of(code)
    while p:
        if p == target:
            return True
        p = GROUP_PARENT.get(p)
    return False


def parent_of(code: str) -> str | None:
    if is_group(code):
        return GROUP_PARENT[code]
    for _acc, (leaf, parent, _n) in FIXED_LEAVES.items():
        if leaf == code:
            return parent
    for _acc, (prefix, parent, _n) in LEAF_RULES.items():
        if code.startswith(prefix):
            return parent
    return None


def nature_of(code: str) -> str:
    if is_group(code):
        return GROUP_NATURE[code]
    for _acc, (leaf, _p, n) in FIXED_LEAVES.items():
        if leaf == code:
            return n
    for _acc, (prefix, _p, n) in LEAF_RULES.items():
        if code.startswith(prefix):
            return n
    return "d"


def level_of(code: str) -> int:
    n, p = 0, parent_of(code)
    while p:
        n, p = n + 1, GROUP_PARENT.get(p)
    return n


@dataclass
class Node:
    code: str
    name: str
    parent: str | None
    level: int
    group: bool
    nature: str
    debit: int = 0          # movements in the period
    credit: int = 0
    open_debit: int = 0     # movements before the period (opening balance)
    open_credit: int = 0
    children: list = field(default_factory=list)

    @property
    def balance(self) -> int:  # debit-positive, all movements up to the end of the period
        return self.open_debit + self.debit - self.open_credit - self.credit

    @property
    def total_debit(self) -> int:
        return self.open_debit + self.debit

    @property
    def total_credit(self) -> int:
        return self.open_credit + self.credit


def leaf_names(s, t) -> dict[str, str]:
    """Every leaf that can exist now, with its display name."""
    names: dict[str, str] = {}
    for a in s.scalars(select(MoneyAccount).order_by(MoneyAccount.id)):
        names[_key("111", a.id)] = a.name
    for c in s.scalars(select(Customer).order_by(Customer.id)):
        names[_key("112", c.id)] = c.name
    for a in s.scalars(select(Asset).order_by(Asset.id)):
        names[_key("120", a.id)] = a.name
    for p in s.scalars(select(Partner).order_by(Partner.id)):
        names[_key("310", p.id)] = t("coa_capital_of", name=p.name)
        names[_key("320", p.id)] = t("coa_dist_of", name=p.name)
    for i, cat in enumerate(EXPENSE_CATEGORIES, start=1):
        names[_key("510", i)] = t("cat_" + cat)
    names["51099"] = t("coa_other_expense")
    names["41001"] = t("coa_41001")
    names["49001"] = t("coa_49001")
    names["59001"] = t("coa_59001")
    return names


def build_chart(s, t, start: dt.date | None = None, end: dt.date | None = None,
                only_used: bool = False) -> list[Node]:
    """All accounts in code order, groups included, with movements of [start, end] and the opening
    balance before ``start``. ``t`` is the translation function for names."""
    names = leaf_names(s, t)
    nodes: dict[str, Node] = {}
    for code, parent, nature in GROUPS:
        nodes[code] = Node(code, t("coa_" + code), parent, level_of(code), True, nature)

    def leaf(code):
        if code not in nodes:
            nodes[code] = Node(code, names.get(code, code), parent_of(code), level_of(code), False,
                               nature_of(code))
        return nodes[code]

    if not only_used:
        for code in names:
            leaf(code)
    q = select(JournalLine)
    if end:
        q = q.where(JournalLine.date <= end)
    for ln in s.scalars(q):
        n = leaf(code_of(ln))
        if start and ln.date < start:
            n.open_debit += ln.debit
            n.open_credit += ln.credit
        else:
            n.debit += ln.debit
            n.credit += ln.credit
    # roll leaves up into their groups
    for n in list(nodes.values()):
        if n.group:
            continue
        p = n.parent
        while p:
            g = nodes[p]
            g.debit += n.debit
            g.credit += n.credit
            g.open_debit += n.open_debit
            g.open_credit += n.open_credit
            p = g.parent
    for n in nodes.values():
        if n.parent:
            nodes[n.parent].children.append(n.code)
    out = sorted(nodes.values(), key=lambda n: n.code)
    if only_used:
        out = [n for n in out if n.group or n.total_debit or n.total_credit]
    return out


def account_ledger(s, target: str, start: dt.date | None = None, end: dt.date | None = None,
                   project_id: int | None = None) -> dict:
    """حساب الأستاذ: the lines of one account (or of a whole group) with a running balance.
    Lines before ``start`` are summed into the opening balance (رصيد سابق)."""
    q = (select(JournalLine, Document).join(Document, Document.id == JournalLine.document_id)
         .order_by(JournalLine.date, Document.id, JournalLine.id))
    if end:
        q = q.where(JournalLine.date <= end)
    if project_id:
        q = q.where(JournalLine.project_id == project_id)
    opening = 0
    rows = []
    for ln, doc in s.execute(q):
        code = code_of(ln)
        if not matches(code, target):
            continue
        if start and ln.date < start:
            opening += ln.debit - ln.credit
            continue
        rows.append({"line": ln, "doc": doc, "code": code})
    bal = opening
    total_d = total_c = 0
    for r in rows:
        bal += r["line"].debit - r["line"].credit
        total_d += r["line"].debit
        total_c += r["line"].credit
        r["balance"] = bal
    return {"opening": opening, "rows": rows, "total_debit": total_d, "total_credit": total_c, "closing": bal}


def voucher_kind(doc: Document) -> str:
    """The Al-Aseel voucher family of a document: سند قبض / سند صرف / سند قيد / إشعار مدين / مذكرة."""
    t = doc.doc_type
    if t in ("RECEIPT", "CONTRIBUTION"):
        return "receipt"
    if t == "DISTRIBUTION":
        return "payment"
    if t in ("EXPENSE", "ASSET_PURCHASE"):
        return "payment" if doc.money_account_id else "journal"  # paid by a partner: no cash moves
    if t == "INVOICE":
        return "invoice"
    if t in ("CAPITAL_CALL", "PRODUCTION"):
        return "memo"
    return "journal"  # OPENING, TRANSFER, CAPITAL_TRANSFER, ASSET_DISPOSAL
