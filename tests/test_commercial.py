"""Quotations, printable invoices and the statement of amount due (financial claim)."""

import datetime as dt

from sqlalchemy import select

from app.models import Document, Quote
from app.services import AR, REV, Ledger
from conftest import OPEN
from test_contracts import code
from test_http import TODAY, Client, app, client, ready, csrf_of  # noqa: F401


# ------------------------------------------------------------------ services

def test_quote_does_not_touch_the_books_until_accepted(opened):
    w = opened
    L = w.L
    q = L.save_quote(None, date=OPEN, valid_until=OPEN + dt.timedelta(days=14), customer_id=w.org.id,
                     project_id=w.kitchen.id, notes="تسليم يومي",
                     lines=[("وجبة رز ولحم", 3_000_000, 1_700), ("توصيل", 1_000, 50_000)])
    w.s.flush()
    assert q.number == 1 and q.total == 3000 * 1_700 + 50_000
    assert L.balance(AR, customer_id=w.org.id) == 0
    docs = L.accept_quote(q, OPEN)
    assert q.status == "ACCEPTED" and len(docs) == 2 and q.invoice_ids == [d.id for d in docs]
    assert {d.ref_no for d in docs} == {"QT-1"}
    assert L.balance(AR, customer_id=w.org.id) == q.total
    assert code(L.accept_quote, q, OPEN) == "err_quote_not_open"
    assert code(L.delete_quote, q) == "err_quote_accepted"
    assert code(L.save_quote, q, date=OPEN, valid_until=None, customer_id=w.org.id, project_id=None,
                notes="", lines=[("x", 1000, 100)]) == "err_quote_not_open"
    L.check_invariants()


def test_quote_validation_and_cancel(opened):
    w = opened
    L = w.L
    base = dict(date=OPEN, valid_until=None, customer_id=w.org.id, project_id=None, notes="")
    assert code(L.save_quote, None, **{**base, "lines": []}) == "err_quote_no_lines"
    assert code(L.save_quote, None, **{**base, "lines": [("", 1000, 100)]}) == "err_quote_line_desc"
    assert code(L.save_quote, None, **{**base, "lines": [("x", 0, 100)]}) == "err_quantity_positive"
    assert code(L.save_quote, None, **{**base, "customer_id": None, "lines": [("x", 1000, 100)]}) \
        == "err_customer_required"
    assert code(L.save_quote, None, **{**base, "valid_until": OPEN - dt.timedelta(days=1),
                                       "lines": [("x", 1000, 100)]}) == "err_quote_valid_until"
    q = L.save_quote(None, **{**base, "lines": [("x", 1000, 100)]})
    assert code(L.accept_quote, q, OPEN) == "err_project_required"  # revenue needs a project
    q = L.save_quote(q, **{**base, "lines": [("y", 2000, 100), ("z", 1000, 300)]})
    assert [ln.description for ln in q.lines] == ["y", "z"] and q.total == 500
    L.cancel_quote(q)
    assert q.status == "CANCELLED"
    L.delete_quote(q)
    w.s.flush()
    assert w.s.scalar(select(Quote)) is None


def test_claim_settles_oldest_invoices_first(opened):
    w = opened
    L = w.L
    d = dt.date(2026, 10, 5)
    i1 = L.create_document("INVOICE", d, {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                          "quantity": 1000, "unit_price": 300_000})
    i2 = L.create_document("INVOICE", d + dt.timedelta(days=5), {"customer_id": w.org.id, "project_id": w.kitchen.id,
                                                                 "quantity": 1000, "unit_price": 200_000})
    L.create_document("RECEIPT", d + dt.timedelta(days=10), {"customer_id": w.org.id, "money_account_id": w.bank.id,
                                                             "amount": 350_000})
    c = L.customer_claim(w.org.id, dt.date(2026, 10, 31))
    assert (c["invoiced"], c["paid"], c["balance"]) == (500_000, 350_000, 150_000)
    by = {r["doc"].id: r for r in c["dues"]}
    assert (by[i1.id]["paid"], by[i1.id]["remaining"]) == (300_000, 0)
    assert (by[i2.id]["paid"], by[i2.id]["remaining"]) == (50_000, 150_000)
    assert [r["doc"].id for r in c["open"]] == [i2.id]
    # as of a date before the payment
    c = L.customer_claim(w.org.id, d + dt.timedelta(days=6))
    assert c["balance"] == 500_000 and c["paid"] == 0
    # a voided invoice is not claimed
    i3 = L.create_document("INVOICE", d + dt.timedelta(days=12), {"customer_id": w.org.id,
                                                                  "project_id": w.kitchen.id,
                                                                  "quantity": 1000, "unit_price": 100_000})
    assert L.customer_claim(w.org.id, dt.date(2026, 10, 31))["balance"] == 250_000
    L.void_document(i3, "خطأ في الكمية")
    c = L.customer_claim(w.org.id, dt.date(2026, 10, 31))
    assert c["invoiced"] == 500_000 and c["balance"] == 150_000


# ------------------------------------------------------------------ web

def test_quote_to_invoice_to_claim_through_the_web(ready):
    c = ready
    pid, cid = c.ids["project"], c.ids["customer"]
    r = c.post("/settings", {"company_name": "شراكة غزة", "threshold": "", "company_legal_name": "ربوع الريان",
                             "company_phone": "0599000000", "bank_details": "بنك فلسطين — حساب شركة جهاد اسليم"},
               page="/settings")
    assert "flash ok" in r.text
    r = c.post("/quotes/save", {"customer_id": str(cid), "project_id": str(pid), "date": "2026-10-20",
                                "valid_until": "2026-11-03", "notes": "التسليم 12 ظهراً",
                                "desc_1": "وجبة رز ولحم", "qty_1": "٣٠٠٠", "price_1": "١٧",
                                "desc_2": "", "qty_2": "", "price_2": ""}, page="/quotes/new")
    assert r.status_code == 200 and 'data-testid="commercial-quote"' in r.text
    assert "QT-1" in r.text and "ربوع الريان" in r.text and "51,000.00" in r.text
    assert "التسليم 12 ظهراً" in r.text
    s = c.db()
    qid = s.scalar(select(Quote.id))
    s.close()
    assert "QT-1" in c.get("/quotes").text
    # a bad line keeps what was typed
    r = c.post("/quotes/save", {"id": str(qid), "customer_id": str(cid), "project_id": str(pid), "date": "2026-10-20",
                                "desc_1": "وجبة", "qty_1": "abc", "price_1": "17"}, page=f"/quotes/{qid}/edit")
    assert 'value="abc"' in r.text and "flash error" in r.text
    # accept -> invoice
    r = c.post(f"/quotes/{qid}/accept", {"date": "2026-10-25"}, page=f"/quotes/{qid}")
    assert 'data-testid="commercial-invoice"' in r.text and "INV-1" in r.text
    assert "بنك فلسطين — حساب شركة جهاد اسليم" in r.text and "51,000.00" in r.text
    s = c.db()
    inv = s.scalar(select(Document).where(Document.doc_type == "INVOICE"))
    assert inv.ref_no == "QT-1" and inv.amount == 5_100_000 and inv.status == "POSTED"
    s.close()
    assert "btn" in c.get(f"/documents/{inv.id}").text and f"/documents/{inv.id}/invoice" in c.get(f"/documents/{inv.id}").text
    # partial payment shows on invoice and claim
    c.post("/documents/new/RECEIPT", {"date": "2026-10-28", "customer_id": str(cid),
                                      "money_account_id": str(c.ids["bank"]), "amount": "20000"},
           page="/documents/new/RECEIPT")
    r = c.get(f"/documents/{inv.id}/invoice")
    assert 'data-testid="cm-remaining">31,000.00' in r.text
    r = c.get(f"/reports/customer/{cid}/claim?date=2026-10-31")
    assert 'data-testid="commercial-claim"' in r.text and '31,000.00 ₪' in r.text
    assert "بنك فلسطين" in r.text
    r = c.get(f"/reports/customer/{cid}/claim?date=2026-10-26")
    assert '51,000.00 ₪' in r.text
    # the internal voucher still prints
    assert c.get(f"/documents/{inv.id}/print").status_code == 200


def test_invoice_print_only_for_invoices_and_logo_upload(ready):
    c = ready
    r = c.get("/documents/1/invoice")  # the opening document
    assert r.status_code == 404
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    r = c.post("/settings", {"company_name": "شراكة غزة", "threshold": ""},
               files={"logo": ("logo.png", png, "image/png")}, page="/settings")
    assert "flash ok" in r.text
    r = c.get("/settings/logo")
    assert r.status_code == 200 and r.content == png
    pdf = b"%PDF-1.4 x"
    r = c.post("/settings", {"company_name": "شراكة غزة", "threshold": ""},
               files={"logo": ("logo.pdf", pdf, "application/pdf")}, page="/settings")
    assert "flash error" in r.text


def test_quote_permissions(ready):
    from test_http import add_user
    c = ready
    add_user(c, "viewer1", "viewer")
    c.login("viewer1")
    assert c.get("/quotes").status_code == 200
    assert c.get("/quotes/new").status_code == 403
