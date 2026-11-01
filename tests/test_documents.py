"""Every transaction type: the exact journal it produces and every validation rule."""

import datetime as dt

import pytest
from sqlalchemy import select

from app.models import Asset, CallShare
from app.services import AR, CAP, CASH, DIST, EXP, FA, GAIN, LOSS, REV, BusinessError
from conftest import OPEN, TODAY, ledger_for, make_user

D = lambda day: dt.date(2026, 10, day)  # noqa: E731


def code(fn, *a, **kw):
    with pytest.raises(BusinessError) as e:
        fn(*a, **kw)
    return e.value.code


def journal(doc):
    return sorted((ln.account, ln.debit, ln.credit, ln.partner_id, ln.money_account_id, ln.customer_id,
                   ln.project_id, ln.asset_id, ln.category) for ln in doc.lines)


# ------------------------------------------------------------------ contribution / capital

def test_contribution(opened):
    w = opened
    doc = w.L.create_document("CONTRIBUTION", D(2), {"partner_id": w.mahmoud.id, "money_account_id": w.bank.id,
                                                     "amount": 3_000_000, "description": "حصتي من البركس"})
    assert doc.number == 1 and doc.status == "POSTED"
    assert journal(doc) == sorted([(CASH, 3_000_000, 0, None, w.bank.id, None, None, None, ""),
                                   (CAP, 0, 3_000_000, w.mahmoud.id, None, None, None, None, "")])
    assert w.L.balance(CAP, partner_id=w.mahmoud.id) == 166_667 + 3_000_000


@pytest.mark.parametrize("data,err", [
    ({"money_account_id": "B", "amount": 1}, "err_partner_required"),
    ({"partner_id": "M", "amount": 1}, "err_money_account_required"),
    ({"partner_id": "M", "money_account_id": "B", "amount": 0}, "err_amount_positive"),
    ({"partner_id": "M", "money_account_id": "B", "amount": -5}, "err_amount_positive"),
    ({"partner_id": 999, "money_account_id": "B", "amount": 5}, "err_not_found"),
    ({"partner_id": "M", "money_account_id": 999, "amount": 5}, "err_not_found"),
])
def test_contribution_validation(opened, data, err):
    w = opened
    data = {k: (w.mahmoud.id if v == "M" else w.bank.id if v == "B" else v) for k, v in data.items()}
    assert code(w.L.create_document, "CONTRIBUTION", D(2), data) == err


def test_capital_call_split_and_contributions(opened):
    w = opened
    call = w.L.create_document("CAPITAL_CALL", D(2), {"amount": 18_000_000, "project_id": w.kitchen.id})
    shares = {cs.partner_id: cs.amount for cs in call.call_shares}
    assert shares == {w.contractor.id: 9_000_000, w.mahmoud.id: 3_000_000, w.momen.id: 3_000_000,
                      w.ahmad.id: 3_000_000}
    assert not call.lines  # memo only
    w.L.create_document("CONTRIBUTION", D(3), {"partner_id": w.mahmoud.id, "money_account_id": w.cash.id,
                                               "amount": 1_000_000, "capital_call_id": call.id})
    w.s.commit()
    rows = {r["partner"].id: r for r in w.L.partner_summary()}
    assert rows[w.mahmoud.id]["called"] == 3_000_000 and rows[w.mahmoud.id]["outstanding_calls"] == 2_000_000
    assert rows[w.contractor.id]["outstanding_calls"] == 9_000_000
    # paying more than called against the call is refused
    with pytest.raises(BusinessError) as e:
        w.L.create_document("CONTRIBUTION", D(3), {"partner_id": w.mahmoud.id, "money_account_id": w.cash.id,
                                                   "amount": 2_000_001, "capital_call_id": call.id})
    assert e.value.code == "err_contribution_exceeds_call" and e.value.params["called"] == 3_000_000
    w.s.rollback()
    # before the call date is refused
    assert code(w.L.create_document, "CONTRIBUTION", D(1), {"partner_id": w.mahmoud.id,
                                                            "money_account_id": w.cash.id, "amount": 1,
                                                            "capital_call_id": call.id}) == "err_contribution_before_call"


def test_capital_call_odd_amount_is_split_exactly(opened):
    w = opened
    call = w.L.create_document("CAPITAL_CALL", D(2), {"amount": 100})  # 1 shekel
    assert sum(cs.amount for cs in call.call_shares) == 100
    assert {cs.partner_id: cs.amount for cs in call.call_shares}[w.contractor.id] == 50


def test_capital_transfer_between_partners(opened):
    w = opened
    # contractor paid the barracks before; Mahmoud pays the contractor directly his part
    w.L.create_document("CONTRIBUTION", D(2), {"partner_id": w.contractor.id, "money_account_id": w.cash.id,
                                               "amount": 18_000_000})
    doc = w.L.create_document("CAPITAL_TRANSFER", D(3), {"partner_id": w.mahmoud.id, "partner2_id": w.contractor.id,
                                                         "amount": 3_000_000})
    assert journal(doc) == sorted([(CAP, 3_000_000, 0, w.contractor.id, None, None, None, None, ""),
                                   (CAP, 0, 3_000_000, w.mahmoud.id, None, None, None, None, "")])
    assert w.L.balance(CAP, partner_id=w.contractor.id) == 500_000 + 18_000_000 - 3_000_000
    assert w.L.balance(CAP, partner_id=w.mahmoud.id) == 166_667 + 3_000_000
    assert w.L.balance(CASH) == 1_000_000 + 18_000_000  # cash did not move
    assert code(w.L.create_document, "CAPITAL_TRANSFER", D(3), {"partner_id": w.mahmoud.id,
                                                                "partner2_id": w.mahmoud.id, "amount": 1}) \
        == "err_same_partner"


def test_capital_transfer_cannot_make_receiver_capital_negative(opened):
    w = opened
    with pytest.raises(BusinessError) as e:
        w.L.create_document("CAPITAL_TRANSFER", D(3), {"partner_id": w.mahmoud.id, "partner2_id": w.ahmad.id,
                                                       "amount": 166_667})
    assert e.value.code == "err_capital_negative" and e.value.params["balance"] == -1


# ------------------------------------------------------------------ expenses & assets

def test_expense_from_account(opened):
    w = opened
    doc = w.L.create_document("EXPENSE", D(3), {"amount": 800_000, "category": "meat", "project_id": w.kitchen.id,
                                                "money_account_id": w.cash.id, "supplier": "ملحمة"})
    assert journal(doc) == sorted([(EXP, 800_000, 0, None, None, None, w.kitchen.id, None, "meat"),
                                   (CASH, 0, 800_000, None, w.cash.id, None, None, None, "")])
    assert doc.supplier == "ملحمة"


def test_expense_paid_by_partner_increases_capital(opened):
    w = opened
    doc = w.L.create_document("EXPENSE", D(3), {"amount": 2_000_000, "category": "rent",
                                                "paid_by_partner_id": w.contractor.id})
    assert (CAP, 0, 2_000_000, w.contractor.id, None, None, None, None, "") in journal(doc)
    assert w.L.balance(CASH) == 1_000_000  # no cash moved
    assert w.L.balance(CAP, partner_id=w.contractor.id) == 2_500_000


def test_expense_overhead_without_project(opened):
    w = opened
    w.L.create_document("EXPENSE", D(3), {"amount": 100, "category": "other", "money_account_id": w.cash.id})
    rep = w.L.project_report(None, None)
    assert [r for r in rep if r["project"] is None][0]["expenses"] == 100


@pytest.mark.parametrize("data,err", [
    ({"amount": 100, "money_account_id": "C"}, "err_category_required"),
    ({"amount": 100, "category": "caviar", "money_account_id": "C"}, "err_category_required"),
    ({"amount": 100, "category": "food"}, "err_pay_source"),
    ({"amount": 100, "category": "food", "money_account_id": "C", "paid_by_partner_id": "M"}, "err_pay_source"),
    ({"amount": 0, "category": "food", "money_account_id": "C"}, "err_amount_positive"),
    ({"amount": 100, "category": "food", "money_account_id": "C", "project_id": 999}, "err_not_found"),
])
def test_expense_validation(opened, data, err):
    w = opened
    data = {k: (w.cash.id if v == "C" else w.mahmoud.id if v == "M" else v) for k, v in data.items()}
    assert code(w.L.create_document, "EXPENSE", D(3), data) == err


def test_expense_cannot_overdraw(opened):
    w = opened
    with pytest.raises(BusinessError) as e:
        w.L.create_document("EXPENSE", D(3), {"amount": 1_000_001, "category": "food", "money_account_id": w.cash.id})
    assert e.value.code == "err_insufficient_balance" and e.value.params["balance"] == -1
    w.s.rollback()
    w.L.create_document("EXPENSE", D(3), {"amount": 1_000_000, "category": "food", "money_account_id": w.cash.id})
    assert w.L.balance(CASH, money_account_id=w.cash.id) == 0


def test_backdated_expense_cannot_overdraw_an_earlier_day(opened):
    w = opened
    w.L.create_document("CONTRIBUTION", D(10), {"partner_id": w.mahmoud.id, "money_account_id": w.cash.id,
                                                "amount": 5_000_000})
    w.s.commit()
    # on the 5th only 10,000 existed
    assert code(w.L.create_document, "EXPENSE", D(5), {"amount": 2_000_000, "category": "food",
                                                       "money_account_id": w.cash.id}) == "err_insufficient_balance"
    w.s.rollback()
    w.L.create_document("EXPENSE", D(10), {"amount": 2_000_000, "category": "food", "money_account_id": w.cash.id})


def test_same_day_inflow_covers_outflow(opened):
    w = opened
    w.L.create_document("CONTRIBUTION", D(5), {"partner_id": w.mahmoud.id, "money_account_id": w.cash.id,
                                               "amount": 500_000})
    w.L.create_document("EXPENSE", D(5), {"amount": 1_500_000, "category": "food", "money_account_id": w.cash.id})
    assert w.L.balance(CASH, money_account_id=w.cash.id) == 0


def test_asset_purchase_and_disposal(opened):
    w = opened
    w.L.create_document("CONTRIBUTION", D(2), {"partner_id": w.contractor.id, "money_account_id": w.cash.id,
                                               "amount": 18_000_000})
    buy = w.L.create_document("ASSET_PURCHASE", D(3), {"amount": 18_000_000, "project_id": w.kitchen.id,
                                                       "money_account_id": w.cash.id}, asset_name="بركس")
    asset = w.s.get(Asset, buy.asset_id)
    assert asset.name == "بركس" and asset.project_id == w.kitchen.id
    assert (FA, 18_000_000, 0, None, None, None, w.kitchen.id, asset.id, "") in journal(buy)
    assert w.L.net_profit() == 0  # an asset is not an expense
    sell = w.L.create_document("ASSET_DISPOSAL", D(20), {"asset_id": asset.id, "amount": 15_000_000,
                                                         "money_account_id": w.cash.id})
    assert journal(sell) == sorted([(CASH, 15_000_000, 0, None, w.cash.id, None, None, None, ""),
                                    (FA, 0, 18_000_000, None, None, None, w.kitchen.id, asset.id, ""),
                                    (LOSS, 3_000_000, 0, None, None, None, w.kitchen.id, None, "")])
    assert w.L.net_profit() == -3_000_000
    assert code(w.L.create_document, "ASSET_DISPOSAL", D(21), {"asset_id": asset.id, "amount": 0}) \
        == "err_asset_already_disposed"


def test_asset_sale_with_gain(opened):
    w = opened
    buy = w.L.create_document("ASSET_PURCHASE", D(3), {"amount": 100_000, "money_account_id": w.cash.id},
                              asset_name="ثلاجة")
    sell = w.L.create_document("ASSET_DISPOSAL", D(4), {"asset_id": buy.asset_id, "amount": 120_000,
                                                        "money_account_id": w.cash.id})
    assert (GAIN, 0, 20_000, None, None, None, None, None, "") in journal(sell)


def test_asset_rules(opened):
    w = opened
    assert code(w.L.create_document, "ASSET_PURCHASE", D(3), {"amount": 1, "money_account_id": w.cash.id},
                asset_name="") == "err_asset_name_required"
    buy = w.L.create_document("ASSET_PURCHASE", D(5), {"amount": 100, "money_account_id": w.cash.id},
                              asset_name="x")
    assert code(w.L.create_document, "ASSET_DISPOSAL", D(4), {"asset_id": buy.asset_id, "amount": 0}) \
        == "err_disposal_before_purchase"
    assert code(w.L.create_document, "ASSET_DISPOSAL", D(5), {"asset_id": buy.asset_id, "amount": 10}) \
        == "err_money_account_required"
    assert code(w.L.create_document, "ASSET_DISPOSAL", D(5), {"asset_id": buy.asset_id, "amount": -1}) \
        == "err_number_negative"
    assert code(w.L.create_document, "ASSET_DISPOSAL", D(5), {"amount": 0}) == "err_asset_required"


# ------------------------------------------------------------------ invoices & receipts

def test_invoice_quantity_times_price(opened):
    w = opened
    inv = w.L.create_document("INVOICE", D(10), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                                 "quantity": 1_250_500, "unit_price": 1700})  # 1250.5 x 17
    assert inv.amount == 2_125_850
    assert journal(inv) == sorted([(AR, 2_125_850, 0, None, None, w.org.id, w.kitchen.id, None, ""),
                                   (REV, 0, 2_125_850, None, None, w.org.id, w.kitchen.id, None, "")])


def test_invoice_ignores_client_supplied_total(opened):
    w = opened
    inv = w.L.create_document("INVOICE", D(10), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                                 "quantity": 1000, "unit_price": 1700, "amount": 99})
    assert inv.amount == 1700


@pytest.mark.parametrize("data,err", [
    ({"project_id": "K", "quantity": 1000, "unit_price": 1}, "err_customer_required"),
    ({"customer_id": "O", "quantity": 1000, "unit_price": 1}, "err_project_required"),
    ({"customer_id": "O", "project_id": "K", "quantity": 0, "unit_price": 1}, "err_quantity_positive"),
    ({"customer_id": "O", "project_id": "K", "quantity": 1000, "unit_price": 0}, "err_price_positive"),
    ({"customer_id": "O", "project_id": "K", "quantity": 1, "unit_price": 1}, "err_amount_positive"),
])
def test_invoice_validation(opened, data, err):
    w = opened
    data = {k: (w.org.id if v == "O" else w.kitchen.id if v == "K" else v) for k, v in data.items()}
    assert code(w.L.create_document, "INVOICE", D(10), data) == err


def test_receipt_with_cash_commission(opened):
    w = opened
    w.L.create_document("INVOICE", D(10), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                           "quantity": 1_000_000, "unit_price": 1700})
    rec = w.L.create_document("RECEIPT", D(15), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                                 "amount": 1_700_000, "fee": 85_000, "project_id": w.kitchen.id})
    assert journal(rec) == sorted([(CASH, 1_615_000, 0, None, w.cash.id, None, None, None, ""),
                                   (EXP, 85_000, 0, None, None, None, w.kitchen.id, None, "fees"),
                                   (AR, 0, 1_700_000, None, None, w.org.id, None, None, "")])
    assert w.L.balance(AR, customer_id=w.org.id) == 0


def test_receipt_cannot_exceed_what_is_owed(opened):
    w = opened
    w.L.create_document("INVOICE", D(10), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                           "quantity": 1000, "unit_price": 1700})
    w.s.commit()
    with pytest.raises(BusinessError) as e:
        w.L.create_document("RECEIPT", D(15), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                               "amount": 1701})
    assert e.value.code == "err_receivable_negative"
    w.s.rollback()
    # nor be dated before the invoice
    assert code(w.L.create_document, "RECEIPT", D(9), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                                       "amount": 1700}) == "err_receivable_negative"


@pytest.mark.parametrize("fee", [100, 101])
def test_receipt_fee_must_be_below_amount(opened, fee):
    w = opened
    w.L.create_document("INVOICE", D(10), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                           "quantity": 1000, "unit_price": 100})
    assert code(w.L.create_document, "RECEIPT", D(15), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                                        "amount": 100, "fee": fee}) == "err_fee_too_large"


# ------------------------------------------------------------------ transfers

def test_transfer_with_fee(opened):
    w = opened
    t = w.L.create_document("TRANSFER", D(4), {"money_account_id": w.cash.id, "money_account2_id": w.bank.id,
                                               "amount": 1_000_000, "fee": 20_000})
    assert journal(t) == sorted([(CASH, 0, 1_000_000, None, w.cash.id, None, None, None, ""),
                                 (CASH, 980_000, 0, None, w.bank.id, None, None, None, ""),
                                 (EXP, 20_000, 0, None, None, None, None, None, "fees")])
    assert w.L.balance(CASH, money_account_id=w.cash.id) == 0
    assert w.L.balance(CASH, money_account_id=w.bank.id) == 980_000
    assert code(w.L.create_document, "TRANSFER", D(4), {"money_account_id": w.bank.id,
                                                        "money_account2_id": w.bank.id, "amount": 1}) \
        == "err_same_account"
    assert code(w.L.create_document, "TRANSFER", D(4), {"money_account_id": w.bank.id,
                                                        "money_account2_id": w.cash.id, "amount": 980_001}) \
        == "err_insufficient_balance"


# ------------------------------------------------------------------ distributions

def _profit(w, amount):
    w.L.create_document("INVOICE", D(10), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                           "quantity": 1000, "unit_price": amount})
    w.L.create_document("RECEIPT", D(11), {"customer_id": w.org.id, "money_account_id": w.cash.id, "amount": amount})


def test_distribution_split_by_shares(opened):
    w = opened
    _profit(w, 600_000)
    d = w.L.create_document("DISTRIBUTION", D(20), {"money_account_id": w.cash.id, "amount": 600_000})
    got = {ln.partner_id: ln.debit for ln in d.lines if ln.account == DIST}
    assert got == {w.contractor.id: 300_000, w.mahmoud.id: 100_000, w.momen.id: 100_000, w.ahmad.id: 100_000}
    assert w.L.distributable(D(20)) == 0
    rows = {r["partner"].id: r for r in w.L.partner_summary()}
    assert rows[w.mahmoud.id]["profit"] == 100_000 and rows[w.mahmoud.id]["distributions"] == 100_000


def test_distribution_odd_agorot_split_exactly(opened):
    w = opened
    _profit(w, 101)
    d = w.L.create_document("DISTRIBUTION", D(20), {"money_account_id": w.cash.id, "amount": 101})
    assert sum(ln.debit for ln in d.lines if ln.account == DIST) == 101


def test_cannot_distribute_uncollected_profit(opened):
    w = opened
    w.L.create_document("INVOICE", D(10), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                           "quantity": 1000, "unit_price": 500_000})
    # profit is 5,000 on paper, but nothing collected -> distributable 0, even though cash exists
    assert w.L.distributable(D(20)) == 0
    with pytest.raises(BusinessError) as e:
        w.L.create_document("DISTRIBUTION", D(20), {"money_account_id": w.cash.id, "amount": 100})
    assert e.value.code == "err_distribution_exceeds_profit" and e.value.params["excess"] == 100


def test_cannot_distribute_more_than_profit(opened):
    w = opened
    _profit(w, 600_000)
    assert code(w.L.create_document, "DISTRIBUTION", D(20), {"money_account_id": w.cash.id, "amount": 600_001}) \
        == "err_distribution_exceeds_profit"


def test_partial_collection_limits_distribution(opened):
    w = opened
    w.L.create_document("INVOICE", D(10), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                           "quantity": 1000, "unit_price": 1_000_000})
    w.L.create_document("EXPENSE", D(10), {"amount": 400_000, "category": "meat", "money_account_id": w.cash.id})
    w.L.create_document("RECEIPT", D(12), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                           "amount": 700_000})
    # profit 6,000 - still owed 3,000 = 3,000 distributable
    assert w.L.distributable(D(12)) == 300_000
    w.L.create_document("DISTRIBUTION", D(12), {"money_account_id": w.cash.id, "amount": 300_000})


# ------------------------------------------------------------------ production & general rules

def test_production_is_memo_only(opened):
    w = opened
    p = w.L.create_document("PRODUCTION", D(10), {"project_id": w.kitchen.id, "quantity": 1_000_000})
    assert not p.lines
    assert code(w.L.create_document, "PRODUCTION", D(10), {"quantity": 1000}) == "err_project_required"


def test_future_date_refused(opened):
    w = opened
    assert code(w.L.create_document, "PRODUCTION", TODAY + dt.timedelta(days=1),
                {"project_id": w.kitchen.id, "quantity": 1000}) == "err_date_future"
    w.L.create_document("PRODUCTION", TODAY, {"project_id": w.kitchen.id, "quantity": 1000})


def test_unknown_type_and_opening_via_create(opened):
    w = opened
    assert code(w.L.create_document, "BRIBE", D(2), {}) == "err_invalid_choice"
    assert code(w.L.create_document, "OPENING", D(2), {}) == "err_invalid_choice"


def test_numbers_are_sequential_per_type(opened):
    w = opened
    a = w.L.create_document("PRODUCTION", D(2), {"project_id": w.kitchen.id, "quantity": 1})
    b = w.L.create_document("PRODUCTION", D(2), {"project_id": w.kitchen.id, "quantity": 1})
    c = w.L.create_document("EXPENSE", D(2), {"amount": 1, "category": "food", "money_account_id": w.cash.id})
    assert (a.number, b.number, c.number) == (1, 2, 1)


def test_fields_of_other_types_are_ignored(opened):
    w = opened
    doc = w.L.create_document("PRODUCTION", D(2), {"project_id": w.kitchen.id, "quantity": 1, "amount": 5000,
                                                   "money_account_id": w.cash.id, "partner_id": w.mahmoud.id})
    assert doc.amount == 0 and doc.money_account_id is None and doc.partner_id is None


def test_inactive_records_cannot_be_used_in_new_documents(opened):
    w = opened
    w.L.set_master_active("project", w.parcels, False)
    assert code(w.L.create_document, "PRODUCTION", D(2), {"project_id": w.parcels.id, "quantity": 1}) \
        == "err_project_closed"
    w.L.set_master_active("customer", w.org, False)
    assert code(w.L.create_document, "INVOICE", D(2), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                                       "quantity": 1000, "unit_price": 1}) == "err_customer_inactive"


def test_role_permissions_on_create(opened):
    w = opened
    viewer = make_user(w.s, "viewer", "viewer")
    partner = make_user(w.s, "mahmoud", "partner", w.mahmoud)
    acc = make_user(w.s, "acc", "accountant")
    assert code(ledger_for(w, viewer).create_document, "EXPENSE", D(2),
                {"amount": 1, "category": "food", "money_account_id": w.cash.id}) == "err_forbidden"
    assert code(ledger_for(w, partner).create_document, "CONTRIBUTION", D(2),
                {"partner_id": w.mahmoud.id, "money_account_id": w.cash.id, "amount": 1}) == "err_forbidden"
    ledger_for(w, partner).create_document("EXPENSE", D(2), {"amount": 1, "category": "food",
                                                             "money_account_id": w.cash.id})
    # a partner can record what he paid himself, not what another partner paid
    ledger_for(w, partner).create_document("EXPENSE", D(2), {"amount": 1, "category": "food",
                                                             "paid_by_partner_id": w.mahmoud.id})
    assert code(ledger_for(w, partner).create_document, "EXPENSE", D(2),
                {"amount": 1, "category": "food", "paid_by_partner_id": w.contractor.id}) \
        == "err_paid_by_other_partner"
    ledger_for(w, acc).create_document("CONTRIBUTION", D(2), {"partner_id": w.mahmoud.id,
                                                              "money_account_id": w.cash.id, "amount": 1})


def test_capital_call_shares_follow_share_version_at_date(opened):
    w = opened
    w.L.create_share_version(D(15), {w.contractor.id: 1, w.mahmoud.id: 1, w.momen.id: 0, w.ahmad.id: 0},
                             mode="ratio")
    early = w.L.create_document("CAPITAL_CALL", D(14), {"amount": 600})
    late = w.L.create_document("CAPITAL_CALL", D(16), {"amount": 600})
    assert {c.partner_id: c.amount for c in early.call_shares}[w.contractor.id] == 300
    assert {c.partner_id: c.amount for c in late.call_shares} == {w.contractor.id: 300, w.mahmoud.id: 300}
    assert w.s.scalars(select(CallShare)).all()


def test_capital_gap_shows_who_owes_whom_and_closes_with_transfers(world):
    w = world
    # the user's case: shares 50 / 20.1 / 19.9 / 10, the contractor alone paid 180,000
    w.L.create_share_version(dt.date(2026, 9, 1), {w.contractor.id: 500_000, w.mahmoud.id: 201_000,
                                                   w.momen.id: 199_000, w.ahmad.id: 100_000})
    w.L.save_opening(OPEN, money={w.cash.id: 18_000_000}, customers={}, assets=[],
                     partners={w.contractor.id: 18_000_000})
    w.s.commit()
    gap = {r["partner"].id: r["capital_gap"] for r in w.L.partner_summary()}
    assert gap == {w.contractor.id: -9_000_000, w.mahmoud.id: 3_618_000, w.momen.id: 3_582_000,
                   w.ahmad.id: 1_800_000}
    assert sum(gap.values()) == 0
    # Mahmoud pays the contractor his part directly
    w.L.create_document("CAPITAL_TRANSFER", D(2), {"partner_id": w.mahmoud.id, "partner2_id": w.contractor.id,
                                                   "amount": 3_618_000})
    w.s.commit()
    gap = {r["partner"].id: r["capital_gap"] for r in w.L.partner_summary()}
    assert gap[w.mahmoud.id] == 0 and gap[w.contractor.id] == -5_382_000
    # a capital call split by shares keeps the gaps unchanged once everyone pays his part
    call = w.L.create_document("CAPITAL_CALL", D(3), {"amount": 1_000_000})
    for cs in call.call_shares:
        w.L.create_document("CONTRIBUTION", D(3), {"partner_id": cs.partner_id, "money_account_id": w.cash.id,
                                                   "amount": cs.amount, "capital_call_id": call.id})
    w.s.commit()
    gap2 = {r["partner"].id: r["capital_gap"] for r in w.L.partner_summary()}
    assert gap2 == gap
