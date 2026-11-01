"""Web application (FastAPI + server-rendered Jinja2 templates)."""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import secrets
import time
import uuid
from collections import defaultdict
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from starlette.middleware.sessions import SessionMiddleware

from . import numbers as N
from . import chart as COA
from .db import init_db, make_engine, make_sessionmaker
from .forms import ATTACHMENT_TYPES, FORM_FIELDS, field_label, parse_date, parse_document_form
from .i18n import tr
from .models import (
    DOC_TYPES,
    EXPENSE_CATEGORIES,
    ROLES,
    Approval,
    Asset,
    AuditLog,
    Contract,
    Item,
    RecipeLine,
    Supplier,
    Customer,
    Document,
    JournalLine,
    MoneyAccount,
    MonthLock,
    MonthSignoff,
    Partner,
    Project,
    ShareVersion,
    User,
)
from .services import CASH, BusinessError, Ledger, check_password, share_display_ppm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_UPLOAD = 10 * 1024 * 1024
UPLOAD_EXT = {".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".png": b"\x89PNG\r\n\x1a\n",
              ".pdf": b"%PDF-", ".webp": b"RIFF"}


class Redirect(Exception):
    def __init__(self, url):
        self.url = url


def _load_secret(data_dir: str) -> str:
    env = os.environ.get("PA_SECRET_KEY")
    if env:
        return env
    path = os.path.join(data_dir, "secret.key")
    if os.path.exists(path):
        return open(path).read().strip()
    os.makedirs(data_dir, exist_ok=True)
    key = secrets.token_urlsafe(48)
    with open(path, "w") as f:
        f.write(key)
    os.chmod(path, 0o600)
    return key


def create_app(database_url: str | None = None, data_dir: str | None = None, today_fn=None,
               secret_key: str | None = None, secure_cookies: bool | None = None) -> FastAPI:
    data_dir = data_dir or os.environ.get("PA_DATA_DIR") or os.path.join(os.getcwd(), "data")
    os.makedirs(data_dir, exist_ok=True)
    upload_dir = os.path.join(data_dir, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    database_url = database_url or os.environ.get("PA_DATABASE_URL") or f"sqlite:///{data_dir}/partnership.db"
    engine = make_engine(database_url)
    init_db(engine)
    Session = make_sessionmaker(engine)
    today_fn = today_fn or dt.date.today
    if secure_cookies is None:
        secure_cookies = os.environ.get("PA_SECURE_COOKIES", "0") == "1"

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret_key or _load_secret(data_dir),
                       session_cookie="pa_session", max_age=60 * 60 * 12, same_site="lax",
                       https_only=secure_cookies)
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
    templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
    app.state.Session = Session
    app.state.engine = engine
    app.state.upload_dir = upload_dir
    app.state.data_dir = data_dir
    failed_logins: dict[str, list[float]] = defaultdict(list)

    # ------------------------------------------------------------ plumbing

    class Ctx:
        """Per-request context: db session, user, ledger, language."""

        def __init__(self, request: Request):
            self.request = request
            self.s = Session()
            uid = request.session.get("uid")
            self.user = self.s.get(User, uid) if uid else None
            if self.user is not None and not self.user.active:
                self.user = None
                request.session.clear()
            self.lang = (self.user.lang if self.user else request.session.get("lang")) or "ar"
            self.digits = self.user.digits if self.user else "latn"
            self.L = Ledger(self.s, self.user, today=today_fn(), upload_dir=upload_dir)

        def t(self, key, **kw):
            return tr(self.lang, key, **kw)

        def close(self):
            self.s.close()

    def csrf_token(request: Request) -> str:
        tok = request.session.get("csrf")
        if not tok:
            tok = secrets.token_urlsafe(32)
            request.session["csrf"] = tok
        return tok

    async def check_csrf(request: Request, form) -> bool:
        sent = form.get("csrf") or request.headers.get("x-csrf-token")
        return bool(sent) and secrets.compare_digest(str(sent), request.session.get("csrf", ""))

    def render(ctx: Ctx, template: str, status_code=200, **kw):
        req = ctx.request
        flashes = req.session.pop("flash", [])
        lang = ctx.lang
        digits = ctx.digits

        def money(v):
            return N.format_money(v or 0, digits)

        def qty(v):
            return N.format_quantity(v or 0, digits)

        def pct(v):
            return N.format_percent(v or 0, digits)

        def num(v):
            s = f"{int(v):,}"
            return s.translate(str.maketrans("0123456789,", "٠١٢٣٤٥٦٧٨٩٬")) if digits == "arab" else s

        def date(v):
            if not v:
                return ""
            s = v.isoformat() if hasattr(v, "isoformat") else str(v)
            return s.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")) if digits == "arab" else s

        hidden = {"updated_at", "created_at", "voided_at", "id", "number", "doc_type", "attachment", "asset_id",
                  "created_by"}

        def field_name(k):
            key = {"partner_id": "f_partner_id", "partner2_id": "f_receiver_partner",
                   "money_account2_id": "f_to_account", "status": "col_status", "void_reason": "f_void_reason",
                   "voided_by": "msg_voided_by", "attachment_name": "f_attachment", "date": "f_date",
                   "opening_lines": "nav_opening", "call_shares": "call_shares"}.get(k, "f_" + k)
            text = ctx.t(key)
            return text if text != key else k

        def field_value(k, v, names):
            if v is None or v == "":
                return "—"
            if k in ("amount", "fee", "unit_price"):
                return money(v)
            if k == "quantity":
                return qty(v)
            if k == "status":
                return ctx.t("status_" + v)
            if k == "category":
                return ctx.t("cat_" + v)
            if k in ("voided_by", "created_by"):
                return names["user"].get(v, v)
            if k in ("partner_id", "partner2_id", "paid_by_partner_id"):
                return names["partner"].get(v, v)
            if k in ("money_account_id", "money_account2_id"):
                return names["money_account"].get(v, v)
            if k == "project_id":
                return names["project"].get(v, v)
            if k == "customer_id":
                return names["customer"].get(v, v)
            if isinstance(v, (list, dict)):
                return "…"
            return v

        ctx_vars = dict(request=req, t=ctx.t, lang=lang, field_name=field_name, field_value=field_value,
                        hidden_fields=hidden, dir="rtl" if lang == "ar" else "ltr", user=ctx.user,
                        csrf=csrf_token(req), money=money, qty=qty, pct=pct, num=num, fdate=date,
                        flashes=flashes, today=today_fn(), N=N, field_label=field_label,
                        company=ctx.L.get_setting("company_name", ""),
                        pending_count=_pending_count(ctx), vk=COA.voucher_kind, code_of=COA.code_of)
        ctx_vars.update(kw)
        return templates.TemplateResponse(req, template, ctx_vars, status_code=status_code)

    def _pending_count(ctx: Ctx) -> int:
        if not ctx.user:
            return 0
        if ctx.user not in ctx.L.approvers():
            return 0
        decided = select(Approval.document_id).where(Approval.user_id == ctx.user.id)
        return ctx.s.scalar(select(func.count(Document.id)).where(Document.status == "PENDING",
                                                                  Document.id.not_in(decided)))

    def flash(request: Request, kind: str, message: str):
        request.session.setdefault("flash", [])
        request.session["flash"] = request.session["flash"] + [[kind, message]]

    def err_text(ctx: Ctx, e) -> str:
        params = dict(getattr(e, "params", {}) or {})
        for k, v in list(params.items()):
            if k in ("debit", "credit", "diff", "called", "paid", "excess", "balance", "owed"):
                params[k] = N.format_money(v, ctx.digits)
            if k == "total":
                params[k] = N.format_percent(v, ctx.digits)
        return ctx.t(e.code, **params)

    def require(ctx: Ctx, *roles):
        if ctx.user is None:
            raise Redirect("/login?" + urlencode({"next": str(ctx.request.url.path)}))
        if roles and ctx.user.role not in roles:
            raise BusinessError("err_forbidden")

    def page(fn):
        """Wrap a handler: context, auth redirects, commit/rollback, business errors."""
        import functools

        @functools.wraps(fn)
        async def wrapper(request: Request, **kw):
            ctx = Ctx(request)
            try:
                if request.method == "POST":
                    form = await request.form()
                    if not await check_csrf(request, form):
                        return HTMLResponse(ctx.t("err_csrf"), status_code=403)
                    kw["form"] = form
                resp = await fn(ctx, **kw)
                ctx.s.commit()
                ctx.L.remove_scheduled_files()
                return resp
            except Redirect as r:
                ctx.s.rollback()
                ctx.L.discard_scheduled_files()
                return RedirectResponse(r.url, status_code=303)
            except N.NumberError as ne:
                ctx.s.rollback()
                ctx.L.discard_scheduled_files()
                flash(request, "error", err_text(ctx, ne))
                return RedirectResponse(request.headers.get("referer") or "/", status_code=303)
            except BusinessError as e:
                ctx.s.rollback()
                ctx.L.discard_scheduled_files()
                if e.code == "err_forbidden":
                    return render(ctx, "message.html", status_code=403, title=ctx.t("err_forbidden"),
                                  message=ctx.t("err_forbidden"))
                if e.code == "err_not_found":
                    return render(ctx, "message.html", status_code=404, title=ctx.t("err_not_found"),
                                  message=ctx.t("err_not_found"))
                flash(request, "error", err_text(ctx, e))
                back = request.headers.get("referer") or "/"
                return RedirectResponse(back, status_code=303)
            finally:
                ctx.close()

        import inspect
        sig = inspect.signature(fn)
        params = [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request)]
        params += [p for name, p in sig.parameters.items() if name not in ("ctx", "form")]
        wrapper.__signature__ = inspect.Signature(params)
        del wrapper.__wrapped__
        return wrapper

    def safe_id(v):
        """Query-string id: invalid or empty -> None (filters are simply ignored)."""
        try:
            return N.parse_id(v)
        except N.NumberError:
            return None

    def get_or_404(ctx, model, obj_id):
        obj = ctx.s.get(model, obj_id)
        if obj is None:
            raise BusinessError("err_not_found")
        return obj

    def opt_date(v):
        if not v:
            return None
        try:
            return parse_date(v)
        except N.NumberError:
            return None

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        ctx = Ctx(request)
        try:
            return render(ctx, "message.html", status_code=404, title=ctx.t("err_not_found"),
                          message=ctx.t("err_not_found"))
        finally:
            ctx.close()

    # ------------------------------------------------------------ auth

    @app.get("/setup", response_class=HTMLResponse)
    @page
    async def setup_page(ctx: Ctx):
        if ctx.s.scalar(select(func.count(User.id))):
            raise Redirect("/login")
        return render(ctx, "setup.html", errors={}, values={})

    @app.post("/setup")
    @page
    async def setup_post(ctx: Ctx, form):
        if ctx.s.scalar(select(func.count(User.id))):
            raise Redirect("/login")
        ctx.L.user = None  # bootstrap: acts as system
        ctx.L.system = True
        try:
            u = ctx.L.save_user(None, username=form.get("username", ""), full_name=form.get("full_name", ""),
                                role="admin", partner_id=None, password=form.get("password", ""),
                                lang=ctx.lang if ctx.lang in ("ar", "en") else "ar")
            if form.get("password") != form.get("password2"):
                raise BusinessError("err_password_mismatch")
        except BusinessError as e:
            # show the error on the same page and keep what was typed (except passwords)
            ctx.s.rollback()
            values = {k: form.get(k, "") for k in ("company_name", "full_name", "username")}
            return render(ctx, "setup.html", status_code=422, errors={"_": err_text(ctx, e)}, values=values)
        company = (form.get("company_name") or "").strip()
        if company:
            ctx.L.set_setting("company_name", company)
        ctx.s.flush()
        ctx.request.session.clear()
        ctx.request.session["uid"] = u.id
        raise_commit_redirect(ctx, "/")

    def raise_commit_redirect(ctx, url):
        ctx.s.commit()
        ctx.L.remove_scheduled_files()
        raise Redirect(url)

    @app.get("/login", response_class=HTMLResponse)
    @page
    async def login_page(ctx: Ctx):
        if not ctx.s.scalar(select(func.count(User.id))):
            raise Redirect("/setup")
        return render(ctx, "login.html", error=None, next=ctx.request.query_params.get("next", "/"))

    @app.post("/login")
    @page
    async def login_post(ctx: Ctx, form):
        username = (form.get("username") or "").strip().lower()
        nxt = form.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        now = time.time()
        if len(failed_logins) > 10_000:
            failed_logins.clear()
        key = username
        attempts = [x for x in failed_logins[key] if now - x < 300]
        failed_logins[key] = attempts
        if len(attempts) >= 5:
            return render(ctx, "login.html", status_code=429, error=ctx.t("err_too_many_attempts"), next=nxt)
        u = ctx.s.scalar(select(User).where(User.username == username))
        if u is None or not u.active or not check_password(form.get("password") or "", u.password_hash):
            failed_logins[key].append(now)
            ctx.L.audit("login_failed", "user", u.id if u else None, note=username)
            ctx.s.commit()
            return render(ctx, "login.html", status_code=401, error=ctx.t("err_login"), next=nxt)
        failed_logins.pop(key, None)
        ctx.request.session.clear()
        ctx.request.session["uid"] = u.id
        ctx.L.user = u
        ctx.L.audit("login", "user", u.id)
        raise_commit_redirect(ctx, nxt)

    @app.post("/logout")
    @page
    async def logout(ctx: Ctx, form):
        lang = ctx.lang
        ctx.request.session.clear()
        ctx.request.session["lang"] = lang
        raise Redirect("/login")

    @app.get("/lang/{lang}")
    @page
    async def set_lang(ctx: Ctx, lang: str):
        if lang not in ("ar", "en"):
            raise BusinessError("err_invalid_choice")
        ctx.request.session["lang"] = lang
        if ctx.user:
            ctx.user.lang = lang
        back = ctx.request.headers.get("referer") or "/"
        raise_commit_redirect(ctx, back)

    @app.get("/account", response_class=HTMLResponse)
    @page
    async def account_page(ctx: Ctx):
        require(ctx)
        return render(ctx, "account.html")

    @app.post("/account/prefs")
    @page
    async def account_prefs(ctx: Ctx, form):
        require(ctx)
        ctx.L.set_own_preferences(form.get("lang", "ar"), form.get("digits", "latn"))
        flash(ctx.request, "ok", tr(form.get("lang", "ar"), "msg_saved"))
        raise_commit_redirect(ctx, "/account")

    @app.post("/account/password")
    @page
    async def account_password(ctx: Ctx, form):
        require(ctx)
        if form.get("new") != form.get("new2"):
            raise BusinessError("err_password_mismatch")
        ctx.L.change_own_password(form.get("current", ""), form.get("new", ""))
        flash(ctx.request, "ok", ctx.t("msg_saved"))
        raise_commit_redirect(ctx, "/account")

    # ------------------------------------------------------------ dashboard

    @app.get("/", response_class=HTMLResponse)
    @page
    async def dashboard(ctx: Ctx):
        if not ctx.s.scalar(select(func.count(User.id))):
            raise Redirect("/setup")
        require(ctx)
        run_due_contracts(ctx)
        L = ctx.L
        accounts = list(ctx.s.scalars(select(MoneyAccount).order_by(MoneyAccount.id)))
        balances = [(a, L.balance(CASH, money_account_id=a.id)) for a in accounts]
        customers = [(c, L.balance("1200", customer_id=c.id)) for c in ctx.s.scalars(select(Customer))]
        tb = L.trial_balance()
        has_opening = L.opening_doc() is not None
        partners = L.partner_summary() if L.share_versions() else []
        projects = L.project_report(dt.date(today_fn().year, today_fn().month, 1), None) if has_opening else []
        recent = list(ctx.s.scalars(select(Document).order_by(Document.id.desc()).limit(10)))
        dist = L.distributable(today_fn()) if has_opening else 0
        return render(ctx, "dashboard.html", balances=balances, customers=customers, tb=tb,
                      has_opening=has_opening, partners=partners, projects=projects, recent=recent,
                      distributable=dist, setup_steps=_setup_steps(ctx))

    def _setup_steps(ctx):
        s = ctx.s
        return [
            ("step_partners", s.scalar(select(func.count(Partner.id))) > 0, "/partners"),
            ("step_shares", s.scalar(select(func.count(ShareVersion.id))) > 0, "/partners"),
            ("step_accounts", s.scalar(select(func.count(MoneyAccount.id))) > 0, "/master/money_account"),
            ("step_customers", s.scalar(select(func.count(Customer.id))) > 0, "/master/customer"),
            ("step_projects", s.scalar(select(func.count(Project.id))) > 0, "/master/project"),
            ("step_opening", ctx.L.opening_doc() is not None, "/opening"),
        ]

    # ------------------------------------------------------------ documents

    def form_options(ctx: Ctx, doc: Document | None = None):
        s = ctx.s
        keep = doc or Document()

        def active_or_current(model, current_ids):
            q = select(model).where(or_(model.active.is_(True), model.id.in_([i for i in current_ids if i])))
            return list(s.scalars(q.order_by(model.id)))

        calls = list(s.scalars(select(Document).where(Document.doc_type == "CAPITAL_CALL",
                                                      Document.status == "POSTED").order_by(Document.id.desc())))
        owned_assets = []
        for a in s.scalars(select(Asset).order_by(Asset.id)):
            src = s.get(Document, a.source_document_id) if a.source_document_id else None
            if src is None or src.status != "POSTED":
                continue
            disposed = s.scalar(select(Document.id).where(Document.doc_type == "ASSET_DISPOSAL",
                                                          Document.asset_id == a.id,
                                                          Document.status.in_(("POSTED", "PENDING")),
                                                          Document.id != (keep.id or 0)))
            if not disposed:
                owned_assets.append(a)
        partners = list(s.scalars(select(Partner).order_by(Partner.id)))
        if ctx.user and ctx.user.role == "partner":
            pay_partners = [p for p in partners if p.id == ctx.user.partner_id]
        else:
            pay_partners = partners
        return {
            "partner": partners,
            "pay_partners": pay_partners,
            "money_account": active_or_current(MoneyAccount, [keep.money_account_id, keep.money_account2_id]),
            "customer": active_or_current(Customer, [keep.customer_id]),
            "project": active_or_current(Project, [keep.project_id]),
            "asset": owned_assets,
            "call": calls,
            "category": EXPENSE_CATEGORIES,
            "supplier": active_or_current(Supplier, [keep.supplier_id]),
            "item": list(s.scalars(select(Item).where(Item.active.is_(True)).order_by(Item.id))),
        }

    def doc_values(doc: Document) -> dict:
        v = {"date": doc.date.isoformat(), "description": doc.description}
        for field, kind, _ in FORM_FIELDS[doc.doc_type]:
            if kind in ("money", "money0"):
                v[field] = N.money_input_value(getattr(doc, field))
            elif kind == "qty":
                v[field] = N.qty_input_value(getattr(doc, field))
            elif kind == "items":
                for n, it in enumerate(doc.items):
                    v[f"item_id_{n}"] = str(it.item_id)
                    v[f"qty_{n}"] = N.qty_input_value(it.quantity)
                    if it.unit_price:
                        v[f"price_{n}"] = N.money_input_value(it.unit_price)
            elif kind in ("paysource", "paysource_opt", "paysource_credit"):
                v["pay_source"] = ("credit" if doc.supplier_id and kind == "paysource_credit"
                                   else "partner" if doc.paid_by_partner_id else "account")
                v["supplier_id"] = str(doc.supplier_id or "")
                v["paid_by_partner_id"] = str(doc.paid_by_partner_id or "")
                v["money_account_id"] = str(doc.money_account_id or "")
            elif field != "asset_name":
                val = getattr(doc, field)
                v[field] = "" if val is None else str(val)
        return v

    async def save_upload(ctx: Ctx, form):
        f = form.get("attachment")
        if f is None or not hasattr(f, "filename") or not f.filename:
            return None
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in UPLOAD_EXT:
            raise BusinessError("err_upload_type")
        data = await f.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise BusinessError("err_upload_size")
        if not data.startswith(UPLOAD_EXT[ext]) or (ext == ".webp" and data[8:12] != b"WEBP"):
            raise BusinessError("err_upload_type")
        name = uuid.uuid4().hex + ext
        with open(os.path.join(upload_dir, name), "wb") as out:
            out.write(data)
        ctx.new_upload = name
        return name, os.path.basename(f.filename)[:200]

    @app.get("/documents", response_class=HTMLResponse)
    @page
    async def documents(ctx: Ctx):
        require(ctx)
        run_due_contracts(ctx)
        q = ctx.request.query_params
        stmt = select(Document)
        if q.get("type") in DOC_TYPES:
            stmt = stmt.where(Document.doc_type == q["type"])
        if q.get("status") in ("PENDING", "POSTED", "REJECTED", "VOID"):
            stmt = stmt.where(Document.status == q["status"])
        if safe_id(q.get("project")):
            stmt = stmt.where(Document.project_id == safe_id(q["project"]))
        if safe_id(q.get("partner")):
            pid = safe_id(q["partner"])
            stmt = stmt.where(or_(Document.partner_id == pid, Document.partner2_id == pid,
                                  Document.paid_by_partner_id == pid))
        d1, d2 = opt_date(q.get("from")), opt_date(q.get("to"))
        if d1:
            stmt = stmt.where(Document.date >= d1)
        if d2:
            stmt = stmt.where(Document.date <= d2)
        if q.get("q"):
            like = f"%{q['q']}%"
            stmt = stmt.where(or_(Document.description.like(like), Document.supplier.like(like)))
        if q.get("awaiting") == "me" and ctx.user:
            decided = select(Approval.document_id).where(Approval.user_id == ctx.user.id)
            stmt = stmt.where(Document.status == "PENDING", Document.id.not_in(decided))
        docs = list(ctx.s.scalars(stmt.order_by(Document.date.desc(), Document.id.desc()).limit(1000)))
        return render(ctx, "documents.html", docs=docs, q=q, doc_types=DOC_TYPES,
                      projects=list(ctx.s.scalars(select(Project))),
                      partners=list(ctx.s.scalars(select(Partner))), names=_names(ctx))

    def _names(ctx: Ctx):
        s = ctx.s
        return {
            "partner": {p.id: p.name for p in s.scalars(select(Partner))},
            "project": {p.id: p.name for p in s.scalars(select(Project))},
            "customer": {p.id: p.name for p in s.scalars(select(Customer))},
            "money_account": {p.id: p.name for p in s.scalars(select(MoneyAccount))},
            "asset": {p.id: p.name for p in s.scalars(select(Asset))},
            "supplier": {p.id: p.name for p in s.scalars(select(Supplier))},
            "item": {p.id: p for p in s.scalars(select(Item))},
            "user": {u.id: u.full_name for u in s.scalars(select(User))},
        }

    @app.get("/documents/new/{doc_type}", response_class=HTMLResponse)
    @page
    async def doc_new(ctx: Ctx, doc_type: str):
        require(ctx, "admin", "accountant", "partner")
        if doc_type not in FORM_FIELDS:
            raise BusinessError("err_not_found")
        ctx.L._can_create(doc_type)
        values = {"date": today_fn().isoformat(), "pay_source": "account"}
        if doc_type == "STOCK_COUNT":  # list every active item, ready to type what was counted
            for n, it in enumerate(ctx.s.scalars(select(Item).where(Item.active.is_(True)).order_by(Item.id))):
                values[f"item_id_{n}"] = str(it.id)
        if safe_id(ctx.request.query_params.get("project")):
            values["project_id"] = str(safe_id(ctx.request.query_params["project"]))
        return render(ctx, "doc_form.html", doc_type=doc_type, doc=None, values=values, errors={},
                      fields=FORM_FIELDS[doc_type], opts=form_options(ctx), attach=doc_type in ATTACHMENT_TYPES,
                      extra=_form_extra(ctx, doc_type))

    def _form_extra(ctx: Ctx, doc_type: str) -> dict:
        extra = {}
        if doc_type == "DISTRIBUTION" and ctx.L.opening_doc():
            extra["distributable"] = ctx.L.distributable(today_fn())
            try:
                extra["shares"] = ctx.L.distribution_weights(today_fn())
            except BusinessError:
                extra["shares"] = []
            extra["share_names"] = {p.id: p.name for p in ctx.s.scalars(select(Partner))}
        if doc_type in ("EXPENSE", "ASSET_PURCHASE", "PURCHASE"):
            extra["threshold"] = ctx.L.approval_threshold
        if doc_type == "SUPPLIER_PAYMENT":
            extra["supplier_debts"] = {sp.id: ctx.L.supplier_owed(sp.id) for sp in ctx.s.scalars(select(Supplier))}
        if doc_type in ("STOCK_ISSUE", "PRODUCTION", "STOCK_COUNT"):
            extra["stock"] = {i.id: ctx.L.stock_of(i.id)[0] for i in ctx.s.scalars(select(Item))}
        if doc_type == "PRODUCTION":
            items = {i.id: i for i in ctx.s.scalars(select(Item))}
            extra["recipes"] = {}
            for p in ctx.s.scalars(select(Project).where(Project.active.is_(True))):
                lines = ctx.L.recipe_of(p.id)
                if lines:
                    cov = ctx.L.coverage(p.id)
                    extra["recipes"][p.id] = {"lines": [(items[r.item_id], r.quantity) for r in lines],
                                              "units": cov["units"], "unit_name": p.unit_name}
        return extra

    @app.post("/documents/new/{doc_type}")
    @page
    async def doc_create(ctx: Ctx, doc_type: str, form):
        require(ctx, "admin", "accountant", "partner")
        if doc_type not in FORM_FIELDS:
            raise BusinessError("err_not_found")
        data, extras, errors = parse_document_form(doc_type, form)
        values = {k: (v if isinstance(v, str) else "") for k, v in form.items()}
        if errors:
            return render(ctx, "doc_form.html", status_code=422, doc_type=doc_type, doc=None, values=values,
                          errors={k: err_text(ctx, e) for k, e in errors.items()}, fields=FORM_FIELDS[doc_type],
                          opts=form_options(ctx), attach=doc_type in ATTACHMENT_TYPES,
                          extra=_form_extra(ctx, doc_type))
        ctx.new_upload = None
        try:
            att = await save_upload(ctx, form) if doc_type in ATTACHMENT_TYPES else None
            doc = ctx.L.create_document(doc_type, extras["date"], data, asset_name=extras.get("asset_name"),
                                        attachment=att)
            ctx.s.commit()
            ctx.L.remove_scheduled_files()
        except BusinessError as e:
            ctx.s.rollback()
            ctx.L.discard_scheduled_files()
            if ctx.new_upload:
                os.remove(os.path.join(upload_dir, ctx.new_upload))
            return render(ctx, "doc_form.html", status_code=422, doc_type=doc_type, doc=None, values=values,
                          errors={"_": err_text(ctx, e), "_confirm": e.code.startswith("warn_")},
                          fields=FORM_FIELDS[doc_type], opts=form_options(ctx),
                          attach=doc_type in ATTACHMENT_TYPES, extra=_form_extra(ctx, doc_type))
        flash(ctx.request, "ok", ctx.t("msg_doc_saved_pending" if doc.status == "PENDING" else "msg_doc_saved"))
        raise Redirect(f"/documents/{doc.id}")

    @app.get("/documents/{doc_id:int}", response_class=HTMLResponse)
    @page
    async def doc_view(ctx: Ctx, doc_id: int):
        require(ctx)
        doc = get_or_404(ctx, Document, doc_id)
        history = list(ctx.s.scalars(select(AuditLog).where(AuditLog.entity == "document",
                                                            AuditLog.entity_id == doc.id).order_by(AuditLog.id)))
        approvers = ctx.L.approvers() if doc.status in ("PENDING", "REJECTED") else []
        linked = list(ctx.s.scalars(select(Document).where(Document.capital_call_id == doc.id)))
        asset = ctx.s.get(Asset, doc.asset_id) if doc.asset_id else None
        return render(ctx, "doc_view.html", doc=doc, history=history, names=_names(ctx), approvers=approvers,
                      linked=linked, asset=asset, can_decide=(doc.status == "PENDING" and ctx.user in ctx.L.approvers()
                                                              and not any(a.user_id == ctx.user.id for a in doc.approvals)),
                      locked=ctx.L.is_locked(doc.date))

    def coa_names(ctx: Ctx) -> dict[str, str]:
        names = COA.leaf_names(ctx.s, ctx.t)
        names.update({c: ctx.t("coa_" + c) for c, _p, _n in COA.GROUPS})
        return names

    @app.get("/documents/{doc_id:int}/print", response_class=HTMLResponse)
    @page
    async def doc_print(ctx: Ctx, doc_id: int):
        require(ctx)
        doc = get_or_404(ctx, Document, doc_id)
        asset = ctx.s.get(Asset, doc.asset_id) if doc.asset_id else None
        cash_amount = doc.amount - (doc.fee or 0) if doc.doc_type == "RECEIPT" else doc.amount
        return render(ctx, "print_voucher.html", doc=doc, names=_names(ctx), coa=coa_names(ctx), asset=asset,
                      kind=COA.voucher_kind(doc), words=N.amount_in_words(doc.amount, ctx.lang),
                      cash_amount=cash_amount, now=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))

    @app.get("/documents/{doc_id:int}/edit", response_class=HTMLResponse)
    @page
    async def doc_edit(ctx: Ctx, doc_id: int):
        require(ctx, "admin", "accountant", "partner")
        doc = get_or_404(ctx, Document, doc_id)
        if doc.doc_type == "OPENING":
            raise Redirect("/opening")
        ctx.L._can_modify(doc)
        values = doc_values(doc)
        if doc.doc_type == "ASSET_PURCHASE" and doc.asset_id:
            values["asset_name"] = ctx.s.get(Asset, doc.asset_id).name
        return render(ctx, "doc_form.html", doc_type=doc.doc_type, doc=doc, values=values, errors={},
                      fields=FORM_FIELDS[doc.doc_type], opts=form_options(ctx, doc),
                      attach=doc.doc_type in ATTACHMENT_TYPES, extra=_form_extra(ctx, doc.doc_type))

    @app.post("/documents/{doc_id:int}/edit")
    @page
    async def doc_update(ctx: Ctx, doc_id: int, form):
        require(ctx, "admin", "accountant", "partner")
        doc = get_or_404(ctx, Document, doc_id)
        data, extras, errors = parse_document_form(doc.doc_type, form)
        values = {k: (v if isinstance(v, str) else "") for k, v in form.items()}
        common = dict(doc_type=doc.doc_type, doc=doc, values=values, fields=FORM_FIELDS[doc.doc_type],
                      attach=doc.doc_type in ATTACHMENT_TYPES, extra=_form_extra(ctx, doc.doc_type))
        if errors:
            return render(ctx, "doc_form.html", status_code=422, opts=form_options(ctx, doc),
                          errors={k: err_text(ctx, e) for k, e in errors.items()}, **common)
        ctx.new_upload = None
        try:
            att = await save_upload(ctx, form) if doc.doc_type in ATTACHMENT_TYPES else None
            ctx.L.update_document(doc, extras["date"], data, asset_name=extras.get("asset_name"), attachment=att,
                                  remove_attachment=form.get("remove_attachment") == "1")
            ctx.s.commit()
            ctx.L.remove_scheduled_files()
        except BusinessError as e:
            ctx.s.rollback()
            ctx.L.discard_scheduled_files()
            if ctx.new_upload:
                os.remove(os.path.join(upload_dir, ctx.new_upload))
            doc = ctx.s.get(Document, doc_id)
            common["doc"] = doc
            return render(ctx, "doc_form.html", status_code=422, opts=form_options(ctx, doc),
                          errors={"_": err_text(ctx, e), "_confirm": e.code.startswith("warn_")}, **common)
        flash(ctx.request, "ok", ctx.t("msg_doc_saved_pending" if doc.status == "PENDING" else "msg_doc_saved"))
        raise Redirect(f"/documents/{doc.id}")

    @app.post("/documents/{doc_id:int}/void")
    @page
    async def doc_void(ctx: Ctx, doc_id: int, form):
        require(ctx, "admin", "accountant")
        doc = get_or_404(ctx, Document, doc_id)
        ctx.L.void_document(doc, form.get("reason", ""))
        flash(ctx.request, "ok", ctx.t("msg_doc_voided"))
        raise_commit_redirect(ctx, f"/documents/{doc.id}")

    @app.post("/documents/{doc_id:int}/delete")
    @page
    async def doc_delete(ctx: Ctx, doc_id: int, form):
        require(ctx, "admin", "accountant", "partner")
        doc = get_or_404(ctx, Document, doc_id)
        ctx.L.delete_document(doc, form.get("reason", ""))
        flash(ctx.request, "ok", ctx.t("msg_doc_deleted"))
        raise_commit_redirect(ctx, "/documents")

    @app.post("/documents/{doc_id:int}/decide")
    @page
    async def doc_decide(ctx: Ctx, doc_id: int, form):
        require(ctx)
        doc = get_or_404(ctx, Document, doc_id)
        status = ctx.L.decide(doc, form.get("decision", ""), form.get("note", ""))
        if ctx.L.last_post_error is not None:
            flash(ctx.request, "error", ctx.t("msg_approved_not_posted") + " " + err_text(ctx, ctx.L.last_post_error))
        else:
            flash(ctx.request, "ok", ctx.t("status_" + status))
        raise_commit_redirect(ctx, f"/documents/{doc.id}")

    @app.post("/documents/{doc_id:int}/post")
    @page
    async def doc_post(ctx: Ctx, doc_id: int, form):
        require(ctx, "admin", "accountant")
        doc = get_or_404(ctx, Document, doc_id)
        ctx.L.retry_post(doc)
        flash(ctx.request, "ok", ctx.t("status_POSTED"))
        raise_commit_redirect(ctx, f"/documents/{doc.id}")

    @app.get("/documents/{doc_id:int}/attachment")
    @page
    async def doc_attachment(ctx: Ctx, doc_id: int):
        require(ctx)
        doc = get_or_404(ctx, Document, doc_id)
        if not doc.attachment:
            raise BusinessError("err_not_found")
        path = os.path.join(upload_dir, os.path.basename(doc.attachment))
        if not os.path.exists(path):
            raise BusinessError("err_not_found")
        ext = os.path.splitext(path)[1].lower()
        media = {".pdf": "application/pdf", ".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
        return FileResponse(path, media_type=media, filename=doc.attachment_name or os.path.basename(path),
                            content_disposition_type="inline",
                            headers={"X-Content-Type-Options": "nosniff"})

    # ------------------------------------------------------------ opening

    @app.get("/opening", response_class=HTMLResponse)
    @page
    async def opening_page(ctx: Ctx):
        require(ctx)
        doc = ctx.L.opening_doc()
        values = {"date": (doc.date if doc else today_fn()).isoformat()}
        assets = []
        if doc:
            for o in doc.opening_lines:
                key = {"money": "m", "customer": "c", "partner": "p"}.get(o.kind)
                if key:
                    ref = {"m": o.money_account_id, "c": o.customer_id, "p": o.partner_id}[key]
                    values[f"{key}_{ref}"] = N.money_input_value(o.amount)
                else:
                    a = ctx.s.get(Asset, o.asset_id)
                    assets.append({"id": a.id, "name": a.name, "amount": N.money_input_value(o.amount),
                                   "project_id": str(a.project_id or "")})
        return render(ctx, "opening.html", doc=doc, values=values, assets=assets, errors={}, **_opening_lists(ctx))

    def _opening_lists(ctx):
        s = ctx.s
        return dict(accounts=list(s.scalars(select(MoneyAccount).order_by(MoneyAccount.id))),
                    customers=list(s.scalars(select(Customer).order_by(Customer.id))),
                    partners=list(s.scalars(select(Partner).order_by(Partner.id))),
                    projects=list(s.scalars(select(Project).order_by(Project.id))),
                    can_edit=ctx.user.role in ("admin", "accountant"), locked=bool(ctx.L.locked_months()))

    @app.post("/opening")
    @page
    async def opening_post(ctx: Ctx, form):
        require(ctx, "admin", "accountant")
        lists = _opening_lists(ctx)
        errors = {}
        values = {k: (v if isinstance(v, str) else "") for k, v in form.items()}
        try:
            date = parse_date(form.get("date", ""))
        except N.NumberError as e:
            errors["date"] = err_text(ctx, e)
            date = None

        def amounts(prefix, items):
            out = {}
            for it in items:
                raw = (form.get(f"{prefix}_{it.id}") or "").strip()
                if raw == "":
                    continue
                try:
                    out[it.id] = N.parse_money(raw, allow_zero=True)
                except N.NumberError as e:
                    errors[f"{prefix}_{it.id}"] = err_text(ctx, e)
            return out

        money = amounts("m", lists["accounts"])
        customers = amounts("c", lists["customers"])
        partners = amounts("p", lists["partners"])
        assets, asset_rows = [], []
        n = 0
        while f"asset_name_{n}" in form or f"asset_amount_{n}" in form:
            name = (form.get(f"asset_name_{n}") or "").strip()
            raw = (form.get(f"asset_amount_{n}") or "").strip()
            aid = form.get(f"asset_id_{n}") or ""
            proj = form.get(f"asset_project_{n}") or ""
            asset_rows.append({"id": aid, "name": name, "amount": raw, "project_id": proj})
            if name or raw:
                try:
                    amt = N.parse_money(raw)
                    if not name:
                        raise N.NumberError("err_name_required")
                    assets.append({"id": N.parse_id(aid), "name": name, "amount": amt,
                                   "project_id": N.parse_id(proj)})
                except N.NumberError as e:
                    errors[f"asset_{n}"] = err_text(ctx, e)
            n += 1
        if not errors:
            try:
                ctx.L.save_opening(date, money, customers, assets, partners)
                ctx.s.commit()
                ctx.L.remove_scheduled_files()
                flash(ctx.request, "ok", ctx.t("msg_saved"))
                raise Redirect("/opening")
            except BusinessError as e:
                ctx.s.rollback()
                errors["_"] = err_text(ctx, e)
        return render(ctx, "opening.html", status_code=422, doc=ctx.L.opening_doc(), values=values,
                      assets=asset_rows, errors=errors, **_opening_lists(ctx))

    # ------------------------------------------------------------ partners & shares

    @app.get("/partners", response_class=HTMLResponse)
    @page
    async def partners_page(ctx: Ctx):
        require(ctx)
        partners = list(ctx.s.scalars(select(Partner).order_by(Partner.id)))
        versions = list(reversed(ctx.L.share_versions()))
        version_pct = {}
        for v in versions:
            version_pct[v.id] = share_display_ppm([(ln.partner_id, ln.weight) for ln in v.lines if ln.weight > 0])
        refs = {p.id: ctx.L.partner_references(p.id) for p in partners}
        return render(ctx, "partners.html", partners=partners, versions=versions, refs=refs,
                      version_pct=version_pct)

    @app.post("/partners/save")
    @page
    async def partner_save(ctx: Ctx, form):
        require(ctx, "admin")
        pid = form.get("id") or ""
        p = get_or_404(ctx, Partner, N.parse_id(pid)) if N.parse_id(pid) else None
        ctx.L.save_partner(p, form.get("name", ""), form.get("id_number", ""), form.get("phone", ""))
        flash(ctx.request, "ok", ctx.t("msg_saved"))
        raise_commit_redirect(ctx, "/partners")

    @app.post("/partners/{pid:int}/delete")
    @page
    async def partner_delete(ctx: Ctx, pid: int, form):
        require(ctx, "admin")
        ctx.L.delete_partner(get_or_404(ctx, Partner, pid))
        flash(ctx.request, "ok", ctx.t("msg_deleted"))
        raise_commit_redirect(ctx, "/partners")

    @app.post("/shares/new")
    @page
    async def shares_new(ctx: Ctx, form):
        require(ctx, "admin")
        date = parse_date(form.get("effective_date", ""))
        mode = form.get("mode", "percent")
        shares = {}
        for p in ctx.s.scalars(select(Partner)):
            raw = (form.get(f"share_{p.id}") or "").strip()
            try:
                if not raw:
                    shares[p.id] = 0
                elif mode == "ratio":
                    shares[p.id] = N.parse_int(raw, allow_zero=True)
                else:
                    shares[p.id] = N.parse_percent(raw, allow_zero=True)
            except N.NumberError as e:
                raise BusinessError(e.code, **e.params) from None
        ctx.L.create_share_version(date, shares, form.get("note", ""), mode=mode)
        flash(ctx.request, "ok", ctx.t("msg_saved"))
        raise_commit_redirect(ctx, "/partners")

    @app.post("/shares/{vid:int}/delete")
    @page
    async def shares_delete(ctx: Ctx, vid: int, form):
        require(ctx, "admin")
        ctx.L.delete_share_version(get_or_404(ctx, ShareVersion, vid))
        flash(ctx.request, "ok", ctx.t("msg_deleted"))
        raise_commit_redirect(ctx, "/partners")

    # ------------------------------------------------------------ master data

    MASTER = {"project": Project, "money_account": MoneyAccount, "customer": Customer, "supplier": Supplier,
              "item": Item}

    # ------------------------------------------------------------ monthly contracts

    def run_due_contracts(ctx):
        """Create this month's rent documents when an admin or accountant opens the app.
        Never breaks the page: a failure is rolled back and logged."""
        try:
            if ctx.L.run_contracts():
                ctx.s.commit()
        except Exception:  # noqa: BLE001
            ctx.s.rollback()
            import logging
            logging.getLogger("pa").exception("running monthly contracts failed")

    @app.get("/contracts", response_class=HTMLResponse)
    @page
    async def contracts_page(ctx: Ctx):
        require(ctx)
        run_due_contracts(ctx)
        items = list(ctx.s.scalars(select(Contract).order_by(Contract.active.desc(), Contract.id)))
        names = {
            "project": {o.id: o.name for o in ctx.s.scalars(select(Project))},
            "customer": {o.id: o.name for o in ctx.s.scalars(select(Customer))},
            "partner": {o.id: o.name for o in ctx.s.scalars(select(Partner))},
            "money_account": {o.id: o.name for o in ctx.s.scalars(select(MoneyAccount))},
        }
        rows = []
        for c in items:
            end = ctx.L.contract_period_date(c.start_date, c.months - 1)
            nxt = None
            if c.active and len(c.runs) < c.months:
                nxt = ctx.L.contract_period_date(c.start_date, len(c.runs))
            err = None
            if c.last_error:
                params = dict(c.last_error.get("params") or {})
                err = ctx.t(c.last_error.get("code", ""), **params)
            rows.append({"c": c, "end": end, "next": nxt, "done": len(c.runs), "error": err,
                         "total": c.amount * c.months})
        return render(ctx, "contracts.html", rows=rows, names=names, opts=form_options(ctx),
                      categories=EXPENSE_CATEGORIES)

    @app.post("/contracts/save")
    @page
    async def contracts_save(ctx: Ctx, form):
        require(ctx, "admin", "accountant")
        kind = form.get("kind", "")
        pay = form.get("pay_source", "account")
        c = ctx.L.save_contract(
            name=form.get("name", ""), kind=kind, amount=N.parse_money(form.get("amount", "")),
            start_date=parse_date(form.get("start_date", "")), months=N.parse_int(form.get("months", ""), allow_zero=False),
            project_id=safe_id(form.get("project_id")), customer_id=safe_id(form.get("customer_id")),
            category=form.get("category", "rent"),
            money_account_id=safe_id(form.get("money_account_id")) if pay == "account" else None,
            paid_by_partner_id=safe_id(form.get("paid_by_partner_id")) if pay == "partner" else None,
            supplier=form.get("supplier", ""))
        made = ctx.L.run_contracts()
        flash(ctx.request, "ok", ctx.t("msg_contract_saved", made=made))
        raise_commit_redirect(ctx, "/contracts")

    @app.post("/contracts/{cid:int}/stop")
    @page
    async def contracts_stop(ctx: Ctx, cid: int, form):
        require(ctx, "admin", "accountant")
        ctx.L.stop_contract(get_or_404(ctx, Contract, cid))
        flash(ctx.request, "ok", ctx.t("msg_saved"))
        raise_commit_redirect(ctx, "/contracts")

    @app.post("/contracts/{cid:int}/delete")
    @page
    async def contracts_delete(ctx: Ctx, cid: int, form):
        require(ctx, "admin", "accountant")
        ctx.L.delete_contract(get_or_404(ctx, Contract, cid))
        flash(ctx.request, "ok", ctx.t("msg_deleted"))
        raise_commit_redirect(ctx, "/contracts")

    # ------------------------------------------------------------ stock

    @app.get("/inventory", response_class=HTMLResponse)
    @page
    async def inventory_page(ctx: Ctx):
        require(ctx)
        q = ctx.request.query_params
        as_of = opt_date(q.get("as_of"))
        d1, d2 = opt_date(q.get("from")), opt_date(q.get("to"))
        L = ctx.L
        items = {i.id: i for i in ctx.s.scalars(select(Item))}
        projects = list(ctx.s.scalars(select(Project).where(Project.active.is_(True)).order_by(Project.id)))
        recipes = []
        for p in projects:
            lines = {r.item_id: r.quantity for r in L.recipe_of(p.id)}
            cov = L.coverage(p.id, as_of) if lines else {"units": None, "limiting_item_id": None}
            # cost per unit in the period: materials taken out for the project / units produced
            fq = [Document.project_id == p.id, Document.status == "POSTED"]
            if d1:
                fq.append(Document.date >= d1)
            if d2:
                fq.append(Document.date <= d2)
            produced = int(ctx.s.scalar(select(func.coalesce(func.sum(Document.quantity), 0))
                                        .where(Document.doc_type == "PRODUCTION", *fq)))
            jq = [JournalLine.account == "1300", JournalLine.project_id == p.id, JournalLine.credit > 0]
            if d1:
                jq.append(JournalLine.date >= d1)
            if d2:
                jq.append(JournalLine.date <= d2)
            used = int(ctx.s.scalar(select(func.coalesce(func.sum(JournalLine.credit), 0)).where(*jq)))
            recipes.append({"project": p, "lines": lines, "cov": cov, "produced": produced, "used": used,
                            "per_unit": (used * 1000 // produced) if produced else None})
        suppliers = [(sp, L.supplier_owed(sp.id))
                     for sp in ctx.s.scalars(select(Supplier).order_by(Supplier.active.desc(), Supplier.id))]
        return render(ctx, "inventory.html", rows=L.inventory(as_of), as_of=as_of, d1=d1, d2=d2, items=items,
                      recipes=recipes, suppliers=suppliers, active_items=[i for i in items.values() if i.active])

    @app.post("/inventory/recipe/{pid:int}")
    @page
    async def recipe_save(ctx: Ctx, pid: int, form):
        require(ctx, "admin", "accountant")
        project = get_or_404(ctx, Project, pid)
        quantities = {}
        for item in ctx.s.scalars(select(Item)):
            raw = (form.get(f"r_{item.id}") or "").strip()
            if raw == "" and f"r_{item.id}" in form:
                quantities[item.id] = 0
            elif raw:
                quantities[item.id] = N.parse_quantity(raw, allow_zero=True)
        ctx.L.set_recipe(project, quantities)
        flash(ctx.request, "ok", ctx.t("msg_saved"))
        raise_commit_redirect(ctx, "/inventory")

    @app.get("/master/{entity}", response_class=HTMLResponse)
    @page
    async def master_list(ctx: Ctx, entity: str):
        require(ctx)
        if entity not in MASTER:
            raise BusinessError("err_not_found")
        model = MASTER[entity]
        items = list(ctx.s.scalars(select(model).order_by(model.active.desc(), model.id)))
        refs = {i.id: ctx.L.master_references(entity, i.id) for i in items}
        bal = {}
        if entity == "money_account":
            bal = {i.id: ctx.L.balance(CASH, money_account_id=i.id) for i in items}
        if entity == "customer":
            bal = {i.id: ctx.L.balance("1200", customer_id=i.id) for i in items}
        if entity == "supplier":
            bal = {i.id: ctx.L.supplier_owed(i.id) for i in items}
        if entity == "item":
            bal = {i.id: ctx.L.stock_of(i.id) for i in items}
        edit = None
        if safe_id(ctx.request.query_params.get("edit")):
            edit = ctx.s.get(model, safe_id(ctx.request.query_params["edit"]))
        return render(ctx, "master.html", entity=entity, items=items, refs=refs, bal=bal, edit=edit,
                      categories=EXPENSE_CATEGORIES)

    @app.post("/master/{entity}/save")
    @page
    async def master_save(ctx: Ctx, entity: str, form):
        require(ctx, "admin", "accountant")
        if entity not in MASTER:
            raise BusinessError("err_not_found")
        oid = form.get("id") or ""
        obj = get_or_404(ctx, MASTER[entity], N.parse_id(oid)) if N.parse_id(oid) else None
        fields = {"name": form.get("name", "")}
        if entity == "money_account":
            fields.update(kind=form.get("kind", ""), holder=form.get("holder", ""))
        if entity == "project":
            fields.update(kind=form.get("kind", ""), unit_name=form.get("unit_name", ""), notes=form.get("notes", ""))
        if entity == "supplier":
            fields.update(phone=form.get("phone", ""))
        if entity == "item":
            fields.update(unit=form.get("unit", ""), category=form.get("category", ""))
        ctx.L.save_master(entity, obj, **fields)
        flash(ctx.request, "ok", ctx.t("msg_saved"))
        raise_commit_redirect(ctx, f"/master/{entity}")

    @app.post("/master/{entity}/{oid:int}/delete")
    @page
    async def master_delete(ctx: Ctx, entity: str, oid: int, form):
        require(ctx, "admin", "accountant")
        if entity not in MASTER:
            raise BusinessError("err_not_found")
        ctx.L.delete_master(entity, get_or_404(ctx, MASTER[entity], oid))
        flash(ctx.request, "ok", ctx.t("msg_deleted"))
        raise_commit_redirect(ctx, f"/master/{entity}")

    @app.post("/master/{entity}/{oid:int}/active")
    @page
    async def master_active(ctx: Ctx, entity: str, oid: int, form):
        require(ctx, "admin", "accountant")
        if entity not in MASTER:
            raise BusinessError("err_not_found")
        ctx.L.set_master_active(entity, get_or_404(ctx, MASTER[entity], oid), form.get("active") == "1")
        flash(ctx.request, "ok", ctx.t("msg_saved"))
        raise_commit_redirect(ctx, f"/master/{entity}")

    # ------------------------------------------------------------ users & settings

    @app.get("/users", response_class=HTMLResponse)
    @page
    async def users_page(ctx: Ctx):
        require(ctx, "admin")
        users = list(ctx.s.scalars(select(User).order_by(User.id)))
        edit = None
        if safe_id(ctx.request.query_params.get("edit")):
            edit = ctx.s.get(User, safe_id(ctx.request.query_params["edit"]))
        return render(ctx, "users.html", users=users, edit=edit, roles=ROLES,
                      partners=list(ctx.s.scalars(select(Partner).order_by(Partner.id))))

    @app.post("/users/save")
    @page
    async def users_save(ctx: Ctx, form):
        require(ctx, "admin")
        uid = form.get("id") or ""
        u = get_or_404(ctx, User, N.parse_id(uid)) if N.parse_id(uid) else None
        pid = form.get("partner_id") or ""
        if form.get("password") and form.get("password") != form.get("password2"):
            raise BusinessError("err_password_mismatch")
        ctx.L.save_user(u, username=form.get("username", ""), full_name=form.get("full_name", ""),
                        role=form.get("role", ""), partner_id=N.parse_id(pid),
                        password=form.get("password") or None, lang=form.get("lang", "ar"),
                        digits=form.get("digits", "latn"))
        flash(ctx.request, "ok", ctx.t("msg_saved"))
        raise_commit_redirect(ctx, "/users")

    @app.post("/users/{uid:int}/active")
    @page
    async def users_active(ctx: Ctx, uid: int, form):
        require(ctx, "admin")
        ctx.L.set_user_active(get_or_404(ctx, User, uid), form.get("active") == "1")
        flash(ctx.request, "ok", ctx.t("msg_saved"))
        raise_commit_redirect(ctx, "/users")

    @app.post("/users/{uid:int}/delete")
    @page
    async def users_delete(ctx: Ctx, uid: int, form):
        require(ctx, "admin")
        ctx.L.delete_user(get_or_404(ctx, User, uid))
        flash(ctx.request, "ok", ctx.t("msg_deleted"))
        raise_commit_redirect(ctx, "/users")

    @app.get("/settings", response_class=HTMLResponse)
    @page
    async def settings_page(ctx: Ctx):
        require(ctx, "admin")
        return render(ctx, "settings.html", threshold=N.money_input_value(ctx.L.approval_threshold),
                      approvers=ctx.L.approvers())

    @app.post("/settings")
    @page
    async def settings_post(ctx: Ctx, form):
        require(ctx, "admin")
        raw = (form.get("threshold") or "").strip()
        ctx.L.set_approval_threshold(N.parse_money(raw, allow_zero=True) if raw else 0)
        ctx.L.set_setting("company_name", (form.get("company_name") or "").strip()[:200])
        flash(ctx.request, "ok", ctx.t("msg_saved"))
        raise_commit_redirect(ctx, "/settings")

    # ------------------------------------------------------------ months

    @app.get("/months", response_class=HTMLResponse)
    @page
    async def months_page(ctx: Ctx):
        require(ctx)
        L = ctx.L
        od = L.opening_date()
        months = []
        if od:
            y, m = od.year, od.month
            t = today_fn()
            while (y, m) <= (t.year, t.month):
                months.append(f"{y:04d}-{m:02d}")
                m += 1
                if m == 13:
                    y, m = y + 1, 1
        locks = {ml.month: ml for ml in ctx.s.scalars(select(MonthLock))}
        signoffs = defaultdict(list)
        for so in ctx.s.scalars(select(MonthSignoff)):
            signoffs[so.month].append(so.user_id)
        rows = []
        for mo in reversed(months):
            y, m = map(int, mo.split("-"))
            start = dt.date(y, m, 1)
            end = (dt.date(y + (m // 12), m % 12 + 1, 1) - dt.timedelta(days=1))
            pending = ctx.s.scalar(select(func.count(Document.id)).where(
                Document.status == "PENDING", Document.date >= start, Document.date <= end))
            rows.append({"month": mo, "lock": locks.get(mo), "signoffs": signoffs.get(mo, []),
                         "profit": L.net_profit(start=start, end=end), "pending": pending})
        return render(ctx, "months.html", rows=rows, approvers=L.approvers(), names=_names(ctx))

    @app.post("/months/{month}/{action}")
    @page
    async def months_action(ctx: Ctx, month: str, action: str, form):
        require(ctx)
        if action == "lock":
            ctx.L.lock_month(month)
        elif action == "unlock":
            ctx.L.unlock_month(month, form.get("reason", ""))
        elif action == "signoff":
            ctx.L.signoff_month(month)
        else:
            raise BusinessError("err_not_found")
        flash(ctx.request, "ok", ctx.t("msg_saved"))
        raise_commit_redirect(ctx, "/months")

    # ------------------------------------------------------------ reports

    @app.get("/reports/trial", response_class=HTMLResponse)
    @page
    async def report_trial(ctx: Ctx):
        require(ctx)
        q = ctx.request.query_params
        as_of = opt_date(q.get("as_of"))
        mode = q.get("mode") if q.get("mode") in ("balances", "totals", "both") else "balances"
        level = q.get("level") if q.get("level") in ("leaf", "main") else "leaf"
        full = COA.build_chart(ctx.s, ctx.t, end=as_of, only_used=True)
        nodes = [n for n in full if n.group] if level == "main" else full
        leaves = [n for n in full if not n.group]
        sums = {"td": sum(n.total_debit for n in leaves), "tc": sum(n.total_credit for n in leaves),
                "bd": sum(n.balance for n in leaves if n.balance > 0),
                "bc": sum(-n.balance for n in leaves if n.balance < 0)}
        return render(ctx, "report_trial.html", tb=ctx.L.trial_balance(as_of), as_of=as_of, nodes=nodes,
                      mode=mode, level=level, sums=sums)

    @app.get("/reports/accounts", response_class=HTMLResponse)
    @page
    async def report_accounts(ctx: Ctx):
        require(ctx)
        as_of = opt_date(ctx.request.query_params.get("as_of"))
        return render(ctx, "report_accounts.html", nodes=COA.build_chart(ctx.s, ctx.t, end=as_of), as_of=as_of)

    @app.get("/reports/ledger", response_class=HTMLResponse)
    @page
    async def report_ledger(ctx: Ctx):
        require(ctx)
        q = ctx.request.query_params
        d1, d2 = opt_date(q.get("from")), opt_date(q.get("to"))
        project = safe_id(q.get("project"))
        nodes = COA.build_chart(ctx.s, ctx.t)
        codes = {n.code for n in nodes}
        code = q.get("code") if q.get("code") in codes else None
        data = COA.account_ledger(ctx.s, code, d1, d2, project) if code else None
        return render(ctx, "report_ledger.html", nodes=nodes, code=code, data=data, d1=d1, d2=d2,
                      groups=set(COA.GROUP_PARENT),
                      project=project, names=_names(ctx), coa=coa_names(ctx),
                      projects=list(ctx.s.scalars(select(Project).order_by(Project.id))))

    @app.get("/reports/journal", response_class=HTMLResponse)
    @page
    async def report_journal(ctx: Ctx):
        require(ctx)
        q = ctx.request.query_params
        d1, d2 = opt_date(q.get("from")), opt_date(q.get("to"))
        if d1 is None and d2 is None:  # default: this month
            t = ctx.L.today
            d1, d2 = t.replace(day=1), t
        stmt = select(Document).where(Document.status == "POSTED")
        if d1:
            stmt = stmt.where(Document.date >= d1)
        if d2:
            stmt = stmt.where(Document.date <= d2)
        docs = [d for d in ctx.s.scalars(stmt.order_by(Document.date, Document.id)) if d.lines]
        total = sum(ln.debit for d in docs for ln in d.lines)
        return render(ctx, "report_journal.html", docs=docs, d1=d1, d2=d2, total=total, names=_names(ctx),
                      coa=coa_names(ctx))

    @app.get("/reports/projects", response_class=HTMLResponse)
    @page
    async def report_projects(ctx: Ctx):
        require(ctx)
        q = ctx.request.query_params
        d1, d2 = opt_date(q.get("from")), opt_date(q.get("to"))
        return render(ctx, "report_projects.html", rows=ctx.L.project_report(d1, d2), d1=d1, d2=d2,
                      categories=EXPENSE_CATEGORIES)

    @app.get("/reports/partners", response_class=HTMLResponse)
    @page
    async def report_partners(ctx: Ctx):
        require(ctx)
        as_of = opt_date(ctx.request.query_params.get("as_of"))
        rows = ctx.L.partner_summary(as_of) if ctx.L.share_versions() else []
        return render(ctx, "report_partners.html", rows=rows, as_of=as_of,
                      distributable=ctx.L.distributable(as_of or today_fn()) if ctx.L.opening_doc() else 0)

    @app.get("/reports/partner/{pid:int}", response_class=HTMLResponse)
    @page
    async def report_partner(ctx: Ctx, pid: int):
        require(ctx)
        p = get_or_404(ctx, Partner, pid)
        lines = ctx.s.execute(select(JournalLine, Document).join(Document, Document.id == JournalLine.document_id)
                              .where(JournalLine.partner_id == pid)
                              .order_by(JournalLine.date, Document.id)).all()
        summary = next((r for r in ctx.L.partner_summary() if r["partner"].id == pid), None) \
            if ctx.L.share_versions() else None
        return render(ctx, "report_partner.html", p=p, lines=lines, summary=summary)

    @app.get("/reports/cash/{ma_id:int}", response_class=HTMLResponse)
    @page
    async def report_cash(ctx: Ctx, ma_id: int):
        require(ctx)
        ma = get_or_404(ctx, MoneyAccount, ma_id)
        q = ctx.request.query_params
        d1, d2 = opt_date(q.get("from")), opt_date(q.get("to"))
        opening, rows, closing = ctx.L.money_account_ledger(ma_id, d1, d2)
        return render(ctx, "report_cash.html", ma=ma, opening=opening, rows=rows, closing=closing, d1=d1, d2=d2)

    @app.get("/reports/customer/{cid:int}", response_class=HTMLResponse)
    @page
    async def report_customer(ctx: Ctx, cid: int):
        require(ctx)
        c = get_or_404(ctx, Customer, cid)
        lines = ctx.s.execute(select(JournalLine, Document).join(Document, Document.id == JournalLine.document_id)
                              .where(JournalLine.account == "1200", JournalLine.customer_id == cid)
                              .order_by(JournalLine.date, Document.id)).all()
        return render(ctx, "report_customer.html", c=c, lines=lines)

    @app.get("/assets", response_class=HTMLResponse)
    @page
    async def assets_page(ctx: Ctx):
        require(ctx)
        rows = []
        for a in ctx.s.scalars(select(Asset).order_by(Asset.id)):
            src = ctx.s.get(Document, a.source_document_id) if a.source_document_id else None
            disp = ctx.s.scalar(select(Document).where(Document.doc_type == "ASSET_DISPOSAL",
                                                       Document.asset_id == a.id,
                                                       Document.status.in_(("POSTED", "PENDING"))))
            rows.append({"asset": a, "src": src, "cost": ctx.L._asset_cost(a.id), "disposal": disp,
                         "book": ctx.L.balance("1500", asset_id=a.id)})
        return render(ctx, "assets.html", rows=rows, names=_names(ctx))

    @app.get("/audit", response_class=HTMLResponse)
    @page
    async def audit_page(ctx: Ctx):
        require(ctx)
        q = ctx.request.query_params
        stmt = select(AuditLog)
        if q.get("action"):
            stmt = stmt.where(AuditLog.action == q["action"])
        if q.get("entity"):
            stmt = stmt.where(AuditLog.entity == q["entity"])
        rows = list(ctx.s.scalars(stmt.order_by(AuditLog.id.desc()).limit(500)))
        return render(ctx, "audit.html", rows=rows, names=_names(ctx), q=q)

    @app.get("/export/documents.csv")
    @page
    async def export_csv(ctx: Ctx):
        require(ctx)
        names = _names(ctx)
        buf = io.StringIO()
        buf.write("﻿")
        w = csv.writer(buf)
        w.writerow([ctx.t(k) for k in ("col_type", "col_number", "f_date", "col_status", "f_amount", "f_fee",
                                       "f_quantity", "f_unit_price", "f_category", "f_partner_id", "f_project_id",
                                       "f_customer_id", "f_money_account_id", "f_description", "f_void_reason")])
        for d in ctx.s.scalars(select(Document).order_by(Document.date, Document.id)):
            w.writerow([ctx.t("doc_" + d.doc_type), d.number, d.date.isoformat(), ctx.t("status_" + d.status),
                        N.money_input_value(d.amount), N.money_input_value(d.fee), N.qty_input_value(d.quantity),
                        N.money_input_value(d.unit_price), ctx.t("cat_" + d.category) if d.category else "",
                        names["partner"].get(d.partner_id or d.paid_by_partner_id, ""),
                        names["project"].get(d.project_id, ""), names["customer"].get(d.customer_id, ""),
                        names["money_account"].get(d.money_account_id, ""), _csv_safe(d.description),
                        _csv_safe(d.void_reason)])
        return Response(buf.getvalue().encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=documents.csv"})

    def _csv_safe(text: str) -> str:
        # prevent spreadsheet formula injection
        if text and text[0] in "=+-@\t\r":
            return "'" + text
        return text

    @app.get("/backup")
    @page
    async def backup(ctx: Ctx):
        require(ctx, "admin")
        path = os.path.join(data_dir, f"backup-{uuid.uuid4().hex}.db")
        raw = engine.raw_connection()
        try:
            import sqlite3
            dst = sqlite3.connect(path)
            raw.driver_connection.backup(dst)
            dst.close()
        finally:
            raw.close()
        ctx.L.audit("backup", "database")
        data = open(path, "rb").read()
        os.remove(path)
        name = f"partnership-{today_fn().isoformat()}.db"
        return Response(data, media_type="application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app

