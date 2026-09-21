"""AnuInvoice - turn a customer message into a reviewed, priced invoice.

Customer message -> AI extraction -> price lookup -> invoice -> human review/approval -> export.
Run:  streamlit run app.py
"""
from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from smartinvoice import (
    ApprovalError, Catalog, CatalogError, Customer, InvoiceSettings, approve, build_invoice,
    extract, load_catalog, rows_from_request, to_html, to_pdf, whatsapp_link,
)
from smartinvoice.extractor import PROVIDERS
from smartinvoice.invoice import fmt_money, fmt_qty, new_invoice_number

ROOT = Path(__file__).parent
DEFAULT_CSV = ROOT / "data" / "services.csv"

SAMPLES = {
    "🧑‍💼 Clean request": (
        "Hi, I'm Rahul Sharma from Acme Traders (rahul@acme.in, +91 98765 43210). We need 3 logo designs, "
        "hosting for 2 years and an SSL certificate. Please deliver by Friday.\nThanks"
    ),
    "🗣️ Messy chat": (
        "hey need an ecommerce website + chatbot, also 10 blog posts and a drone photography shoot. "
        "budget around Rs. 1,50,000. - Vikram (98110 22334)"
    ),
    "📧 Formal email": (
        "Hello team, this is Anita Desai from Bloom Cafe. Can you please send me an invoice for a 5 page website, "
        "1 year hosting and domain name? My email is anita@bloomcafe.in. Need it by next Monday."
    ),
    "🔤 Typos": "Regards from Neha (neha@kapoor.co). i want logoo desing, hostng and 4 hours of consultng",
}

st.set_page_config(page_title="AnuInvoice · AI invoice builder", page_icon="🧾", layout="wide")


def secret(name: str) -> str:
    try:
        return str(st.secrets[name])
    except Exception:
        return os.getenv(name, "")


def blank(v):
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else v


# ------------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("⚙️ Settings")

    st.subheader("1 · AI extractor")
    labels = {"rules": "Offline rule-based (no API key)", **{k: v["label"] for k, v in PROVIDERS.items()}}
    provider = st.selectbox("Engine", list(labels), format_func=labels.get, index=0)
    api_key = model = ""
    if provider == "rules":
        st.caption("💡 The offline parser handles common phrasing. For free-form messages pick an AI engine "
                   "(Groq and Gemini offer free API keys) — the price list still controls every price.")
    if provider != "rules":
        api_key = st.text_input(f"{labels[provider]} API key", type="password",
                                value=secret(PROVIDERS[provider]["env"]), key=f"key_{provider}")
        model = st.text_input("Model", value=PROVIDERS[provider]["model"], key=f"model_{provider}")
        st.caption("Only the customer message and service *names* are sent. Prices never leave your machine.")

    st.subheader("2 · Price source")
    source = st.radio("Where are prices stored?", ["Bundled sample CSV", "Upload CSV / Excel", "Google Sheet / CSV URL"])
    url = upload = None
    if source == "Upload CSV / Excel":
        upload = st.file_uploader("Price list", type=["csv", "xlsx", "xls"])
    elif source == "Google Sheet / CSV URL":
        url = st.text_input("Sheet link (shared: 'Anyone with the link can view')", value=secret("PRICE_SHEET_URL"))
    reload_clicked = st.button("🔄 Reload prices")

    st.subheader("3 · Business & tax")
    with st.expander("Invoice settings", expanded=False):
        biz_name = st.text_input("Business name", "Pixel & Pine Studio")
        biz_addr = st.text_input("Address", "B-14, Sector 62, Noida, UP 201301")
        biz_mail = st.text_input("Billing email", "billing@pixelpine.in")
        biz_tax = st.text_input("GSTIN / Tax ID", "")
        currency = st.selectbox("Currency", ["INR", "USD", "EUR", "GBP"])
        tax_label = st.text_input("Tax label", "GST")
        tax_rate = st.number_input("Tax rate %", 0.0, 100.0, 18.0, 0.5)
        discount = st.number_input("Discount %", 0.0, 100.0, 0.0, 0.5)
        terms = st.number_input("Payment terms (days)", 0, 365, 15)
        pay_info = st.text_area("Payment instructions", "Bank transfer / UPI. Details available on request.", height=70)

settings = InvoiceSettings(
    business_name=biz_name, business_address=biz_addr, business_email=biz_mail, business_tax_id=biz_tax,
    currency=currency, tax_label=tax_label, tax_rate=Decimal(str(tax_rate)), discount_pct=Decimal(str(discount)),
    payment_terms_days=int(terms), payment_info=pay_info,
)

# ------------------------------------------------------------------- catalog


def read_catalog() -> Catalog:
    if source == "Upload CSV / Excel" and upload is not None:
        if upload.name.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(upload)
            return Catalog.from_rows(df.astype(object).where(pd.notna(df), None).to_dict("records"), upload.name)
        return load_catalog(upload.getvalue())
    if source == "Google Sheet / CSV URL" and url:
        return load_catalog(url)
    return load_catalog(DEFAULT_CSV)


sig = (source, url, upload.name if upload else None, upload.size if upload else None)
if reload_clicked or st.session_state.get("catalog_sig") != sig:
    try:
        st.session_state.catalog = read_catalog()
        st.session_state.catalog_sig = sig
    except Exception as exc:  # CatalogError, pandas errors, network...
        st.sidebar.error(f"Could not load prices: {exc}")
        if "catalog" not in st.session_state:
            st.session_state.catalog = load_catalog(DEFAULT_CSV)
        st.session_state.catalog_sig = sig
catalog: Catalog = st.session_state.catalog

with st.sidebar:
    st.success(f"{len(catalog.items)} services loaded")
    for w in catalog.warnings:
        st.warning(w)

# ---------------------------------------------------------------------- main
st.session_state.setdefault("history", {})
st.title("🧾 AnuInvoice")
st.caption("Customer message → AI extraction → price lookup → invoice → **human approval** → export. "
           "Prices come only from your price list; the AI never invents one.")

tab_build, tab_dash = st.tabs(["Invoice builder", "📊 Dashboard"])

with tab_build:
    st.subheader("1 · Paste the customer's message")
    cols = st.columns(len(SAMPLES))
    for col, (label, text) in zip(cols, SAMPLES.items()):
        col.button(label, on_click=lambda t=text: st.session_state.update(msg=t))
    msg = st.text_area("Customer message", key="msg", height=150,
                       placeholder="e.g. Hi, I'm Priya. I need a landing page and 2 hours of consulting…")

    if st.button("✨ Extract & price", type="primary", disabled=not msg.strip()):
        with st.spinner("Reading the message…"):
            req = extract(msg, catalog, provider, api_key, model or None)
        gen = st.session_state.get("gen", 0) + 1
        st.session_state.update(
            gen=gen, req=req, approval=None,
            draft={"rows": rows_from_request(req, catalog), "number": new_invoice_number(),
                   "customer": {"name": req.customer_name, "email": req.email,
                                "phone": req.phone, "company": req.company},
                   "notes": req.notes},
        )

    draft = st.session_state.get("draft")
    if not draft:
        st.info("Pick a sample message above or paste your own, then click **Extract & price**.")
        st.stop()

    req = st.session_state.req
    gen = st.session_state.gen
    with st.expander(f"🔎 What the extractor found  ·  method: `{req.method}`", expanded=False):
        for w in req.warnings:
            st.warning(w)
        st.json(json.loads(json.dumps(asdict(req), default=str)))

    # ---------------------------------------------------------------- review
    st.subheader("2 · Review & correct")
    c1, c2, c3, c4 = st.columns(4)
    cust = Customer(
        c1.text_input("Customer name", draft["customer"]["name"], key=f"n{gen}"),
        c2.text_input("Email", draft["customer"]["email"], key=f"e{gen}"),
        c3.text_input("Phone / WhatsApp", draft["customer"]["phone"], key=f"p{gen}"),
        c4.text_input("Company", draft["customer"]["company"], key=f"c{gen}"),
    )
    notes = st.text_input("Notes shown on the invoice", draft["notes"], key=f"notes{gen}")

    st.markdown("**Line items** — pick the right service for any flagged row. Prices are read from the price list and can't be typed in.")
    df = pd.DataFrame(draft["rows"], columns=["requested", "service", "quantity", "suggestion"])
    df["service"] = df["service"].replace("", None)
    edited = st.data_editor(
        df, key=f"rows{gen}", num_rows="dynamic", hide_index=True, disabled=["suggestion"],
        column_config={
            "requested": st.column_config.TextColumn("Customer asked for"),
            "service": st.column_config.SelectboxColumn("Matched service", options=catalog.names, required=False),
            "quantity": st.column_config.NumberColumn("Qty", min_value=0.0, step=0.5, format="%g"),
            "suggestion": st.column_config.TextColumn("AI/text suggestion"),
        },
    )
    rows = [
        {"requested": blank(r["requested"]), "service": blank(r["service"]),
         "quantity": None if pd.isna(r["quantity"]) else r["quantity"]}
        for r in edited.to_dict("records")
    ]
    inv = build_invoice(cust, rows, catalog, settings, notes, draft["number"],
                        approval=st.session_state.get("approval"))

    for issue in inv.blocking_issues():
        st.error("🚫 " + issue)
    for w in inv.warnings():
        st.warning("⚠️ " + w)
    if inv.approval_is_stale:
        st.warning("✏️ The invoice changed after approval. Please review and approve again.")

    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Invoice preview**")
        components.html(to_html(inv), height=720, scrolling=True)
    with right:
        t = inv.totals
        st.markdown("**Totals**")
        m1, m2 = st.columns(2)
        m1.metric("Subtotal", fmt_money(t.subtotal, currency))
        m2.metric(f"{tax_label} ({tax_rate:g}%)", fmt_money(t.tax, currency))
        st.metric("Total due", fmt_money(t.total, currency))
        priced = sum(l.resolved for l in inv.lines)
        st.progress(priced / max(len(inv.lines), 1), text=f"{priced}/{len(inv.lines)} lines priced")

        # ------------------------------------------------------------ approval
        st.subheader("3 · Approve")
        reviewer = st.text_input("Reviewer name", key="reviewer", placeholder="Who is approving this invoice?")
        a, b = st.columns(2)
        if a.button("✅ Approve invoice", type="primary", disabled=bool(inv.blocking_issues()) or inv.is_approved):
            try:
                st.session_state.approval = approve(inv, reviewer)
                st.session_state.history[draft["number"]] = {
                    "invoice": draft["number"], "customer": cust.name or cust.company or "-",
                    "total": float(t.total), "currency": currency, "approved_by": reviewer.strip(),
                    "lines": [(l.name, float(l.quantity), float(l.amount)) for l in inv.lines if l.resolved],
                }
                st.rerun()
            except ApprovalError as exc:
                st.error(str(exc))
        if b.button("↩️ Revoke approval", disabled=not inv.is_approved):
            st.session_state.approval = None
            st.session_state.history.pop(draft["number"], None)
            st.rerun()

        # -------------------------------------------------------------- export
        st.subheader("4 · Export")
        if inv.is_approved:
            st.success(f"Approved by {inv.approval.reviewer} — exports unlocked.")
            try:
                st.download_button("⬇️ Download PDF", to_pdf(inv), f"{inv.number}.pdf", "application/pdf")
            except ImportError as exc:
                st.error(f"PDF export needs a missing package ({exc.name}). Run `python -m pip install -r requirements.txt` "
                         "in the same environment as Streamlit, then restart the app.")
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["SKU", "Service", "Qty", "Unit", "Rate", "Amount"])
            for l in inv.lines:
                if l.resolved:
                    w.writerow([l.sku, l.name, fmt_qty(l.quantity), l.unit, l.unit_price, l.amount])
            e1, e2 = st.columns(2)
            e1.download_button("CSV", buf.getvalue(), f"{inv.number}.csv", "text/csv")
            e2.download_button("JSON", json.dumps(inv.to_dict(), indent=2), f"{inv.number}.json",
                               "application/json")
            st.link_button("💬 Share on WhatsApp", whatsapp_link(inv))
        else:
            st.info("🔒 PDF / CSV / JSON / WhatsApp unlock after approval.")

with tab_dash:
    hist = list(st.session_state.history.values())
    if not hist:
        st.info("Approved invoices from this session appear here.")
    else:
        st.subheader("Session dashboard")
        k1, k2, k3 = st.columns(3)
        k1.metric("Approved invoices", len(hist))
        by_cur: dict[str, float] = {}
        for h in hist:
            by_cur[h["currency"]] = by_cur.get(h["currency"], 0) + h["total"]
        k2.metric("Billed", " · ".join(fmt_money(Decimal(str(v)), c) for c, v in by_cur.items()))
        k3.metric("Average invoice", fmt_money(Decimal(str(sum(h["total"] for h in hist) / len(hist))), hist[0]["currency"]))
        st.dataframe(pd.DataFrame([{k: v for k, v in h.items() if k != "lines"} for h in hist]), hide_index=True)
        rev: dict[str, float] = {}
        for h in hist:
            for name, _, amt in h["lines"]:
                rev[name] = rev.get(name, 0) + amt
        st.markdown("**Revenue by service (pre-tax)**")
        st.bar_chart(pd.Series(rev).sort_values(ascending=False))
