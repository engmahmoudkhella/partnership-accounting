"""Stock and suppliers: purchases on credit, supplier payments, issues and production by recipe.
Uses the user's real case: rice, oil, spices, firewood, beef for the meals project."""

import datetime as dt

import pytest
from sqlalchemy import select

from app.models import Document
from app.services import AP, CAP, CASH, EXP, INV, BusinessError, Ledger

D = lambda day: dt.date(2026, 10, day)  # noqa: E731
KG = 1000  # quantities are thousandths


def code(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except BusinessError as e:
        return e.code
    return None


@pytest.fixture
def kitchen(opened):
    w = opened
    L = w.L
    w.younja = L.save_master("supplier", None, name="يونجا - أكرم السوافيري")
    w.hani = L.save_master("supplier", None, name="هاني حبوش")
    w.sharif = L.save_master("supplier", None, name="شريف السوافيري")
    w.rice = L.save_master("item", None, name="رز", unit="كغ", category="food")
    w.oil = L.save_master("item", None, name="سيرج", unit="لتر", category="food")
    w.spice = L.save_master("item", None, name="بهارات", unit="كغ", category="food")
    w.wood = L.save_master("item", None, name="حطب", unit="كغ", category="fuel")
    w.beef = L.save_master("item", None, name="لحم عجل", unit="كغ", category="meat")
    # one meal: 400 g rice, 150 g beef
    L.set_recipe(w.kitchen, {w.rice.id: 400, w.beef.id: 150})
    # partners bring the money (enough for the cash part of the purchases)
    for p, amount in ((w.contractor, 24_000_000),):
        L.create_document("CONTRIBUTION", D(1), {"partner_id": p.id, "money_account_id": w.cash.id, "amount": amount})
    w.s.commit()
    return w


def buy_all(w):
    L = w.L
    p1 = L.create_document("PURCHASE", D(2), {
        "supplier_id": w.younja.id, "project_id": w.kitchen.id, "paid": 10_000_000, "money_account_id": w.cash.id,
        "items": [{"item_id": w.rice.id, "quantity": 10_000 * KG, "unit_price": 900},
                  {"item_id": w.oil.id, "quantity": 4_000 * KG, "unit_price": 850},
                  {"item_id": w.spice.id, "quantity": 100 * KG, "unit_price": 5_000}]})
    p2 = L.create_document("PURCHASE", D(2), {
        "supplier_id": w.hani.id, "paid": 4_000_000, "money_account_id": w.cash.id,
        "items": [{"item_id": w.wood.id, "quantity": 5_000 * KG, "unit_price": 800}]})
    p3 = L.create_document("PURCHASE", D(2), {
        "supplier_id": w.sharif.id, "paid": 10_000_000, "money_account_id": w.cash.id,
        "items": [{"item_id": w.beef.id, "quantity": 5_000 * KG, "unit_price": 3_500}]})
    w.s.commit()
    return p1, p2, p3


def test_purchases_go_to_stock_and_debts_to_suppliers(kitchen):
    w = kitchen
    p1, p2, p3 = buy_all(w)
    assert (p1.amount, p2.amount, p3.amount) == (12_900_000, 4_000_000, 17_500_000)
    L = w.L
    assert L.balance(INV) == 34_400_000
    assert L.balance(AP, supplier_id=w.younja.id) == 2_900_000
    assert L.balance(AP, supplier_id=w.hani.id) == 0
    assert L.balance(AP, supplier_id=w.sharif.id) == 7_500_000
    assert L.balance(CASH, money_account_id=w.cash.id) == 1_000_000 + 24_000_000 - 24_000_000
    assert L.net_profit() == 0  # buying stock is not an expense yet
    assert L.stock_of(w.rice.id) == (10_000 * KG, 9_000_000)
    tb = L.trial_balance()
    assert tb["sheet_ok"] and tb["inventory"] == 34_400_000 and tb["payables"] == 10_400_000


def test_production_uses_the_recipe_at_average_cost(kitchen):
    w = kitchen
    buy_all(w)
    L = w.L
    assert L.coverage(w.kitchen.id) == {"units": 25_000, "limiting_item_id": w.rice.id}
    prod = L.create_document("PRODUCTION", D(3), {"project_id": w.kitchen.id, "quantity": 5_000 * KG})
    w.s.commit()
    used = {i.item_id: (i.quantity, i.amount) for i in prod.items}
    assert used == {w.rice.id: (2_000 * KG, 1_800_000), w.beef.id: (750 * KG, 2_625_000)}
    assert L.balance(EXP, project_id=w.kitchen.id) == 4_425_000  # 8.85 a meal x 5,000
    assert L.stock_of(w.rice.id) == (8_000 * KG, 7_200_000)
    # oil, spices and firewood: issued by actual use
    L.create_document("STOCK_ISSUE", D(3), {"project_id": w.kitchen.id, "items": [
        {"item_id": w.oil.id, "quantity": 200 * KG}, {"item_id": w.spice.id, "quantity": 5 * KG},
        {"item_id": w.wood.id, "quantity": 250 * KG}]})
    w.s.commit()
    assert L.balance(EXP, project_id=w.kitchen.id) == 4_425_000 + 170_000 + 25_000 + 200_000
    assert L.trial_balance()["sheet_ok"]
    L.check_invariants()


def test_cannot_use_more_than_in_stock_and_last_unit_takes_the_rest(kitchen):
    w = kitchen
    buy_all(w)
    L = w.L
    assert code(L.create_document, "PRODUCTION", D(3), {"project_id": w.kitchen.id,
                                                         "quantity": 25_001 * KG}) == "err_stock_insufficient"
    w.s.rollback()
    # three issues of one third each: the last one takes exactly what is left, no agora left behind
    for _ in range(3):
        L.create_document("STOCK_ISSUE", D(4), {"project_id": w.kitchen.id,
                                                 "items": [{"item_id": w.spice.id, "quantity": 33_334}]}) \
            if _ < 2 else L.create_document("STOCK_ISSUE", D(4), {"project_id": w.kitchen.id,
                                                                  "items": [{"item_id": w.spice.id, "quantity": 33_332}]})
    w.s.commit()
    assert L.stock_of(w.spice.id) == (0, 0)


def test_supplier_payment_and_its_limits(kitchen):
    w = kitchen
    buy_all(w)
    L = w.L
    L.create_document("CONTRIBUTION", D(5), {"partner_id": w.mahmoud.id, "money_account_id": w.cash.id,
                                             "amount": 7_200_000})
    L.create_document("SUPPLIER_PAYMENT", D(5), {"supplier_id": w.sharif.id, "amount": 7_500_000,
                                                 "money_account_id": w.cash.id})
    w.s.commit()
    assert L.balance(AP, supplier_id=w.sharif.id) == 0
    assert code(L.create_document, "SUPPLIER_PAYMENT", D(5), {"supplier_id": w.sharif.id, "amount": 1,
                                                              "money_account_id": w.cash.id}) == "err_payable_negative"
    w.s.rollback()
    # a partner pays a supplier from his own pocket: his capital grows
    before = L.balance(CAP, partner_id=w.contractor.id)
    L.create_document("SUPPLIER_PAYMENT", D(6), {"supplier_id": w.younja.id, "amount": 2_900_000,
                                                 "paid_by_partner_id": w.contractor.id})
    w.s.commit()
    assert L.balance(CAP, partner_id=w.contractor.id) == before + 2_900_000


def test_purchase_validation(kitchen):
    w = kitchen
    L = w.L
    base = {"supplier_id": w.younja.id, "items": [{"item_id": w.rice.id, "quantity": KG, "unit_price": 900}]}
    assert code(L.create_document, "PURCHASE", D(2), {**base, "supplier_id": None}) == "err_supplier_required"
    assert code(L.create_document, "PURCHASE", D(2), {**base, "items": []}) == "err_items_required"
    assert code(L.create_document, "PURCHASE", D(2), {**base, "paid": 901, "money_account_id": w.cash.id}) \
        == "err_paid_over_total"
    assert code(L.create_document, "PURCHASE", D(2), {**base, "paid": 100}) == "err_pay_source"
    # fully on credit: no pay source needed
    doc = L.create_document("PURCHASE", D(2), base)
    assert doc.status == "POSTED" and L.balance(AP, supplier_id=w.younja.id) == 900


def test_void_purchase_refused_once_stock_was_used(kitchen):
    w = kitchen
    p1, _p2, p3 = buy_all(w)
    L = w.L
    L.create_document("PRODUCTION", D(3), {"project_id": w.kitchen.id, "quantity": 100 * KG})
    w.s.commit()
    assert code(L.void_document, p3, "خطأ في الكمية") in ("err_stock_negative_qty", "err_stock_negative")
    w.s.rollback()
    assert w.s.get(Document, p3.id).status == "POSTED"


def test_production_without_recipe_stays_a_memo(opened):
    w = opened
    doc = w.L.create_document("PRODUCTION", D(3), {"project_id": w.parcels.id, "quantity": 10 * KG})
    assert not doc.items and not doc.lines


def test_master_rules_for_items_and_suppliers(kitchen):
    w = kitchen
    buy_all(w)
    L = w.L
    assert code(L.set_master_active, "supplier", w.sharif, False) == "err_supplier_not_settled"
    assert code(L.set_master_active, "item", w.rice, False) == "err_item_in_stock"
    assert code(L.delete_master, "item", w.rice) == "err_in_use_deactivate"
    assert code(L.save_master, "item", None, name="ملح", unit="", category="food") == "err_unit_required"
