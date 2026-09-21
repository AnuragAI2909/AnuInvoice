"""Invoice model, price resolution, totals and the approval gate.

Prices are looked up from the ``Catalog`` at build time and are never accepted from
extraction output or from the editable UI table. All money uses ``Decimal``.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping

from .extractor import ExtractedRequest
from .pricing import AUTO_THRESHOLD, PREFILL_THRESHOLD, Catalog

TWO = Decimal("0.01")
EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
RESOLVED = {"matched", "reviewer_confirmed"}


class ApprovalError(Exception):
    pass


def money(x: Decimal) -> Decimal:
    return x.quantize(TWO, rounding=ROUND_HALF_UP)


@dataclass
class Customer:
    name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""


@dataclass
class InvoiceSettings:
    business_name: str = "Your Business Name"
    business_address: str = "Your address, City, State, PIN"
    business_email: str = "billing@example.com"
    business_tax_id: str = ""          # e.g. GSTIN
    currency: str = "INR"
    tax_label: str = "GST"
    tax_rate: Decimal = Decimal("18")   # percent
    discount_pct: Decimal = Decimal("0")
    payment_terms_days: int = 15
    payment_info: str = "Bank transfer / UPI. Details available on request."


@dataclass
class LineItem:
    requested: str
    quantity: Decimal | None
    status: str                      # matched | reviewer_confirmed | unmatched | invalid_quantity
    confidence: float = 0.0
    sku: str = ""
    name: str = ""
    description: str = ""
    unit: str = ""
    unit_price: Decimal | None = None
    suggestion: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status in RESOLVED

    @property
    def amount(self) -> Decimal | None:
        if not self.resolved or self.unit_price is None or self.quantity is None:
            return None
        return money(self.unit_price * self.quantity)


@dataclass(frozen=True)
class Approval:
    reviewer: str
    approved_at: str
    fingerprint: str


@dataclass
class Totals:
    subtotal: Decimal
    discount: Decimal
    taxable: Decimal
    tax: Decimal
    total: Decimal


@dataclass
class Invoice:
    number: str
    issue_date: date
    due_date: date
    customer: Customer
    lines: list[LineItem]
    settings: InvoiceSettings
    notes: str = ""
    approval: Approval | None = None

    # -- money ---------------------------------------------------------------
    @property
    def totals(self) -> Totals:
        subtotal = money(sum((l.amount for l in self.lines if l.amount is not None), Decimal("0")))
        pct = max(Decimal("0"), min(Decimal("100"), Decimal(self.settings.discount_pct)))
        discount = money(subtotal * pct / 100)
        taxable = subtotal - discount
        tax = money(taxable * Decimal(self.settings.tax_rate) / 100)
        return Totals(subtotal, discount, taxable, tax, taxable + tax)

    # -- review flags --------------------------------------------------------
    def blocking_issues(self) -> list[str]:
        issues = []
        for l in self.lines:
            label = l.requested or l.name or "(blank)"
            if l.status == "unmatched":
                hint = f" Did you mean '{l.suggestion}'?" if l.suggestion else ""
                issues.append(f"'{label}' is not in the price list.{hint} Pick a service or remove the row.")
            elif l.status == "invalid_quantity":
                issues.append(f"'{label}' has an invalid quantity (must be a number greater than 0).")
        if not any(l.resolved for l in self.lines):
            issues.append("The invoice has no priced line items.")
        return issues

    def warnings(self) -> list[str]:
        w = []
        if not self.customer.name.strip():
            w.append("Customer name is missing.")
        if not self.customer.email.strip():
            w.append("Customer email is missing.")
        elif not EMAIL_OK.match(self.customer.email.strip()):
            w.append("Customer email looks invalid.")
        for l in self.lines:
            if l.status == "reviewer_confirmed":
                w.append(f"'{l.requested}' was matched to '{l.name}' with low text similarity - please double-check.")
        return w

    # -- approval ------------------------------------------------------------
    def fingerprint(self) -> str:
        payload = {
            "n": self.number,
            "c": [self.customer.name, self.customer.email, self.customer.phone, self.customer.company],
            "l": [[l.sku, str(l.quantity), str(l.unit_price), l.status] for l in self.lines],
            "s": [str(self.settings.tax_rate), str(self.settings.discount_pct), self.settings.currency],
            "notes": self.notes,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @property
    def is_approved(self) -> bool:
        return (
            self.approval is not None
            and self.approval.fingerprint == self.fingerprint()
            and not self.blocking_issues()
        )

    @property
    def approval_is_stale(self) -> bool:
        return self.approval is not None and not self.is_approved

    # -- export --------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        t = self.totals
        return {
            "invoice_number": self.number,
            "issue_date": self.issue_date.isoformat(),
            "due_date": self.due_date.isoformat(),
            "status": "approved" if self.is_approved else "draft",
            "approved_by": self.approval.reviewer if self.is_approved else None,
            "approved_at": self.approval.approved_at if self.is_approved else None,
            "customer": vars(self.customer),
            "currency": self.settings.currency,
            "lines": [
                {"sku": l.sku, "service": l.name, "requested_as": l.requested, "unit": l.unit,
                 "quantity": str(l.quantity), "unit_price": str(l.unit_price),
                 "amount": str(l.amount), "match_confidence": round(l.confidence, 2), "status": l.status}
                for l in self.lines if l.resolved
            ],
            "unresolved": [l.requested for l in self.lines if not l.resolved],
            "totals": {"subtotal": str(t.subtotal), "discount": str(t.discount),
                       f"{self.settings.tax_label.lower()}_{self.settings.tax_rate}pct": str(t.tax),
                       "total": str(t.total)},
            "notes": self.notes,
        }


# ----------------------------------------------------------------------- builders


def new_invoice_number(now: datetime | None = None) -> str:
    return "INV-" + (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def _to_qty(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v).replace(",", "").strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def rows_from_request(req: ExtractedRequest, catalog: Catalog) -> list[dict]:
    """Turn extraction output into editable rows, pre-selecting only high-confidence matches."""
    rows = []
    for it in req.items:
        m = catalog.match(it.text)
        service, conf, suggestion = "", m.score, (m.item.name if m.item else "")
        if it.catalog_hint:
            hinted = catalog.get(it.catalog_hint)
            if hinted is not None and catalog.score(it.text, hinted) >= 0.5:
                service, conf, suggestion = hinted.name, max(catalog.score(it.text, hinted), AUTO_THRESHOLD), hinted.name
        if not service and m.item and (m.status == "auto" or (m.status == "suggest" and m.score >= PREFILL_THRESHOLD)):
            service = m.item.name
        rows.append({
            "requested": it.text,
            "service": service,
            "quantity": float(it.quantity) if it.quantity is not None else 1.0,
            "suggestion": "" if service else suggestion,
        })
    return rows


def build_invoice(
    customer: Customer,
    rows: list[Mapping[str, Any]],
    catalog: Catalog,
    settings: InvoiceSettings,
    notes: str = "",
    invoice_number: str | None = None,
    issue_date: date | None = None,
    approval: Approval | None = None,
) -> Invoice:
    lines: list[LineItem] = []
    for row in rows:
        requested = str(row.get("requested") or "").strip()
        service = str(row.get("service") or "").strip()
        qty = _to_qty(row.get("quantity"))
        if not requested and not service and row.get("quantity") in (None, ""):
            continue  # blank row from the editor

        if service:
            item = catalog.get(service)
            if item is None:  # not a real catalog entry: never invent a price
                lines.append(LineItem(requested or service, qty, "unmatched"))
                continue
            conf = catalog.score(requested or service, item)
            status = "matched" if conf >= AUTO_THRESHOLD else "reviewer_confirmed"
            if qty is None or qty <= 0:
                status = "invalid_quantity"
            lines.append(LineItem(requested or item.name, qty, status, conf, item.sku, item.name,
                                  item.description, item.unit, item.price))
        else:
            m = catalog.match(requested)
            lines.append(LineItem(requested, qty, "unmatched", m.score,
                                  suggestion=m.item.name if m.item else None))

    issued = issue_date or date.today()
    return Invoice(
        number=invoice_number or new_invoice_number(),
        issue_date=issued,
        due_date=issued + timedelta(days=int(settings.payment_terms_days)),
        customer=customer,
        lines=lines,
        settings=settings,
        notes=notes,
        approval=approval,
    )


def approve(invoice: Invoice, reviewer: str, now: datetime | None = None) -> Approval:
    """Create an approval bound to the invoice's current contents."""
    if not reviewer or not reviewer.strip():
        raise ApprovalError("Enter the reviewer's name to approve.")
    issues = invoice.blocking_issues()
    if issues:
        raise ApprovalError("Resolve all flagged items before approving:\n- " + "\n- ".join(issues))
    return Approval(reviewer.strip(), (now or datetime.now()).isoformat(timespec="seconds"), invoice.fingerprint())


# ------------------------------------------------------------------ money format

_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


def _indian_group(int_part: str) -> str:
    if len(int_part) <= 3:
        return int_part
    head, tail = int_part[:-3], int_part[-3:]
    return re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head) + "," + tail


def fmt_money(amount: Decimal | None, currency: str = "INR", ascii_only: bool = False) -> str:
    if amount is None:
        return "-"
    whole, frac = f"{money(amount):.2f}".split(".")
    neg = whole.startswith("-")
    whole = whole.lstrip("-")
    grouped = _indian_group(whole) if currency == "INR" else f"{int(whole):,}"
    sym = _SYMBOLS.get(currency, currency + " ")
    if ascii_only and currency == "INR":
        sym = "Rs. "
    return f"{'-' if neg else ''}{sym}{grouped}.{frac}"


def fmt_qty(q: Decimal | None) -> str:
    """1.0 -> '1', 2.50 -> '2.5', 100 -> '100'."""
    if q is None:
        return "-"
    return format(q.normalize(), "f")
