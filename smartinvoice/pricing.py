"""Price catalog loading and deterministic service matching.

The catalog is the *only* source of prices. Nothing in this module (or anywhere
else in the app) lets a language model supply a number that ends up on an invoice.
"""
from __future__ import annotations

import csv
import io
import re
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Mapping

AUTO_THRESHOLD = 0.85      # >= this: auto-match (still visible to the reviewer)
SUGGEST_THRESHOLD = 0.60   # >= this (and < AUTO): suggestion only, human must confirm
PREFILL_THRESHOLD = 0.75   # unambiguous suggestions >= this are pre-selected (flagged for double-check)
AMBIGUITY_MARGIN = 0.05    # two different services this close => ambiguous

STOPWORDS = {
    "a", "an", "the", "of", "for", "and", "my", "our", "your", "some", "with", "to",
    "per", "new", "please", "i", "we", "on", "in", "any", "also", "want", "need",
}


class CatalogError(Exception):
    """Raised when a price source cannot be loaded or is unusable."""


@dataclass(frozen=True)
class ServiceItem:
    sku: str
    name: str
    unit: str
    price: Decimal
    aliases: tuple[str, ...] = ()
    category: str = ""
    description: str = ""


@dataclass
class MatchResult:
    item: ServiceItem | None
    score: float
    status: str  # "auto" | "suggest" | "ambiguous" | "none"
    candidates: list[tuple[ServiceItem, float]] = field(default_factory=list)


# --------------------------------------------------------------------------- text


def _stem(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def normalize(text: str) -> list[str]:
    t = (text or "").lower().replace("&", " and ")
    t = re.sub(r"\b([a-z])-(?=[a-z])", r"\1", t)  # e-commerce -> ecommerce, e-mail -> email
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return [_stem(w) for w in t.split() if w not in STOPWORDS]


def _tok_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and a[0] == b[0]:  # typos keep the first letter
        ratio = SequenceMatcher(None, a, b).ratio()
        return ratio >= (0.86 if min(len(a), len(b)) == 4 else 0.82)
    return False


def _dice(q: list[str], c: list[str]) -> float:
    if not q or not c:
        return 0.0
    remaining = list(c)
    hits = 0
    for tok in q:
        for i, other in enumerate(remaining):
            if _tok_eq(tok, other):
                hits += 1
                remaining.pop(i)
                break
    return 2 * hits / (len(q) + len(c))


def _score(q: list[str], c: list[str]) -> float:
    if not q or not c:
        return 0.0
    if sorted(q) == sorted(c):
        return 1.0
    seq = SequenceMatcher(None, " ".join(q), " ".join(c)).ratio()
    return round(max(_dice(q, c) * 0.98, seq * 0.95), 4)


# ------------------------------------------------------------------------ catalog


class Catalog:
    def __init__(self, items: list[ServiceItem], warnings: list[str] | None = None, source: str = ""):
        if not items:
            raise CatalogError("The price source contains no usable rows.")
        self.items = items
        self.warnings = warnings or []
        self.source = source
        self._by_key: dict[str, ServiceItem] = {}
        for it in items:
            self._by_key[it.name.lower()] = it
        for it in items:
            self._by_key.setdefault(it.sku.lower(), it)
        self._tokens = {
            it.sku: [normalize(x) for x in (it.name, *it.aliases)] for it in items
        }

    @property
    def names(self) -> list[str]:
        return [i.name for i in self.items]

    def get(self, name_or_sku: str) -> ServiceItem | None:
        return self._by_key.get((name_or_sku or "").strip().lower())

    def score(self, query: str, item: ServiceItem) -> float:
        q = normalize(query)
        return max((_score(q, c) for c in self._tokens[item.sku]), default=0.0)

    def match(self, query: str, top_k: int = 3) -> MatchResult:
        q = normalize(query)
        if not q:
            return MatchResult(None, 0.0, "none")
        scored = sorted(
            ((it, max(_score(q, c) for c in self._tokens[it.sku])) for it in self.items),
            key=lambda p: p[1],
            reverse=True,
        )
        cands = [(it, s) for it, s in scored[:top_k] if s > 0]
        if not cands or cands[0][1] < SUGGEST_THRESHOLD:
            return MatchResult(None, cands[0][1] if cands else 0.0, "none", cands)
        best, best_score = cands[0]
        if len(cands) > 1 and best_score - cands[1][1] < AMBIGUITY_MARGIN and cands[1][1] >= SUGGEST_THRESHOLD:
            return MatchResult(best, best_score, "ambiguous", cands)
        status = "auto" if best_score >= AUTO_THRESHOLD else "suggest"
        return MatchResult(best, best_score, status, cands)

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping], source: str = "") -> "Catalog":
        items: list[ServiceItem] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for n, raw in enumerate(rows, start=2):  # row 1 is the header
            row = {_canon_header(k): v for k, v in raw.items() if k is not None}
            name = _s(row.get("name"))
            if not name:
                continue
            price = _parse_price(row.get("price"))
            if price is None:
                warnings.append(f"Row {n} ('{name}') skipped: missing or invalid price.")
                continue
            if name.lower() in seen:
                warnings.append(f"Row {n} ('{name}') skipped: duplicate service name.")
                continue
            seen.add(name.lower())
            sku = _s(row.get("sku")) or re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")
            aliases = tuple(a.strip() for a in re.split(r"[;|]", _s(row.get("aliases"))) if a.strip())
            items.append(ServiceItem(sku, name, _s(row.get("unit")) or "unit", price, aliases,
                                     _s(row.get("category")), _s(row.get("description"))))
        return cls(items, warnings, source)


_HEADERS = {
    "sku": {"sku", "code", "id", "item code", "item id"},
    "name": {"service", "name", "item", "service name", "product", "item name"},
    "unit": {"unit", "uom", "per", "unit of measure"},
    "price": {"price", "unit price", "unit_price", "rate", "cost", "unit cost"},
    "aliases": {"aliases", "alias", "keywords", "synonyms", "also known as"},
    "description": {"description", "details", "desc"},
    "category": {"category", "type", "group"},
}


def _canon_header(h: str) -> str:
    key = (h or "").strip().lower()
    for canon, names in _HEADERS.items():
        if key in names:
            return canon
    return key


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _parse_price(v) -> Decimal | None:
    s = re.sub(r"[^\d.\-]", "", _s(v))
    if not s or s in {".", "-"}:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d.quantize(Decimal("0.01")) if d >= 0 else None


def normalize_sheet_url(url: str) -> str:
    """Turn a normal Google Sheets link into its CSV export URL."""
    m = re.search(r"docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if not m or "output=csv" in url or "format=csv" in url or "/pub" in url:
        return url
    gid = re.search(r"[#&?]gid=(\d+)", url)
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv&gid={gid.group(1) if gid else 0}"


def load_catalog(source: str | Path | bytes | io.IOBase) -> Catalog:
    """Load a catalog from a CSV path, a CSV / Google Sheet URL, raw bytes or a file object."""
    label = "upload"
    if isinstance(source, (bytes, bytearray)):
        text = bytes(source).decode("utf-8-sig", errors="replace")
    elif hasattr(source, "read"):
        data = source.read()
        text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    else:
        src = str(source).strip()
        label = src
        if re.match(r"https?://", src):
            text = _fetch(normalize_sheet_url(src))
        else:
            path = Path(src)
            if not path.exists():
                raise CatalogError(f"Price file not found: {src}")
            text = path.read_text(encoding="utf-8-sig")
    if text.lstrip().lower().startswith(("<!doctype", "<html")):
        raise CatalogError("The URL returned a web page, not CSV. Publish the sheet ('Anyone with the link can view').")
    return Catalog.from_rows(csv.DictReader(io.StringIO(text)), source=label)


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "anuinvoice/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8-sig", errors="replace")
    except Exception as exc:  # network errors are user-facing
        raise CatalogError(f"Could not fetch price sheet: {exc}") from exc
