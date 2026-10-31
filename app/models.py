"""Database models.

All money columns are integers in agorot. Shares are parts-per-million.
Quantities are thousandths. Dates are ``datetime.date``.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ people

ROLES = ("admin", "accountant", "partner", "viewer")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    lang: Mapped[str] = mapped_column(String(2), default="ar")
    digits: Mapped[str] = mapped_column(String(4), default="latn")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    partner = relationship("Partner")


class Partner(Base):
    __tablename__ = "partners"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    id_number: Mapped[str] = mapped_column(String(50), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class ShareVersion(Base):
    """Shares of all partners, effective from ``effective_date``."""

    __tablename__ = "share_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    effective_date: Mapped[dt.date] = mapped_column(Date, unique=True)
    # "percent": weights are parts-per-million and must total 1,000,000 (100%)
    # "ratio":   weights are whole numbers, e.g. 3:1:1:1 (exact thirds of one half)
    mode: Mapped[str] = mapped_column(String(10), default="percent")
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    lines = relationship("ShareLine", cascade="all, delete-orphan", order_by="ShareLine.partner_id")


class ShareLine(Base):
    __tablename__ = "share_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("share_versions.id", ondelete="CASCADE"))
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"))
    weight: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("version_id", "partner_id"),)


# ---------------------------------------------------------- master records

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(30), default="other")  # kitchen, parcels, other
    unit_name: Mapped[str] = mapped_column(String(50), default="")  # e.g. "وجبة"
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class MoneyAccount(Base):
    __tablename__ = "money_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="cash")  # cash, bank, wallet
    holder: Mapped[str] = mapped_column(String(200), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    # the document that brought the asset in (ASSET_PURCHASE or OPENING)
    source_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    location: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------- documents

DOC_TYPES = (
    "OPENING",
    "CONTRIBUTION",
    "CAPITAL_CALL",
    "CAPITAL_TRANSFER",
    "EXPENSE",
    "ASSET_PURCHASE",
    "ASSET_DISPOSAL",
    "INVOICE",
    "RECEIPT",
    "TRANSFER",
    "DISTRIBUTION",
    "PRODUCTION",
)
# document types that never create ledger lines
MEMO_TYPES = ("CAPITAL_CALL", "PRODUCTION")
# document types that can require approval of all partners
APPROVAL_TYPES = ("EXPENSE", "ASSET_PURCHASE")

STATUSES = ("PENDING", "POSTED", "REJECTED", "VOID")

EXPENSE_CATEGORIES = (
    "meat",
    "food",
    "fuel",
    "wages",
    "rent",
    "transport",
    "packaging",
    "equipment",
    "maintenance",
    "fees",
    "other",
)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(30), index=True)
    number: Mapped[int] = mapped_column(Integer)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20), default="POSTED", index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    amount: Mapped[int] = mapped_column(Integer, default=0)
    fee: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[int] = mapped_column(Integer, default=0)  # milli
    unit_price: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str] = mapped_column(String(30), default="")
    supplier: Mapped[str] = mapped_column(String(200), default="")

    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    partner2_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    money_account_id: Mapped[int | None] = mapped_column(ForeignKey("money_accounts.id"), nullable=True)
    money_account2_id: Mapped[int | None] = mapped_column(ForeignKey("money_accounts.id"), nullable=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    capital_call_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    # who paid an expense / asset when not from a money account
    paid_by_partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)

    attachment: Mapped[str] = mapped_column(String(300), default="")
    attachment_name: Mapped[str] = mapped_column(String(300), default="")

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    void_reason: Mapped[str] = mapped_column(Text, default="")
    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    voided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    lines = relationship("JournalLine", cascade="all, delete-orphan", back_populates="document")
    opening_lines = relationship("OpeningLine", cascade="all, delete-orphan", order_by="OpeningLine.id")
    call_shares = relationship("CallShare", cascade="all, delete-orphan", order_by="CallShare.partner_id")
    approvals = relationship("Approval", cascade="all, delete-orphan", order_by="Approval.id")

    __table_args__ = (UniqueConstraint("doc_type", "number"),)


class OpeningLine(Base):
    __tablename__ = "opening_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))  # money, customer, asset, partner
    money_account_id: Mapped[int | None] = mapped_column(ForeignKey("money_accounts.id"), nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    amount: Mapped[int] = mapped_column(Integer)


class CallShare(Base):
    __tablename__ = "call_shares"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"))
    amount: Mapped[int] = mapped_column(Integer)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(10))  # approve / reject
    note: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("document_id", "user_id"),)


ACCOUNTS = {
    "1100": "cash",
    "1200": "receivable",
    "1500": "fixed_assets",
    "3100": "capital",
    "3200": "distributions",
    "4100": "revenue",
    "4900": "asset_gain",
    "5100": "expenses",
    "5900": "asset_loss",
}


class JournalLine(Base):
    __tablename__ = "journal_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    account: Mapped[str] = mapped_column(String(10), index=True)
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    money_account_id: Mapped[int | None] = mapped_column(ForeignKey("money_accounts.id"), nullable=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    category: Mapped[str] = mapped_column(String(30), default="")
    debit: Mapped[int] = mapped_column(Integer, default=0)
    credit: Mapped[int] = mapped_column(Integer, default=0)

    document = relationship("Document", back_populates="lines")


# ------------------------------------------------------- control & history

class MonthLock(Base):
    __tablename__ = "month_locks"
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), unique=True)  # YYYY-MM
    locked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    locked_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class MonthSignoff(Base):
    __tablename__ = "month_signoffs"
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("month", "user_id"),)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
