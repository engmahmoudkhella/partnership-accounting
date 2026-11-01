"""Monthly contracts: rents paid and rents charged, recorded automatically each month."""

import datetime as dt

from sqlalchemy import select

from app.models import Contract, Document
from app.services import AR, CAP, BusinessError, Ledger
from conftest import OPEN


def code(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except BusinessError as e:
        return e.code
    return None


def at(w, today):
    return Ledger(w.s, w.admin, today=today)


def rent_contracts(L, w, start=OPEN):
    land = L.save_contract(name="إيجار الأرض", kind="expense", amount=200_000, start_date=start, months=12,
                           project_id=w.kitchen.id, category="rent", paid_by_partner_id=w.contractor.id,
                           supplier="المؤجر")
    shed = L.save_contract(name="تأجير البركس", kind="income", amount=300_000, start_date=start, months=12,
                           project_id=w.kitchen.id, customer_id=w.org.id)
    return land, shed


def test_due_months_are_recorded_once_with_the_right_effect(opened):
    w = opened
    L = at(w, dt.date(2027, 3, 15))
    land, shed = rent_contracts(L, w)
    assert L.run_contracts() == 12  # Oct..Mar = 6 months x 2 contracts
    w.s.commit()
    assert L.run_contracts() == 0  # running again never duplicates
    docs = list(w.s.scalars(select(Document).where(Document.doc_type.in_(("EXPENSE", "INVOICE")))))
    assert len(docs) == 12
    assert {d.date for d in docs if d.doc_type == "INVOICE"} == {dt.date(2026, m, 1) for m in (10, 11, 12)} | \
        {dt.date(2027, m, 1) for m in (1, 2, 3)}
    first = min((d for d in docs if d.doc_type == "EXPENSE"), key=lambda d: d.date)
    assert first.description == "إيجار الأرض — 10/2026 (1/12)" and first.category == "rent"
    # land rent paid personally by the contractor: added to his capital; building rent owed by the customer
    assert L.balance(CAP, partner_id=w.contractor.id) == 500_000 + 6 * 200_000
    assert L.balance(AR, customer_id=w.org.id) == 6 * 300_000
    assert L.net_profit() == 6 * 100_000
    L.check_invariants()


def test_months_appear_as_time_passes_and_stop_at_the_end(opened):
    w = opened
    L = at(w, dt.date(2026, 10, 31))
    rent_contracts(L, w)
    assert L.run_contracts() == 2
    assert at(w, dt.date(2026, 11, 30)).run_contracts() == 2
    assert at(w, dt.date(2030, 1, 1)).run_contracts() == 20  # the remaining 10 months x 2, never more
    assert at(w, dt.date(2031, 1, 1)).run_contracts() == 0


def test_stop_and_delete(opened):
    w = opened
    L = at(w, dt.date(2026, 11, 15))
    land, shed = rent_contracts(L, w)
    L.run_contracts()
    L.stop_contract(land)
    assert at(w, dt.date(2027, 6, 1)).run_contracts() == 7  # only the building rent continues
    assert code(L.delete_contract, land) == "err_contract_has_docs"
    later = L.save_contract(name="عقد مستقبلي", kind="income", amount=100, start_date=dt.date(2027, 1, 1),
                            months=3, project_id=w.kitchen.id, customer_id=w.org.id)
    L.delete_contract(later)
    assert w.s.get(Contract, later.id) is None


def test_voided_month_is_not_created_again(opened):
    w = opened
    L = at(w, dt.date(2026, 11, 15))
    land, shed = rent_contracts(L, w)
    L.run_contracts()
    inv = w.s.scalars(select(Document).where(Document.doc_type == "INVOICE").order_by(Document.date)).first()
    L.void_document(inv, "لم يُستحق هذا الشهر")
    assert L.run_contracts() == 0
    assert L.balance(AR, customer_id=w.org.id) == 300_000


def test_a_month_that_cannot_be_recorded_waits_with_the_reason(opened):
    w = opened
    L = at(w, dt.date(2026, 11, 15))
    bad = L.save_contract(name="قبل البداية", kind="income", amount=1000, start_date=dt.date(2026, 9, 1),
                          months=3, project_id=w.kitchen.id, customer_id=w.org.id)
    good = L.save_contract(name="سليم", kind="income", amount=1000, start_date=OPEN, months=3,
                           project_id=w.kitchen.id, customer_id=w.org.id)
    assert L.run_contracts() == 2  # only the good one (Oct, Nov)
    assert bad.last_error["code"] == "err_date_before_opening" and not bad.runs
    assert good.last_error is None
    L.check_invariants()


def test_contract_validation(opened):
    w = opened
    L = w.L
    base = dict(name="x", kind="expense", amount=100, start_date=OPEN, months=12, category="rent")
    assert code(L.save_contract, **base) == "err_pay_source"
    assert code(L.save_contract, **{**base, "money_account_id": w.cash.id, "paid_by_partner_id": w.mahmoud.id}) \
        == "err_pay_source"
    assert code(L.save_contract, **{**base, "money_account_id": w.cash.id, "months": 0}) == "err_contract_months"
    assert code(L.save_contract, **{**base, "money_account_id": w.cash.id, "amount": 0}) == "err_amount_positive"
    assert code(L.save_contract, **{**base, "kind": "income"}) == "err_customer_required"
    assert code(L.save_contract, **{**base, "kind": "income", "customer_id": w.org.id}) == "err_project_required"
    assert code(L.save_contract, **{**base, "money_account_id": w.cash.id, "name": " "}) == "err_name_required"


def test_month_end_dates_are_clamped():
    d = Ledger.contract_period_date
    assert d(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28)
    assert d(dt.date(2026, 1, 31), 2) == dt.date(2026, 3, 31)
    assert d(dt.date(2026, 11, 1), 2) == dt.date(2027, 1, 1)
    assert d(dt.date(2026, 12, 15), 12) == dt.date(2027, 12, 15)


def test_profit_and_expense_reports_see_contract_documents(opened):
    w = opened
    L = at(w, dt.date(2026, 10, 1))
    rent_contracts(L, w)
    L.run_contracts()
    assert L.net_profit() == 100_000  # 3,000 rent charged - 2,000 rent paid
