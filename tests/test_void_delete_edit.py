"""Deleting, voiding and editing — and everything that follows from them.

Rules under test:
* A posted transaction is never deleted. It is voided with a written reason: its journal lines are
  removed (balances return exactly to what they would be without it), the document stays visible,
  and the audit log keeps the full before/after.
* A void is refused when the books would become impossible (money already spent, invoice already
  paid, profit already distributed, asset already sold, capital call already paid...).
* Only transactions that never reached the books (pending / rejected) can be deleted permanently,
  and the deletion itself is logged with a full snapshot.
* Master records are deleted only when unused; otherwise deactivated.
"""

import datetime as dt

import pytest
from sqlalchemy import func, select

from app.models import Asset, AuditLog, Document, JournalLine, MonthSignoff
from app.services import AR, CAP, CASH, FA, BusinessError, Ledger
from conftest import TODAY, ledger_for, make_user

D = lambda day: dt.date(2026, 10, day)  # noqa: E731


def code(fn, *a, **kw):
    with pytest.raises(BusinessError) as e:
        fn(*a, **kw)
    return e.value.code


def snapshot(L: Ledger):
    """Every balance that matters, to compare before/after."""
    tb = L.trial_balance()
    return {"tb": tb["rows"], "partners": [(r["partner"].id, r["capital"], r["profit"], r["distributions"],
                                             r["called"], r["paid_to_calls"]) for r in L.partner_summary()],
            "dist": L.distributable(TODAY)}


# ------------------------------------------------------------------ void restores exactly

def _setup_activity(w):
    L = w.L
    L.create_document("CONTRIBUTION", D(2), {"partner_id": w.contractor.id, "money_account_id": w.cash.id,
                                             "amount": 5_000_000})
    L.create_document("INVOICE", D(5), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                        "quantity": 1_000_000, "unit_price": 1700})
    L.create_document("EXPENSE", D(5), {"amount": 800_000, "category": "meat", "project_id": w.kitchen.id,
                                        "money_account_id": w.cash.id})
    L.create_document("RECEIPT", D(7), {"customer_id": w.org.id, "money_account_id": w.bank.id,
                                        "amount": 1_000_000, "fee": 10_000})
    w.s.commit()


VOIDABLE = {
    "CONTRIBUTION": lambda w: {"partner_id": w.mahmoud.id, "money_account_id": w.cash.id, "amount": 123_456},
    "CAPITAL_TRANSFER": lambda w: {"partner_id": w.mahmoud.id, "partner2_id": w.contractor.id, "amount": 77_777},
    "EXPENSE": lambda w: {"amount": 33_333, "category": "fuel", "project_id": w.kitchen.id,
                          "money_account_id": w.cash.id},
    "INVOICE": lambda w: {"customer_id": w.org.id, "project_id": w.parcels.id, "quantity": 3000, "unit_price": 4999},
    "RECEIPT": lambda w: {"customer_id": w.org.id, "money_account_id": w.cash.id, "amount": 50_000, "fee": 1},
    "TRANSFER": lambda w: {"money_account_id": w.cash.id, "money_account2_id": w.bank.id, "amount": 10_000,
                           "fee": 3},
    "DISTRIBUTION": lambda w: {"money_account_id": w.bank.id, "amount": 10_001},
    "PRODUCTION": lambda w: {"project_id": w.kitchen.id, "quantity": 999},
    "CAPITAL_CALL": lambda w: {"amount": 1_000_000},
}


@pytest.mark.parametrize("doc_type", sorted(VOIDABLE))
def test_void_returns_every_balance_to_exactly_before(opened, doc_type):
    w = opened
    _setup_activity(w)
    before = snapshot(w.L)
    doc = w.L.create_document(doc_type, D(20), VOIDABLE[doc_type](w))
    w.s.commit()
    if doc_type not in ("PRODUCTION", "CAPITAL_CALL"):
        assert snapshot(w.L) != before
    w.L.void_document(doc, "إدخال خاطئ")
    w.s.commit()
    assert snapshot(w.L) == before
    assert doc.status == "VOID" and doc.void_reason == "إدخال خاطئ" and doc.voided_by == w.admin.id
    assert doc.voided_at is not None
    assert w.s.scalar(select(func.count(JournalLine.id)).where(JournalLine.document_id == doc.id)) == 0
    # still visible, never deleted
    assert w.s.get(Document, doc.id) is not None
    log = w.s.scalar(select(AuditLog).where(AuditLog.action == "void", AuditLog.entity_id == doc.id))
    assert log.before["status"] == "POSTED" and log.after["status"] == "VOID" and log.note == "إدخال خاطئ"


def test_void_asset_purchase_and_disposal(opened):
    w = opened
    before = snapshot(w.L)
    buy = w.L.create_document("ASSET_PURCHASE", D(3), {"amount": 400_000, "money_account_id": w.cash.id},
                              asset_name="مولد")
    sell = w.L.create_document("ASSET_DISPOSAL", D(9), {"asset_id": buy.asset_id, "amount": 300_000,
                                                        "money_account_id": w.cash.id})
    w.s.commit()
    # the purchase cannot be voided while the sale exists
    assert code(w.L.void_document, buy, "خطأ") == "err_asset_has_disposal"
    w.L.void_document(sell, "لم يُبع")
    w.s.commit()
    assert w.L.balance(FA, asset_id=buy.asset_id) == 400_000  # owned again
    w.L.void_document(buy, "لم يُشترَ")
    w.s.commit()
    assert snapshot(w.L) == before
    assert w.s.get(Asset, buy.asset_id) is not None  # the asset record stays for history


# ------------------------------------------------------------------ void refused when it would break the books

def test_cannot_void_contribution_whose_money_was_spent(opened):
    w = opened
    c = w.L.create_document("CONTRIBUTION", D(2), {"partner_id": w.mahmoud.id, "money_account_id": w.bank.id,
                                                   "amount": 500_000})
    w.L.create_document("EXPENSE", D(3), {"amount": 400_000, "category": "food", "money_account_id": w.bank.id})
    w.s.commit()
    with pytest.raises(BusinessError) as e:
        w.L.void_document(c, "خطأ")
    assert e.value.code == "err_insufficient_balance"
    assert e.value.params == {"id": w.bank.id, "date": "2026-10-03", "balance": -400_000}
    w.s.rollback()
    assert w.s.get(Document, c.id).status == "POSTED"
    assert w.L.balance(CASH, money_account_id=w.bank.id) == 100_000


def test_cannot_void_invoice_already_paid(opened):
    w = opened
    inv = w.L.create_document("INVOICE", D(5), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                                "quantity": 1000, "unit_price": 100_000})
    rec = w.L.create_document("RECEIPT", D(6), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                                "amount": 100_000})
    w.s.commit()
    assert code(w.L.void_document, inv, "خطأ") == "err_void_invoice_paid"
    w.s.rollback()
    # the advice in the message works: void the receipt, then the invoice
    w.L.void_document(rec, "خطأ في التسجيل")
    w.L.void_document(inv, "خطأ في التسجيل")
    w.s.commit()
    assert w.s.get(Document, inv.id).status == "VOID"


def test_cannot_void_receipt_when_its_profit_was_distributed(opened):
    w = opened
    w.L.create_document("INVOICE", D(5), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                          "quantity": 1000, "unit_price": 600_000})
    rec = w.L.create_document("RECEIPT", D(6), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                                "amount": 600_000})
    w.L.create_document("DISTRIBUTION", D(7), {"money_account_id": w.cash.id, "amount": 600_000})
    w.s.commit()
    assert code(w.L.void_document, rec, "خطأ") == "err_distribution_exceeds_profit"


def test_cannot_void_expense_paid_by_partner_if_his_capital_was_given_away(opened):
    w = opened
    exp = w.L.create_document("EXPENSE", D(3), {"amount": 1_000_000, "category": "rent",
                                                "paid_by_partner_id": w.ahmad.id})
    w.L.create_document("CAPITAL_TRANSFER", D(4), {"partner_id": w.momen.id, "partner2_id": w.ahmad.id,
                                                   "amount": 1_100_000})
    w.s.commit()
    assert code(w.L.void_document, exp, "خطأ") == "err_capital_negative"


def test_cannot_void_capital_call_with_contributions(opened):
    w = opened
    call = w.L.create_document("CAPITAL_CALL", D(2), {"amount": 600})
    contrib = w.L.create_document("CONTRIBUTION", D(3), {"partner_id": w.mahmoud.id, "money_account_id": w.cash.id,
                                                         "amount": 100, "capital_call_id": call.id})
    w.s.commit()
    assert code(w.L.void_document, call, "خطأ") == "err_call_has_contributions"
    w.L.void_document(contrib, "خطأ")
    w.L.void_document(call, "خطأ")
    w.s.commit()


def test_void_transfer_refused_if_destination_spent(opened):
    w = opened
    t = w.L.create_document("TRANSFER", D(2), {"money_account_id": w.cash.id, "money_account2_id": w.bank.id,
                                               "amount": 1_000_000})
    w.L.create_document("EXPENSE", D(3), {"amount": 1, "category": "food", "money_account_id": w.bank.id})
    w.s.commit()
    assert code(w.L.void_document, t, "خطأ") == "err_insufficient_balance"


# ------------------------------------------------------------------ void rules

def test_void_needs_a_real_reason(opened):
    w = opened
    doc = w.L.create_document("PRODUCTION", D(2), {"project_id": w.kitchen.id, "quantity": 1})
    for reason in ("", "  ", "ab", None):
        assert code(w.L.void_document, doc, reason) == "err_reason_required"


def test_void_twice_and_edit_void_refused(opened):
    w = opened
    doc = w.L.create_document("PRODUCTION", D(2), {"project_id": w.kitchen.id, "quantity": 1})
    w.L.void_document(doc, "خطأ")
    assert code(w.L.void_document, doc, "خطأ") == "err_only_posted_void"
    assert code(w.L.update_document, doc, D(2), {"project_id": w.kitchen.id, "quantity": 2}) \
        == "err_doc_not_editable"
    assert code(w.L.delete_document, doc) == "err_only_unposted_delete"


def test_only_admin_and_accountant_void(opened):
    w = opened
    doc = w.L.create_document("PRODUCTION", D(2), {"project_id": w.kitchen.id, "quantity": 1})
    for role, partner in (("partner", w.mahmoud), ("viewer", None)):
        u = make_user(w.s, f"u{role}", role, partner)
        assert code(ledger_for(w, u).void_document, doc, "خطأ") == "err_forbidden"
    acc = make_user(w.s, "acc", "accountant")
    ledger_for(w, acc).void_document(doc, "خطأ")


def test_void_in_locked_month_refused(opened):
    w = opened
    doc = w.L.create_document("PRODUCTION", D(2), {"project_id": w.kitchen.id, "quantity": 1})
    w.L.lock_month("2026-10")
    assert code(w.L.void_document, doc, "خطأ") == "err_month_locked"


# ------------------------------------------------------------------ hard delete

def _approval_world(w):
    w.L.set_approval_threshold(100_000)
    m = make_user(w.s, "mahmoud", "partner", w.mahmoud)
    c = make_user(w.s, "contractor", "partner", w.contractor)
    w.s.commit()
    return m, c


def test_posted_documents_can_never_be_deleted(opened):
    w = opened
    doc = w.L.create_document("EXPENSE", D(2), {"amount": 1, "category": "food", "money_account_id": w.cash.id})
    assert code(w.L.delete_document, doc) == "err_only_unposted_delete"
    assert code(w.L.delete_document, w.L.opening_doc()) == "err_only_unposted_delete"


def test_delete_pending_document_logs_full_snapshot(opened):
    w = opened
    _approval_world(w)
    doc = w.L.create_document("EXPENSE", D(2), {"amount": 200_000, "category": "meat",
                                                "money_account_id": w.cash.id, "description": "لحم"})
    assert doc.status == "PENDING" and not doc.lines
    doc_id = doc.id
    w.L.delete_document(doc, "طلب مكرر")
    w.s.commit()
    assert w.s.get(Document, doc_id) is None
    log = w.s.scalar(select(AuditLog).where(AuditLog.action == "delete", AuditLog.entity == "document"))
    assert log.entity_id == doc_id and log.before["amount"] == 200_000 and log.before["description"] == "لحم"
    assert log.note == "طلب مكرر" and log.after is None


def test_delete_pending_asset_purchase_removes_its_asset(opened):
    w = opened
    _approval_world(w)
    doc = w.L.create_document("ASSET_PURCHASE", D(2), {"amount": 200_000, "money_account_id": w.cash.id},
                              asset_name="بركس")
    aid = doc.asset_id
    w.L.delete_document(doc)
    w.s.commit()
    assert w.s.get(Asset, aid) is None


def test_delete_removes_attachment_file(opened, tmp_path):
    w = opened
    _approval_world(w)
    up = tmp_path / "uploads"
    up.mkdir()
    (up / "abc.jpg").write_bytes(b"\xff\xd8\xff data")
    L = Ledger(w.s, w.admin, today=TODAY, upload_dir=str(up))
    doc = L.create_document("EXPENSE", D(2), {"amount": 200_000, "category": "meat", "money_account_id": w.cash.id},
                            attachment=("abc.jpg", "فاتورة.jpg"))
    L.delete_document(doc)
    assert (up / "abc.jpg").exists()  # nothing is removed before the commit succeeds
    w.s.commit()
    L.remove_scheduled_files()
    assert not (up / "abc.jpg").exists()


def test_rolled_back_delete_keeps_attachment(opened, tmp_path):
    w = opened
    _approval_world(w)
    up = tmp_path / "uploads"
    up.mkdir()
    (up / "keep.jpg").write_bytes(b"\xff\xd8\xff data")
    L = Ledger(w.s, w.admin, today=TODAY, upload_dir=str(up))
    doc = L.create_document("EXPENSE", D(2), {"amount": 200_000, "category": "meat", "money_account_id": w.cash.id},
                            attachment=("keep.jpg", "x.jpg"))
    w.s.commit()
    L.delete_document(doc)
    w.s.rollback()
    L.discard_scheduled_files()
    L.remove_scheduled_files()
    assert (up / "keep.jpg").exists()


def test_attachment_path_traversal_is_ignored(opened, tmp_path):
    w = opened
    _approval_world(w)
    up = tmp_path / "uploads"
    up.mkdir()
    victim = tmp_path / "secret.txt"
    victim.write_text("x")
    L = Ledger(w.s, w.admin, today=TODAY, upload_dir=str(up))
    doc = L.create_document("EXPENSE", D(2), {"amount": 200_000, "category": "meat", "money_account_id": w.cash.id},
                            attachment=("../secret.txt", "x"))
    L.delete_document(doc)
    w.s.commit()
    L.remove_scheduled_files()
    assert victim.exists()


def test_partner_can_delete_only_his_own_unposted(opened):
    w = opened
    m, c = _approval_world(w)
    Lm, Lc = ledger_for(w, m), ledger_for(w, c)
    mine = Lm.create_document("EXPENSE", D(2), {"amount": 200_000, "category": "meat", "money_account_id": w.cash.id})
    assert mine.status == "PENDING"
    assert code(Lc.delete_document, mine) == "err_forbidden"
    Lm.delete_document(mine)
    w.s.commit()


def test_rejected_document_can_be_deleted(opened):
    w = opened
    m, c = _approval_world(w)
    doc = ledger_for(w, m).create_document("EXPENSE", D(2), {"amount": 200_000, "category": "meat",
                                                             "money_account_id": w.cash.id})
    ledger_for(w, c).decide(doc, "reject", "السعر مرتفع")
    assert doc.status == "REJECTED"
    w.L.delete_document(doc)
    w.s.commit()


def test_month_with_pending_documents_cannot_be_locked(opened):
    w = opened
    _approval_world(w)
    doc = w.L.create_document("EXPENSE", D(2), {"amount": 200_000, "category": "meat", "money_account_id": w.cash.id})
    assert code(w.L.lock_month, "2026-10") == "err_month_has_pending"
    w.L.delete_document(doc)
    w.L.lock_month("2026-10")


# ------------------------------------------------------------------ edit

def test_edit_changes_balances_and_logs(opened):
    w = opened
    doc = w.L.create_document("EXPENSE", D(2), {"amount": 100_000, "category": "meat",
                                                "money_account_id": w.cash.id})
    w.L.update_document(doc, D(3), {"amount": 150_000, "category": "food", "money_account_id": w.cash.id})
    w.s.commit()
    assert w.L.balance(CASH, money_account_id=w.cash.id) == 1_000_000 - 150_000
    assert {ln.date for ln in doc.lines} == {D(3)}
    log = w.s.scalar(select(AuditLog).where(AuditLog.action == "update", AuditLog.entity_id == doc.id))
    assert log.before["amount"] == 100_000 and log.after["amount"] == 150_000
    assert log.before["category"] == "meat" and log.after["category"] == "food"


def test_edit_that_would_overdraw_is_rolled_back(opened):
    w = opened
    doc = w.L.create_document("EXPENSE", D(2), {"amount": 100_000, "category": "meat",
                                                "money_account_id": w.cash.id})
    w.s.commit()
    assert code(w.L.update_document, doc, D(2), {"amount": 1_000_001, "category": "meat",
                                                 "money_account_id": w.cash.id}) == "err_insufficient_balance"
    w.s.rollback()
    assert w.s.get(Document, doc.id).amount == 100_000
    assert w.L.balance(CASH, money_account_id=w.cash.id) == 900_000


def test_edit_cannot_move_into_or_out_of_locked_month(opened):
    w = opened
    w.L.save_opening(dt.date(2026, 9, 1), {w.cash.id: 1_000_000}, {}, [], {w.contractor.id: 1_000_000})
    sep = w.L.create_document("PRODUCTION", dt.date(2026, 9, 10), {"project_id": w.kitchen.id, "quantity": 1})
    octo = w.L.create_document("PRODUCTION", D(10), {"project_id": w.kitchen.id, "quantity": 1})
    w.L.lock_month("2026-09")
    assert code(w.L.update_document, sep, D(10), {"project_id": w.kitchen.id, "quantity": 1}) == "err_month_locked"
    assert code(w.L.update_document, octo, dt.date(2026, 9, 10), {"project_id": w.kitchen.id, "quantity": 1}) \
        == "err_month_locked"
    assert code(w.L.create_document, "PRODUCTION", dt.date(2026, 9, 11), {"project_id": w.kitchen.id,
                                                                          "quantity": 1}) == "err_month_locked"


def test_edit_asset_purchase_with_disposal(opened):
    w = opened
    buy = w.L.create_document("ASSET_PURCHASE", D(3), {"amount": 100_000, "money_account_id": w.cash.id},
                              asset_name="ثلاجة")
    w.L.create_document("ASSET_DISPOSAL", D(5), {"asset_id": buy.asset_id, "amount": 0})
    w.s.commit()
    assert code(w.L.update_document, buy, D(3), {"amount": 90_000, "money_account_id": w.cash.id},
                asset_name="ثلاجة") == "err_asset_has_disposal"
    w.s.rollback()
    assert code(w.L.update_document, buy, D(6), {"amount": 100_000, "money_account_id": w.cash.id},
                asset_name="ثلاجة") == "err_asset_has_disposal"
    w.s.rollback()
    # renaming / description is fine
    buy = w.s.get(Document, buy.id)
    w.L.update_document(buy, D(3), {"amount": 100_000, "money_account_id": w.cash.id, "description": "x"},
                        asset_name="ثلاجة كبيرة")
    assert w.s.get(Asset, buy.asset_id).name == "ثلاجة كبيرة"


def test_edit_capital_call_below_paid_contributions_refused(opened):
    w = opened
    call = w.L.create_document("CAPITAL_CALL", D(2), {"amount": 600})
    w.L.create_document("CONTRIBUTION", D(3), {"partner_id": w.mahmoud.id, "money_account_id": w.cash.id,
                                               "amount": 100, "capital_call_id": call.id})
    w.s.commit()
    assert code(w.L.update_document, call, D(2), {"amount": 300}) == "err_contribution_exceeds_call"
    w.s.rollback()
    assert code(w.L.update_document, w.s.get(Document, call.id), D(4), {"amount": 600}) \
        == "err_contribution_before_call"


def test_edit_receipt_above_owed_refused(opened):
    w = opened
    w.L.create_document("INVOICE", D(5), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                          "quantity": 1000, "unit_price": 1000})
    rec = w.L.create_document("RECEIPT", D(6), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                                "amount": 500})
    assert code(w.L.update_document, rec, D(6), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                                 "amount": 1001}) == "err_receivable_negative"


def test_partner_cannot_edit_posted(opened):
    w = opened
    m = make_user(w.s, "m", "partner", w.mahmoud)
    doc = ledger_for(w, m).create_document("EXPENSE", D(2), {"amount": 1, "category": "food",
                                                             "money_account_id": w.cash.id})
    assert doc.status == "POSTED"
    assert code(ledger_for(w, m).update_document, doc, D(2), {"amount": 2, "category": "food",
                                                              "money_account_id": w.cash.id}) == "err_forbidden"


def test_edit_keeps_inactive_reference_but_cannot_switch_to_one(opened):
    w = opened
    doc = w.L.create_document("PRODUCTION", D(2), {"project_id": w.parcels.id, "quantity": 1})
    w.L.set_master_active("project", w.parcels, False)
    w.L.update_document(doc, D(2), {"project_id": w.parcels.id, "quantity": 5})  # unchanged reference is fine
    doc2 = w.L.create_document("PRODUCTION", D(2), {"project_id": w.kitchen.id, "quantity": 1})
    assert code(w.L.update_document, doc2, D(2), {"project_id": w.parcels.id, "quantity": 1}) == "err_project_closed"


# ------------------------------------------------------------------ sign-offs cleared by any change

def test_any_change_clears_month_signoffs(opened):
    w = opened
    m = make_user(w.s, "m", "partner", w.mahmoud)
    Lm = ledger_for(w, m)
    doc = w.L.create_document("PRODUCTION", D(2), {"project_id": w.kitchen.id, "quantity": 1})
    for action in ("create", "update", "void"):
        Lm.signoff_month("2026-10")
        w.s.flush()
        assert w.s.scalar(select(func.count(MonthSignoff.id))) == 1
        if action == "create":
            w.L.create_document("PRODUCTION", D(3), {"project_id": w.kitchen.id, "quantity": 1})
        elif action == "update":
            w.L.update_document(doc, D(2), {"project_id": w.kitchen.id, "quantity": 2})
        else:
            w.L.void_document(doc, "خطأ")
        assert w.s.scalar(select(func.count(MonthSignoff.id))) == 0
    assert w.s.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "signoffs_cleared")) == 3


def test_change_in_other_month_keeps_signoff(opened):
    w = opened
    w.L.save_opening(dt.date(2026, 9, 1), {w.cash.id: 1_000_000}, {}, [], {w.contractor.id: 1_000_000})
    m = make_user(w.s, "m", "partner", w.mahmoud)
    ledger_for(w, m).signoff_month("2026-09")
    w.L.create_document("PRODUCTION", D(3), {"project_id": w.kitchen.id, "quantity": 1})
    assert w.s.scalar(select(func.count(MonthSignoff.id))) == 1


# ------------------------------------------------------------------ master records

def test_master_delete_only_when_unused(opened):
    w = opened
    extra = w.L.save_master("project", None, name="مشروع تجريبي", kind="other")
    w.L.delete_master("project", extra)  # unused -> deleted
    assert code(w.L.delete_master, "money_account", w.cash) == "err_in_use_deactivate"  # opening uses it
    w.L.create_document("PRODUCTION", D(2), {"project_id": w.kitchen.id, "quantity": 1})
    assert code(w.L.delete_master, "project", w.kitchen) == "err_in_use_deactivate"
    w.L.void_document(w.s.scalars(select(Document).where(Document.doc_type == "PRODUCTION")).one(), "خطأ")
    # even a void document keeps the reference: history must stay readable
    assert code(w.L.delete_master, "project", w.kitchen) == "err_in_use_deactivate"
    log = w.s.scalar(select(AuditLog).where(AuditLog.action == "delete", AuditLog.entity == "project"))
    assert log.before["name"] == "مشروع تجريبي"


def test_deactivate_rules(opened):
    w = opened
    assert code(w.L.set_master_active, "money_account", w.cash, False) == "err_account_not_empty"
    w.L.set_master_active("money_account", w.bank, False)  # empty -> allowed
    assert code(w.L.create_document, "CONTRIBUTION", D(2), {"partner_id": w.mahmoud.id,
                                                            "money_account_id": w.bank.id, "amount": 1}) \
        == "err_money_account_inactive"
    w.L.set_master_active("money_account", w.bank, True)
    w.L.create_document("CONTRIBUTION", D(2), {"partner_id": w.mahmoud.id, "money_account_id": w.bank.id,
                                               "amount": 1})
    w.L.create_document("INVOICE", D(2), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                          "quantity": 1000, "unit_price": 1})
    assert code(w.L.set_master_active, "customer", w.org, False) == "err_customer_not_settled"


def test_deactivate_blocked_by_pending_documents(opened):
    w = opened
    _approval_world(w)
    w.L.create_document("EXPENSE", D(2), {"amount": 200_000, "category": "meat", "project_id": w.parcels.id,
                                          "money_account_id": w.cash.id})
    assert code(w.L.set_master_active, "project", w.parcels, False) == "err_has_pending_docs"


def test_master_names_unique_case_insensitive(opened):
    w = opened
    assert code(w.L.save_master, "money_account", None, name="bank of palestine", kind="bank") == "err_name_exists"
    assert code(w.L.save_master, "money_account", None, name="  ", kind="bank") == "err_name_required"
    assert code(w.L.save_master, "money_account", None, name="x", kind="gold") == "err_invalid_choice"


def test_partner_delete_rules(world):
    w = world
    assert code(w.L.delete_partner, w.mahmoud) == "err_partner_in_use"  # has a share
    p = w.L.save_partner(None, "شريك مؤقت")
    w.L.delete_partner(p)
    assert code(w.L.save_partner, None, "مؤمن سليم") == "err_name_exists"


def test_share_version_rules(opened):
    w = opened
    v1 = w.L.share_versions()[0]
    # a version before or on the opening date is not allowed once the books started
    assert code(w.L.create_share_version, dt.date(2026, 10, 1), {w.contractor.id: 1}, mode="ratio") \
        == "err_share_date_before_opening"
    assert code(w.L.create_share_version, D(10), {w.contractor.id: 600_000, w.mahmoud.id: 400_001}) \
        == "err_shares_sum"
    assert code(w.L.create_share_version, D(10), {w.contractor.id: 0}, mode="ratio") == "err_ratio_zero"
    assert code(w.L.create_share_version, D(10), {w.contractor.id: -1, w.mahmoud.id: 2}, mode="ratio") \
        == "err_number_negative"
    assert code(w.L.delete_share_version, v1) == "err_share_last_version"
    v2 = w.L.create_share_version(D(10), {w.contractor.id: 1, w.mahmoud.id: 1}, mode="ratio")
    assert code(w.L.delete_share_version, v1) == "err_share_not_latest"
    w.L.delete_share_version(v2)
    # after a distribution, a new version must start later
    w.L.create_document("INVOICE", D(12), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                           "quantity": 1000, "unit_price": 600})
    w.L.create_document("RECEIPT", D(12), {"customer_id": w.org.id, "money_account_id": w.cash.id, "amount": 600})
    w.L.create_document("DISTRIBUTION", D(13), {"money_account_id": w.cash.id, "amount": 600})
    assert code(w.L.create_share_version, D(13), {w.contractor.id: 1}, mode="ratio") \
        == "err_share_date_before_distribution"
    w.L.create_share_version(D(14), {w.contractor.id: 1}, mode="ratio")


def test_profit_follows_share_changes_by_day(opened):
    w = opened
    w.L.create_share_version(D(15), {w.contractor.id: 1, w.mahmoud.id: 1}, mode="ratio")
    for day in (10, 20):
        w.L.create_document("INVOICE", D(day), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                                "quantity": 1000, "unit_price": 600_000})
    alloc = w.L.profit_allocation()
    # day 10: 3:1:1:1 of 6,000 ; day 20: 1:1 of 6,000
    assert alloc == {w.contractor.id: 300_000 + 300_000, w.mahmoud.id: 100_000 + 300_000,
                     w.momen.id: 100_000, w.ahmad.id: 100_000}
    assert sum(alloc.values()) == w.L.net_profit()


def test_moving_distribution_across_share_change_refused(opened):
    w = opened
    w.L.create_document("INVOICE", D(2), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                          "quantity": 1000, "unit_price": 600})
    w.L.create_document("RECEIPT", D(2), {"customer_id": w.org.id, "money_account_id": w.cash.id, "amount": 600})
    w.L.create_share_version(D(10), {w.contractor.id: 1, w.mahmoud.id: 1}, mode="ratio")
    d = w.L.create_document("DISTRIBUTION", D(12), {"money_account_id": w.cash.id, "amount": 600})
    assert code(w.L.update_document, d, D(5), {"money_account_id": w.cash.id, "amount": 600}) \
        == "err_share_period_change"


# ------------------------------------------------------------------ users

def test_user_rules(world):
    w = world
    L = w.L
    assert code(L.set_user_active, w.admin, False) == "err_cannot_disable_self"
    assert code(L.delete_user, w.admin) == "err_cannot_disable_self"
    u = L.save_user(None, username="Momen", full_name="مؤمن", role="partner", partner_id=w.momen.id,
                    password="12345678")
    assert u.username == "momen"
    assert code(L.save_user, None, username="momen", full_name="x", role="viewer", partner_id=None,
                password="12345678") == "err_username_exists"
    assert code(L.save_user, None, username="momen2", full_name="x", role="partner", partner_id=w.momen.id,
                password="12345678") == "err_partner_has_user"
    assert code(L.save_user, None, username="v", full_name="x", role="viewer", partner_id=None,
                password="12345678") == "err_username_format"
    assert code(L.save_user, None, username="viewer1", full_name="x", role="viewer", partner_id=w.ahmad.id,
                password="12345678") == "err_viewer_partner"
    assert code(L.save_user, None, username="viewer1", full_name="x", role="viewer", partner_id=None,
                password="1234567") == "err_password_short"
    assert code(L.save_user, None, username="viewer1", full_name="x", role="god", partner_id=None,
                password="12345678") == "err_invalid_choice"
    # a user without history can be deleted, with history only deactivated
    L.delete_user(u)
    u2 = L.save_user(None, username="ahmad", full_name="أحمد", role="partner", partner_id=w.ahmad.id,
                     password="12345678")
    ledger_for(w, u2).signoff_month("2026-10")
    assert code(L.delete_user, u2) == "err_user_has_history"
    L.set_user_active(u2, False)
    # the last active admin cannot be demoted or disabled
    other = make_user(w.s, "boss", "admin")
    Lo = ledger_for(w, other)
    Lo.set_user_active(w.admin, False)
    assert code(Lo.save_user, other, username="boss", full_name="b", role="viewer", partner_id=None,
                password=None) == "err_last_admin"


def test_password_change(world):
    w = world
    u = w.L.save_user(None, username="acc", full_name="محاسب", role="accountant", partner_id=None,
                      password="first-pass")
    Lu = ledger_for(w, u)
    assert code(Lu.change_own_password, "wrong", "new-password") == "err_wrong_password"
    assert code(Lu.change_own_password, "first-pass", "short") == "err_password_short"
    Lu.change_own_password("first-pass", "second-pass")
    from app.services import check_password
    assert check_password("second-pass", u.password_hash)
    # the audit log never stores the hash
    for row in w.s.scalars(select(AuditLog).where(AuditLog.entity == "user")):
        for snap in (row.before, row.after):
            if snap:
                assert snap.get("password_hash") == "***"


def test_balances_never_touched_by_failed_operations(opened):
    """Any refused operation leaves nothing behind after rollback (atomicity)."""
    w = opened
    before = snapshot(w.L)
    n_docs = w.s.scalar(select(func.count(Document.id)))
    n_audit = w.s.scalar(select(func.count(AuditLog.id)))
    for fn in (
        lambda: w.L.create_document("EXPENSE", D(3), {"amount": 10**9, "category": "food",
                                                      "money_account_id": w.cash.id}),
        lambda: w.L.create_document("DISTRIBUTION", D(3), {"money_account_id": w.cash.id, "amount": 1}),
        lambda: w.L.create_document("RECEIPT", D(3), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                                      "amount": 1}),
    ):
        with pytest.raises(BusinessError):
            fn()
        w.s.rollback()
    assert snapshot(w.L) == before
    assert w.s.scalar(select(func.count(Document.id))) == n_docs
    assert w.s.scalar(select(func.count(AuditLog.id))) == n_audit
    assert w.L.balance(AR) == 0 and w.L.balance(CAP) == 1_000_000


# ------------------------------------------------------------------ findings of the independent audit

def test_distribution_after_share_change_pays_what_each_partner_earned(opened):
    """Profit earned under 3:1:1:1 and paid after a change to 1:1:1:1 is still paid 3:1:1:1."""
    w = opened
    w.L.create_document("INVOICE", D(5), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                          "quantity": 1000, "unit_price": 600_000})
    w.L.create_document("RECEIPT", D(6), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                          "amount": 600_000})
    w.L.create_share_version(D(10), {w.contractor.id: 1, w.mahmoud.id: 1, w.momen.id: 1, w.ahmad.id: 1},
                             mode="ratio")
    d = w.L.create_document("DISTRIBUTION", D(12), {"money_account_id": w.cash.id, "amount": 600_000})
    got = {ln.partner_id: ln.debit for ln in d.lines if ln.account == "3200"}
    assert got == {w.contractor.id: 300_000, w.mahmoud.id: 100_000, w.momen.id: 100_000, w.ahmad.id: 100_000}
    for r in w.L.partner_summary():
        assert r["profit"] - r["distributions"] == 0


def test_distribution_mixes_periods_correctly(opened):
    w = opened
    w.L.create_document("INVOICE", D(5), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                          "quantity": 1000, "unit_price": 600_000})
    w.L.create_share_version(D(10), {w.contractor.id: 1, w.mahmoud.id: 1, w.momen.id: 1, w.ahmad.id: 1},
                             mode="ratio")
    w.L.create_document("INVOICE", D(11), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                           "quantity": 1000, "unit_price": 400_000})
    w.L.create_document("RECEIPT", D(12), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                           "amount": 1_000_000})
    d = w.L.create_document("DISTRIBUTION", D(13), {"money_account_id": w.cash.id, "amount": 1_000_000})
    got = {ln.partner_id: ln.debit for ln in d.lines if ln.account == "3200"}
    assert got == {w.contractor.id: 300_000 + 100_000, w.mahmoud.id: 200_000, w.momen.id: 200_000,
                   w.ahmad.id: 200_000}


def test_rounding_does_not_build_up_over_many_days(opened):
    w = opened
    w.L.create_share_version(D(2), {w.mahmoud.id: 1, w.momen.id: 1, w.ahmad.id: 1}, mode="ratio")
    for day in range(2, 31):
        w.L.create_document("INVOICE", dt.date(2026, 10, day), {"customer_id": w.org.id,
                                                                "project_id": w.kitchen.id,
                                                                "quantity": 1000, "unit_price": 10_000})
    alloc = w.L.profit_allocation()
    # 29 x 100 ILS = 2,900 ILS split in three = 966.666… -> parts differ by at most one agora
    parts = sorted(alloc[p.id] for p in (w.mahmoud, w.momen, w.ahmad))
    assert parts[-1] - parts[0] <= 1 and sum(parts) == 290_000


def test_repeated_distributions_do_not_favour_one_partner(opened):
    w = opened
    w.L.create_share_version(D(2), {w.mahmoud.id: 1, w.momen.id: 1, w.ahmad.id: 1}, mode="ratio")
    w.L.create_document("INVOICE", D(3), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                          "quantity": 1000, "unit_price": 10_000})
    w.L.create_document("RECEIPT", D(3), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                          "amount": 10_000})
    for _ in range(50):
        w.L.create_document("DISTRIBUTION", D(4), {"money_account_id": w.cash.id, "amount": 100})
    rows = {r["partner"].id: r["distributions"] for r in w.L.partner_summary()}
    got = sorted(rows[p.id] for p in (w.mahmoud, w.momen, w.ahmad))
    assert sum(got) == 5000 and got[-1] - got[0] <= 1


def test_months_lock_in_order_and_unlock_newest_first(opened):
    w = opened
    w.L.save_opening(dt.date(2026, 8, 1), {w.cash.id: 1_000_000}, {}, [], {w.contractor.id: 1_000_000})
    with pytest.raises(BusinessError) as e:
        w.L.lock_month("2026-09")
    assert e.value.code == "err_lock_previous_first" and e.value.params["month"] == "2026-08"
    assert code(w.L.lock_month, "2026-07") == "err_month_before_opening"
    w.L.lock_month("2026-08")
    w.L.lock_month("2026-09")
    with pytest.raises(BusinessError) as e:
        w.L.unlock_month("2026-08", "تصحيح")
    assert e.value.code == "err_unlock_later_first" and e.value.params["month"] == "2026-09"
    w.L.unlock_month("2026-09", "تصحيح")
    w.L.unlock_month("2026-08", "تصحيح")


def test_backdated_change_cannot_alter_a_locked_month(opened):
    """With locks in order, any open month is after every locked month."""
    w = opened
    w.L.save_opening(dt.date(2026, 8, 1), {w.cash.id: 1_000_000}, {}, [], {w.contractor.id: 1_000_000})
    w.L.create_document("EXPENSE", dt.date(2026, 9, 5), {"amount": 100, "category": "food",
                                                         "money_account_id": w.cash.id})
    w.L.lock_month("2026-08")
    w.L.lock_month("2026-09")
    closing_sep = w.L.balance(CASH, dt.date(2026, 9, 30))
    assert code(w.L.create_document, "EXPENSE", dt.date(2026, 8, 20),
                {"amount": 100, "category": "food", "money_account_id": w.cash.id}) == "err_month_locked"
    w.L.create_document("EXPENSE", D(1), {"amount": 100, "category": "food", "money_account_id": w.cash.id})
    assert w.L.balance(CASH, dt.date(2026, 9, 30)) == closing_sep


def test_voided_asset_has_no_cost(opened):
    w = opened
    buy = w.L.create_document("ASSET_PURCHASE", D(3), {"amount": 100_000, "money_account_id": w.cash.id},
                              asset_name="x")
    assert w.L._asset_cost(buy.asset_id) == 100_000
    w.L.void_document(buy, "خطأ")
    assert w.L._asset_cost(buy.asset_id) == 0


def test_anonymous_ledger_is_not_admin(opened):
    w = opened
    anon = Ledger(w.s, None, today=TODAY)
    assert code(anon.lock_month, "2026-10") == "err_forbidden"
    assert code(anon.create_document, "PRODUCTION", D(2), {"project_id": w.kitchen.id, "quantity": 1}) \
        == "err_forbidden"
