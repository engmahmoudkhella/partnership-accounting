"""Opening balances: every rule around the start of the books."""

import datetime as dt

import pytest
from sqlalchemy import func, select

from app.models import Asset, AuditLog, Document, JournalLine
from app.services import AR, CAP, CASH, FA, BusinessError
from conftest import OPEN, TODAY, ledger_for, make_user


def caps(w):
    return {w.contractor.id: 500_000, w.mahmoud.id: 166_667, w.momen.id: 166_667, w.ahmad.id: 166_666}


def code(fn, *a, **kw):
    with pytest.raises(BusinessError) as e:
        fn(*a, **kw)
    return e.value.code


# ------------------------------------------------------------------ creation

def test_nothing_can_be_entered_before_opening(world):
    w = world
    assert code(w.L.create_document, "EXPENSE", OPEN, {"amount": 100, "category": "food",
                                                        "money_account_id": w.cash.id}) == "err_opening_required"


def test_opening_balanced_creates_lines(world):
    w = world
    doc = w.L.save_opening(OPEN, money={w.cash.id: 600_000, w.bank.id: 400_000}, customers={w.org.id: 50_000},
                           assets=[{"name": "بركس", "amount": 18_000_000, "project_id": w.kitchen.id}],
                           partners={w.contractor.id: 9_525_000, w.mahmoud.id: 3_175_000, w.momen.id: 3_175_000,
                                     w.ahmad.id: 3_175_000})
    w.s.commit()
    L = w.L
    assert doc.status == "POSTED" and doc.date == OPEN
    assert L.balance(CASH, money_account_id=w.cash.id) == 600_000
    assert L.balance(CASH, money_account_id=w.bank.id) == 400_000
    assert L.balance(AR, customer_id=w.org.id) == 50_000
    assert L.balance(FA) == 18_000_000
    assert L.balance(CAP, partner_id=w.contractor.id) == 9_525_000
    tb = L.trial_balance()
    assert tb["balanced"] and tb["sheet_ok"] and tb["assets"] == 19_050_000
    asset = w.s.scalar(select(Asset))
    assert asset.name == "بركس" and asset.source_document_id == doc.id and asset.project_id == w.kitchen.id
    assert w.s.scalar(select(func.count(AuditLog.id)).where(AuditLog.entity == "opening")) == 1


def test_opening_all_zero_is_allowed_and_sets_start_date(world):
    w = world
    doc = w.L.save_opening(OPEN, {}, {}, [], {})
    assert doc.amount == 0 and not doc.lines
    assert w.L.opening_date() == OPEN


def test_opening_zero_lines_are_dropped(world):
    w = world
    doc = w.L.save_opening(OPEN, {w.cash.id: 0, w.bank.id: 100}, {w.org.id: 0}, [], {w.contractor.id: 100,
                                                                                    w.mahmoud.id: 0})
    kinds = sorted((o.kind, o.amount) for o in doc.opening_lines)
    assert kinds == [("money", 100), ("partner", 100)]


@pytest.mark.parametrize("money,capital,diff", [(1_000_000, 999_999, 1), (1_000_000, 1_000_001, -1),
                                                 (1, 0, 1), (0, 5, -5)])
def test_opening_must_balance_to_the_agora(world, money, capital, diff):
    w = world
    with pytest.raises(BusinessError) as e:
        w.L.save_opening(OPEN, {w.cash.id: money}, {}, [], {w.contractor.id: capital})
    assert e.value.code == "err_opening_unbalanced"
    assert e.value.params["diff"] == diff
    assert w.L.opening_doc() is None


def test_opening_rejects_negative_and_bad_assets(world):
    w = world
    assert code(w.L.save_opening, OPEN, {w.cash.id: -1}, {}, [], {}) == "err_number_negative"
    assert code(w.L.save_opening, OPEN, {}, {}, [{"name": "x", "amount": 0}], {}) == "err_amount_positive"
    assert code(w.L.save_opening, OPEN, {}, {}, [{"name": "  ", "amount": 5}], {w.contractor.id: 5}) \
        == "err_name_required"
    assert code(w.L.save_opening, OPEN, {9999: 5}, {}, [], {w.contractor.id: 5}) == "err_not_found"
    assert code(w.L.save_opening, OPEN, {}, {9999: 5}, [], {w.contractor.id: 5}) == "err_not_found"
    assert code(w.L.save_opening, OPEN, {w.cash.id: 5}, {}, [], {9999: 5}) == "err_not_found"


def test_opening_date_rules(world):
    w = world
    assert code(w.L.save_opening, TODAY + dt.timedelta(days=1), {}, {}, [], {}) == "err_date_future"
    assert code(w.L.save_opening, None, {}, {}, [], {}) == "err_date_required"
    # shares start 2026-01-01; an opening before that has no shares
    assert code(w.L.save_opening, dt.date(2025, 12, 31), {}, {}, [], {}) == "err_no_shares"


def test_only_admin_or_accountant(world):
    w = world
    viewer = make_user(w.s, "v", "viewer")
    partner_user = make_user(w.s, "p", "partner", w.mahmoud)
    for u in (viewer, partner_user):
        assert code(ledger_for(w, u).save_opening, OPEN, {}, {}, [], {}) == "err_forbidden"
    acc = make_user(w.s, "acc", "accountant")
    ledger_for(w, acc).save_opening(OPEN, {}, {}, [], {})


# ------------------------------------------------------------------ editing

def test_edit_opening_replaces_lines_and_logs_before_after(opened):
    w = opened
    w.L.save_opening(OPEN, {w.cash.id: 600_000, w.bank.id: 400_000}, {}, [], caps(w))
    w.s.commit()
    assert w.L.balance(CASH, money_account_id=w.cash.id) == 600_000
    assert w.L.balance(CASH, money_account_id=w.bank.id) == 400_000
    assert w.s.scalar(select(func.count(Document.id)).where(Document.doc_type == "OPENING")) == 1
    log = w.s.scalars(select(AuditLog).where(AuditLog.entity == "opening").order_by(AuditLog.id)).all()
    assert [x.action for x in log] == ["create", "update"]
    assert log[1].before["opening_lines"][0]["amount"] == 1_000_000


def test_edit_opening_keeps_and_renames_asset(world):
    w = world
    w.L.save_opening(OPEN, {}, {}, [{"name": "بركس", "amount": 100}], {w.contractor.id: 100})
    a = w.s.scalar(select(Asset))
    w.L.save_opening(OPEN, {}, {}, [{"id": a.id, "name": "بركس كبير", "amount": 200}], {w.contractor.id: 200})
    w.s.commit()
    assets = w.s.scalars(select(Asset)).all()
    assert len(assets) == 1 and assets[0].name == "بركس كبير" and w.L.balance(FA, asset_id=a.id) == 200


def test_edit_opening_removing_an_asset_deletes_it(world):
    w = world
    w.L.save_opening(OPEN, {}, {}, [{"name": "a", "amount": 100}, {"name": "b", "amount": 50}],
                     {w.contractor.id: 150})
    a, b = w.s.scalars(select(Asset).order_by(Asset.id)).all()
    w.L.save_opening(OPEN, {}, {}, [{"id": a.id, "name": "a", "amount": 100}], {w.contractor.id: 100})
    w.s.commit()
    assert [x.name for x in w.s.scalars(select(Asset))] == ["a"]


def test_edit_opening_cannot_reference_foreign_asset(opened):
    w = opened
    w.L.create_document("ASSET_PURCHASE", OPEN, {"amount": 100, "money_account_id": w.cash.id}, asset_name="x")
    other = w.s.scalar(select(Asset))
    assert code(w.L.save_opening, OPEN, {w.cash.id: 1_000_000}, {},
                [{"id": other.id, "name": "x", "amount": 1}], {**caps(w), w.contractor.id: 500_001}) == "err_not_found"


def test_opening_asset_with_disposal_cannot_be_removed(world):
    w = world
    w.L.save_opening(OPEN, {w.cash.id: 0}, {}, [{"name": "a", "amount": 100}], {w.contractor.id: 100})
    a = w.s.scalar(select(Asset))
    w.L.create_document("ASSET_DISPOSAL", OPEN, {"asset_id": a.id, "amount": 0})
    assert code(w.L.save_opening, OPEN, {}, {}, [], {}) == "err_asset_has_disposal"


def test_opening_date_cannot_move_after_first_transaction(opened):
    w = opened
    w.L.create_document("PRODUCTION", dt.date(2026, 10, 5), {"project_id": w.kitchen.id, "quantity": 1000})
    assert code(w.L.save_opening, dt.date(2026, 10, 6), {w.cash.id: 1_000_000}, {}, [], caps(w)) \
        == "err_opening_after_docs"
    # moving it earlier is fine, and moving it to the same day as the first transaction too
    w.L.save_opening(dt.date(2026, 10, 5), {w.cash.id: 1_000_000}, {}, [], caps(w))
    w.L.save_opening(dt.date(2026, 9, 1), {w.cash.id: 1_000_000}, {}, [], caps(w))


def test_transactions_before_opening_are_rejected(opened):
    w = opened
    assert code(w.L.create_document, "PRODUCTION", OPEN - dt.timedelta(days=1),
                {"project_id": w.kitchen.id, "quantity": 1000}) == "err_date_before_opening"
    w.L.create_document("PRODUCTION", OPEN, {"project_id": w.kitchen.id, "quantity": 1000})


def test_opening_cannot_change_after_any_lock(opened):
    w = opened
    w.L.lock_month("2026-10")
    assert code(w.L.save_opening, OPEN, {w.cash.id: 1_000_000}, {}, [], caps(w)) == "err_opening_locked"


def test_opening_edit_that_breaks_later_balance_is_refused(opened):
    w = opened
    # spend 9,000 out of the 10,000 opening cash
    w.L.create_document("EXPENSE", dt.date(2026, 10, 2), {"amount": 900_000, "category": "meat",
                                                          "money_account_id": w.cash.id})
    w.s.commit()
    # lowering opening cash to 5,000 would make the box negative on the 2nd
    with pytest.raises(BusinessError) as e:
        w.L.save_opening(OPEN, {w.cash.id: 500_000}, {}, [], {w.contractor.id: 500_000})
    assert e.value.code == "err_insufficient_balance"
    assert e.value.params["date"] == "2026-10-02" and e.value.params["balance"] == -400_000
    w.s.rollback()
    assert w.L.balance(CASH, money_account_id=w.cash.id) == 100_000


def test_void_opening_only_without_other_documents(opened):
    w = opened
    op = w.L.opening_doc()
    w.L.create_document("PRODUCTION", OPEN, {"project_id": w.kitchen.id, "quantity": 1000})
    assert code(w.L.void_document, op, "خطأ في الإدخال") == "err_opening_has_docs"


def test_void_opening_alone(opened):
    w = opened
    op = w.L.opening_doc()
    w.L.void_document(op, "بداية خاطئة")
    w.s.commit()
    assert w.L.opening_doc() is None
    assert w.s.scalar(select(func.count(JournalLine.id))) == 0
    # a new opening can then be entered
    w.L.save_opening(OPEN, {}, {}, [], {})


def test_opening_cannot_be_edited_through_document_edit(opened):
    w = opened
    assert code(w.L.update_document, w.L.opening_doc(), OPEN, {}) == "err_use_opening_page"


def test_opening_asset_disposal_gain_and_loss(world):
    w = world
    w.L.save_opening(OPEN, {w.cash.id: 0}, {}, [{"name": "مولد", "amount": 500_000, "project_id": w.kitchen.id},
                                               {"name": "خيمة", "amount": 100_000}], {w.contractor.id: 600_000})
    gen, tent = w.s.scalars(select(Asset).order_by(Asset.id)).all()
    w.L.create_document("ASSET_DISPOSAL", dt.date(2026, 10, 10), {"asset_id": gen.id, "amount": 650_000,
                                                                  "money_account_id": w.cash.id})
    w.L.create_document("ASSET_DISPOSAL", dt.date(2026, 10, 10), {"asset_id": tent.id, "amount": 0})
    w.s.commit()
    assert w.L.balance(FA) == 0
    assert w.L.balance(CASH) == 650_000
    assert w.L.net_profit() == 150_000 - 100_000
    rep = {r["project"].id if r["project"] else None: r for r in w.L.project_report(None, None)}
    assert rep[w.kitchen.id]["gain"] == 150_000 and rep[None]["loss"] == 100_000
    assert w.L.trial_balance()["sheet_ok"]
    # changing the opening cost of the generator re-computes the gain
    w.L.save_opening(OPEN, {}, {}, [{"id": gen.id, "name": "مولد", "amount": 600_000, "project_id": w.kitchen.id},
                                   {"id": tent.id, "name": "خيمة", "amount": 100_000}], {w.contractor.id: 700_000})
    assert w.L.net_profit() == 50_000 - 100_000
    assert w.L.trial_balance()["sheet_ok"]
