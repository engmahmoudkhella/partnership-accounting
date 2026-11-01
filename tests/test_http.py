"""End-to-end through the web forms (no browser): Arabic/English input, errors, roles, security."""

import datetime as dt
import io
import os
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.i18n import T
from app.main import create_app
from app.models import Asset, Document, MoneyAccount, Partner, Project, ShareVersion, User
from app.services import CASH, Ledger, hash_password

TODAY = dt.date(2026, 10, 31)
APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


def csrf_of(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf token on page"
    return m.group(1)


class Client:
    def __init__(self, app):
        self.c = TestClient(app)
        self.app = app

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, data=None, files=None, page=None, follow=True):
        page = page or url
        html = self.c.get(page).text
        data = dict(data or {})
        data.setdefault("csrf", csrf_of(html))
        return self.c.post(url, data=data, files=files, follow_redirects=follow,
                           headers={"referer": "http://testserver" + page})

    def login(self, username, password="password123"):
        self.c.cookies.clear()
        return self.post("/login", {"username": username, "password": password}, page="/login")

    def db(self):
        return self.app.state.Session()


@pytest.fixture
def app(tmp_path):
    return create_app(f"sqlite:///{tmp_path}/web.db", str(tmp_path), today_fn=lambda: TODAY, secret_key="test")


@pytest.fixture
def client(app):
    c = Client(app)
    r = c.post("/setup", {"username": "admin", "full_name": "المدير", "password": "password123",
                          "password2": "password123", "company_name": "شراكة غزة"}, page="/setup")
    assert r.status_code == 200 and "شراكة غزة" in r.text
    return c


def add_user(client, username, role, partner_id=None):
    s = client.db()
    s.add(User(username=username, full_name=username, password_hash=hash_password("password123"), role=role,
               partner_id=partner_id))
    s.commit()
    s.close()


@pytest.fixture
def ready(client):
    """Partners (ratio 3:1:1:1 typed with Arabic digits), accounts, customer, project, opening."""
    c = client
    for name in ("عبد الوهاب سهيل مزيد", "محمود عدنان خله", "مؤمن سليم", "أحمد خالد سليم"):
        assert c.post("/partners/save", {"name": name}, page="/partners").status_code == 200
    s = c.db()
    ids = [p.id for p in s.scalars(select(Partner).order_by(Partner.id))]
    s.close()
    form = {"effective_date": "2026-01-01", "mode": "ratio", "note": ""}
    form.update({f"share_{pid}": v for pid, v in zip(ids, ["٣", "١", "١", "١"])})
    r = c.post("/shares/new", form, page="/partners")
    assert "error" not in r.text.split("<main>")[1][:300], r.text
    c.post("/master/money_account/save", {"name": "الصندوق", "kind": "cash"}, page="/master/money_account")
    c.post("/master/money_account/save", {"name": "البنك", "kind": "bank"}, page="/master/money_account")
    c.post("/master/customer/save", {"name": "Gift of the Givers"}, page="/master/customer")
    c.post("/master/project/save", {"name": "المطبخ", "kind": "kitchen", "unit_name": "وجبة"},
           page="/master/project")
    s = c.db()
    c.ids = {"partners": ids, "cash": s.scalar(select(MoneyAccount.id).where(MoneyAccount.name == "الصندوق")),
             "bank": s.scalar(select(MoneyAccount.id).where(MoneyAccount.name == "البنك")),
             "project": s.scalar(select(Project.id)), "customer": 1}
    s.close()
    # opening: 10,000.50 in the box typed in Arabic digits, funded by the contractor
    r = c.post("/opening", {"date": "2026-10-01", f"m_{c.ids['cash']}": "١٠٬٠٠٠٫٥٠",
                            f"p_{ids[0]}": "10,000.50"}, page="/opening")
    assert r.status_code == 200 and "flash ok" in r.text, r.text
    return c


def ledger(c):
    s = c.db()
    return s, Ledger(s, None, today=TODAY)


# ------------------------------------------------------------------ setup / auth / csrf

def test_first_visit_goes_to_setup_then_setup_closes(app):
    c = Client(app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    c.post("/setup", {"username": "admin", "full_name": "a", "password": "password123", "password2": "password123"},
           page="/setup")
    c.c.cookies.clear()
    r = c.get("/setup", follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_setup_password_mismatch(app):
    c = Client(app)
    r = c.post("/setup", {"username": "admin", "full_name": "a", "password": "password123",
                          "password2": "different1"}, page="/setup")
    assert "كلمتا المرور غير متطابقتين" in r.text
    s = c.db()
    assert s.scalar(select(User)) is None


def test_setup_error_keeps_typed_values(app):
    c = Client(app)
    r = c.post("/setup", {"username": "x", "full_name": "محمود خله", "company_name": "شراكة المطبخ",
                          "password": "password123", "password2": "password123"}, page="/setup")
    assert r.status_code == 422
    assert 'value="محمود خله"' in r.text and 'value="شراكة المطبخ"' in r.text
    assert "password123" not in r.text
    assert c.db().scalar(select(User)) is None


def test_login_logout_and_rate_limit(client):
    c = client
    c.c.cookies.clear()
    r = c.login("admin", "wrong-password")
    assert r.status_code == 401 and "غير صحيحة" in r.text
    for _ in range(4):
        c.login("admin", "wrong-password")
    r = c.login("admin", "password123")
    assert r.status_code == 429  # locked for 5 minutes even with the right password
    r = c.login("ADMIN ", "password123")  # same username normalised -> still locked
    assert r.status_code == 429


def test_login_success_and_logout(client):
    c = client
    c.c.cookies.clear()
    r = c.login("admin")
    assert r.status_code == 200 and "المدير" in r.text
    r = c.post("/logout", {}, page="/")
    assert "/login" in str(r.url)
    assert c.get("/documents", follow_redirects=False).status_code == 303


def test_open_redirect_blocked(client):
    c = client
    c.c.cookies.clear()
    r = c.post("/login", {"username": "admin", "password": "password123", "next": "//evil.example"},
               page="/login", follow=False)
    assert r.headers["location"] == "/"
    c.c.cookies.clear()
    r = c.post("/login", {"username": "admin", "password": "password123", "next": "https://evil.example"},
               page="/login", follow=False)
    assert r.headers["location"] == "/"


def test_csrf_required(ready):
    c = ready
    r = c.c.post("/documents/new/PRODUCTION", data={"date": "2026-10-02", "project_id": c.ids["project"],
                                                    "quantity": "5"})
    assert r.status_code == 403
    r = c.c.post("/documents/new/PRODUCTION", data={"csrf": "forged", "date": "2026-10-02",
                                                    "project_id": c.ids["project"], "quantity": "5"})
    assert r.status_code == 403
    s, L = ledger(c)
    assert s.scalar(select(Document).where(Document.doc_type == "PRODUCTION")) is None


def test_inactive_user_session_is_dropped(ready):
    c = ready
    add_user(c, "acc", "accountant")
    c.login("acc")
    s = c.db()
    u = s.scalar(select(User).where(User.username == "acc"))
    u.active = False
    s.commit()
    assert c.get("/documents", follow_redirects=False).status_code == 303


# ------------------------------------------------------------------ Arabic / English number entry through forms

def test_opening_saved_exactly_from_arabic_digits(ready):
    s, L = ledger(ready)
    assert L.balance(CASH, money_account_id=ready.ids["cash"]) == 1_000_050
    assert L.trial_balance()["sheet_ok"]
    v = s.scalar(select(ShareVersion))
    assert v.mode == "ratio" and sorted(ln.weight for ln in v.lines) == [1, 1, 1, 3]


@pytest.mark.parametrize("typed,expected", [
    ("١٧٫٥", 1750), ("17.5", 1750), ("١٬٢٣٤٫٥٦", 123456), ("1,234.56", 123456), ("۲۰۰", 20000),
    ("‏١٧‏", 1700), ("17 ₪", 1700), ("١٧ شيكل", 1700), (" 0.01 ", 1),
])
def test_expense_amount_from_form(ready, typed, expected):
    c = ready
    r = c.post("/documents/new/EXPENSE", {"date": "2026-10-02", "category": "meat", "project_id": c.ids["project"],
                                          "amount": typed, "pay_source": "account",
                                          "money_account_id": c.ids["cash"]}, page="/documents/new/EXPENSE")
    assert r.status_code == 200, r.text
    s, _ = ledger(c)
    doc = s.scalar(select(Document).where(Document.doc_type == "EXPENSE"))
    assert doc.amount == expected


@pytest.mark.parametrize("typed,msg_ar,msg_en", [
    ("12,5", "استخدم النقطة", "Use a dot"),
    ("abc", "صيغة الرقم غير صحيحة", "Invalid number"),
    ("-5", "لا يُقبل رقم سالب", "Negative numbers"),
    ("17.555", "هل تقصد 17555", "Did you mean 17555"),
    ("1.500", "هل تقصد 1500", "Did you mean 1500"),
    ("1.5555", "الحد الأقصى 2 خانات", "At most 2 decimal"),
    ("0", "أكبر من صفر", "greater than zero"),
    ("", "أدخل رقماً", "Enter a number"),
    ("$5", "صيغة الرقم غير صحيحة", "Invalid number"),
])
def test_bad_numbers_show_clear_error_in_both_languages(ready, typed, msg_ar, msg_en):
    c = ready
    form = {"date": "2026-10-02", "category": "meat", "amount": typed, "pay_source": "account",
            "money_account_id": c.ids["cash"], "description": "وصف يبقى"}
    r = c.post("/documents/new/EXPENSE", form, page="/documents/new/EXPENSE")
    assert r.status_code == 422
    assert msg_ar in r.text
    assert "وصف يبقى" in r.text  # what the user typed is kept
    assert f'value="{typed}"' in r.text or typed == ""
    c.get("/lang/en")
    r = c.post("/documents/new/EXPENSE", form, page="/documents/new/EXPENSE")
    assert r.status_code == 422 and msg_en in r.text
    s, _ = ledger(c)
    assert s.scalar(select(Document).where(Document.doc_type == "EXPENSE")) is None


def test_invoice_quantity_and_price_in_arabic(ready):
    c = ready
    r = c.post("/documents/new/INVOICE", {"date": "2026-10-10", "customer_id": "1", "project_id": c.ids["project"],
                                          "quantity": "١٠٠٠", "unit_price": "١٧"}, page="/documents/new/INVOICE")
    assert r.status_code == 200
    s, _ = ledger(c)
    inv = s.scalar(select(Document).where(Document.doc_type == "INVOICE"))
    assert (inv.quantity, inv.unit_price, inv.amount) == (1_000_000, 1700, 1_700_000)
    assert "17,000.00" in r.text


def test_dates_typed_with_arabic_digits(ready):
    c = ready
    r = c.post("/documents/new/PRODUCTION", {"date": "٢٠٢٦-١٠-٠٥", "project_id": c.ids["project"], "quantity": "٥٠٠"},
               page="/documents/new/PRODUCTION")
    assert r.status_code == 200
    s, _ = ledger(c)
    assert s.scalar(select(Document.date).where(Document.doc_type == "PRODUCTION")) == dt.date(2026, 10, 5)


@pytest.mark.parametrize("date,msg", [("2026-02-30", "التاريخ غير صحيح"), ("2026-11-01", "المستقبل"),
                                      ("2026-09-30", "قبل بداية الحسابات"), ("", "التاريخ غير صحيح"),
                                      ("yesterday", "التاريخ غير صحيح")])
def test_bad_dates(ready, date, msg):
    c = ready
    r = c.post("/documents/new/PRODUCTION", {"date": date, "project_id": c.ids["project"], "quantity": "5"},
               page="/documents/new/PRODUCTION")
    assert r.status_code == 422 and msg in r.text


def test_business_error_keeps_form_values(ready):
    c = ready
    r = c.post("/documents/new/EXPENSE", {"date": "2026-10-02", "category": "meat", "amount": "50000",
                                          "pay_source": "account", "money_account_id": c.ids["cash"],
                                          "supplier": "ملحمة الأمل"}, page="/documents/new/EXPENSE")
    assert r.status_code == 422
    assert "الرصيد لا يكفي" in r.text and "-39,999.50" in r.text and "ملحمة الأمل" in r.text


def test_arabic_digit_display_preference(ready):
    c = ready
    c.post("/account/prefs", {"lang": "ar", "digits": "arab"}, page="/account")
    r = c.get("/")
    assert "١٠٬٠٠٠٫٥٠" in r.text


def test_opening_errors_show_per_field(ready):
    c = ready
    ids = c.ids
    r = c.post("/opening", {"date": "2026-10-01", f"m_{ids['cash']}": "12,5", f"p_{ids['partners'][0]}": "10"},
               page="/opening")
    assert r.status_code == 422 and "استخدم النقطة" in r.text
    r = c.post("/opening", {"date": "2026-10-01", f"m_{ids['cash']}": "100", f"p_{ids['partners'][0]}": "99.99"},
               page="/opening")
    assert r.status_code == 422 and "غير متوازنة" in r.text and "0.01" in r.text
    r = c.post("/opening", {"date": "2026-10-01", "asset_name_0": "", "asset_amount_0": "500",
                            f"p_{ids['partners'][0]}": "500"}, page="/opening")
    assert r.status_code == 422 and "أدخل الاسم" in r.text


def test_opening_with_assets_through_form(ready):
    c = ready
    ids = c.ids
    r = c.post("/opening", {"date": "2026-10-01", f"m_{ids['cash']}": "10000.50", "asset_name_0": "بركس",
                            "asset_amount_0": "١٨٠٬٠٠٠", "asset_project_0": str(ids["project"]),
                            f"p_{ids['partners'][0]}": "100000.50", f"p_{ids['partners'][1]}": "90000"},
               page="/opening")
    assert r.status_code == 200 and "flash ok" in r.text
    s, L = ledger(c)
    assert L.balance("1500") == 18_000_000
    asset = s.scalar(select(Asset))
    assert asset.name == "بركس"
    # page shows the asset back for editing with its id
    page = c.get("/opening").text
    assert f'name="asset_id_0" value="{asset.id}"' in page and "180000.00" in page


def test_shares_percent_must_total_100_via_form(ready):
    c = ready
    ids = c.ids["partners"]
    form = {"effective_date": "2026-10-15", "mode": "percent"}
    form.update({f"share_{pid}": v for pid, v in zip(ids, ["50", "16.6", "16.6", "16.6"])})
    r = c.post("/shares/new", form, page="/partners")
    assert "100% بالضبط" in r.text
    # 16.6667 x 3 + 50 = 100.0001 is accepted and saved as exact thirds (ratio 3:1:1:1)
    form.update({f"share_{pid}": v for pid, v in zip(ids, ["50", "16.6667", "16.6667", "16.6667"])})
    r = c.post("/shares/new", form, page="/partners")
    assert "flash ok" in r.text
    s = c.db()
    v = s.scalars(select(ShareVersion).order_by(ShareVersion.effective_date.desc())).first()
    assert v.mode == "ratio" and sorted(ln.weight for ln in v.lines) == [1, 1, 1, 3]


# ------------------------------------------------------------------ roles over HTTP

def test_viewer_is_read_only(ready):
    c = ready
    add_user(c, "viewer", "viewer")
    c.login("viewer")
    for url in ("/", "/documents", "/reports/trial", "/reports/partners", "/audit", "/opening"):
        assert c.get(url).status_code == 200, url
    r = c.post("/documents/new/PRODUCTION", {"date": "2026-10-02", "project_id": c.ids["project"], "quantity": "5"},
               page="/documents")
    assert r.status_code == 403
    assert c.get("/users").status_code == 403
    assert c.get("/settings").status_code == 403
    assert c.get("/backup").status_code == 403
    assert c.post("/opening", {"date": "2026-10-01"}, page="/opening").status_code == 403


def test_partner_permissions(ready):
    c = ready
    add_user(c, "mahmoud", "partner", c.ids["partners"][1])
    c.login("mahmoud")
    r = c.post("/documents/new/EXPENSE", {"date": "2026-10-02", "category": "food", "amount": "10",
                                          "pay_source": "account", "money_account_id": c.ids["cash"]},
               page="/documents/new/EXPENSE")
    assert r.status_code == 200
    assert c.get("/documents/new/CONTRIBUTION").status_code == 403
    s, _ = ledger(c)
    doc = s.scalar(select(Document).where(Document.doc_type == "EXPENSE"))
    assert c.post(f"/documents/{doc.id}/void", {"reason": "خطأ خطأ"}, page=f"/documents/{doc.id}").status_code == 403
    assert c.post("/months/2026-10/lock", {}, page="/months").status_code == 403
    assert c.get("/users").status_code == 403


def test_accountant_cannot_manage_users_or_lock(ready):
    c = ready
    add_user(c, "acc", "accountant")
    c.login("acc")
    assert c.get("/users").status_code == 403
    assert c.post("/months/2026-10/lock", {}, page="/months").status_code == 403
    r = c.post("/documents/new/CONTRIBUTION", {"date": "2026-10-02", "partner_id": c.ids["partners"][1],
                                               "money_account_id": c.ids["cash"], "amount": "١٠٠"},
               page="/documents/new/CONTRIBUTION")
    assert r.status_code == 200 and "flash ok" in r.text


# ------------------------------------------------------------------ void / delete / approval over HTTP

def test_void_via_http(ready):
    c = ready
    c.post("/documents/new/EXPENSE", {"date": "2026-10-02", "category": "food", "amount": "100",
                                      "pay_source": "account", "money_account_id": c.ids["cash"]},
           page="/documents/new/EXPENSE")
    s, L = ledger(c)
    doc = s.scalar(select(Document).where(Document.doc_type == "EXPENSE"))
    r = c.post(f"/documents/{doc.id}/void", {"reason": ""}, page=f"/documents/{doc.id}")
    assert "اكتب السبب" in r.text
    r = c.post(f"/documents/{doc.id}/void", {"reason": "فاتورة مكررة"}, page=f"/documents/{doc.id}")
    assert "ملغاة" in r.text and "فاتورة مكررة" in r.text
    s, L = ledger(c)
    assert L.balance(CASH, money_account_id=c.ids["cash"]) == 1_000_050
    # posted -> delete is refused with an explanation
    r = c.post(f"/documents/{doc.id}/delete", {}, page=f"/documents/{doc.id}")
    assert "لا يمكن حذف حركة مرحَّلة" in r.text


def test_approval_flow_via_http(ready):
    c = ready
    c.post("/settings", {"threshold": "١٠٠٠", "company_name": "x"}, page="/settings")
    add_user(c, "abd", "partner", c.ids["partners"][0])
    add_user(c, "mahmoud", "partner", c.ids["partners"][1])
    c.post("/documents/new/EXPENSE", {"date": "2026-10-02", "category": "meat", "amount": "1000.01",
                                      "pay_source": "account", "money_account_id": c.ids["cash"]},
           page="/documents/new/EXPENSE")
    s, _ = ledger(c)
    doc = s.scalar(select(Document).where(Document.doc_type == "EXPENSE"))
    assert doc.status == "PENDING"
    c.login("abd")
    assert "badge" in c.get("/").text
    r = c.post(f"/documents/{doc.id}/decide", {"decision": "approve"}, page=f"/documents/{doc.id}")
    assert "بانتظار الموافقة" in r.text
    c.login("mahmoud")
    r = c.post(f"/documents/{doc.id}/decide", {"decision": "approve"}, page=f"/documents/{doc.id}")
    assert "مُرحَّلة" in r.text
    s, L = ledger(c)
    assert L.balance(CASH, money_account_id=c.ids["cash"]) == 1_000_050 - 100_001


# ------------------------------------------------------------------ uploads

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_upload_valid_and_download(ready, tmp_path):
    c = ready
    r = c.post("/documents/new/EXPENSE", {"date": "2026-10-02", "category": "food", "amount": "5",
                                          "pay_source": "account", "money_account_id": c.ids["cash"]},
               files={"attachment": ("فاتورة.png", io.BytesIO(PNG), "image/png")}, page="/documents/new/EXPENSE")
    assert r.status_code == 200
    s, _ = ledger(c)
    doc = s.scalar(select(Document).where(Document.doc_type == "EXPENSE"))
    assert doc.attachment.endswith(".png") and doc.attachment_name == "فاتورة.png"
    r = c.get(f"/documents/{doc.id}/attachment")
    assert r.status_code == 200 and r.content == PNG and r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("name,content", [("x.exe", b"MZ"), ("fake.jpg", b"not an image"),
                                          ("fake.pdf", b"<html>"), ("x.svg", b"<svg/>"),
                                          ("x.webp", b"RIFF1234XXXX")])
def test_upload_rejected(ready, name, content):
    c = ready
    up = os.path.join(c.app.state.upload_dir)
    before = set(os.listdir(up))
    r = c.post("/documents/new/EXPENSE", {"date": "2026-10-02", "category": "food", "amount": "5",
                                          "pay_source": "account", "money_account_id": c.ids["cash"]},
               files={"attachment": (name, io.BytesIO(content), "application/octet-stream")},
               page="/documents/new/EXPENSE")
    assert r.status_code == 422 and "نوع الملف غير مسموح" in r.text
    assert set(os.listdir(up)) == before


def test_upload_too_large(ready):
    c = ready
    big = b"%PDF-" + b"0" * (10 * 1024 * 1024)
    r = c.post("/documents/new/EXPENSE", {"date": "2026-10-02", "category": "food", "amount": "5",
                                          "pay_source": "account", "money_account_id": c.ids["cash"]},
               files={"attachment": ("big.pdf", io.BytesIO(big), "application/pdf")}, page="/documents/new/EXPENSE")
    assert r.status_code == 422 and "10" in r.text


def test_upload_removed_when_document_refused(ready):
    c = ready
    up = c.app.state.upload_dir
    before = set(os.listdir(up))
    r = c.post("/documents/new/EXPENSE", {"date": "2026-10-02", "category": "food", "amount": "999999",
                                          "pay_source": "account", "money_account_id": c.ids["cash"]},
               files={"attachment": ("a.png", io.BytesIO(PNG), "image/png")}, page="/documents/new/EXPENSE")
    assert r.status_code == 422
    assert set(os.listdir(up)) == before  # no orphan file left behind


# ------------------------------------------------------------------ translations

def _keys_used():
    keys = set()
    for root, _, files in os.walk(APP_DIR):
        for f in files:
            if f.endswith((".html", ".py")):
                text = open(os.path.join(root, f), encoding="utf-8").read()
                keys |= set(re.findall(r"""\bt\(\s*['"]([a-zA-Z0-9_]+)['"]\s*[,)]""", text))
                keys |= set(re.findall(r"""BusinessError\(\s*["']([a-z0-9_]+)["']""", text))
                keys |= set(re.findall(r"""NumberError\(\s*["']([a-z0-9_]+)["']""", text))
    return keys


def test_every_used_key_is_translated():
    missing = sorted(k for k in _keys_used() if k not in T)
    assert not missing, missing


def test_dynamic_keys_are_translated():
    from app.models import DOC_TYPES, EXPENSE_CATEGORIES, ROLES, STATUSES
    need = [f"doc_{d}" for d in DOC_TYPES] + [f"status_{s}" for s in STATUSES] + \
        [f"cat_{c}" for c in EXPENSE_CATEGORIES] + [f"role_{r}" for r in ROLES] + \
        [f"acc_{a}" for a in ("1100", "1200", "1500", "3100", "3200", "4100", "4900", "5100", "5900")] + \
        [f"help_{d}" for d in DOC_TYPES if d != "OPENING"] + \
        [f"journal_none_{x}" for x in ("memo", "pending", "rejected", "void", "posted")] + \
        [f"ent_{e}" for e in ("document", "opening", "partner", "share_version", "project", "money_account",
                              "customer", "user", "setting", "month", "database")] + \
        [f"act_{a}" for a in ("create", "update", "void", "delete", "approve", "reject", "post", "lock",
                              "unlock", "signoff", "signoffs_cleared", "login", "login_failed", "activate",
                              "deactivate", "change_password", "backup")] + \
        ["ak_cash", "ak_bank", "ak_wallet", "pk_kitchen", "pk_parcels", "pk_other", "decision_approve",
         "decision_reject", "share_mode_ratio", "share_mode_percent"]
    from app.forms import FIELD_LABELS, FORM_FIELDS
    for doc_type, fields in FORM_FIELDS.items():
        for f, _, _ in fields:
            need.append(FIELD_LABELS.get((doc_type, f), "f_" + f))
    missing = sorted(k for k in need if k not in T)
    assert not missing, missing


def test_both_languages_present_and_placeholders_match():
    for key, (ar, en) in T.items():
        assert ar.strip() and en.strip(), key
        assert sorted(re.findall(r"\{(\w+)\}", ar)) == sorted(re.findall(r"\{(\w+)\}", en)), key


RAW_KEY = re.compile(r">\s*((?:err|msg|f|doc|status|cat|act|ent|acc|role|help|col|nav|btn|tile)_[A-Za-z0-9_]+)\s*<")


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_no_page_shows_a_raw_translation_key(ready, lang):
    c = ready
    c.get(f"/lang/{lang}")
    # create one of each document so the detail pages render too
    c.post("/documents/new/PRODUCTION", {"date": "2026-10-02", "project_id": c.ids["project"], "quantity": "5"},
           page="/documents/new/PRODUCTION")
    s, _ = ledger(c)
    urls = ["/", "/documents", "/opening", "/partners", "/master/project", "/master/money_account",
            "/master/customer", "/assets", "/months", "/reports/partners", "/reports/projects", "/reports/trial",
            "/audit", "/users", "/settings", "/account", f"/reports/cash/{c.ids['cash']}", "/reports/customer/1",
            f"/reports/partner/{c.ids['partners'][0]}"]
    urls += [f"/documents/new/{t}" for t in ("CONTRIBUTION", "CAPITAL_CALL", "CAPITAL_TRANSFER", "EXPENSE",
                                             "ASSET_PURCHASE", "ASSET_DISPOSAL", "INVOICE", "RECEIPT", "TRANSFER",
                                             "DISTRIBUTION", "PRODUCTION")]
    urls += [f"/documents/{d.id}" for d in s.scalars(select(Document))]
    for url in urls:
        r = c.get(url)
        assert r.status_code == 200, url
        assert f'lang="{lang}"' in r.text and (f'dir="rtl"' if lang == "ar" else f'dir="ltr"') in r.text
        raw = RAW_KEY.findall(r.text)
        assert not raw, (url, raw)


# ------------------------------------------------------------------ export / backup / misc

def test_odd_digit_characters_do_not_crash(ready):
    c = ready
    r = c.post("/documents/new/CONTRIBUTION", {"date": "2026-10-02", "partner_id": "²",
                                               "money_account_id": c.ids["cash"], "amount": "1"},
               page="/documents/new/CONTRIBUTION")
    assert r.status_code == 422 and "اختيار غير صالح" in r.text
    assert c.get("/documents?project=²").status_code == 200
    assert c.get("/master/project?edit=²").status_code == 200
    r = c.post("/partners/save", {"id": "²", "name": "x"}, page="/partners")
    assert r.status_code == 200 and "اختيار غير صالح" in r.text


def test_failed_edit_keeps_the_old_attachment(ready):
    c = ready
    c.post("/documents/new/EXPENSE", {"date": "2026-10-02", "category": "food", "amount": "5",
                                      "pay_source": "account", "money_account_id": c.ids["cash"]},
           files={"attachment": ("old.png", io.BytesIO(PNG), "image/png")}, page="/documents/new/EXPENSE")
    s, _ = ledger(c)
    doc = s.scalar(select(Document).where(Document.doc_type == "EXPENSE"))
    old_file = os.path.join(c.app.state.upload_dir, doc.attachment)
    assert os.path.exists(old_file)
    files_before = set(os.listdir(c.app.state.upload_dir))
    r = c.post(f"/documents/{doc.id}/edit", {"date": "2026-10-02", "category": "food", "amount": "999999",
                                             "pay_source": "account", "money_account_id": c.ids["cash"]},
               files={"attachment": ("new.png", io.BytesIO(PNG), "image/png")}, page=f"/documents/{doc.id}/edit")
    assert r.status_code == 422
    assert os.path.exists(old_file)  # the receipt is not lost
    assert set(os.listdir(c.app.state.upload_dir)) == files_before  # and the new upload was cleaned up
    # a successful replacement removes the old file
    r = c.post(f"/documents/{doc.id}/edit", {"date": "2026-10-02", "category": "food", "amount": "6",
                                             "pay_source": "account", "money_account_id": c.ids["cash"]},
               files={"attachment": ("new.png", io.BytesIO(PNG), "image/png")}, page=f"/documents/{doc.id}/edit")
    assert r.status_code == 200 and not os.path.exists(old_file)


def test_csv_export(ready):
    c = ready
    c.post("/documents/new/EXPENSE", {"date": "2026-10-02", "category": "food", "amount": "5",
                                      "pay_source": "account", "money_account_id": c.ids["cash"],
                                      "description": "=HYPERLINK(\"http://evil\")"}, page="/documents/new/EXPENSE")
    r = c.get("/export/documents.csv")
    body = r.content.decode("utf-8")
    assert body.startswith("﻿")  # Excel opens Arabic correctly
    assert "'=HYPERLINK" in body  # formula injection neutralised
    assert "5.00" in body and "مصروف" in body


def test_backup_is_a_sqlite_file(ready):
    r = ready.get("/backup")
    assert r.status_code == 200 and r.content.startswith(b"SQLite format 3")


def test_unknown_pages(ready):
    c = ready
    assert c.get("/documents/99999").status_code == 404
    assert c.get("/master/nothing").status_code == 404
    assert c.get("/documents/new/BRIBE").status_code == 404
    assert c.get("/no-such-page").status_code == 404


def test_opening_edit_link_redirects(ready):
    s, L = ledger(ready)
    op = L.opening_doc()
    r = ready.get(f"/documents/{op.id}/edit", follow_redirects=False)
    assert r.headers["location"] == "/opening"


def test_security_headers_and_cookie(app, tmp_path):
    c = Client(app)
    r = c.get("/setup")
    cookie = r.headers.get("set-cookie", "")
    assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()


def test_monthly_contracts_via_form(ready):
    c = ready
    ids = c.ids
    # land rent 2,000 a month for 12 months, paid personally by the contractor, typed with Arabic digits
    r = c.post("/contracts/save", {"kind": "expense", "name": "إيجار الأرض", "amount": "٢٬٠٠٠",
                                   "start_date": "2026-10-01", "months": "١٢", "project_id": str(ids["project"]),
                                   "category": "rent", "supplier": "المؤجر", "pay_source": "partner",
                                   "paid_by_partner_id": str(ids["partners"][0])}, page="/contracts")
    assert "flash ok" in r.text and "إيجار الأرض" in r.text
    # building rented to the customer for 3,000 a month
    r = c.post("/contracts/save", {"kind": "income", "name": "تأجير البركس", "amount": "3000",
                                   "start_date": "2026-10-01", "months": "12", "project_id": str(ids["project"]),
                                   "customer_id": str(ids["customer"])}, page="/contracts")
    assert "flash ok" in r.text
    s, L = ledger(c)
    docs = list(s.scalars(select(Document).where(Document.doc_type.in_(("EXPENSE", "INVOICE")))))
    assert sorted((d.doc_type, d.amount) for d in docs) == [("EXPENSE", 200_000), ("INVOICE", 300_000)]
    s.close()
    # opening the dashboard again does not duplicate anything
    c.get("/")
    c.get("/contracts")
    s, L = ledger(c)
    assert s.scalar(select(func.count(Document.id)).where(Document.doc_type.in_(("EXPENSE", "INVOICE")))) == 2
    s.close()
    r = c.get("/contracts")
    assert "1 / 12" in r.text
    # an income contract without a customer is refused with a clear message
    r = c.post("/contracts/save", {"kind": "income", "name": "خطأ", "amount": "1", "start_date": "2026-10-01",
                                   "months": "1", "project_id": str(ids["project"])}, page="/contracts")
    assert "اختر الجهة" in r.text


def test_al_aseel_reports_and_printable_vouchers(ready):
    c = ready
    ids = c.ids
    # a contribution printed as a receipt voucher, amount in words
    r = c.post("/documents/new/CONTRIBUTION", {"date": "2026-10-05", "partner_id": str(ids["partners"][1]),
                                               "money_account_id": str(ids["cash"]), "amount": "1,250"},
               page="/documents/new/CONTRIBUTION")
    s = c.db()
    doc = s.scalar(select(Document).where(Document.doc_type == "CONTRIBUTION"))
    s.close()
    r = c.get(f"/documents/{doc.id}")
    assert "سند قبض" in r.text and "رقم القيد" in r.text
    r = c.get(f"/documents/{doc.id}/print")
    assert r.status_code == 200 and "سند قبض" in r.text and "فقط ألف ومئتان وخمسون شيكل لا غير" in r.text
    assert "محمود عدنان خله" in r.text and "المحاسب" in r.text
    # chart of accounts, general ledger, journal book, trial balance variants
    cash_code = f"111{ids['cash']:02d}"
    r = c.get("/reports/accounts")
    assert cash_code in r.text and "حقوق الملكية" in r.text
    r = c.get(f"/reports/ledger?code={cash_code}")
    assert "رصيد" in r.text and "11,250.50" in r.text  # 10,000.50 opening + 1,250
    r = c.get("/reports/ledger?code=11100&from=2026-10-02")
    assert "رصيد سابق" in r.text
    r = c.get("/reports/journal?from=2026-10-01&to=2026-10-31")
    assert "رقم القيد" in r.text and "11,250.50" in r.text  # period total
    for q in ("mode=balances", "mode=totals", "mode=both&level=main"):
        r = c.get("/reports/trial?" + q)
        assert r.status_code == 200 and "✓" in r.text, q
    r = c.get("/reports/ledger?code=99999")  # unknown code: just the chooser
    assert r.status_code == 200


def test_stock_via_forms(ready):
    c = ready
    ids = c.ids
    c.post("/master/supplier/save", {"name": "شريف السوافيري"}, page="/master/supplier")
    c.post("/master/item/save", {"name": "لحم عجل", "unit": "كغ", "category": "meat"}, page="/master/item")
    c.post("/master/item/save", {"name": "رز", "unit": "كغ", "category": "food"}, page="/master/item")
    s = c.db()
    from app.models import Item, Supplier
    sup = s.scalar(select(Supplier.id))
    beef, rice = [i.id for i in s.scalars(select(Item).order_by(Item.id))]
    s.close()
    # purchase typed with Arabic digits: 50 kg beef at 35, 100 kg rice at 9 = 2,650; paid 1,000 now
    r = c.post("/documents/new/PURCHASE", {"date": "2026-10-03", "supplier_id": str(sup),
                                           "item_id_0": str(beef), "qty_0": "٥٠", "price_0": "٣٥",
                                           "item_id_1": str(rice), "qty_1": "100", "price_1": "9",
                                           "paid": "1,000", "pay_source": "account",
                                           "money_account_id": str(ids["cash"])}, page="/documents/new/PURCHASE")
    assert "flash ok" in r.text, r.text[:3000]
    assert "1,650.00" in r.text  # remaining debt
    # recipe: one meal = 0.150 kg beef + 0.400 kg rice
    r = c.post(f"/inventory/recipe/{ids['project']}", {f"r_{beef}": "0.150", f"r_{rice}": "٠٫٤٠٠"}, page="/inventory")
    assert "flash ok" in r.text
    r = c.get("/inventory")
    assert "250" in r.text  # 100 kg rice / 0.4 = 250 meals (beef allows 333)
    r = c.post("/documents/new/PRODUCTION", {"date": "2026-10-04", "project_id": str(ids["project"]),
                                             "quantity": "100"}, page="/documents/new/PRODUCTION")
    assert "flash ok" in r.text and "إذن صرف مواد" in r.text
    r = c.get("/inventory")
    assert "8.85" in r.text  # cost per meal: 0.4 x 9 + 0.15 x 35
    # issuing more than in stock is refused with the available quantity
    r = c.post("/documents/new/STOCK_ISSUE", {"date": "2026-10-04", "project_id": str(ids["project"]),
                                              "item_id_0": str(rice), "qty_0": "61"}, page="/documents/new/STOCK_ISSUE")
    assert "المتوفر 60" in r.text
    r = c.post("/documents/new/SUPPLIER_PAYMENT", {"date": "2026-10-05", "supplier_id": str(sup), "amount": "1650",
                                                   "pay_source": "account", "money_account_id": str(ids["cash"])},
               page="/documents/new/SUPPLIER_PAYMENT")
    assert "flash ok" in r.text
    r = c.get("/reports/trial")
    assert "✓" in r.text


def test_expense_on_credit_via_form(ready):
    c = ready
    c.post("/master/supplier/save", {"name": "محمد علي"}, page="/master/supplier")
    s = c.db()
    from app.models import Supplier
    sup = s.scalar(select(Supplier.id))
    s.close()
    r = c.get("/documents/new/EXPENSE")
    assert 'value="credit"' in r.text
    r = c.post("/documents/new/EXPENSE", {"date": "2026-10-03", "category": "wages", "amount": "٣٠٠٠",
                                          "pay_source": "credit", "supplier_id": str(sup)}, page="/documents/new/EXPENSE")
    assert "flash ok" in r.text and "سند قيد" in r.text
    r = c.post("/documents/new/EXPENSE", {"date": "2026-10-03", "category": "wages", "amount": "1",
                                          "pay_source": "credit"}, page="/documents/new/EXPENSE")
    assert "اختر المورد" in r.text
    r = c.get("/inventory")
    assert "3,000.00" in r.text  # owed to the cook
