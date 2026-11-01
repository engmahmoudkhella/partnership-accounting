"""Per-user permissions: sections x actions, chosen on the users page (the role only gives defaults)."""

import datetime as dt

from sqlalchemy import select

from app import permissions as PERM
from app.models import Document, User
from conftest import ledger_for, make_user
from test_contracts import code
from test_http import TODAY, Client, add_user, app, client, ready  # noqa: F401

D = dt.date(2026, 10, 5)
EXP = lambda w: {"amount": 10_000, "category": "food", "money_account_id": w.cash.id}  # noqa: E731
INV = lambda w: {"customer_id": w.org.id, "project_id": w.kitchen.id, "quantity": 1000, "unit_price": 10_000}  # noqa: E731


def custom(w, username, role, s, own_only=False):
    u = make_user(w.s, username, role)
    u.permissions = PERM.clean({"s": s, "own_only": own_only}, role)
    w.s.commit()
    return u, ledger_for(w, u)


def fails(w, fn, *a, **kw):
    w.s.commit()
    c = code(fn, *a, **kw)
    w.s.rollback()
    return c


def test_presets_keep_the_old_roles():
    acc = PERM.preset("accountant")["s"]
    assert acc["sales"] == {"view", "add", "edit", "delete"} and acc["partners"] == {"view"}
    assert acc["months"] == {"view"}
    p = PERM.preset("partner")
    assert p["own_only"] and p["s"]["expenses"] >= {"add", "edit", "delete"} and p["s"]["sales"] == {"view"}
    assert all(v == {"view"} for v in PERM.preset("viewer")["s"].values())


def test_clean_drops_unknown_and_doing_implies_seeing():
    p = PERM.clean({"s": {"sales": ["add"], "hack": ["view"], "reports": ["delete", "view"]}, "own_only": 1}, "viewer")
    assert p == {"s": {"sales": ["view", "add"], "reports": ["view"]}, "own_only": True}


def test_a_user_can_only_do_what_he_was_given(opened):
    w = opened
    u, L = custom(w, "sales1", "accountant", {"sales": ["view", "add"]})
    L.create_document("INVOICE", D, INV(w))
    assert fails(w, L.create_document, "EXPENSE", D, EXP(w)) == "err_forbidden"
    inv = w.s.scalar(select(Document).where(Document.doc_type == "INVOICE"))
    assert fails(w, L.update_document, inv, D, INV(w)) == "err_forbidden"
    assert fails(w, L.void_document, inv, "خطأ") == "err_forbidden"
    assert fails(w, L.save_master, "customer", None, name="جهة") == "err_forbidden"
    # a viewer can be allowed to enter expenses
    v, Lv = custom(w, "viewer1", "viewer", {"expenses": ["view", "add"]})
    Lv.create_document("EXPENSE", D, EXP(w))


def test_own_only_limits_to_what_he_entered(opened):
    w = opened
    a, La = custom(w, "clerk1", "accountant", {"expenses": ["view", "add", "edit", "delete"]}, own_only=True)
    b, Lb = custom(w, "clerk2", "accountant", {"expenses": ["view", "add", "edit", "delete"]}, own_only=True)
    mine = La.create_document("EXPENSE", D, EXP(w))
    w.s.commit()
    La.update_document(mine, D, {**EXP(w), "amount": 20_000})
    assert fails(w, Lb.update_document, mine, D, EXP(w)) == "err_forbidden"
    assert fails(w, Lb.void_document, mine, "خطأ") == "err_forbidden"
    La.void_document(mine, "خطأ مني")


def test_admin_always_has_everything(opened):
    w = opened
    w.admin.permissions = {"s": {}, "own_only": True}
    w.s.commit()
    w.L.create_document("EXPENSE", D, EXP(w))


# ------------------------------------------------------------------ web

def test_users_page_saves_permissions_and_the_app_follows_them(ready):
    c = ready
    r = c.get("/users")
    assert 'data-testid="perms"' in r.text and "perm_sales_add" in r.text
    r = c.post("/users/save", {"full_name": "محاسب المبيعات", "username": "salesman", "role": "accountant",
                               "password": "password123", "password2": "password123", "lang": "ar", "digits": "latn",
                               "perm_sales_view": "1", "perm_sales_add": "1", "perm_reports_view": "1"},
               page="/users")
    assert "flash ok" in r.text
    s = c.db()
    u = s.scalar(select(User).where(User.username == "salesman"))
    assert u.permissions == {"s": {"sales": ["view", "add"], "reports": ["view"]}, "own_only": False}
    s.close()
    # an expense exists (made by the admin)
    c.post("/documents/new/EXPENSE", {"date": "2026-10-05", "category": "food", "amount": "50",
                                      "pay_source": "account", "money_account_id": str(c.ids["cash"])},
           page="/documents/new/EXPENSE")
    s = c.db()
    exp_id = s.scalar(select(Document.id).where(Document.doc_type == "EXPENSE"))
    s.close()
    c.login("salesman")
    home = c.get("/").text
    assert "/documents/new/INVOICE" in home and "/documents/new/EXPENSE" not in home
    assert 'href="/master/project"' not in home and 'href="/quotes"' in home
    assert c.get("/documents/new/EXPENSE").status_code == 403
    assert c.get(f"/documents/{exp_id}").status_code == 403
    assert f'href="/documents/{exp_id}"' not in c.get("/documents").text
    assert c.get("/master/customer").status_code == 403
    assert c.get("/quotes/new").status_code == 200
    assert c.get("/reports/trial").status_code == 200
    assert c.get("/audit").status_code == 403
    r = c.post("/documents/new/INVOICE", {"date": "2026-10-06", "customer_id": str(c.ids["customer"]),
                                          "project_id": str(c.ids["project"]), "quantity": "10", "unit_price": "17"},
               page="/documents/new/INVOICE")
    assert "flash ok" in r.text
    # no edit permission: no edit button, and the edit page is refused
    s = c.db()
    inv_id = s.scalar(select(Document.id).where(Document.doc_type == "INVOICE"))
    s.close()
    assert 'data-testid="edit"' not in c.get(f"/documents/{inv_id}").text
    assert c.get(f"/documents/{inv_id}/edit").status_code == 403
    assert c.get("/users").status_code == 403
