"""Al-Aseel style views of the same books: numbered chart of accounts, general ledger, journal book,
trial balance variants, voucher families and printable vouchers."""

import datetime as dt

from sqlalchemy import select

from app import chart as COA
from app import services
from app.i18n import tr
from app.models import Document, JournalLine
from conftest import OPEN

D = lambda day: dt.date(2026, 10, day)  # noqa: E731
t = lambda key, **kw: tr("ar", key, **kw)  # noqa: E731


def busy(w):
    """A month of typical activity."""
    L = w.L
    L.create_document("CONTRIBUTION", D(2), {"partner_id": w.mahmoud.id, "money_account_id": w.bank.id,
                                             "amount": 500_000})
    L.create_document("EXPENSE", D(3), {"amount": 120_000, "category": "meat", "project_id": w.kitchen.id,
                                        "money_account_id": w.cash.id, "supplier": "ملحمة"})
    L.create_document("EXPENSE", D(4), {"amount": 200_000, "category": "rent", "paid_by_partner_id": w.contractor.id})
    L.create_document("INVOICE", D(5), {"customer_id": w.org.id, "project_id": w.kitchen.id, "quantity": 1000,
                                        "unit_price": 900_000})
    L.create_document("RECEIPT", D(6), {"customer_id": w.org.id, "money_account_id": w.bank.id, "amount": 900_000,
                                        "fee": 45_000})
    L.create_document("TRANSFER", D(7), {"money_account_id": w.bank.id, "money_account2_id": w.cash.id,
                                         "amount": 100_000})
    L.create_document("CAPITAL_CALL", D(8), {"amount": 600_000})
    w.s.commit()


def test_control_accounts_match_the_ledger():
    assert (COA.CASH, COA.AR, COA.FA, COA.CAP, COA.DIST, COA.REV, COA.GAIN, COA.EXP, COA.LOSS) == (
        services.CASH, services.AR, services.FA, services.CAP, services.DIST, services.REV, services.GAIN,
        services.EXP, services.LOSS)


def test_every_line_has_a_leaf_inside_the_tree_and_the_tree_balances(opened):
    w = opened
    busy(w)
    nodes = {n.code: n for n in COA.build_chart(w.s, t)}
    for ln in w.s.scalars(select(JournalLine)):
        code = COA.code_of(ln)
        assert code in nodes and not nodes[code].group
        assert COA.matches(code, str(code)[0] + "0000")
    assert nodes[f"111{w.cash.id:02d}"].name == "الصندوق"
    assert nodes[f"310{w.mahmoud.id:02d}"].name == "رأس مال محمود عدنان خله"
    assert nodes["51001"].name == t("cat_meat")
    leaves = [n for n in nodes.values() if not n.group]
    assert sum(n.total_debit for n in leaves) == sum(n.total_credit for n in leaves)
    assert sum(n.balance for n in leaves) == 0
    for g in (n for n in nodes.values() if n.group):  # a main account is the sum of what is under it
        assert g.balance == sum(nodes[c].balance for c in g.children)
    # assets = equity + profit, read straight from the tree
    assert nodes["10000"].balance == -(nodes["30000"].balance + nodes["40000"].balance + nodes["50000"].balance)
    assert nodes["11100"].balance == w.L.balance(services.CASH)


def test_general_ledger_running_balance_and_opening(opened):
    w = opened
    busy(w)
    cash = f"111{w.cash.id:02d}"
    full = COA.account_ledger(w.s, cash)
    assert full["closing"] == w.L.balance(services.CASH, money_account_id=w.cash.id)
    assert [r["balance"] for r in full["rows"]][-1] == full["closing"]
    part = COA.account_ledger(w.s, cash, start=D(4))
    assert part["opening"] == 1_000_000 - 120_000  # opening balance and the meat bought on the 3rd
    assert part["closing"] == full["closing"]
    # a main account gathers its sub-accounts
    group = COA.account_ledger(w.s, "11100")
    assert group["closing"] == w.L.balance(services.CASH)
    # cost-centre filter
    kitchen = COA.account_ledger(w.s, "51001", project_id=w.kitchen.id)
    assert kitchen["closing"] == 120_000


def test_voucher_families(opened):
    w = opened
    busy(w)
    kinds = {(d.doc_type, bool(d.money_account_id)): COA.voucher_kind(d) for d in w.s.scalars(select(Document))}
    assert kinds[("CONTRIBUTION", True)] == "receipt"
    assert kinds[("RECEIPT", True)] == "receipt"
    assert kinds[("EXPENSE", True)] == "payment"
    assert kinds[("EXPENSE", False)] == "journal"  # paid by a partner personally: no cash moved
    assert kinds[("INVOICE", False)] == "invoice"
    assert kinds[("TRANSFER", True)] == "journal"
    assert kinds[("OPENING", False)] == "journal"
    assert kinds[("CAPITAL_CALL", False)] == "memo"
