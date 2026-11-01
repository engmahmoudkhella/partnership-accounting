"""Shares of one project: a partner could not fund his whole share, others covered it (option A)."""

import datetime as dt

from app.services import BusinessError

D = lambda day: dt.date(2026, 10, day)  # noqa: E731


def code(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except BusinessError as e:
        return e.code
    return None


def sell(w, project, day, amount):
    w.L.create_document("INVOICE", D(day), {"customer_id": w.org.id, "project_id": project.id, "quantity": 1000,
                                            "unit_price": amount})


def test_project_profit_follows_its_own_shares_from_their_date(opened):
    w = opened  # general shares 3:1:1:1 (50% / 16.6667% x3)
    L = w.L
    sell(w, w.parcels, 5, 120_000)  # before the project shares: general split
    # parcels project: the contractor funds 40,000 of 100,000, Mahmoud 30,000 (his 16.67% + the 10% gap) ...
    pv = L.create_project_share_version(w.parcels, D(10), {w.contractor.id: 4_000_000, w.mahmoud.id: 2_666_667,
                                                           w.momen.id: 1_666_667, w.ahmad.id: 1_666_666},
                                        mode="ratio", note="أبو مزيد دفع 40 ألف فقط")
    assert pv.mode == "ratio"
    sell(w, w.parcels, 12, 100_000)   # split 40 / 26.67 / 16.67 / 16.67
    sell(w, w.kitchen, 12, 600_000)   # other projects keep the general shares
    w.s.commit()
    alloc = L.profit_allocation()
    assert sum(alloc.values()) == L.net_profit() == 820_000
    # 1,200 general + 1,000 parcels (40 / 26.67 / 16.67 / 16.67) + 6,000 kitchen general
    assert alloc[w.contractor.id] == 60_000 + 40_000 + 300_000
    assert alloc[w.mahmoud.id] == 20_000 + 26_667 + 100_000
    assert abs(alloc[w.momen.id] - (20_000 + 16_667 + 100_000)) <= 1
    # shares used for a capital call of that project
    call = L.create_document("CAPITAL_CALL", D(13), {"amount": 10_000_000, "project_id": w.parcels.id})
    parts = {cs.partner_id: cs.amount for cs in call.call_shares}
    assert parts[w.contractor.id] == 4_000_000 and parts[w.mahmoud.id] == 2_666_667
    # and a distribution pays each one what he earned
    w.L.create_document("RECEIPT", D(14), {"customer_id": w.org.id, "money_account_id": w.cash.id, "amount": 820_000})
    dist = w.L.create_document("DISTRIBUTION", D(14), {"money_account_id": w.cash.id, "amount": 820_000})
    w.s.commit()
    paid = {ln.partner_id: ln.debit for ln in dist.lines if ln.partner_id}
    assert paid == alloc
    L.check_invariants()


def test_project_share_rules(opened):
    w = opened
    L = w.L
    good = {w.contractor.id: 400_000, w.mahmoud.id: 300_000, w.momen.id: 150_000, w.ahmad.id: 150_000}
    assert code(L.create_project_share_version, w.parcels, D(10), {**good, w.ahmad.id: 149_000}) == "err_shares_sum"
    assert code(L.create_project_share_version, w.parcels, dt.date(2026, 9, 1), good) == "err_share_date_before_opening"
    v1 = L.create_project_share_version(w.parcels, D(10), good)
    assert code(L.create_project_share_version, w.parcels, D(10), good) == "err_share_version_exists"
    v2 = L.create_project_share_version(w.parcels, D(20), good)
    assert code(L.delete_project_share_version, v1) == "err_share_not_latest"
    L.delete_project_share_version(v2)
    # a distribution fixes the past: no project shares dated on or before it
    sell(w, w.kitchen, 11, 100_000)
    w.L.create_document("RECEIPT", D(11), {"customer_id": w.org.id, "money_account_id": w.cash.id, "amount": 100_000})
    w.L.create_document("DISTRIBUTION", D(12), {"money_account_id": w.cash.id, "amount": 1_000})
    assert code(L.create_project_share_version, w.parcels, D(12), good) == "err_share_date_before_distribution"
    assert code(L.delete_project_share_version, v1) == "err_share_date_before_distribution"


def test_profit_without_a_project_is_still_shared_when_a_project_has_its_own_shares(opened):
    """A NULL project must not drop out of the general split (SQL: NOT (NULL = x) is NULL)."""
    w = opened
    L = w.L
    L.create_project_share_version(w.parcels, D(10), {w.contractor.id: 4_000_000, w.mahmoud.id: 2_666_667,
                                                      w.momen.id: 1_666_667, w.ahmad.id: 1_666_666}, mode="ratio")
    sell(w, w.parcels, 12, 100_000)
    # a general expense (no project) and general income
    L.create_document("EXPENSE", D(12), {"category": "fees", "amount": 6_000, "money_account_id": w.cash.id})
    L.create_document("INVOICE", D(12), {"customer_id": w.org.id, "project_id": w.kitchen.id, "quantity": 1000,
                                         "unit_price": 60_000})
    w.s.commit()
    alloc = L.profit_allocation()
    assert sum(alloc.values()) == L.net_profit() == 100_000 + 60_000 - 6_000
