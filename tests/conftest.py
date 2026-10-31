import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import init_db, make_engine, make_sessionmaker  # noqa: E402
from app.models import Customer, MoneyAccount, Partner, Project, User  # noqa: E402
from app.services import Ledger, hash_password  # noqa: E402

TODAY = dt.date(2026, 10, 31)
OPEN = dt.date(2026, 10, 1)

# 50% contractor, the other 50% split exactly in three: ratio 3:1:1:1
SHARES = [("عبد الوهاب سهيل مزيد", 3), ("محمود عدنان خله", 1),
          ("مؤمن سليم", 1), ("أحمد خالد سليم", 1)]


@pytest.fixture
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    init_db(engine)
    S = make_sessionmaker(engine)
    s = S()
    yield s
    s.close()
    engine.dispose()


_HASH = hash_password("password123")  # computed once: bcrypt is deliberately slow


def make_user(s, username, role, partner=None):
    u = User(username=username, full_name=username, password_hash=_HASH, role=role,
             partner_id=partner.id if partner else None)
    s.add(u)
    s.flush()
    return u


@pytest.fixture
def world(session):
    """Partners, shares, accounts, a customer, two projects. No opening yet."""
    s = session
    admin = make_user(s, "admin", "admin")
    L = Ledger(s, admin, today=TODAY)
    partners = [L.save_partner(None, n) for n, _ in SHARES]
    L.create_share_version(dt.date(2026, 1, 1), {p.id: w for p, (_, w) in zip(partners, SHARES)}, mode="ratio")
    cash = L.save_master("money_account", None, name="الصندوق", kind="cash")
    bank = L.save_master("money_account", None, name="Bank of Palestine", kind="bank")
    org = L.save_master("customer", None, name="Gift of the Givers")
    kitchen = L.save_master("project", None, name="مطبخ", kind="kitchen", unit_name="وجبة")
    parcels = L.save_master("project", None, name="طرود", kind="parcels", unit_name="طرد")
    s.commit()

    class W:
        pass

    w = W()
    w.s, w.admin, w.L, w.partners = s, admin, L, partners
    w.cash, w.bank, w.org, w.kitchen, w.parcels = cash, bank, org, kitchen, parcels
    w.contractor, w.mahmoud, w.momen, w.ahmad = partners
    return w


@pytest.fixture
def opened(world):
    """World plus an opening balance: 10,000 cash funded 50/50."""
    w = world
    w.L.save_opening(OPEN, money={w.cash.id: 1_000_000}, customers={}, assets=[],
                     partners={w.contractor.id: 500_000, w.mahmoud.id: 166_667, w.momen.id: 166_667,
                               w.ahmad.id: 166_666})
    w.s.commit()
    return w


def ledger_for(w, user):
    return Ledger(w.s, user, today=TODAY)
