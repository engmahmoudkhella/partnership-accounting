import datetime as dt

from conftest import OPEN


def test_full_kitchen_month(opened):
    w = opened
    L = w.L
    d = lambda day: dt.date(2026, 10, day)  # noqa: E731
    # partners each pay their half of a 180,000 barracks
    for p, amt in ((w.contractor, 9_000_000), (w.mahmoud, 3_000_000), (w.momen, 3_000_000), (w.ahmad, 3_000_000)):
        L.create_document("CONTRIBUTION", d(2), {"partner_id": p.id, "money_account_id": w.cash.id, "amount": amt})
    L.create_document("ASSET_PURCHASE", d(3), {"amount": 18_000_000, "project_id": w.kitchen.id,
                                               "money_account_id": w.cash.id}, asset_name="بركس المطبخ")
    L.create_document("EXPENSE", d(3), {"amount": 200_000, "category": "rent", "project_id": w.kitchen.id,
                                        "money_account_id": w.cash.id})
    L.create_document("PRODUCTION", d(10), {"project_id": w.kitchen.id, "quantity": 1000_000})
    inv = L.create_document("INVOICE", d(10), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                               "quantity": 1000_000, "unit_price": 1700})
    assert inv.amount == 1_700_000
    L.create_document("EXPENSE", d(9), {"amount": 800_000, "category": "meat", "project_id": w.kitchen.id,
                                        "money_account_id": w.cash.id})
    L.create_document("RECEIPT", d(20), {"customer_id": w.org.id, "money_account_id": w.cash.id,
                                         "amount": 1_700_000, "fee": 0})
    w.s.commit()
    tb = L.trial_balance()
    assert tb["balanced"] and tb["sheet_ok"]
    assert tb["profit"] == 1_700_000 - 200_000 - 800_000
    rep = {r["project"].id if r["project"] else None: r for r in L.project_report(None, None)}
    assert rep[w.kitchen.id]["net"] == 700_000
    assert rep[w.kitchen.id]["cost_per_unit"] == 1000  # 10 ILS per meal
    alloc = L.profit_allocation()
    assert sum(alloc.values()) == 700_000
    assert alloc[w.contractor.id] == 350_000
    assert L.distributable(d(31)) == 700_000
