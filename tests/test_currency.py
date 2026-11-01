"""Dollars: dollar accounts, dollar invoices and receipts, exchange differences. Books stay in shekels."""

import datetime as dt

from sqlalchemy import select

from app.models import Document, JournalLine
from app.services import AR, CAP, CASH, EXP, GAIN, LOSS, REV, Ledger
from conftest import OPEN
from test_contracts import code
from test_http import TODAY, Client, app, client, ready  # noqa: F401

D = dt.date(2026, 10, 10)


def usd_world(w):
    L = w.L
    usd = L.save_master("money_account", None, name="حساب شركة جهاد اسليم", kind="bank", currency="USD")
    w.s.flush()
    return L, usd


def doc(L, t, d, **data):
    return L.create_document(t, d, data)


def fails(w, fn, *a, **kw):
    """The error code of a call that must fail; the session is put back as it was."""
    w.s.commit()
    c = code(fn, *a, **kw)
    w.s.rollback()
    return c


def test_dollar_flow_keeps_books_in_shekels(opened):
    w = opened
    L, usd = usd_world(w)
    # a partner deposits $10,000 at 3.70 -> 37,000 shekels of capital
    doc(L, "CONTRIBUTION", D, partner_id=w.mahmoud.id, money_account_id=usd.id, amount=1_000_000, rate=37_000)
    assert L.fx_balance(usd.id) == 1_000_000
    assert L.balance(CASH, money_account_id=usd.id) == 3_700_000
    assert L.balance(CAP, partner_id=w.mahmoud.id) == 166_667 + 3_700_000
    # invoice 3,000 meals x $5 at 3.70
    inv = doc(L, "INVOICE", D, customer_id=w.org.id, project_id=w.kitchen.id, quantity=3_000_000,
              unit_price=500, currency="USD", rate=37_000)
    assert (inv.fx_amount, inv.amount, inv.currency) == (1_500_000, 5_550_000, "USD")
    assert L.customer_due(w.org.id, D, "USD") == 1_500_000
    assert L.balance(REV) == 5_550_000
    # they pay $10,000 when the dollar is 3.60: the receivable leaves at 3.70 -> 1,000 loss on exchange
    d2 = D + dt.timedelta(days=5)
    rec = doc(L, "RECEIPT", d2, customer_id=w.org.id, money_account_id=usd.id, amount=1_000_000, rate=36_000)
    lines = {(ln.account, ln.category): ln for ln in rec.lines}
    assert lines[(CASH, "")].debit == 3_600_000 and lines[(CASH, "")].fx == 1_000_000
    assert lines[(AR, "")].credit == 3_700_000 and lines[(AR, "")].fx == -1_000_000
    assert lines[(LOSS, "fx")].debit == 100_000
    assert L.customer_due(w.org.id, d2, "USD") == 500_000
    claim = L.customer_claim(w.org.id, d2, "USD")
    assert (claim["invoiced"], claim["paid"], claim["balance"]) == (1_500_000, 1_000_000, 500_000)
    # pay a $1,000 expense from the dollar account at 3.80: cost 3,800, dollars held at 3.65 -> 150 gain
    exp = doc(L, "EXPENSE", d2, category="food", project_id=w.kitchen.id, money_account_id=usd.id,
              amount=100_000, rate=38_000)
    lines = {(ln.account, ln.category): ln for ln in exp.lines}
    assert lines[(EXP, "food")].debit == 380_000
    assert lines[(CASH, "")].credit == 365_000 and lines[(CASH, "")].fx == -100_000
    assert lines[(GAIN, "fx")].credit == 15_000
    assert L.fx_balance(usd.id) == 1_900_000
    # sell $2,000 for 7,400 shekels into the box
    tr = doc(L, "TRANSFER", d2, money_account_id=usd.id, money_account2_id=w.cash.id, amount=200_000,
             amount2=740_000)
    assert tr.rate == 37_000 and tr.fx_amount == 200_000 and tr.amount == 740_000
    assert L.fx_balance(usd.id) == 1_700_000
    L.check_invariants()
    assert L.trial_balance()["sheet_ok"]
    # profit: revenue 55,500 - food 3,800 - exchange loss 1,000 + exchange gain 150 + gain on selling dollars
    held = 3_700_000 + 3_600_000 - 365_000  # shekel value of the $19,000 before the sale
    sold_at = held * 200_000 // 1_900_000
    assert L.net_profit() == 5_550_000 - 380_000 - 100_000 + 15_000 + (740_000 - sold_at)


def test_dollar_rules(opened):
    w = opened
    L, usd = usd_world(w)
    assert fails(w, doc, L, "CONTRIBUTION", D, partner_id=w.mahmoud.id, money_account_id=usd.id,
                amount=100_000) == "err_rate_required"
    doc(L, "CONTRIBUTION", D, partner_id=w.mahmoud.id, money_account_id=usd.id, amount=100_000, rate=37_000)
    # more dollars than the account holds
    assert fails(w, doc, L, "EXPENSE", D, category="food", money_account_id=usd.id, amount=100_001,
                rate=40_000) == "err_insufficient_balance_usd"
    # a dollar debt is not received into a shekel account, and vice versa
    doc(L, "INVOICE", D, customer_id=w.org.id, project_id=w.kitchen.id, quantity=1000, unit_price=10_000,
        currency="USD", rate=37_000)
    assert fails(w, doc, L, "RECEIPT", D, customer_id=w.org.id, money_account_id=w.bank.id,
                amount=10_000) == "err_receipt_currency_usd"
    assert fails(w, doc, L, "RECEIPT", D, customer_id=w.org.id, money_account_id=usd.id, amount=20_000,
                rate=37_000) == "err_receivable_negative_usd"
    assert fails(w, doc, L, "RECEIPT", D, customer_id=w.org.id, money_account_id=usd.id, amount=5_000, fee=100,
                rate=37_000) == "err_fx_no_fee"
    # cannot switch the currency of an account that was used
    assert fails(w, L.save_master, "money_account", usd, name=usd.name, kind="bank", currency="") == "err_currency_in_use"
    # transfers between shekels and dollars need what arrived
    assert fails(w, doc, L, "TRANSFER", D, money_account_id=w.cash.id, money_account2_id=usd.id,
                amount=10_000) == "err_amount2_required"
    t = doc(L, "TRANSFER", D, money_account_id=w.cash.id, money_account2_id=usd.id, amount=36_500, amount2=10_000)
    assert t.rate == 36_500 and L.fx_balance(usd.id) == 110_000


def test_dollar_to_dollar_transfer_has_no_exchange_difference(opened):
    w = opened
    L, usd = usd_world(w)
    usd2 = L.save_master("money_account", None, name="صندوق دولار", kind="cash", currency="USD")
    doc(L, "CONTRIBUTION", D, partner_id=w.mahmoud.id, money_account_id=usd.id, amount=100_000, rate=37_000)
    t = doc(L, "TRANSFER", D, money_account_id=usd.id, money_account2_id=usd2.id, amount=40_000, rate=39_000)
    assert {ln.account for ln in t.lines} == {CASH}
    assert sorted((ln.debit, ln.credit) for ln in t.lines) == [(0, 148_000), (148_000, 0)]
    assert L.fx_balance(usd2.id) == 40_000 and L.fx_balance(usd.id) == 60_000
    L.check_invariants()


def test_dollar_opening_balance_and_quote(world):
    w = world
    L, usd = usd_world(w)
    L.save_opening(OPEN, money={w.cash.id: 100_000}, customers={}, assets=[],
                   partners={w.contractor.id: 100_000 + 370_000}, money_fx={usd.id: 100_000}, rate=37_000)
    assert L.fx_balance(usd.id) == 100_000 and L.balance(CASH, money_account_id=usd.id) == 370_000
    q = L.save_quote(None, date=OPEN, valid_until=None, customer_id=w.org.id, project_id=w.kitchen.id, notes="",
                     lines=[("وجبة", 1_000_000, 500)], currency="USD")
    assert fails(w, L.accept_quote, q, OPEN) == "err_rate_required"
    docs = L.accept_quote(q, OPEN, 36_000)
    assert docs[0].currency == "USD" and docs[0].fx_amount == 500_000 and docs[0].amount == 1_800_000
    L.check_invariants()


def test_voiding_and_editing_dollar_documents(opened):
    w = opened
    L, usd = usd_world(w)
    c = doc(L, "CONTRIBUTION", D, partner_id=w.mahmoud.id, money_account_id=usd.id, amount=100_000, rate=37_000)
    L.update_document(c, D, {"partner_id": w.mahmoud.id, "money_account_id": usd.id, "amount": 200_000,
                             "rate": 36_000})
    assert (c.fx_amount, c.amount) == (200_000, 720_000) and L.fx_balance(usd.id) == 200_000
    L.void_document(c, "خطأ")
    assert L.fx_balance(usd.id) == 0 and L.balance(CASH, money_account_id=usd.id) == 0
    L.check_invariants()


# ------------------------------------------------------------------ web

def test_dollar_receipt_invoice_and_claim_through_the_web(ready):
    c = ready
    pid, cid = c.ids["project"], c.ids["customer"]
    r = c.post("/master/money_account/save", {"name": "حساب جهاد اسليم", "kind": "bank", "currency": "USD"},
               page="/master/money_account")
    assert "flash ok" in r.text
    s = c.db()
    from app.models import MoneyAccount
    usd_id = s.scalar(select(MoneyAccount.id).where(MoneyAccount.currency == "USD"))
    s.close()
    r = c.get("/documents/new/INVOICE")
    assert 'data-testid="fxbox"' in r.text and 'name="currency"' in r.text
    assert 'data-cur="USD"' in c.get("/documents/new/RECEIPT").text
    r = c.post("/documents/new/INVOICE", {"date": "2026-10-20", "customer_id": str(cid), "project_id": str(pid),
                                          "quantity": "3000", "unit_price": "5", "currency": "USD", "rate": "٣٫٧"},
               page="/documents/new/INVOICE")
    assert "flash ok" in r.text, r.text[:3000]
    assert "$15,000.00" in r.text
    s = c.db()
    inv = s.scalar(select(Document).where(Document.doc_type == "INVOICE"))
    assert inv.amount == 5_550_000 and inv.fx_amount == 1_500_000
    s.close()
    # no rate typed and no rates table: the last rate typed (3.7) is used
    r = c.post("/documents/new/RECEIPT", {"date": "2026-10-25", "customer_id": str(cid),
                                          "money_account_id": str(usd_id), "amount": "4000"},
               page="/documents/new/RECEIPT")
    assert "flash ok" in r.text
    # and it is offered on the next form
    assert 'placeholder="3.7"' in c.get("/documents/new/EXPENSE").text
    r = c.get(f"/documents/{inv.id}/invoice")
    assert "$15,000.00" in r.text and "دولار أمريكي" in r.text and 'data-testid="cm-remaining">$11,000.00' in r.text
    r = c.get(f"/reports/customer/{cid}/claim?date=2026-10-31")
    assert "$11,000.00" in r.text and "INV-1" in r.text
    assert "$4,000.00" in c.get(f"/reports/cash/{usd_id}").text
    assert "$4,000.00" in c.get("/").text
    # editing a dollar receipt shows dollars
    s = c.db()
    rec = s.scalar(select(Document).where(Document.doc_type == "RECEIPT"))
    s.close()
    r = c.get(f"/documents/{rec.id}/edit")
    assert 'value="4000.00"' in r.text and 'value="3.7"' in r.text
    r = c.get(f"/documents/{rec.id}/print")
    assert "$4,000.00" in r.text and "دولار" in r.text


# ------------------------------------------------------------------ rates table and revaluation (Al-Aseel)

def test_documents_take_the_rate_of_their_date(opened):
    w = opened
    L, usd = usd_world(w)
    L.save_rate(dt.date(2026, 10, 1), 37_000)
    L.save_rate(dt.date(2026, 10, 15), 36_000)
    a = doc(L, "CONTRIBUTION", dt.date(2026, 10, 10), partner_id=w.mahmoud.id, money_account_id=usd.id, amount=100_000)
    b = doc(L, "CONTRIBUTION", dt.date(2026, 10, 20), partner_id=w.mahmoud.id, money_account_id=usd.id, amount=100_000)
    c = doc(L, "CONTRIBUTION", dt.date(2026, 10, 20), partner_id=w.mahmoud.id, money_account_id=usd.id,
            amount=100_000, rate=35_500)  # a typed rate wins
    assert (a.rate, a.amount) == (37_000, 370_000)
    assert (b.rate, b.amount) == (36_000, 360_000)
    assert c.rate == 35_500
    L.save_rate(dt.date(2026, 10, 15), 36_500)  # correcting a day's rate
    assert L.rate_on(dt.date(2026, 10, 31)) == 36_500
    assert fails(w, L.save_rate, dt.date(2026, 10, 2), 0) == "err_rate_required"


def test_revaluation_brings_dollar_balances_to_the_day_rate(opened):
    w = opened
    L, usd = usd_world(w)
    doc(L, "CONTRIBUTION", D, partner_id=w.mahmoud.id, money_account_id=usd.id, amount=1_000_000, rate=40_000)
    doc(L, "INVOICE", D, customer_id=w.org.id, project_id=w.kitchen.id, quantity=1000, unit_price=500_000,
        currency="USD", rate=40_000)
    end = dt.date(2026, 10, 31)
    rows = L.fx_revaluation_preview(end, 38_000)
    assert {(r["account"], r["diff"]) for r in rows} == {(CASH, -200_000), (AR, -100_000)}
    rv = doc(L, "FX_REVALUATION", end, rate=38_000)
    assert rv.amount == 300_000
    assert L.balance(CASH, money_account_id=usd.id) == 3_800_000 and L.fx_balance(usd.id) == 1_000_000
    assert L.balance(AR, customer_id=w.org.id) == 1_900_000
    assert L.balance(LOSS) == 300_000
    assert L.fx_revaluation_preview(end, 38_000) == []
    assert fails(w, doc, L, "FX_REVALUATION", end, rate=38_000) == "err_fx_nothing"
    # after revaluation, paying the dollars out at 3.80 makes no further difference
    rec = doc(L, "RECEIPT", end, customer_id=w.org.id, money_account_id=usd.id, amount=500_000, rate=38_000)
    assert {ln.account for ln in rec.lines} == {CASH, AR}
    L.check_invariants()
    assert L.trial_balance()["sheet_ok"]


def test_rates_page_and_revaluation_through_the_web(ready):
    c = ready
    c.post("/master/money_account/save", {"name": "دولار", "kind": "bank", "currency": "USD"},
           page="/master/money_account")
    from app.models import MoneyAccount
    s = c.db()
    usd_id = s.scalar(select(MoneyAccount.id).where(MoneyAccount.currency == "USD"))
    s.close()
    r = c.post("/rates/save", {"date": "2026-10-01", "rate": "٣٫٧"}, page="/rates")
    assert "flash ok" in r.text and "3.7" in r.text
    # no rate typed: the day's rate is used
    r = c.post("/documents/new/CONTRIBUTION", {"date": "2026-10-05", "partner_id": str(c.ids["partners"][1]),
                                               "money_account_id": str(usd_id), "amount": "1000"},
               page="/documents/new/CONTRIBUTION")
    assert "flash ok" in r.text and "3,700.00" in r.text
    r = c.get("/rates?date=2026-10-31&rate=3.5")
    assert 'data-testid="reval-preview"' in r.text and "-200.00" in r.text
    r = c.post("/documents/new/FX_REVALUATION", {"date": "2026-10-31", "rate": "3.5"}, page="/rates")
    assert "flash ok" in r.text
    s = c.db()
    rv = s.scalar(select(Document).where(Document.doc_type == "FX_REVALUATION"))
    assert rv.amount == 20_000 and rv.status == "POSTED"
    s.close()
    assert "قيد تسوية فروق العملة" in c.get(f"/documents/{rv.id}").text
    assert c.get(f"/documents/{rv.id}/print").status_code == 200


def test_exchange_differences_have_their_own_accounts(opened):
    from app import chart as COA
    w = opened
    L, usd = usd_world(w)
    doc(L, "CONTRIBUTION", D, partner_id=w.mahmoud.id, money_account_id=usd.id, amount=100_000, rate=37_000)
    e = doc(L, "EXPENSE", D, category="food", money_account_id=usd.id, amount=10_000, rate=38_000)
    codes = {COA.code_of(ln) for ln in e.lines}
    assert "49002" in codes and "49001" not in codes
    assert COA.parent_of("49002") == "40000" and COA.parent_of("59002") == "50000"
