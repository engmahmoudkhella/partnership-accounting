"""Randomised check: whatever sequence of operations people do, the books stay consistent.

Hypothesis generates hundreds of random sequences of creates, edits, voids, deletes and approvals
(most of them invalid). After every step that succeeds — and after every step that is refused and
rolled back — the following must hold:

* total debits == total credits
* assets == partners' equity (balance sheet)
* no money account, customer balance, partner capital or asset is negative on any day
* partners' profit shares add up exactly to net profit
* distributions never exceed collected profit, and no partner receives more than his own profit
* voiding a document returns all balances exactly to what they were before it was created
"""

import datetime as dt
import os

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from app.db import init_db, make_engine, make_sessionmaker
from app.models import Document, User
from app.services import AR, CAP, CASH, FA, BusinessError, Ledger

TODAY = dt.date(2026, 10, 31)
OPEN = dt.date(2026, 10, 1)

op = st.one_of(
    st.tuples(st.just("CONTRIBUTION"), st.integers(1, 30), st.integers(0, 3), st.integers(0, 1),
              st.integers(1, 3_000_000)),
    st.tuples(st.just("EXPENSE"), st.integers(1, 30), st.integers(0, 1), st.integers(0, 1),
              st.integers(1, 3_000_000)),
    st.tuples(st.just("EXPENSE_BY_PARTNER"), st.integers(1, 30), st.integers(0, 3), st.integers(0, 1),
              st.integers(1, 1_000_000)),
    st.tuples(st.just("INVOICE"), st.integers(1, 30), st.integers(1, 2000), st.integers(0, 1),
              st.integers(1, 5000)),
    st.tuples(st.just("RECEIPT"), st.integers(1, 30), st.integers(0, 1), st.integers(0, 1000),
              st.integers(1, 3_000_000)),
    st.tuples(st.just("TRANSFER"), st.integers(1, 30), st.integers(0, 1), st.integers(0, 500),
              st.integers(1, 2_000_000)),
    st.tuples(st.just("DISTRIBUTION"), st.integers(1, 30), st.integers(0, 1), st.integers(0, 1),
              st.integers(1, 2_000_000)),
    st.tuples(st.just("ASSET"), st.integers(1, 30), st.integers(0, 1), st.integers(0, 1),
              st.integers(1, 2_000_000)),
    st.tuples(st.just("DISPOSE"), st.integers(1, 30), st.integers(0, 50), st.integers(0, 1),
              st.integers(0, 2_000_000)),
    st.tuples(st.just("CAPITAL_TRANSFER"), st.integers(1, 30), st.integers(0, 3), st.integers(0, 3),
              st.integers(1, 1_000_000)),
    st.tuples(st.just("VOID"), st.integers(0, 50), st.just(0), st.just(0), st.just(0)),
    st.tuples(st.just("EDIT_AMOUNT"), st.integers(0, 50), st.integers(1, 30), st.just(0),
              st.integers(1, 3_000_000)),
    st.tuples(st.just("APPROVE_ALL"), st.integers(0, 50), st.just(0), st.just(0), st.just(0)),
    st.tuples(st.just("DELETE"), st.integers(0, 50), st.just(0), st.just(0), st.just(0)),
)


def check(L: Ledger):
    tb = L.trial_balance()
    assert tb["balanced"], tb
    assert tb["sheet_ok"], tb
    L.check_invariants()  # raises on any negative running balance / over-distribution
    alloc = L.profit_allocation()
    assert sum(alloc.values()) == tb["profit"]
    for acc in (CASH, AR, FA):
        assert L.balance(acc) >= 0
    assert L.balance(CAP) >= 0
    for r in L.partner_summary():
        assert r["distributions"] <= r["profit"] or r["distributions"] == 0, r


def state(L: Ledger):
    return (L.trial_balance()["rows"], L.distributable(TODAY), tuple(sorted(L.profit_allocation().items())))


@settings(max_examples=int(os.environ.get("PROP_EXAMPLES", "80")), deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(op, min_size=5, max_size=40))
def test_random_operations_keep_books_consistent(tmp_path_factory, ops):
    path = tmp_path_factory.mktemp("prop") / "p.db"
    engine = make_engine(f"sqlite:///{path}")
    init_db(engine)
    s = make_sessionmaker(engine)()
    try:
        admin = User(username="admin", full_name="a", password_hash="x", role="admin")
        s.add(admin)
        s.flush()
        L = Ledger(s, admin, today=TODAY)
        partners = [L.save_partner(None, n) for n in ("A", "B", "C", "D")]
        L.create_share_version(dt.date(2026, 1, 1), {partners[0].id: 3, partners[1].id: 1, partners[2].id: 1,
                                                     partners[3].id: 1}, mode="ratio")
        accts = [L.save_master("money_account", None, name=n, kind="cash") for n in ("cash", "bank")]
        cust = L.save_master("customer", None, name="org")
        proj = L.save_master("project", None, name="kitchen", kind="kitchen")
        L.save_opening(OPEN, {accts[0].id: 500_000}, {}, [], {partners[0].id: 500_000})
        users = []
        for p in partners:
            u = User(username=f"u{p.id}", full_name="p", password_hash="x", role="partner", partner_id=p.id)
            s.add(u)
            users.append(u)
        s.flush()
        L.set_approval_threshold(1_000_000)
        s.commit()
        check(L)
        for kind, day, a, b, amount in ops:
            d = dt.date(2026, 10, min(max(day, 1), 30))
            before = state(L)
            try:
                docs = list(s.scalars(select(Document).where(Document.doc_type != "OPENING").order_by(Document.id)))
                if kind == "CONTRIBUTION":
                    L.create_document(kind, d, {"partner_id": partners[a].id, "money_account_id": accts[b].id,
                                                "amount": amount})
                elif kind == "EXPENSE":
                    L.create_document(kind, d, {"amount": amount, "category": "food", "project_id": proj.id,
                                                "money_account_id": accts[a].id})
                elif kind == "EXPENSE_BY_PARTNER":
                    L.create_document("EXPENSE", d, {"amount": amount, "category": "rent",
                                                     "paid_by_partner_id": partners[a].id})
                elif kind == "INVOICE":
                    L.create_document(kind, d, {"customer_id": cust.id, "project_id": proj.id,
                                                "quantity": a * 1000, "unit_price": amount})
                elif kind == "RECEIPT":
                    L.create_document(kind, d, {"customer_id": cust.id, "money_account_id": accts[a].id,
                                                "amount": amount, "fee": min(b, amount - 1)})
                elif kind == "TRANSFER":
                    L.create_document(kind, d, {"money_account_id": accts[a].id, "money_account2_id": accts[1 - a].id,
                                                "amount": amount, "fee": min(b, amount - 1)})
                elif kind == "DISTRIBUTION":
                    L.create_document(kind, d, {"money_account_id": accts[a].id, "amount": amount})
                elif kind == "ASSET":
                    L.create_document("ASSET_PURCHASE", d, {"amount": amount, "money_account_id": accts[a].id},
                                      asset_name=f"asset{len(docs)}")
                elif kind == "DISPOSE":
                    buys = [x for x in docs if x.doc_type == "ASSET_PURCHASE" and x.status == "POSTED"]
                    if buys:
                        L.create_document("ASSET_DISPOSAL", d, {"asset_id": buys[a % len(buys)].asset_id,
                                                                "amount": amount, "money_account_id": accts[0].id})
                elif kind == "CAPITAL_TRANSFER":
                    L.create_document(kind, d, {"partner_id": partners[a].id, "partner2_id": partners[b].id,
                                                "amount": amount})
                elif kind == "VOID" and docs:
                    doc = docs[day % len(docs)]
                    if doc.status == "POSTED":
                        L.void_document(doc, "random void")
                        s.commit()
                        check(L)
                        continue
                elif kind == "EDIT_AMOUNT" and docs:
                    doc = docs[day % len(docs)]
                    if doc.status in ("POSTED", "PENDING") and doc.doc_type in ("EXPENSE", "CONTRIBUTION"):
                        data = {f: getattr(doc, f) for f in L.FIELDS[doc.doc_type]}
                        data["amount"] = amount
                        L.update_document(doc, dt.date(2026, 10, a), data)
                elif kind == "APPROVE_ALL" and docs:
                    doc = docs[day % len(docs)]
                    if doc.status == "PENDING":
                        for u in users:
                            if doc.status == "PENDING" and not any(x.user_id == u.id for x in doc.approvals):
                                Ledger(s, u, today=TODAY).decide(doc, "approve")
                elif kind == "DELETE" and docs:
                    doc = docs[day % len(docs)]
                    if doc.status in ("PENDING", "REJECTED"):
                        L.delete_document(doc)
                s.commit()
            except BusinessError:
                s.rollback()
                assert state(L) == before  # a refused operation changes nothing
            check(L)
    finally:
        s.close()
        engine.dispose()


@settings(max_examples=int(os.environ.get("PROP_EXAMPLES", "60")), deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.tuples(st.sampled_from(["CONTRIBUTION", "EXPENSE", "INVOICE", "TRANSFER"]),
                          st.integers(1, 30), st.integers(1, 500_000)), min_size=1, max_size=15),
       st.data())
def test_void_is_exact_inverse_of_create(tmp_path_factory, ops, data):
    """Create random valid documents; voiding the newest one restores the previous state exactly."""
    path = tmp_path_factory.mktemp("inv") / "p.db"
    engine = make_engine(f"sqlite:///{path}")
    init_db(engine)
    s = make_sessionmaker(engine)()
    try:
        admin = User(username="admin", full_name="a", password_hash="x",
                     role="admin")
        s.add(admin)
        s.flush()
        L = Ledger(s, admin, today=TODAY)
        p = [L.save_partner(None, n) for n in ("A", "B")]
        L.create_share_version(dt.date(2026, 1, 1), {p[0].id: 1, p[1].id: 1}, mode="ratio")
        a = [L.save_master("money_account", None, name=n, kind="cash") for n in ("x", "y")]
        c = L.save_master("customer", None, name="c")
        pr = L.save_master("project", None, name="k", kind="kitchen")
        L.save_opening(OPEN, {a[0].id: 10_000_000}, {}, [], {p[0].id: 10_000_000})
        s.commit()
        for kind, day, amount in ops:
            d = dt.date(2026, 10, day)
            before = state(L)
            payload = {
                "CONTRIBUTION": {"partner_id": p[1].id, "money_account_id": a[1].id, "amount": amount},
                "EXPENSE": {"amount": amount, "category": "food", "money_account_id": a[0].id, "project_id": pr.id},
                "INVOICE": {"customer_id": c.id, "project_id": pr.id, "quantity": 1000, "unit_price": amount},
                "TRANSFER": {"money_account_id": a[0].id, "money_account2_id": a[1].id, "amount": amount, "fee": 0},
            }[kind]
            try:
                doc = L.create_document(kind, d, payload)
                s.commit()
            except BusinessError:
                s.rollback()
                continue
            if data.draw(st.booleans()):
                L.void_document(doc, "inverse")
                s.commit()
                assert state(L) == before
            check(L)
    finally:
        s.close()
        engine.dispose()
