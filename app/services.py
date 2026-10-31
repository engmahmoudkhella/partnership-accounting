"""Business rules of the partnership ledger.

Every operation goes through :class:`Ledger`. An operation either succeeds
completely (and is written to the audit log) or raises :class:`BusinessError`
and the caller rolls the session back, so a failed operation never leaves a
partial change behind.

Accounts (double entry, amounts in agorot):

    1100 cash/bank (per money account)      3100 partner capital (per partner)
    1200 receivable (per customer)          3200 distributions (per partner)
    1500 fixed assets (per asset)           4100 revenue, 4900 gain on assets
                                            5100 expenses, 5900 loss on assets
"""

from __future__ import annotations

import datetime as dt
import os
from collections import defaultdict
from dataclasses import dataclass

import bcrypt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    APPROVAL_TYPES,
    DOC_TYPES,
    EXPENSE_CATEGORIES,
    MEMO_TYPES,
    ROLES,
    Approval,
    Asset,
    AuditLog,
    CallShare,
    Customer,
    Document,
    JournalLine,
    MoneyAccount,
    MonthLock,
    MonthSignoff,
    OpeningLine,
    Partner,
    Project,
    Setting,
    ShareLine,
    ShareVersion,
    User,
    utcnow,
)
from .numbers import PPM_TOTAL, allocate, line_total, round_half_up_div

CASH, AR, FA, CAP, DIST, REV, GAIN, EXP, LOSS = (
    "1100", "1200", "1500", "3100", "3200", "4100", "4900", "5100", "5900",
)
PL_CREDIT = (REV, GAIN)
PL_DEBIT = (EXP, LOSS)


def share_display_ppm(weights: list[tuple[int, int]]) -> dict[int, int]:
    """Each share rounded on its own, for display only: 3:1:1:1 shows 50% and 16.6667% three times
    (which adds up to 100.0001% on screen). Calculations always use the exact weights."""
    total = sum(w for _, w in weights)
    if total <= 0:
        return {}
    return {pid: round_half_up_div(w * PPM_TOTAL, total) for pid, w in weights}


class BusinessError(Exception):
    """A rule was broken. ``code`` is a translation key."""

    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = params

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"BusinessError({self.code!r}, {self.params!r})"


def month_of(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def check_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        return False


# Which document types each role may create. Admin and accountant: all.
PARTNER_CREATABLE = ("EXPENSE", "PRODUCTION")


@dataclass
class Line:
    account: str
    debit: int = 0
    credit: int = 0
    partner_id: int | None = None
    project_id: int | None = None
    customer_id: int | None = None
    money_account_id: int | None = None
    asset_id: int | None = None
    category: str = ""


class Ledger:
    def __init__(self, session: Session, user: User | None, today: dt.date | None = None,
                 upload_dir: str | None = None):
        self.last_post_error: BusinessError | None = None
        self.system = False  # True only for bootstrap/maintenance code acting without a user
        self.files_to_remove: list[str] = []  # removed only after the transaction commits
        self.s = session
        self.user = user
        self.today = today or dt.date.today()
        self.upload_dir = upload_dir

    # ================================================================ helpers

    @property
    def role(self) -> str:
        if self.user:
            return self.user.role
        return "admin" if self.system else "anonymous"

    def _require_role(self, *roles):
        if self.role not in roles:
            raise BusinessError("err_forbidden")

    def audit(self, action: str, entity: str, entity_id=None, before=None, after=None, note=""):
        self.s.add(AuditLog(user_id=self.user.id if self.user else None, action=action, entity=entity,
                            entity_id=entity_id, before=before, after=after, note=note or ""))

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.s.get(Setting, key)
        return row.value if row else default

    def set_setting(self, key: str, value: str):
        self._require_role("admin")
        row = self.s.get(Setting, key)
        before = row.value if row else None
        if row:
            row.value = value
        else:
            self.s.add(Setting(key=key, value=value))
        self.audit("update", "setting", None, {key: before}, {key: value})

    @property
    def approval_threshold(self) -> int:
        """Amount (agorot) above which expenses/assets need all partners' approval. 0 = never."""
        try:
            return int(self.get_setting("approval_threshold", "0"))
        except ValueError:
            return 0

    def set_approval_threshold(self, minor: int):
        if minor < 0:
            raise BusinessError("err_number_negative")
        self.set_setting("approval_threshold", str(int(minor)))

    # ---------------------------------------------------------------- dates

    def opening_doc(self) -> Document | None:
        return self.s.scalar(select(Document).where(Document.doc_type == "OPENING",
                                                    Document.status == "POSTED"))

    def opening_date(self) -> dt.date | None:
        doc = self.opening_doc()
        return doc.date if doc else None

    def is_locked(self, d: dt.date) -> bool:
        return self.s.scalar(select(MonthLock.id).where(MonthLock.month == month_of(d))) is not None

    def locked_months(self) -> list[str]:
        return list(self.s.scalars(select(MonthLock.month).order_by(MonthLock.month)))

    def _check_date_open(self, d: dt.date):
        if self.is_locked(d):
            raise BusinessError("err_month_locked", month=month_of(d))

    def _check_doc_date(self, d: dt.date, doc_type: str):
        if not isinstance(d, dt.date):
            raise BusinessError("err_date_required")
        if d > self.today:
            raise BusinessError("err_date_future")
        if doc_type != "OPENING":
            od = self.opening_date()
            if od is None:
                raise BusinessError("err_opening_required")
            if d < od:
                raise BusinessError("err_date_before_opening", opening=od.isoformat())
        self._check_date_open(d)

    # ---------------------------------------------------------------- shares

    def share_versions(self) -> list[ShareVersion]:
        return list(self.s.scalars(select(ShareVersion).order_by(ShareVersion.effective_date)))

    def shares_at(self, d: dt.date) -> list[tuple[int, int]]:
        """(partner_id, weight) in force on ``d``. Weights are relative; only their ratio matters."""
        v = self.s.scalar(select(ShareVersion).where(ShareVersion.effective_date <= d)
                          .order_by(ShareVersion.effective_date.desc()))
        if v is None:
            raise BusinessError("err_no_shares", date=d.isoformat())
        return [(ln.partner_id, ln.weight) for ln in sorted(v.lines, key=lambda x: x.partner_id) if ln.weight > 0]

    def share_percent_at(self, d: dt.date) -> dict[int, int]:
        """Display only: each partner's share in parts-per-million, each rounded on its own."""
        return share_display_ppm(self.shares_at(d))

    def _last_share_dependent_date(self) -> dt.date | None:
        return self.s.scalar(select(func.max(Document.date)).where(
            Document.doc_type.in_(("DISTRIBUTION", "CAPITAL_CALL")),
            Document.status.in_(("POSTED", "PENDING"))))

    def create_share_version(self, effective_date: dt.date, shares: dict[int, int], note: str = "",
                             mode: str = "percent") -> ShareVersion:
        """``shares`` maps partner id to weight: ppm when mode="percent", whole units when mode="ratio"."""
        self._require_role("admin")
        if not isinstance(effective_date, dt.date):
            raise BusinessError("err_date_required")
        if mode not in ("percent", "ratio"):
            raise BusinessError("err_invalid_choice")
        partners = {p.id for p in self.s.scalars(select(Partner))}
        if not partners:
            raise BusinessError("err_no_partners")
        for pid, w in shares.items():
            if pid not in partners:
                raise BusinessError("err_not_found")
            if not isinstance(w, int) or w < 0:
                raise BusinessError("err_number_negative")
            if mode == "percent" and w > PPM_TOTAL:
                raise BusinessError("err_percent_over_100")
        total = sum(shares.values())
        if mode == "percent" and total != PPM_TOTAL:
            raise BusinessError("err_shares_sum", total=total)
        if mode == "ratio" and total <= 0:
            raise BusinessError("err_ratio_zero")
        if self.s.scalar(select(ShareVersion.id).where(ShareVersion.effective_date == effective_date)):
            raise BusinessError("err_share_version_exists")
        self._check_share_change_allowed(effective_date)
        v = ShareVersion(effective_date=effective_date, note=note or "", mode=mode,
                         created_by=self.user.id if self.user else None)
        for pid in sorted(partners):
            v.lines.append(ShareLine(partner_id=pid, weight=int(shares.get(pid, 0))))
        self.s.add(v)
        self.s.flush()
        self.audit("create", "share_version", v.id, None, self._share_snapshot(v))
        self._clear_signoffs_from(effective_date)
        return v

    def _check_share_change_allowed(self, effective_date: dt.date):
        locks = self.locked_months()
        if locks and month_of(effective_date) <= locks[-1]:
            raise BusinessError("err_share_date_locked", month=locks[-1])
        last = self._last_share_dependent_date()
        if last is not None and effective_date <= last:
            raise BusinessError("err_share_date_before_distribution", date=last.isoformat())
        od = self.opening_date()
        versions = self.share_versions()
        if od and versions and effective_date <= od:
            raise BusinessError("err_share_date_before_opening", opening=od.isoformat())

    def delete_share_version(self, version: ShareVersion):
        self._require_role("admin")
        versions = self.share_versions()
        if len(versions) <= 1 and self.s.scalar(select(func.count(Document.id))) > 0:
            raise BusinessError("err_share_last_version")
        if versions and versions[-1].id != version.id:
            raise BusinessError("err_share_not_latest")
        self._check_share_change_allowed(version.effective_date)
        snap = self._share_snapshot(version)
        self.s.delete(version)
        self.s.flush()
        self.audit("delete", "share_version", version.id, snap, None)
        self._clear_signoffs_from(version.effective_date)

    @staticmethod
    def _share_snapshot(v: ShareVersion) -> dict:
        return {"effective_date": v.effective_date.isoformat(), "note": v.note, "mode": v.mode,
                "shares": {str(ln.partner_id): ln.weight for ln in v.lines}}

    # ================================================================ master data

    def _entity_snapshot(self, obj) -> dict:
        out = {}
        for col in obj.__table__.columns:
            v = getattr(obj, col.key)
            if isinstance(v, (dt.date, dt.datetime)):
                v = v.isoformat()
            if col.key == "password_hash":
                v = "***"
            out[col.key] = v
        return out

    def _name_clean(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise BusinessError("err_name_required")
        if len(name) > 200:
            raise BusinessError("err_name_too_long")
        return name

    def _unique_name(self, model, name: str, exclude_id=None):
        q = select(model.id).where(func.lower(model.name) == name.lower())
        if exclude_id:
            q = q.where(model.id != exclude_id)
        if self.s.scalar(q):
            raise BusinessError("err_name_exists", name=name)

    def save_partner(self, partner: Partner | None, name: str, id_number: str = "", phone: str = "") -> Partner:
        self._require_role("admin")
        name = self._name_clean(name)
        self._unique_name(Partner, name, partner.id if partner else None)
        before = self._entity_snapshot(partner) if partner else None
        if partner is None:
            partner = Partner(name=name)
            self.s.add(partner)
        partner.name = name
        partner.id_number = (id_number or "").strip()
        partner.phone = (phone or "").strip()
        self.s.flush()
        self.audit("update" if before else "create", "partner", partner.id, before, self._entity_snapshot(partner))
        return partner

    def partner_references(self, pid: int) -> int:
        n = 0
        for col in (Document.partner_id, Document.partner2_id, Document.paid_by_partner_id):
            n += self.s.scalar(select(func.count(Document.id)).where(col == pid))
        n += self.s.scalar(select(func.count(JournalLine.id)).where(JournalLine.partner_id == pid))
        n += self.s.scalar(select(func.count(OpeningLine.id)).where(OpeningLine.partner_id == pid))
        n += self.s.scalar(select(func.count(CallShare.id)).where(CallShare.partner_id == pid))
        n += self.s.scalar(select(func.count(ShareLine.id)).where(ShareLine.partner_id == pid,
                                                                    ShareLine.weight > 0))
        n += self.s.scalar(select(func.count(User.id)).where(User.partner_id == pid))
        return n

    def delete_partner(self, partner: Partner):
        self._require_role("admin")
        if self.partner_references(partner.id):
            raise BusinessError("err_partner_in_use")
        snap = self._entity_snapshot(partner)
        for ln in list(self.s.scalars(select(ShareLine).where(ShareLine.partner_id == partner.id))):
            self.s.delete(ln)
        self.s.delete(partner)
        self.s.flush()
        self.audit("delete", "partner", partner.id, snap, None)

    _MASTER = {"project": Project, "money_account": MoneyAccount, "customer": Customer}

    def save_master(self, entity: str, obj, **fields):
        self._require_role("admin", "accountant")
        model = self._MASTER[entity]
        name = self._name_clean(fields.pop("name", ""))
        self._unique_name(model, name, obj.id if obj else None)
        if entity == "money_account" and fields.get("kind") not in ("cash", "bank", "wallet"):
            raise BusinessError("err_invalid_choice")
        if entity == "project" and fields.get("kind") not in ("kitchen", "parcels", "other"):
            raise BusinessError("err_invalid_choice")
        before = self._entity_snapshot(obj) if obj else None
        if obj is None:
            obj = model(name=name)
            self.s.add(obj)
        obj.name = name
        for k, v in fields.items():
            setattr(obj, k, (v or "").strip() if isinstance(v, str) else v)
        self.s.flush()
        self.audit("update" if before else "create", entity, obj.id, before, self._entity_snapshot(obj))
        return obj

    def master_references(self, kind: str, obj_id: int) -> int:
        col = {"project": "project_id", "money_account": "money_account_id", "customer": "customer_id"}[kind]
        n = self.s.scalar(select(func.count(Document.id)).where(getattr(Document, col) == obj_id))
        n += self.s.scalar(select(func.count(JournalLine.id)).where(getattr(JournalLine, col) == obj_id))
        if kind == "money_account":
            n += self.s.scalar(select(func.count(Document.id)).where(Document.money_account2_id == obj_id))
            n += self.s.scalar(select(func.count(OpeningLine.id)).where(OpeningLine.money_account_id == obj_id))
        if kind == "customer":
            n += self.s.scalar(select(func.count(OpeningLine.id)).where(OpeningLine.customer_id == obj_id))
        if kind == "project":
            n += self.s.scalar(select(func.count(Asset.id)).where(Asset.project_id == obj_id))
        return n

    def delete_master(self, kind: str, obj):
        self._require_role("admin", "accountant")
        if self.master_references(kind, obj.id):
            raise BusinessError("err_in_use_deactivate")
        snap = self._entity_snapshot(obj)
        self.s.delete(obj)
        self.s.flush()
        self.audit("delete", kind, obj.id, snap, None)

    def set_master_active(self, kind: str, obj, active: bool):
        self._require_role("admin", "accountant")
        if obj.active == active:
            return
        if not active:
            col = {"project": Document.project_id, "money_account": Document.money_account_id,
                   "customer": Document.customer_id}[kind]
            q = select(func.count(Document.id)).where(col == obj.id, Document.status == "PENDING")
            if kind == "money_account":
                q2 = select(func.count(Document.id)).where(Document.money_account2_id == obj.id,
                                                            Document.status == "PENDING")
                pending = self.s.scalar(q) + self.s.scalar(q2)
            else:
                pending = self.s.scalar(q)
            if pending:
                raise BusinessError("err_has_pending_docs")
            if kind == "money_account" and self.balance(CASH, money_account_id=obj.id) != 0:
                raise BusinessError("err_account_not_empty")
            if kind == "customer" and self.balance(AR, customer_id=obj.id) != 0:
                raise BusinessError("err_customer_not_settled")
        before = self._entity_snapshot(obj)
        obj.active = active
        self.s.flush()
        self.audit("activate" if active else "deactivate", kind, obj.id, before, self._entity_snapshot(obj))

    # ---------------------------------------------------------------- users

    def save_user(self, user: User | None, *, username: str, full_name: str, role: str,
                  partner_id: int | None, password: str | None, lang: str = "ar", digits: str = "latn") -> User:
        self._require_role("admin")
        username = (username or "").strip().lower()
        import re
        if not re.fullmatch(r"[a-z0-9._-]{3,64}", username):
            raise BusinessError("err_username_format")
        if role not in ROLES:
            raise BusinessError("err_invalid_choice")
        if lang not in ("ar", "en") or digits not in ("latn", "arab"):
            raise BusinessError("err_invalid_choice")
        q = select(User.id).where(User.username == username)
        if user:
            q = q.where(User.id != user.id)
        if self.s.scalar(q):
            raise BusinessError("err_username_exists")
        if partner_id is not None:
            if self.s.get(Partner, partner_id) is None:
                raise BusinessError("err_not_found")
            q = select(User.id).where(User.partner_id == partner_id, User.active.is_(True))
            if user:
                q = q.where(User.id != user.id)
            if self.s.scalar(q):
                raise BusinessError("err_partner_has_user")
        if role == "viewer" and partner_id is not None:
            raise BusinessError("err_viewer_partner")
        if user is None and not password:
            raise BusinessError("err_password_short")
        if password is not None and password != "" and len(password) < 8:
            raise BusinessError("err_password_short")
        before = self._entity_snapshot(user) if user else None
        if user is not None and user.role == "admin" and role != "admin":
            self._ensure_other_admin(user)
        if user is None:
            user = User(username=username, full_name="", password_hash="", role=role)
            self.s.add(user)
        user.username = username
        user.full_name = self._name_clean(full_name)
        user.role = role
        user.partner_id = partner_id
        user.lang = lang
        user.digits = digits
        if password:
            user.password_hash = hash_password(password)
        self.s.flush()
        self.audit("update" if before else "create", "user", user.id, before, self._entity_snapshot(user))
        return user

    def _ensure_other_admin(self, user: User):
        others = self.s.scalar(select(func.count(User.id)).where(User.role == "admin", User.active.is_(True),
                                                                   User.id != user.id))
        if not others:
            raise BusinessError("err_last_admin")

    def set_user_active(self, user: User, active: bool):
        self._require_role("admin")
        if self.user and user.id == self.user.id and not active:
            raise BusinessError("err_cannot_disable_self")
        if not active and user.role == "admin":
            self._ensure_other_admin(user)
        if active and user.partner_id is not None:
            if self.s.scalar(select(User.id).where(User.partner_id == user.partner_id, User.active.is_(True),
                                                   User.id != user.id)):
                raise BusinessError("err_partner_has_user")
        before = self._entity_snapshot(user)
        user.active = active
        self.s.flush()
        self.audit("activate" if active else "deactivate", "user", user.id, before, self._entity_snapshot(user))

    def delete_user(self, user: User):
        self._require_role("admin")
        if self.user and user.id == self.user.id:
            raise BusinessError("err_cannot_disable_self")
        if user.role == "admin":
            self._ensure_other_admin(user)
        refs = (self.s.scalar(select(func.count(AuditLog.id)).where(AuditLog.user_id == user.id))
                + self.s.scalar(select(func.count(Document.id)).where(
                    (Document.created_by == user.id) | (Document.voided_by == user.id)))
                + self.s.scalar(select(func.count(Approval.id)).where(Approval.user_id == user.id))
                + self.s.scalar(select(func.count(MonthSignoff.id)).where(MonthSignoff.user_id == user.id)))
        if refs:
            raise BusinessError("err_user_has_history")
        snap = self._entity_snapshot(user)
        self.s.delete(user)
        self.s.flush()
        self.audit("delete", "user", user.id, snap, None)

    def change_own_password(self, current: str, new: str):
        if not self.user or not check_password(current, self.user.password_hash):
            raise BusinessError("err_wrong_password")
        if len(new or "") < 8:
            raise BusinessError("err_password_short")
        self.user.password_hash = hash_password(new)
        self.audit("change_password", "user", self.user.id)

    def set_own_preferences(self, lang: str, digits: str):
        if lang not in ("ar", "en") or digits not in ("latn", "arab"):
            raise BusinessError("err_invalid_choice")
        self.user.lang = lang
        self.user.digits = digits

    # ================================================================ documents

    def approvers(self) -> list[User]:
        return list(self.s.scalars(select(User).where(User.active.is_(True), User.partner_id.is_not(None),
                                                      User.role != "viewer").order_by(User.id)))

    def needs_approval(self, doc: Document) -> bool:
        t = self.approval_threshold
        return doc.doc_type in APPROVAL_TYPES and t > 0 and doc.amount > t and bool(self.approvers())

    def _next_number(self, doc_type: str) -> int:
        return (self.s.scalar(select(func.max(Document.number)).where(Document.doc_type == doc_type)) or 0) + 1

    def _can_create(self, doc_type: str):
        if self.role in ("admin", "accountant"):
            return
        if self.role == "partner" and doc_type in PARTNER_CREATABLE:
            return
        raise BusinessError("err_forbidden")

    def _can_modify(self, doc: Document):
        if self.role in ("admin", "accountant"):
            return
        if (self.role == "partner" and doc.doc_type in PARTNER_CREATABLE and self.user
                and doc.created_by == self.user.id and doc.status in ("PENDING", "REJECTED")):
            return
        raise BusinessError("err_forbidden")

    @staticmethod
    def doc_snapshot(doc: Document) -> dict:
        out = {}
        for col in doc.__table__.columns:
            v = getattr(doc, col.key)
            if isinstance(v, (dt.date, dt.datetime)):
                v = v.isoformat()
            out[col.key] = v
        if doc.opening_lines:
            out["opening_lines"] = [
                {"kind": o.kind, "money_account_id": o.money_account_id, "customer_id": o.customer_id,
                 "asset_id": o.asset_id, "partner_id": o.partner_id, "amount": o.amount}
                for o in doc.opening_lines]
        if doc.call_shares:
            out["call_shares"] = {str(c.partner_id): c.amount for c in doc.call_shares}
        return out

    # fields each type uses (anything else is reset to its empty value)
    FIELDS = {
        "CONTRIBUTION": ("partner_id", "money_account_id", "amount", "capital_call_id"),
        "CAPITAL_CALL": ("amount", "project_id"),
        "CAPITAL_TRANSFER": ("partner_id", "partner2_id", "amount"),
        "EXPENSE": ("amount", "category", "project_id", "money_account_id", "paid_by_partner_id", "supplier"),
        "ASSET_PURCHASE": ("amount", "project_id", "money_account_id", "paid_by_partner_id", "supplier"),
        "ASSET_DISPOSAL": ("asset_id", "amount", "money_account_id"),
        "INVOICE": ("customer_id", "project_id", "quantity", "unit_price"),
        "RECEIPT": ("customer_id", "money_account_id", "amount", "fee", "project_id"),
        "TRANSFER": ("money_account_id", "money_account2_id", "amount", "fee"),
        "DISTRIBUTION": ("money_account_id", "amount"),
        "PRODUCTION": ("project_id", "quantity"),
    }
    _EMPTY = {"amount": 0, "fee": 0, "quantity": 0, "unit_price": 0, "category": "", "supplier": ""}
    _ALL_FIELDS = ("partner_id", "partner2_id", "project_id", "customer_id", "money_account_id",
                   "money_account2_id", "asset_id", "capital_call_id", "paid_by_partner_id",
                   "amount", "fee", "quantity", "unit_price", "category", "supplier")

    def _apply_fields(self, doc: Document, data: dict, old: Document | None):
        allowed = self.FIELDS[doc.doc_type]
        for f in self._ALL_FIELDS:
            if f in allowed:
                v = data.get(f)
                if f in self._EMPTY:
                    v = self._EMPTY[f] if v in (None, "") else v
                    if isinstance(v, str):
                        v = v.strip()
                else:
                    v = None if v in ("", None) else int(v)
                setattr(doc, f, v)
            else:
                setattr(doc, f, None if f not in self._EMPTY else self._EMPTY[f])
        doc.description = (data.get("description") or "").strip()

    def _active_ref(self, model, obj_id, old_id, err):
        if obj_id is None:
            return None
        obj = self.s.get(model, obj_id)
        if obj is None:
            raise BusinessError("err_not_found")
        if not getattr(obj, "active", True) and obj_id != old_id:
            raise BusinessError(err)
        return obj

    def _validate(self, doc: Document, old: dict | None, asset_name: str | None):
        t = doc.doc_type
        o = old or {}
        req = lambda v, code: v if v not in (None, "", 0) else (_ for _ in ()).throw(BusinessError(code))  # noqa: E731

        def positive(v, code="err_amount_positive"):
            if not isinstance(v, int) or v <= 0:
                raise BusinessError(code)

        def partner(pid, code="err_partner_required"):
            req(pid, code)
            if self.s.get(Partner, pid) is None:
                raise BusinessError("err_not_found")

        def money(mid, key="money_account_id"):
            req(mid, "err_money_account_required")
            self._active_ref(MoneyAccount, mid, o.get(key), "err_money_account_inactive")

        def project(pid, required=False):
            if required:
                req(pid, "err_project_required")
            self._active_ref(Project, pid, o.get("project_id"), "err_project_closed")

        def customer(cid):
            req(cid, "err_customer_required")
            self._active_ref(Customer, cid, o.get("customer_id"), "err_customer_inactive")

        if t == "CONTRIBUTION":
            partner(doc.partner_id)
            money(doc.money_account_id)
            positive(doc.amount)
            if doc.capital_call_id is not None:
                call = self.s.get(Document, doc.capital_call_id)
                if call is None or call.doc_type != "CAPITAL_CALL" or call.status != "POSTED":
                    raise BusinessError("err_call_invalid")
                if doc.date < call.date:
                    raise BusinessError("err_contribution_before_call")
                if not any(cs.partner_id == doc.partner_id for cs in call.call_shares):
                    raise BusinessError("err_partner_not_in_call")
        elif t == "CAPITAL_CALL":
            positive(doc.amount)
            project(doc.project_id)
        elif t == "CAPITAL_TRANSFER":
            partner(doc.partner_id)
            partner(doc.partner2_id)
            if doc.partner_id == doc.partner2_id:
                raise BusinessError("err_same_partner")
            positive(doc.amount)
        elif t in ("EXPENSE", "ASSET_PURCHASE"):
            positive(doc.amount)
            if t == "EXPENSE" and doc.category not in EXPENSE_CATEGORIES:
                raise BusinessError("err_category_required")
            project(doc.project_id)
            has_acc = doc.money_account_id is not None
            has_partner = doc.paid_by_partner_id is not None
            if has_acc == has_partner:
                raise BusinessError("err_pay_source")
            if has_acc:
                money(doc.money_account_id)
            else:
                partner(doc.paid_by_partner_id)
                if self.role == "partner" and doc.paid_by_partner_id != self.user.partner_id:
                    raise BusinessError("err_paid_by_other_partner")
            if t == "ASSET_PURCHASE":
                if asset_name is not None:
                    self._name_clean(asset_name)
        elif t == "ASSET_DISPOSAL":
            req(doc.asset_id, "err_asset_required")
            asset = self.s.get(Asset, doc.asset_id)
            if asset is None:
                raise BusinessError("err_not_found")
            src = self.s.get(Document, asset.source_document_id) if asset.source_document_id else None
            if src is None or src.status != "POSTED":
                raise BusinessError("err_asset_not_owned")
            if doc.date < src.date:
                raise BusinessError("err_disposal_before_purchase")
            other = self.s.scalar(select(Document.id).where(
                Document.doc_type == "ASSET_DISPOSAL", Document.asset_id == asset.id,
                Document.status.in_(("POSTED", "PENDING")), Document.id != (doc.id or 0)))
            if other:
                raise BusinessError("err_asset_already_disposed")
            if not isinstance(doc.amount, int) or doc.amount < 0:
                raise BusinessError("err_number_negative")
            if doc.amount > 0:
                money(doc.money_account_id)
            else:
                doc.money_account_id = None
        elif t == "INVOICE":
            customer(doc.customer_id)
            project(doc.project_id, required=True)
            positive(doc.quantity, "err_quantity_positive")
            positive(doc.unit_price, "err_price_positive")
            doc.amount = line_total(doc.quantity, doc.unit_price)
            positive(doc.amount)
        elif t == "RECEIPT":
            customer(doc.customer_id)
            money(doc.money_account_id)
            positive(doc.amount)
            project(doc.project_id)
            if doc.fee < 0:
                raise BusinessError("err_number_negative")
            if doc.fee >= doc.amount:
                raise BusinessError("err_fee_too_large")
        elif t == "TRANSFER":
            money(doc.money_account_id)
            money(doc.money_account2_id, "money_account2_id")
            if doc.money_account_id == doc.money_account2_id:
                raise BusinessError("err_same_account")
            positive(doc.amount)
            if doc.fee < 0:
                raise BusinessError("err_number_negative")
            if doc.fee >= doc.amount:
                raise BusinessError("err_fee_too_large")
        elif t == "DISTRIBUTION":
            money(doc.money_account_id)
            positive(doc.amount)
            self.shares_at(doc.date)
        elif t == "PRODUCTION":
            project(doc.project_id, required=True)
            positive(doc.quantity, "err_quantity_positive")

    # ---------------------------------------------------------------- lines

    def _lines_for(self, doc: Document) -> list[Line]:
        t = doc.doc_type
        L: list[Line] = []
        if t == "OPENING":
            for o in doc.opening_lines:
                if o.kind == "money":
                    L.append(Line(CASH, debit=o.amount, money_account_id=o.money_account_id))
                elif o.kind == "customer":
                    L.append(Line(AR, debit=o.amount, customer_id=o.customer_id))
                elif o.kind == "asset":
                    asset = self.s.get(Asset, o.asset_id)
                    L.append(Line(FA, debit=o.amount, asset_id=o.asset_id,
                                  project_id=asset.project_id if asset else None))
                elif o.kind == "partner":
                    L.append(Line(CAP, credit=o.amount, partner_id=o.partner_id))
        elif t == "CONTRIBUTION":
            L.append(Line(CASH, debit=doc.amount, money_account_id=doc.money_account_id))
            L.append(Line(CAP, credit=doc.amount, partner_id=doc.partner_id))
        elif t == "CAPITAL_TRANSFER":
            # partner_id paid partner2_id directly: partner2 gives up capital, partner gains it
            L.append(Line(CAP, debit=doc.amount, partner_id=doc.partner2_id))
            L.append(Line(CAP, credit=doc.amount, partner_id=doc.partner_id))
        elif t in ("EXPENSE", "ASSET_PURCHASE"):
            if t == "EXPENSE":
                L.append(Line(EXP, debit=doc.amount, project_id=doc.project_id, category=doc.category))
            else:
                L.append(Line(FA, debit=doc.amount, asset_id=doc.asset_id, project_id=doc.project_id))
            if doc.money_account_id is not None:
                L.append(Line(CASH, credit=doc.amount, money_account_id=doc.money_account_id))
            else:
                L.append(Line(CAP, credit=doc.amount, partner_id=doc.paid_by_partner_id))
        elif t == "ASSET_DISPOSAL":
            asset = self.s.get(Asset, doc.asset_id)
            cost = self._asset_cost(asset.id)
            if doc.amount:
                L.append(Line(CASH, debit=doc.amount, money_account_id=doc.money_account_id))
            L.append(Line(FA, credit=cost, asset_id=asset.id, project_id=asset.project_id))
            diff = doc.amount - cost
            if diff > 0:
                L.append(Line(GAIN, credit=diff, project_id=asset.project_id))
            elif diff < 0:
                L.append(Line(LOSS, debit=-diff, project_id=asset.project_id))
        elif t == "INVOICE":
            L.append(Line(AR, debit=doc.amount, customer_id=doc.customer_id, project_id=doc.project_id))
            L.append(Line(REV, credit=doc.amount, customer_id=doc.customer_id, project_id=doc.project_id))
        elif t == "RECEIPT":
            L.append(Line(CASH, debit=doc.amount - doc.fee, money_account_id=doc.money_account_id))
            if doc.fee:
                L.append(Line(EXP, debit=doc.fee, category="fees", project_id=doc.project_id))
            L.append(Line(AR, credit=doc.amount, customer_id=doc.customer_id))
        elif t == "TRANSFER":
            L.append(Line(CASH, credit=doc.amount, money_account_id=doc.money_account_id))
            L.append(Line(CASH, debit=doc.amount - doc.fee, money_account_id=doc.money_account2_id))
            if doc.fee:
                L.append(Line(EXP, debit=doc.fee, category="fees"))
        elif t == "DISTRIBUTION":
            parts = allocate(doc.amount, self.distribution_weights(doc.date, exclude_doc_id=doc.id),
                             tie_offset=doc.number)
            for pid, amt in parts.items():
                if amt:
                    L.append(Line(DIST, debit=amt, partner_id=pid))
            L.append(Line(CASH, credit=doc.amount, money_account_id=doc.money_account_id))
        return L

    def distribution_weights(self, d: dt.date, exclude_doc_id: int | None = None) -> list[tuple[int, int]]:
        """How a distribution on ``d`` is split: by each partner's profit not yet distributed.

        With unchanged shares this equals the share ratio. After a change of shares it pays every
        partner what he actually earned (profit earned under the old shares is paid by the old shares),
        and it self-corrects any agora that rounding gave one partner earlier.
        """
        alloc = self.profit_allocation(end=d)
        q = select(JournalLine.partner_id, func.sum(JournalLine.debit - JournalLine.credit)).where(
            JournalLine.account == DIST, JournalLine.date <= d)
        if exclude_doc_id:
            q = q.where(JournalLine.document_id != exclude_doc_id)
        paid = {pid: int(v) for pid, v in self.s.execute(q.group_by(JournalLine.partner_id)).all()}
        pids = sorted(set(alloc) | set(paid))
        weights = [(pid, max(0, alloc.get(pid, 0) - paid.get(pid, 0))) for pid in pids]
        weights = [(pid, w) for pid, w in weights if w > 0]
        if not weights:
            raise BusinessError("err_distribution_exceeds_profit", date=d.isoformat(), excess=0)
        return weights

    def _asset_cost(self, asset_id: int) -> int:
        asset = self.s.get(Asset, asset_id)
        src = self.s.get(Document, asset.source_document_id) if asset and asset.source_document_id else None
        if src is None or src.status != "POSTED":
            return 0
        if src.doc_type == "OPENING":
            for o in src.opening_lines:
                if o.kind == "asset" and o.asset_id == asset_id:
                    return o.amount
            return 0
        return src.amount

    def _rebuild_lines(self, doc: Document):
        if doc.lines and self.is_locked(doc.date):
            raise BusinessError("err_month_locked", month=month_of(doc.date))
        for ln in list(doc.lines):
            self.s.delete(ln)
        doc.lines.clear()
        self.s.flush()
        if doc.status != "POSTED" or doc.doc_type in MEMO_TYPES:
            return
        lines = self._lines_for(doc)
        dr = sum(x.debit for x in lines)
        cr = sum(x.credit for x in lines)
        if dr != cr:
            raise BusinessError("err_unbalanced", debit=dr, credit=cr)
        for x in lines:
            if x.debit < 0 or x.credit < 0:
                raise BusinessError("err_unbalanced", debit=dr, credit=cr)
            if x.debit == 0 and x.credit == 0:
                continue
            doc.lines.append(JournalLine(date=doc.date, account=x.account, debit=x.debit, credit=x.credit,
                                         partner_id=x.partner_id, project_id=x.project_id,
                                         customer_id=x.customer_id, money_account_id=x.money_account_id,
                                         asset_id=x.asset_id, category=x.category))
        self.s.flush()

    def _rebuild_call_shares(self, doc: Document):
        doc.call_shares.clear()
        self.s.flush()
        if doc.doc_type != "CAPITAL_CALL":
            return
        for pid, amt in allocate(doc.amount, self.shares_at(doc.date), tie_offset=doc.number).items():
            doc.call_shares.append(CallShare(partner_id=pid, amount=amt))
        self.s.flush()

    # ---------------------------------------------------------------- invariants

    def _running_check(self, account: str, key: str, err: str):
        col = getattr(JournalLine, key)
        rows = self.s.execute(
            select(col, JournalLine.date, func.sum(JournalLine.debit - JournalLine.credit))
            .where(JournalLine.account == account).group_by(col, JournalLine.date)
            .order_by(col, JournalLine.date)).all()
        running: dict = defaultdict(int)
        sign = 1 if account in (CASH, AR, FA) else -1
        for k, d, amount in rows:
            running[k] += sign * int(amount)
            if running[k] < 0:
                raise BusinessError(err, id=k, date=d.isoformat(), balance=running[k])

    def check_invariants(self):
        """Raise BusinessError if the books are in an impossible state."""
        self.s.flush()
        dr, cr = self.s.execute(select(func.coalesce(func.sum(JournalLine.debit), 0),
                                       func.coalesce(func.sum(JournalLine.credit), 0))).one()
        if dr != cr:
            raise BusinessError("err_unbalanced", debit=dr, credit=cr)
        self._running_check(CASH, "money_account_id", "err_insufficient_balance")
        self._running_check(AR, "customer_id", "err_receivable_negative")
        self._running_check(CAP, "partner_id", "err_capital_negative")
        self._running_check(FA, "asset_id", "err_asset_negative")
        # contributions linked to a capital call never exceed that partner's called amount
        rows = self.s.execute(
            select(Document.capital_call_id, Document.partner_id, func.sum(Document.amount))
            .where(Document.doc_type == "CONTRIBUTION", Document.status == "POSTED",
                   Document.capital_call_id.is_not(None))
            .group_by(Document.capital_call_id, Document.partner_id)).all()
        for call_id, pid, paid in rows:
            call = self.s.get(Document, call_id)
            if call is None or call.status != "POSTED":
                raise BusinessError("err_call_has_contributions")
            called = sum(cs.amount for cs in call.call_shares if cs.partner_id == pid)
            if paid > called:
                raise BusinessError("err_contribution_exceeds_call", called=called, paid=int(paid))
        for c_date, call_id in self.s.execute(
                select(Document.date, Document.capital_call_id).where(
                    Document.doc_type == "CONTRIBUTION", Document.status == "POSTED",
                    Document.capital_call_id.is_not(None))).all():
            call = self.s.get(Document, call_id)
            if call is not None and c_date < call.date:
                raise BusinessError("err_contribution_before_call")
        # distributions never exceed realised, undistributed profit
        for d in self.s.scalars(select(Document.date).where(Document.doc_type == "DISTRIBUTION",
                                                            Document.status == "POSTED").distinct()):
            avail = self.distributable(d)
            if avail < 0:
                raise BusinessError("err_distribution_exceeds_profit", date=d.isoformat(), excess=-avail)
            # and no partner has received more than his own share of profit
            alloc = self.profit_allocation(end=d)
            for pid, paid in self.s.execute(
                    select(JournalLine.partner_id, func.sum(JournalLine.debit - JournalLine.credit))
                    .where(JournalLine.account == DIST, JournalLine.date <= d)
                    .group_by(JournalLine.partner_id)).all():
                if int(paid) > alloc.get(pid, 0):
                    raise BusinessError("err_partner_overdistributed", date=d.isoformat(),
                                        excess=int(paid) - alloc.get(pid, 0))

    # ---------------------------------------------------------------- lifecycle

    def _after_change(self, *dates: dt.date):
        for d in dates:
            if d is not None:
                self._clear_signoffs(month_of(d))

    def _clear_signoffs(self, month: str):
        rows = list(self.s.scalars(select(MonthSignoff).where(MonthSignoff.month == month)))
        if rows:
            for r in rows:
                self.s.delete(r)
            self.audit("signoffs_cleared", "month", None, {"month": month, "users": [r.user_id for r in rows]},
                       None)

    def _clear_signoffs_from(self, d: dt.date):
        for m in set(self.s.scalars(select(MonthSignoff.month).where(MonthSignoff.month >= month_of(d)))):
            self._clear_signoffs(m)

    def create_document(self, doc_type: str, date: dt.date, data: dict, *, asset_name: str | None = None,
                        attachment: tuple[str, str] | None = None) -> Document:
        if doc_type not in DOC_TYPES or doc_type == "OPENING":
            raise BusinessError("err_invalid_choice")
        self._can_create(doc_type)
        self._check_doc_date(date, doc_type)
        doc = Document(doc_type=doc_type, date=date, number=self._next_number(doc_type),
                       created_by=self.user.id if self.user else None)
        self._apply_fields(doc, data, None)
        if doc_type == "ASSET_PURCHASE" and not (asset_name or "").strip():
            raise BusinessError("err_asset_name_required")
        self._validate(doc, None, asset_name)
        doc.status = "PENDING" if self.needs_approval(doc) else "POSTED"
        if attachment:
            doc.attachment, doc.attachment_name = attachment
        self.s.add(doc)
        self.s.flush()
        if doc_type == "ASSET_PURCHASE":
            asset = Asset(name=self._name_clean(asset_name), project_id=doc.project_id, source_document_id=doc.id)
            self.s.add(asset)
            self.s.flush()
            doc.asset_id = asset.id
        self._rebuild_call_shares(doc)
        if doc.status == "PENDING" and self.user and self.user.partner_id and self.user in self.approvers():
            self.s.add(Approval(document_id=doc.id, user_id=self.user.id, decision="approve",
                                note="auto: creator"))
            self.s.flush()
            self.s.refresh(doc)
            self._maybe_post(doc, raise_on_fail=True)
        self._rebuild_lines(doc)
        self.check_invariants()
        self.audit("create", "document", doc.id, None, self.doc_snapshot(doc))
        self._after_change(date)
        return doc

    def update_document(self, doc: Document, date: dt.date, data: dict, *, asset_name: str | None = None,
                        attachment: tuple[str, str] | None = None, remove_attachment: bool = False) -> Document:
        if doc.doc_type == "OPENING":
            raise BusinessError("err_use_opening_page")
        self._can_modify(doc)
        if doc.status not in ("POSTED", "PENDING", "REJECTED"):
            raise BusinessError("err_doc_not_editable")
        self._check_date_open(doc.date)
        self._check_doc_date(date, doc.doc_type)
        before = self.doc_snapshot(doc)
        old_date = doc.date
        old_amount = doc.amount
        if doc.doc_type == "CAPITAL_CALL" and date != doc.date:
            self._check_share_dependent_move(doc, date)
        if doc.doc_type == "DISTRIBUTION" and date != doc.date:
            self._check_share_dependent_move(doc, date)
        doc.date = date
        self._apply_fields(doc, data, None)
        if doc.doc_type == "ASSET_PURCHASE":
            doc.asset_id = before["asset_id"]
            if asset_name is not None:
                asset = self.s.get(Asset, doc.asset_id)
                asset.name = self._name_clean(asset_name)
                asset.project_id = doc.project_id
            disposal = self.s.scalar(select(Document).where(Document.doc_type == "ASSET_DISPOSAL",
                                                            Document.asset_id == doc.asset_id,
                                                            Document.status.in_(("POSTED", "PENDING"))))
            if disposal and (doc.amount != old_amount or doc.date > disposal.date):
                raise BusinessError("err_asset_has_disposal")
        self._validate(doc, before, asset_name)
        if attachment:
            self._remove_file(doc.attachment)
            doc.attachment, doc.attachment_name = attachment
        elif remove_attachment:
            self._remove_file(doc.attachment)
            doc.attachment, doc.attachment_name = "", ""
        doc.updated_at = utcnow()
        # approval: any change to a document that needs approval restarts the approval round
        if self.needs_approval(doc):
            for a in list(doc.approvals):
                self.s.delete(a)
            doc.approvals.clear()
            doc.status = "PENDING"
            self.s.flush()
            if self.user and self.user.partner_id and self.user in self.approvers():
                self.s.add(Approval(document_id=doc.id, user_id=self.user.id, decision="approve",
                                    note="auto: editor"))
                self.s.flush()
                self.s.refresh(doc)
                self._maybe_post(doc, raise_on_fail=True)
        else:
            for a in list(doc.approvals):
                self.s.delete(a)
            doc.approvals.clear()
            doc.status = "POSTED"
        self._rebuild_call_shares(doc)
        self._rebuild_lines(doc)
        # an asset disposal's lines depend on the asset's cost
        if doc.doc_type == "ASSET_PURCHASE":
            for disp in self.s.scalars(select(Document).where(Document.doc_type == "ASSET_DISPOSAL",
                                                              Document.asset_id == doc.asset_id,
                                                              Document.status == "POSTED")):
                self._rebuild_lines(disp)
        self.check_invariants()
        self.audit("update", "document", doc.id, before, self.doc_snapshot(doc))
        self._after_change(old_date, date)
        return doc

    def _check_share_dependent_move(self, doc: Document, new_date: dt.date):
        later = self.s.scalar(select(ShareVersion.id).where(
            ShareVersion.effective_date > min(doc.date, new_date),
            ShareVersion.effective_date <= max(doc.date, new_date)))
        if later:
            raise BusinessError("err_share_period_change")

    def void_document(self, doc: Document, reason: str):
        self._require_role("admin", "accountant")
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise BusinessError("err_reason_required")
        if doc.status != "POSTED":
            raise BusinessError("err_only_posted_void")
        self._check_date_open(doc.date)
        before = self.doc_snapshot(doc)
        if doc.doc_type == "OPENING":
            others = self.s.scalar(select(func.count(Document.id)).where(Document.doc_type != "OPENING"))
            if others:
                raise BusinessError("err_opening_has_docs")
        if doc.doc_type == "ASSET_PURCHASE":
            if self.s.scalar(select(Document.id).where(Document.doc_type == "ASSET_DISPOSAL",
                                                       Document.asset_id == doc.asset_id,
                                                       Document.status.in_(("POSTED", "PENDING")))):
                raise BusinessError("err_asset_has_disposal")
        if doc.doc_type == "OPENING":
            for o in doc.opening_lines:
                if o.kind == "asset" and self.s.scalar(select(Document.id).where(
                        Document.doc_type == "ASSET_DISPOSAL", Document.asset_id == o.asset_id,
                        Document.status.in_(("POSTED", "PENDING")))):
                    raise BusinessError("err_asset_has_disposal")
        if doc.doc_type == "CAPITAL_CALL":
            if self.s.scalar(select(Document.id).where(Document.capital_call_id == doc.id,
                                                       Document.status.in_(("POSTED", "PENDING")))):
                raise BusinessError("err_call_has_contributions")
        doc.status = "VOID"
        doc.void_reason = reason
        doc.voided_by = self.user.id if self.user else None
        doc.voided_at = utcnow()
        try:
            self._rebuild_lines(doc)
            self.check_invariants()
        except BusinessError as e:
            if doc.doc_type == "INVOICE" and e.code == "err_receivable_negative":
                # the generic message ("record the invoice first") is misleading here
                raise BusinessError("err_void_invoice_paid") from e
            raise
        self.audit("void", "document", doc.id, before, self.doc_snapshot(doc), note=reason)
        self._after_change(doc.date)

    def delete_document(self, doc: Document, reason: str = ""):
        """Hard delete. Only for documents that never reached the books."""
        self._can_modify(doc)
        if doc.status not in ("PENDING", "REJECTED"):
            raise BusinessError("err_only_unposted_delete")
        self._check_date_open(doc.date)
        if doc.doc_type == "CAPITAL_CALL":
            if self.s.scalar(select(Document.id).where(Document.capital_call_id == doc.id)):
                raise BusinessError("err_call_has_contributions")
        snap = self.doc_snapshot(doc)
        asset_id = doc.asset_id if doc.doc_type == "ASSET_PURCHASE" else None
        self._remove_file(doc.attachment)
        self.s.delete(doc)
        self.s.flush()
        if asset_id:
            asset = self.s.get(Asset, asset_id)
            if asset is not None:
                self.s.delete(asset)
                self.s.flush()
        self.check_invariants()
        self.audit("delete", "document", snap["id"], snap, None, note=reason or "")
        self._after_change(doc.date)

    def _remove_file(self, name: str):
        """Schedule a file for removal; it is deleted by remove_scheduled_files() after commit."""
        if name:
            self.files_to_remove.append(name)

    def discard_scheduled_files(self):
        """Call after a rollback: nothing is removed."""
        self.files_to_remove.clear()

    def remove_scheduled_files(self):
        """Call after a successful commit."""
        names, self.files_to_remove = self.files_to_remove, []
        for name in names:
            self._delete_file_now(name)

    def _delete_file_now(self, name: str):
        if name and self.upload_dir:
            path = os.path.join(self.upload_dir, os.path.basename(name))
            # never delete outside the upload directory
            if os.path.dirname(os.path.abspath(path)) == os.path.abspath(self.upload_dir) and os.path.exists(path):
                os.remove(path)

    # ---------------------------------------------------------------- approval

    def decide(self, doc: Document, decision: str, note: str = "") -> str:
        """Record a partner's approval/rejection. Returns the new status."""
        if decision not in ("approve", "reject"):
            raise BusinessError("err_invalid_choice")
        if doc.status != "PENDING":
            raise BusinessError("err_not_pending")
        if not self.user or self.user not in self.approvers():
            raise BusinessError("err_not_approver")
        self._check_date_open(doc.date)
        if decision == "reject" and len((note or "").strip()) < 3:
            raise BusinessError("err_reason_required")
        existing = self.s.scalar(select(Approval).where(Approval.document_id == doc.id,
                                                        Approval.user_id == self.user.id))
        if existing:
            raise BusinessError("err_already_decided")
        self.s.add(Approval(document_id=doc.id, user_id=self.user.id, decision=decision, note=note or ""))
        self.s.flush()
        self.s.refresh(doc)
        before_status = doc.status
        self.last_post_error = None
        if decision == "reject":
            doc.status = "REJECTED"
        else:
            self._maybe_post(doc, raise_on_fail=False)
        self._rebuild_lines(doc)
        if doc.status == "POSTED":
            try:
                self.check_invariants()
            except BusinessError as e:
                # every partner approved, but the books cannot take it yet (e.g. not enough cash
                # on that date). Keep it PENDING with the approvals; an accountant can post it later.
                self.last_post_error = e
                doc.status = "PENDING"
                self._rebuild_lines(doc)
        self.check_invariants()
        self.audit(decision, "document", doc.id, {"status": before_status}, {"status": doc.status}, note=note)
        self._after_change(doc.date)
        return doc.status

    def _maybe_post(self, doc: Document, raise_on_fail: bool):
        approved = {a.user_id for a in doc.approvals if a.decision == "approve"}
        if any(a.decision == "reject" for a in doc.approvals):
            doc.status = "REJECTED"
            return
        if all(u.id in approved for u in self.approvers()):
            doc.status = "POSTED"

    def retry_post(self, doc: Document):
        """Post a PENDING document whose approvals are complete (e.g. after topping up cash)."""
        self._require_role("admin", "accountant")
        if doc.status != "PENDING":
            raise BusinessError("err_not_pending")
        self._check_date_open(doc.date)
        approved = {a.user_id for a in doc.approvals if a.decision == "approve"}
        if not all(u.id in approved for u in self.approvers()):
            raise BusinessError("err_approvals_missing")
        doc.status = "POSTED"
        self._rebuild_lines(doc)
        self.check_invariants()
        self.audit("post", "document", doc.id, {"status": "PENDING"}, {"status": "POSTED"})
        self._after_change(doc.date)

    # ---------------------------------------------------------------- opening

    def save_opening(self, date: dt.date, money: dict[int, int], customers: dict[int, int],
                     assets: list[dict], partners: dict[int, int]) -> Document:
        """Create or replace the opening balances.

        ``assets`` items: {"id": existing asset id or None, "name", "amount", "project_id"}.
        Total of money + customers + assets must equal total partner capital.
        """
        self._require_role("admin", "accountant")
        if not isinstance(date, dt.date):
            raise BusinessError("err_date_required")
        if date > self.today:
            raise BusinessError("err_date_future")
        if self.locked_months():
            raise BusinessError("err_opening_locked")
        doc = self.opening_doc()
        first_other = self.s.scalar(select(func.min(Document.date)).where(Document.doc_type != "OPENING"))
        if first_other is not None and date > first_other:
            raise BusinessError("err_opening_after_docs", date=first_other.isoformat())
        versions = self.share_versions()
        if not versions or versions[0].effective_date > date:
            raise BusinessError("err_no_shares", date=date.isoformat())
        for amount in list(money.values()) + list(customers.values()) + list(partners.values()) + \
                [a["amount"] for a in assets]:
            if not isinstance(amount, int) or amount < 0:
                raise BusinessError("err_number_negative")
        for mid, amt in money.items():
            if amt and self.s.get(MoneyAccount, mid) is None:
                raise BusinessError("err_not_found")
        for cid, amt in customers.items():
            if amt and self.s.get(Customer, cid) is None:
                raise BusinessError("err_not_found")
        for pid, amt in partners.items():
            if amt and self.s.get(Partner, pid) is None:
                raise BusinessError("err_not_found")
        for a in assets:
            if a["amount"] <= 0:
                raise BusinessError("err_amount_positive")
            self._name_clean(a.get("name", ""))
            if a.get("project_id") and self.s.get(Project, a["project_id"]) is None:
                raise BusinessError("err_not_found")
        debit = sum(money.values()) + sum(customers.values()) + sum(a["amount"] for a in assets)
        credit = sum(partners.values())
        if debit != credit:
            raise BusinessError("err_opening_unbalanced", debit=debit, credit=credit, diff=debit - credit)
        before = self.doc_snapshot(doc) if doc else None
        if doc is None:
            doc = Document(doc_type="OPENING", number=self._next_number("OPENING"), date=date, status="POSTED",
                           created_by=self.user.id if self.user else None)
            self.s.add(doc)
            self.s.flush()
        old_date = doc.date
        doc.date = date
        doc.amount = debit
        doc.updated_at = utcnow()
        old_asset_ids = {o.asset_id for o in doc.opening_lines if o.kind == "asset"}
        for ln in list(doc.lines):
            self.s.delete(ln)
        doc.lines.clear()
        doc.opening_lines.clear()
        self.s.flush()
        kept_assets = set()
        for mid, amt in sorted(money.items()):
            if amt:
                doc.opening_lines.append(OpeningLine(kind="money", money_account_id=mid, amount=amt))
        for cid, amt in sorted(customers.items()):
            if amt:
                doc.opening_lines.append(OpeningLine(kind="customer", customer_id=cid, amount=amt))
        for a in assets:
            asset = None
            if a.get("id"):
                asset = self.s.get(Asset, a["id"])
                if asset is None or asset.id not in old_asset_ids:
                    raise BusinessError("err_not_found")
            if asset is None:
                asset = Asset(name="", source_document_id=doc.id)
                self.s.add(asset)
            asset.name = self._name_clean(a["name"])
            asset.project_id = a.get("project_id")
            asset.source_document_id = doc.id
            self.s.flush()
            kept_assets.add(asset.id)
            doc.opening_lines.append(OpeningLine(kind="asset", asset_id=asset.id, amount=a["amount"]))
        for pid, amt in sorted(partners.items()):
            if amt:
                doc.opening_lines.append(OpeningLine(kind="partner", partner_id=pid, amount=amt))
        self.s.flush()
        for aid in old_asset_ids - kept_assets:
            if self.s.scalar(select(Document.id).where(Document.asset_id == aid)):
                raise BusinessError("err_asset_has_disposal")
            asset = self.s.get(Asset, aid)
            if asset:
                self.s.delete(asset)
        self.s.flush()
        self._rebuild_lines(doc)
        # asset disposals of opening assets depend on their opening cost
        for disp in self.s.scalars(select(Document).where(Document.doc_type == "ASSET_DISPOSAL",
                                                          Document.status == "POSTED")):
            self._rebuild_lines(disp)
        self.check_invariants()
        self.audit("update" if before else "create", "opening", doc.id, before, self.doc_snapshot(doc))
        self._after_change(old_date, date)
        return doc

    # ---------------------------------------------------------------- months

    def lock_month(self, month: str):
        self._require_role("admin")
        self._valid_month(month)
        if month > month_of(self.today):
            raise BusinessError("err_month_future")
        if self.s.scalar(select(MonthLock.id).where(MonthLock.month == month)):
            raise BusinessError("err_month_already_locked")
        # months are locked in order, so a change in an open month can never alter a locked one
        od = self.opening_date()
        if od is None:
            raise BusinessError("err_opening_required")
        if month < month_of(od):
            raise BusinessError("err_month_before_opening")
        prev = self._previous_month(month)
        if month > month_of(od) and not self.s.scalar(select(MonthLock.id).where(MonthLock.month == prev)):
            raise BusinessError("err_lock_previous_first", month=prev)
        pending = self.s.scalar(select(func.count(Document.id)).where(
            Document.status == "PENDING", func.strftime("%Y-%m", Document.date) == month))
        if pending:
            raise BusinessError("err_month_has_pending", count=pending)
        self.s.add(MonthLock(month=month, locked_by=self.user.id if self.user else None))
        self.s.flush()
        self.audit("lock", "month", None, None, {"month": month})

    def unlock_month(self, month: str, reason: str):
        self._require_role("admin")
        self._valid_month(month)
        if len((reason or "").strip()) < 3:
            raise BusinessError("err_reason_required")
        row = self.s.scalar(select(MonthLock).where(MonthLock.month == month))
        if row is None:
            raise BusinessError("err_month_not_locked")
        later = self.s.scalar(select(MonthLock.month).where(MonthLock.month > month).order_by(MonthLock.month.desc()))
        if later:
            raise BusinessError("err_unlock_later_first", month=later)
        self.s.delete(row)
        self.s.flush()
        self.audit("unlock", "month", None, {"month": month}, None, note=reason)

    def signoff_month(self, month: str):
        self._valid_month(month)
        if not self.user or self.user not in self.approvers():
            raise BusinessError("err_not_approver")
        if self.s.scalar(select(MonthSignoff.id).where(MonthSignoff.month == month,
                                                       MonthSignoff.user_id == self.user.id)):
            raise BusinessError("err_already_decided")
        self.s.add(MonthSignoff(month=month, user_id=self.user.id))
        self.s.flush()
        self.audit("signoff", "month", None, None, {"month": month})

    @staticmethod
    def _previous_month(month: str) -> str:
        y, m = map(int, month.split("-"))
        return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"

    @staticmethod
    def _valid_month(month: str):
        import re
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
            raise BusinessError("err_month_format")

    # ================================================================ queries

    def balance(self, account: str, as_of: dt.date | None = None, **filters) -> int:
        """Natural balance: debit-positive for 1xxx/5xxx, credit-positive otherwise."""
        q = select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0)).where(
            JournalLine.account == account)
        if as_of:
            q = q.where(JournalLine.date <= as_of)
        for k, v in filters.items():
            q = q.where(getattr(JournalLine, k) == v)
        v = int(self.s.scalar(q))
        return v if account[0] in "15" else -v

    def net_profit(self, start: dt.date | None = None, end: dt.date | None = None, project_id=None,
                   project_filter: bool = False) -> int:
        q = select(JournalLine.account, func.sum(JournalLine.credit - JournalLine.debit)).where(
            JournalLine.account.in_(PL_CREDIT + PL_DEBIT))
        if start:
            q = q.where(JournalLine.date >= start)
        if end:
            q = q.where(JournalLine.date <= end)
        if project_filter:
            q = q.where(JournalLine.project_id.is_(None) if project_id is None
                        else JournalLine.project_id == project_id)
        return int(sum(v for _, v in self.s.execute(q.group_by(JournalLine.account)).all()))

    def distributable(self, as_of: dt.date) -> int:
        """Profit that has been collected and not yet distributed, as of a date.

        = net profit to date - amount still owed by customers - distributions to date
        """
        return self.net_profit(end=as_of) - self.balance(AR, as_of) - self._dist_total(as_of)

    def _dist_total(self, as_of: dt.date | None) -> int:
        q = select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0)).where(
            JournalLine.account == DIST)
        if as_of:
            q = q.where(JournalLine.date <= as_of)
        return int(self.s.scalar(q))

    def profit_allocation(self, end: dt.date | None = None, start: dt.date | None = None) -> dict[int, int]:
        """Each partner's share of net profit, by the shares in force when it was earned.

        Profit is totalled per share period and each period total is split once, so rounding never
        builds up day after day: the parts always add up to net profit, and each partner is within
        one agora of his exact share per period.
        """
        versions = self.share_versions()
        out: dict[int, int] = defaultdict(int)
        for i, v in enumerate(versions):
            q = select(func.coalesce(func.sum(JournalLine.credit - JournalLine.debit), 0)).where(
                JournalLine.account.in_(PL_CREDIT + PL_DEBIT), JournalLine.date >= v.effective_date)
            if i + 1 < len(versions):
                q = q.where(JournalLine.date < versions[i + 1].effective_date)
            if end:
                q = q.where(JournalLine.date <= end)
            if start:
                q = q.where(JournalLine.date >= start)
            amount = int(self.s.scalar(q))
            weights = [(ln.partner_id, ln.weight) for ln in sorted(v.lines, key=lambda x: x.partner_id)
                       if ln.weight > 0]
            if amount and weights:
                for pid, part in allocate(amount, weights, tie_offset=i).items():
                    out[pid] += part
        return dict(out)

    def partner_summary(self, as_of: dt.date | None = None) -> list[dict]:
        alloc = self.profit_allocation(end=as_of)
        rows = []
        try:
            latest = self.share_percent_at(as_of or self.today)
        except BusinessError:
            latest = {}
        for p in self.s.scalars(select(Partner).order_by(Partner.id)):
            cap = self.balance(CAP, as_of, partner_id=p.id)
            dist = self._partner_dist(p.id, as_of)
            profit = alloc.get(p.id, 0)
            called = int(self.s.scalar(
                select(func.coalesce(func.sum(CallShare.amount), 0)).join(Document, Document.id == CallShare.document_id)
                .where(CallShare.partner_id == p.id, Document.status == "POSTED",
                       *( [Document.date <= as_of] if as_of else []))))
            paid_to_calls = int(self.s.scalar(
                select(func.coalesce(func.sum(Document.amount), 0)).where(
                    Document.doc_type == "CONTRIBUTION", Document.status == "POSTED",
                    Document.partner_id == p.id, Document.capital_call_id.is_not(None),
                    *([Document.date <= as_of] if as_of else []))))
            rows.append({"partner": p, "share": latest.get(p.id, 0), "capital": cap, "profit": profit,
                         "distributions": dist, "equity": cap + profit - dist, "called": called,
                         "paid_to_calls": paid_to_calls, "outstanding_calls": called - paid_to_calls})
        return rows

    def _partner_dist(self, pid: int, as_of: dt.date | None) -> int:
        q = select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0)).where(
            JournalLine.account == DIST, JournalLine.partner_id == pid)
        if as_of:
            q = q.where(JournalLine.date <= as_of)
        return int(self.s.scalar(q))

    def trial_balance(self, as_of: dt.date | None = None) -> dict:
        q = select(JournalLine.account, func.sum(JournalLine.debit), func.sum(JournalLine.credit))
        if as_of:
            q = q.where(JournalLine.date <= as_of)
        rows = {a: (int(d), int(c)) for a, d, c in self.s.execute(q.group_by(JournalLine.account)).all()}
        total_d = sum(d for d, _ in rows.values())
        total_c = sum(c for _, c in rows.values())
        cash = self.balance(CASH, as_of)
        ar = self.balance(AR, as_of)
        fa = self.balance(FA, as_of)
        cap = self.balance(CAP, as_of)
        dist = self._dist_total(as_of)
        profit = self.net_profit(end=as_of)
        assets = cash + ar + fa
        equity = cap + profit - dist
        return {"rows": rows, "total_debit": total_d, "total_credit": total_c, "balanced": total_d == total_c,
                "cash": cash, "receivable": ar, "fixed_assets": fa, "assets": assets, "capital": cap,
                "profit": profit, "distributions": dist, "equity": equity, "sheet_ok": assets == equity}

    def money_account_ledger(self, ma_id: int, start: dt.date | None = None, end: dt.date | None = None):
        opening = 0
        if start:
            opening = self.balance(CASH, start - dt.timedelta(days=1), money_account_id=ma_id)
        q = (select(JournalLine, Document).join(Document, Document.id == JournalLine.document_id)
             .where(JournalLine.account == CASH, JournalLine.money_account_id == ma_id))
        if start:
            q = q.where(JournalLine.date >= start)
        if end:
            q = q.where(JournalLine.date <= end)
        q = q.order_by(JournalLine.date, Document.id, JournalLine.id)
        rows, running = [], opening
        for jl, doc in self.s.execute(q).all():
            running += jl.debit - jl.credit
            rows.append({"line": jl, "doc": doc, "in": jl.debit, "out": jl.credit, "balance": running})
        return opening, rows, running

    def project_report(self, start: dt.date | None, end: dt.date | None) -> list[dict]:
        out = []
        projects = list(self.s.scalars(select(Project).order_by(Project.id))) + [None]
        for p in projects:
            pid = p.id if p else None
            def total(account, side, category=None, _pid=pid):
                col = JournalLine.credit if side == "c" else JournalLine.debit
                other = JournalLine.debit if side == "c" else JournalLine.credit
                q = select(func.coalesce(func.sum(col - other), 0)).where(JournalLine.account == account)
                q = q.where(JournalLine.project_id.is_(None) if _pid is None else JournalLine.project_id == _pid)
                if category:
                    q = q.where(JournalLine.category == category)
                if start:
                    q = q.where(JournalLine.date >= start)
                if end:
                    q = q.where(JournalLine.date <= end)
                return int(self.s.scalar(q))
            revenue = total(REV, "c")
            gain = total(GAIN, "c")
            loss = total(LOSS, "d")
            cats = {c: total(EXP, "d", c) for c in EXPENSE_CATEGORIES}
            expenses = sum(cats.values())
            def qty(doc_type, _pid=pid):
                q = select(func.coalesce(func.sum(Document.quantity), 0)).where(
                    Document.doc_type == doc_type, Document.status == "POSTED")
                q = q.where(Document.project_id.is_(None) if _pid is None else Document.project_id == _pid)
                if start:
                    q = q.where(Document.date >= start)
                if end:
                    q = q.where(Document.date <= end)
                return int(self.s.scalar(q))
            produced = qty("PRODUCTION")
            invoiced = qty("INVOICE")
            net = revenue + gain - expenses - loss
            if p is None and not (revenue or gain or loss or expenses):
                continue
            out.append({"project": p, "revenue": revenue, "gain": gain, "loss": loss, "expenses": expenses,
                        "categories": cats, "net": net, "produced": produced, "invoiced": invoiced,
                        "cost_per_unit": round_half_up_div(expenses * 1000, produced) if produced else None,
                        "profit_per_unit": round_half_up_div(net * 1000, invoiced) if invoiced else None})
        return out
