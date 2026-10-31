"""Joint approval of large expenses, and month locking / sign-off."""

import datetime as dt

import pytest
from sqlalchemy import func, select

from app.models import Approval, AuditLog, MonthLock
from app.services import CASH, BusinessError
from conftest import TODAY, ledger_for, make_user

D = lambda day: dt.date(2026, 10, day)  # noqa: E731


def code(fn, *a, **kw):
    with pytest.raises(BusinessError) as e:
        fn(*a, **kw)
    return e.value.code


@pytest.fixture
def team(opened):
    w = opened
    w.L.set_approval_threshold(100_000)  # 1,000 ILS
    w.u = {p: make_user(w.s, name, "partner", getattr(w, p)) for p, name in
           (("contractor", "abd"), ("mahmoud", "mahmoud"), ("momen", "momen"), ("ahmad", "ahmad"))}
    w.Lp = {p: ledger_for(w, u) for p, u in w.u.items()}
    w.acc = make_user(w.s, "acc", "accountant")
    w.La = ledger_for(w, w.acc)
    w.s.commit()
    return w


EXP = lambda w, amount: {"amount": amount, "category": "meat", "money_account_id": w.cash.id}  # noqa: E731


def test_threshold_boundary(team):
    w = team
    at = w.La.create_document("EXPENSE", D(2), EXP(w, 100_000))  # equal to the limit -> no approval
    above = w.La.create_document("EXPENSE", D(2), EXP(w, 100_001))
    assert at.status == "POSTED" and above.status == "PENDING"
    assert not above.lines
    assert w.L.balance(CASH, money_account_id=w.cash.id) == 1_000_000 - 100_000


def test_threshold_zero_means_no_approval(team):
    w = team
    w.L.set_approval_threshold(0)
    assert w.La.create_document("EXPENSE", D(2), EXP(w, 999_999)).status == "POSTED"


def test_threshold_applies_to_asset_purchase_not_to_other_types(team):
    w = team
    a = w.La.create_document("ASSET_PURCHASE", D(2), EXP(w, 200_000) | {"category": ""}, asset_name="x")
    assert a.status == "PENDING"
    t = w.La.create_document("TRANSFER", D(2), {"money_account_id": w.cash.id, "money_account2_id": w.bank.id,
                                                "amount": 500_000})
    assert t.status == "POSTED"


def test_all_partners_must_approve(team):
    w = team
    doc = w.La.create_document("EXPENSE", D(2), EXP(w, 500_000))
    for i, p in enumerate(("contractor", "mahmoud", "momen")):
        assert w.Lp[p].decide(doc, "approve") == "PENDING"
        assert w.L.balance(CASH, money_account_id=w.cash.id) == 1_000_000
    assert w.Lp["ahmad"].decide(doc, "approve") == "POSTED"
    assert w.L.balance(CASH, money_account_id=w.cash.id) == 500_000
    assert w.s.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "approve")) == 4


def test_creator_partner_approves_automatically(team):
    w = team
    doc = w.Lp["mahmoud"].create_document("EXPENSE", D(2), EXP(w, 500_000))
    assert doc.status == "PENDING"
    assert [a.user_id for a in doc.approvals] == [w.u["mahmoud"].id]
    assert code(w.Lp["mahmoud"].decide, doc, "approve") == "err_already_decided"


def test_one_rejection_rejects(team):
    w = team
    doc = w.La.create_document("EXPENSE", D(2), EXP(w, 500_000))
    w.Lp["contractor"].decide(doc, "approve")
    assert code(w.Lp["momen"].decide, doc, "reject", "") == "err_reason_required"
    assert w.Lp["momen"].decide(doc, "reject", "السعر أعلى من السوق") == "REJECTED"
    assert code(w.Lp["ahmad"].decide, doc, "approve") == "err_not_pending"
    assert not doc.lines


def test_only_partner_users_decide(team):
    w = team
    doc = w.La.create_document("EXPENSE", D(2), EXP(w, 500_000))
    assert code(w.La.decide, doc, "approve") == "err_not_approver"
    assert code(w.L.decide, doc, "approve") == "err_not_approver"  # admin without partner link
    viewer = make_user(w.s, "viewer", "viewer")
    assert code(ledger_for(w, viewer).decide, doc, "approve") == "err_not_approver"
    assert code(w.Lp["ahmad"].decide, doc, "maybe") == "err_invalid_choice"


def test_inactive_partner_user_is_not_an_approver(team):
    w = team
    w.L.set_user_active(w.u["ahmad"], False)
    doc = w.La.create_document("EXPENSE", D(2), EXP(w, 500_000))
    for p in ("contractor", "mahmoud", "momen"):
        w.Lp[p].decide(doc, "approve")
    assert doc.status == "POSTED"


def test_edit_restarts_the_approval_round(team):
    w = team
    doc = w.La.create_document("EXPENSE", D(2), EXP(w, 500_000))
    for p in ("contractor", "mahmoud", "momen", "ahmad"):
        w.Lp[p].decide(doc, "approve")
    assert doc.status == "POSTED"
    w.La.update_document(doc, D(2), EXP(w, 600_000))
    assert doc.status == "PENDING" and not doc.lines and not doc.approvals
    assert w.L.balance(CASH, money_account_id=w.cash.id) == 1_000_000
    # lowering below the threshold posts it directly
    w.La.update_document(doc, D(2), EXP(w, 50_000))
    assert doc.status == "POSTED"
    assert w.L.balance(CASH, money_account_id=w.cash.id) == 950_000


def test_rejected_can_be_corrected_and_resubmitted(team):
    w = team
    doc = w.Lp["mahmoud"].create_document("EXPENSE", D(2), EXP(w, 500_000))
    w.Lp["momen"].decide(doc, "reject", "كثير")
    w.Lp["mahmoud"].update_document(doc, D(2), EXP(w, 400_000))
    assert doc.status == "PENDING"
    assert [a.user_id for a in doc.approvals] == [w.u["mahmoud"].id]


def test_approved_but_not_enough_cash_stays_pending_then_posts(team):
    w = team
    doc = w.La.create_document("EXPENSE", D(5), EXP(w, 1_500_000))
    for p in ("contractor", "mahmoud", "momen"):
        w.Lp[p].decide(doc, "approve")
    status = w.Lp["ahmad"].decide(doc, "approve")
    assert status == "PENDING" and w.Lp["ahmad"].last_post_error.code == "err_insufficient_balance"
    assert len(doc.approvals) == 4 and not doc.lines
    w.s.commit()
    assert code(w.La.retry_post, doc) == "err_insufficient_balance"
    w.s.rollback()
    w.La.create_document("CONTRIBUTION", D(5), {"partner_id": w.contractor.id, "money_account_id": w.cash.id,
                                                "amount": 500_000})
    w.La.retry_post(doc)
    assert doc.status == "POSTED" and w.L.balance(CASH, money_account_id=w.cash.id) == 0


def test_retry_post_requires_all_approvals(team):
    w = team
    doc = w.La.create_document("EXPENSE", D(5), EXP(w, 500_000))
    w.Lp["contractor"].decide(doc, "approve")
    assert code(w.La.retry_post, doc) == "err_approvals_missing"


def test_deleting_pending_removes_approvals(team):
    w = team
    doc = w.La.create_document("EXPENSE", D(5), EXP(w, 500_000))
    w.Lp["contractor"].decide(doc, "approve")
    w.La.delete_document(doc)
    assert w.s.scalar(select(func.count(Approval.id))) == 0


# ------------------------------------------------------------------ month lock & sign-off

def test_lock_and_unlock(team):
    w = team
    doc = w.La.create_document("PRODUCTION", D(2), {"project_id": w.kitchen.id, "quantity": 1})
    assert code(w.La.lock_month, "2026-10") == "err_forbidden"
    w.L.lock_month("2026-10")
    assert code(w.L.lock_month, "2026-10") == "err_month_already_locked"
    assert code(w.La.create_document, "PRODUCTION", D(3), {"project_id": w.kitchen.id, "quantity": 1}) \
        == "err_month_locked"
    assert code(w.La.update_document, doc, D(2), {"project_id": w.kitchen.id, "quantity": 2}) == "err_month_locked"
    assert code(w.L.unlock_month, "2026-10", "") == "err_reason_required"
    w.L.unlock_month("2026-10", "تصحيح فاتورة")
    w.La.update_document(doc, D(2), {"project_id": w.kitchen.id, "quantity": 2})
    log = w.s.scalar(select(AuditLog).where(AuditLog.action == "unlock"))
    assert log.note == "تصحيح فاتورة"


@pytest.mark.parametrize("month,err", [("2026-13", "err_month_format"), ("2026-1", "err_month_format"),
                                       ("abcd-10", "err_month_format"), ("2026-11", "err_month_future"),
                                       ("", "err_month_format")])
def test_lock_month_validation(team, month, err):
    assert code(team.L.lock_month, month) == err


def test_unlock_month_not_locked(team):
    assert code(team.L.unlock_month, "2026-10", "سبب") == "err_month_not_locked"


def test_signoff(team):
    w = team
    w.Lp["mahmoud"].signoff_month("2026-10")
    assert code(w.Lp["mahmoud"].signoff_month, "2026-10") == "err_already_decided"
    assert code(w.La.signoff_month, "2026-10") == "err_not_approver"


def test_approval_in_locked_month_refused(team):
    w = team
    # set up a pending doc dated in September (books opened in September), then lock September
    w.L.save_opening(dt.date(2026, 9, 1), {w.cash.id: 1_000_000}, {}, [], {w.contractor.id: 1_000_000})
    w.s.commit()
    doc = w.La.create_document("EXPENSE", dt.date(2026, 9, 5), EXP(w, 500_000))
    assert code(w.L.lock_month, "2026-09") == "err_month_has_pending"
    assert w.s.scalar(select(func.count(MonthLock.id))) == 0
    assert doc.status == "PENDING"
    assert TODAY.month == 10
