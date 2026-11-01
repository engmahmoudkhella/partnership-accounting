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


class ProjectShareVersion(Base):
    """Shares that apply to ONE project from ``effective_date`` (e.g. a partner could not fund his whole
    share of a parcels project and others covered it). Profit and capital calls of that project follow
    these; every other project keeps the general shares."""

    __tablename__ = "project_share_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    effective_date: Mapped[dt.date] = mapped_column(Date)
    mode: Mapped[str] = mapped_column(String(10), default="percent")
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    lines = relationship("ProjectShareLine", cascade="all, delete-orphan", order_by="ProjectShareLine.partner_id")

    __table_args__ = (UniqueConstraint("project_id", "effective_date"),)


class ProjectShareLine(Base):
    __tablename__ = "project_share_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("project_share_versions.id", ondelete="CASCADE"))
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
    # costing card of one unit (meal): sale price and per-unit costs not taken from stock
    sale_price: Mapped[int] = mapped_column(Integer, default=0)
    packaging_per_unit: Mapped[int] = mapped_column(Integer, default=0)
    labor_per_unit: Mapped[int] = mapped_column(Integer, default=0)
    other_per_unit: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class MoneyAccount(Base):
    __tablename__ = "money_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="cash")  # cash, bank, wallet
    holder: Mapped[str] = mapped_column(String(200), default="")
    currency: Mapped[str] = mapped_column(String(3), default="")  # "" = shekel, "USD"
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    phone: Mapped[str] = mapped_column(String(50), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Item(Base):
    """A stock item kept in the store (rice, meat, oil...). Quantities are thousandths of ``unit``."""
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    unit: Mapped[str] = mapped_column(String(30), default="كغ")
    category: Mapped[str] = mapped_column(String(30), default="food")  # expense category when used
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class RecipeLine(Base):
    """How much of an item one unit produced by a project uses (e.g. one meal uses 0.400 kg rice)."""
    __tablename__ = "recipe_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    quantity: Mapped[int] = mapped_column(Integer)  # thousandths of the item unit per produced unit

    __table_args__ = (UniqueConstraint("project_id", "item_id"),)


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
    "PURCHASE",
    "SUPPLIER_PAYMENT",
    "STOCK_ISSUE",
    "STOCK_COUNT",
    "STOCK_RETURN",
    "FX_REVALUATION",
)
# document types that never create ledger lines
MEMO_TYPES = ("CAPITAL_CALL", "PRODUCTION")
# document types that can require approval of all partners
APPROVAL_TYPES = ("EXPENSE", "ASSET_PURCHASE", "PURCHASE")

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
    "stock_diff",  # stock count: shortage / spoilage (debit) or surplus (credit)
)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(30), index=True)
    number: Mapped[int] = mapped_column(Integer)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20), default="POSTED", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    ref_no: Mapped[str] = mapped_column(String(100), default="")  # supplier invoice / external voucher number

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
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    paid: Mapped[int] = mapped_column(Integer, default=0)  # purchase: paid now; the rest is owed to the supplier
    # foreign currency: amounts above are always shekels; these keep what was typed in dollars
    currency: Mapped[str] = mapped_column(String(3), default="")  # "" = shekel, "USD"
    fx_amount: Mapped[int] = mapped_column(Integer, default=0)  # dollar cents (money side / invoice total)
    fx_amount2: Mapped[int] = mapped_column(Integer, default=0)  # transfer: dollar cents received
    rate: Mapped[int] = mapped_column(Integer, default=0)  # shekels per dollar x 10,000

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
    items = relationship("DocumentItem", cascade="all, delete-orphan", order_by="DocumentItem.id")

    __table_args__ = (UniqueConstraint("doc_type", "number"),)


class DocumentItem(Base):
    """Item lines of a purchase (quantity x unit price) or of a stock issue / production
    (quantity; ``amount`` = cost taken out of stock at average cost)."""
    __tablename__ = "document_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    quantity: Mapped[int] = mapped_column(Integer)  # thousandths
    unit_price: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    book_quantity: Mapped[int] = mapped_column(Integer, default=0)  # stock count: quantity in the books


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
    fx: Mapped[int] = mapped_column(Integer, default=0)  # dollar cents, for a dollar account


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
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), nullable=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)  # stock lines: + in / - out, thousandths
    debit: Mapped[int] = mapped_column(Integer, default=0)
    credit: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="")  # "USD" on dollar cash / receivable lines
    fx: Mapped[int] = mapped_column(Integer, default=0)  # dollar cents: + debit / - credit

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


# ------------------------------------------------------------------ monthly contracts

class Contract(Base):
    """A monthly amount for a fixed number of months: a rent the partnership pays (expense)
    or a rent it charges a customer (income). One document is created per month when due."""
    __tablename__ = "contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10))  # expense | income
    amount: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[dt.date] = mapped_column(Date)
    months: Mapped[int] = mapped_column(Integer)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    category: Mapped[str] = mapped_column(String(30), default="rent")
    money_account_id: Mapped[int | None] = mapped_column(ForeignKey("money_accounts.id"), nullable=True)
    paid_by_partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    supplier: Mapped[str] = mapped_column(String(200), default="")
    currency: Mapped[str] = mapped_column(String(3), default="")  # income contracts may be in dollars
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    runs = relationship("ContractRun", cascade="all, delete-orphan", order_by="ContractRun.period")


class ContractRun(Base):
    __tablename__ = "contract_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("contracts.id"))
    period: Mapped[int] = mapped_column(Integer)  # 0 = first month
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)

    document = relationship("Document")

    __table_args__ = (UniqueConstraint("contract_id", "period"),)


# ------------------------------------------------------------------ quotations

class Quote(Base):
    """A price offer to a customer. It does not touch the accounts; when the customer accepts,
    it is turned into invoices (one per line)."""
    __tablename__ = "quotes"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True)
    date: Mapped[dt.date] = mapped_column(Date)
    valid_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN | ACCEPTED | CANCELLED
    currency: Mapped[str] = mapped_column(String(3), default="")
    invoice_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    lines = relationship("QuoteLine", cascade="all, delete-orphan", order_by="QuoteLine.id")

    @property
    def total(self) -> int:
        return sum(ln.amount for ln in self.lines)


class QuoteLine(Base):
    __tablename__ = "quote_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id", ondelete="CASCADE"))
    description: Mapped[str] = mapped_column(String(300))
    quantity: Mapped[int] = mapped_column(Integer)  # milli
    unit_price: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)


class CurrencyRate(Base):
    """The dollar rate of a day (shekels per dollar x 10,000), like Al-Aseel's currency rates file.
    A document uses the rate of its date, or the latest one before it."""
    __tablename__ = "currency_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, unique=True, index=True)
    rate: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
