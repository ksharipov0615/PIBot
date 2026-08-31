"""
Price Indication Bot — v4
Workflows:
  WSF — Sales Representative → Admin/User C (availability check)
           └─ Not available  → Representative (done)
           └─ Available      → User L (logistics) → Admin/User C (pricing) → Representative

  MOP/NP — Sales Representative → Admin (availability check)
           └─ Not available  → Representative (done)
           └─ Available      → User L (logistics) → Admin (pricing) → Representative

Changes over v3:
  - New /newrequest step 0: rep selects WSF or MOP/NP workflow.
  - MOP/NP representative flow: products WSt/WFn/PSt/PGr/NP; packaging 1000 kg/bulkcntr/bulk/900 kg/50 kg;
    pallets Yes/No (only active for bagged packaging: 1000 kg/900 kg/50 kg).
  - Both workflows share the same User L and User C conversation handlers;
    the 'workflow' column drives label differences in recaps.
  - l_recap shows a Total/mt line (Handling + THC + Freight + Extras) for both workflows;
    for WSF this line is now labelled Subtotal/mt, followed by a Total/mt line that adds
    whichever of STP tariff/mt or Novo tariff/mt applies (see below).
  - New DB column 'workflow' (TEXT, default 'WSF') with non-destructive migration.
  - New conversation state R_WORKFLOW added to representative flow.

WSF-only additions:
  - After User C confirms availability (Yes), User C now enters STP tariff/mt,
    Novo tariff/mt, and Packaging/mt (all numeric, all may be 0) before the
    request is forwarded to User L. New DB columns 'stp_tariff' / 'novo_tariff' /
    'packaging_mt' (TEXT), non-destructive migration. MOP/NP is unaffected —
    Admin's availability confirmation still forwards immediately.
  - The recap forwarded to User L when availability is confirmed now includes
    STP tariff/mt, Novo tariff/mt, and Packaging/mt as entered by User C.
  - User L's POL step is now a selector (STP / Novo buttons) instead of free text.
    User L's choice determines which tariff/mt is added. The recap forwarded
    back to User C (and shown in l_recap) now sums Subtotal/mt + the selected
    STP/Novo tariff/mt + Packaging/mt into Total/mt. MOP/NP's POL step remains
    free text, unaffected.
"""

import html
import logging
import sqlite3
import os
import json
import base64
import asyncio
import csv
import io
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

load_dotenv()

# ── Environment validation ─────────────────────────────────────────────────────
def _require_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Missing required environment variable: {key}\n"
            f"Add it to your .env file and restart."
        )
    return val


BOT_TOKEN = _require_env("BOT_TOKEN")
USER_L_ID = int(_require_env("USER_L_ID"))
USER_C_ID = int(_require_env("USER_C_ID"))
ADMIN_ID  = int(_require_env("ADMIN_ID"))

# Whitelist of authorised Sales Rep Telegram IDs.
# In .env:  REP_IDS=111111111,222222222,333333333
# Leave blank (REP_IDS=) to allow no reps (useful while testing).
_rep_ids_raw = os.getenv("REP_IDS", "")
REP_IDS: set[int] = {
    int(x.strip())
    for x in _rep_ids_raw.split(",")
    if x.strip().lstrip("-").isdigit()
}

# Combined set of every ID that may interact with the bot
KNOWN_IDS: set[int] = REP_IDS | {USER_L_ID, USER_C_ID, ADMIN_ID}

# ── Email (Gmail API) configuration ─────────────────────────────────────────────
# Optional feature: if GCP_TOKEN_JSON / GCP_CREDENTIALS_JSON are not set, email
# sending is silently disabled and the bot behaves exactly as before.
#
# On Railway (or in .env for local runs):
#   GCP_TOKEN_JSON=<contents of the OAuth token.json produced by the one-time
#                   local auth flow — refresh_token, client info, etc.>
#   GCP_CREDENTIALS_JSON=<contents of the OAuth client secrets JSON from
#                   Google Cloud Console — used only to refresh an expired token>
#   ADMIN_EMAIL=admin@yourdomain.com          # CC'd on every outgoing email
#   USER_C_EMAIL=userc@yourdomain.com         # CC'd on WSF price-recap emails
#   REP_EMAILS=111111111:rep1@yourdomain.com,222222222:rep2@yourdomain.com
#
# Scope required when generating GCP_TOKEN_JSON: gmail.send
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
GCP_TOKEN_JSON = os.getenv("GCP_TOKEN_JSON", "").strip()
GCP_CREDENTIALS_JSON = os.getenv("GCP_CREDENTIALS_JSON", "").strip()
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()
USER_C_EMAIL = os.getenv("USER_C_EMAIL", "").strip()
EMAIL_ENABLED = bool(GCP_TOKEN_JSON)

_rep_emails_raw = os.getenv("REP_EMAILS", "")
REP_EMAILS: dict[int, str] = {}
for _pair in _rep_emails_raw.split(","):
    _pair = _pair.strip()
    if not _pair or ":" not in _pair:
        continue
    _tg_id_str, _addr = _pair.split(":", 1)
    _tg_id_str = _tg_id_str.strip()
    _addr = _addr.strip()
    if _tg_id_str.isdigit() and _addr:
        REP_EMAILS[int(_tg_id_str)] = _addr

if not EMAIL_ENABLED:
    logging.warning(
        "Email sending disabled (GCP_TOKEN_JSON not set in .env / Railway). "
        "The bot will run normally without sending recap emails."
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "requests.db")

# ── ConversationHandler states ─────────────────────────────────────────────────
# Representative flow (0–8): state 0 = workflow selector, 1–8 = product→confirm
R_WORKFLOW, R_PRODUCT, R_PACKAGING, R_LABELS, R_PALLETS, R_VOLUME, R_POD, R_BASIS, R_COMMENTS, R_CONFIRM = range(10)

# User L flow (10–20)
(
    L_SERVICE, L_COPY_CHOICE, L_COPY_SELECT, L_COPY_REVIEW,
    L_POL, L_TERMINAL, L_LINE, L_EQUIPMENT,
    L_HANDLING, L_THC, L_FREIGHT, L_EXTRAS, L_LABELS_COST, L_MAXPAYLOAD, L_COMMENTS,
) = range(10, 25)

# User C flow (30–39)
(
    C_AVAILABILITY, C_STP_TARIFF, C_NOVO_TARIFF, C_PACKAGING_MT,
    C_VOLUME, C_PRICE, C_ETD, C_VALIDITY, C_COMMENTS,
) = range(30, 39)
C_CONFIRM_NO_SERVICE = 39

# /edit flow (40–42): group picker → field picker → new-value entry
EDIT_GROUP, EDIT_FIELD, EDIT_VALUE = range(40, 43)

# ── Keyboard option lists ──────────────────────────────────────────────────────
WORKFLOWS = ["WSF", "MOP/NP"]

# WSF product/packaging/pallets
PRODUCTS   = [
    "SNI", "SNA", "PNA", "NKS44", "NKS43", "NKSM", "UMP", "FeedU", "TechU",
    "CNC", "CNCM", "CNCB", "MAP", "MKP", "NPK11", "NPK13", "NPK15", "NPK18",
    "NPK19", "NPK20", "NPK3", "NPK12", "NPK157", "AD5", "AD13", "AD18", "AD20",
]
PACKAGINGS = ["22.7 kg", "25 kg", "50 kg", "500 kg", "800 kg", "850 kg", "900 kg", "1000 kg"]
PALLETS    = ["Default", "No", "1L", "2L"]
LABELS     = ["Yes", "No"]  # WSF only — asked right after Packaging

# MOP/NP product/packaging/pallets
MOP_PRODUCTS   = ["WSt", "WFn", "PSt", "PGr", "NP"]
MOP_PACKAGINGS = ["1000 kg", "bulkcntr", "bulk", "900 kg", "50 kg"]
MOP_PALLETS    = ["Yes", "No"]   # only shown for bagged packaging: 1000 kg/900 kg/50 kg

BASIS    = ["CIF", "CFR", "DAP", "CPT", "CIP", "FOB", "FCA"]
SKIP_BTN = "— Skip —"

# Shared filter for every conversation-step MessageHandler: plain text,
# not a command, and NOT an edited message. Without the edited-message
# exclusion, a user editing an earlier message mid-conversation delivers
# an update where update.message is None (it's update.edited_message
# instead) — every handler below unconditionally accesses
# update.message.text / update.message.reply_text(...), so that would
# raise AttributeError and silently drop the update.
TEXT_FILTER = filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE


def _chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def kb(options: list[str], cols: int = 3) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        list(_chunked(options, cols)),
        one_time_keyboard=True,
        resize_keyboard=True,
    )


def kb_skip() -> ReplyKeyboardMarkup:
    """Single-button keyboard with just the Skip option."""
    return ReplyKeyboardMarkup([[SKIP_BTN]], one_time_keyboard=True, resize_keyboard=True)


CLEAR_BTN = "— Clear —"


def kb_clear() -> ReplyKeyboardMarkup:
    """Single-button keyboard used when editing an optional free-text field
    down to empty (distinct from SKIP_BTN, which means 'wasn't answered
    yet' rather than 'deliberately blanked out during a correction')."""
    return ReplyKeyboardMarkup([[CLEAR_BTN]], one_time_keyboard=True, resize_keyboard=True)


# ── Utility helpers ────────────────────────────────────────────────────────────
def e(value) -> str:
    """HTML-escape any user-supplied value before embedding in messages."""
    return html.escape(str(value or ""))


def is_valid_number(text: str) -> bool:
    """Accept positive integers and decimals (e.g. 500, 22.5, 12.50)."""
    try:
        return float(text.strip().replace(",", ".")) > 0
    except ValueError:
        return False


def is_valid_number_or_zero(text: str) -> bool:
    """Accept zero or positive numbers (e.g. 0, 0.00, 12.50). Used for
    fields that may legitimately be zero (Handling/mt, THC/mt, Extras/mt)."""
    try:
        return float(text.strip().replace(",", ".")) >= 0
    except ValueError:
        return False


def now_utc() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def uid_of(update: Update) -> int:
    return update.effective_user.id


# ── Database ───────────────────────────────────────────────────────────────────
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        # WAL mode lets reads proceed without blocking on a concurrent
        # writer (and vice versa), which matters here since every DB call
        # opens its own short-lived connection — the default rollback-
        # journal mode serializes those far more aggressively and is more
        # prone to "database is locked" under overlapping requests.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                rep_id              INTEGER,
                rep_name            TEXT,
                status              TEXT DEFAULT 'pending_L',
                workflow            TEXT DEFAULT 'WSF',
                -- Rep fields
                product             TEXT,
                packaging           TEXT,
                labels              TEXT,
                pallets             TEXT,
                volume              TEXT,
                pod                 TEXT,
                basis               TEXT,
                rep_comments        TEXT,
                -- L fields
                service             TEXT,
                pol                 TEXT,
                terminal            TEXT,
                line                TEXT,
                equipment           TEXT,
                handling            TEXT,
                thc                 TEXT,
                freight             TEXT,
                extras              TEXT,
                labels_cost         TEXT,
                max_payload         TEXT,
                l_comments          TEXT,
                -- C fields
                availability        TEXT,
                stp_tariff          TEXT,
                novo_tariff         TEXT,
                packaging_mt        TEXT,
                c_volume            TEXT,
                price               TEXT,
                etd                 TEXT,
                validity            TEXT,
                c_comments          TEXT,
                -- Timestamps
                created_at          TEXT,
                c_avail_at          TEXT,
                l_answered_at       TEXT,
                c_answered_at       TEXT,
                -- Reminder throttle (one reminder per 24 h per stage)
                last_reminded_l_at  TEXT,
                last_reminded_c_at  TEXT,
                -- Message IDs (kept for reference)
                l_msg_id            INTEGER,
                c_msg_id            INTEGER
            )
        """)
        # Non-destructive migration: add new columns to existing databases
        existing_cols = {row[1] for row in con.execute("PRAGMA table_info(requests)")}
        for col, typedef in [
            ("last_reminded_l_at", "TEXT"),
            ("last_reminded_c_at", "TEXT"),
            ("c_avail_at",         "TEXT"),
            ("workflow",           "TEXT DEFAULT 'WSF'"),
            ("stp_tariff",         "TEXT"),
            ("novo_tariff",        "TEXT"),
            ("packaging_mt",       "TEXT"),
            ("labels",             "TEXT"),
            ("labels_cost",        "TEXT"),
        ]:
            if col not in existing_cols:
                con.execute(f"ALTER TABLE requests ADD COLUMN {col} {typedef}")
        con.commit()


def get_request(req_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    return dict(row) if row else None


def get_recent_l_requests(workflow: str, exclude_id: int, limit: int = 8) -> list[dict]:
    """Most recent requests User L has already filled logistics in, restricted
    to the same workflow (field sets differ between WSF and MOP/NP) — feeds
    the 'copy from previous request' option in the User L flow."""
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM requests "
            "WHERE workflow=? AND id != ? AND service='Yes' "
            "AND l_answered_at IS NOT NULL AND l_answered_at != '' "
            "ORDER BY l_answered_at DESC LIMIT ?",
            (workflow, exclude_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def update_request(req_id: int, **kwargs) -> None:
    if not kwargs:
        return
    cols = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [req_id]
    with sqlite3.connect(DB_PATH) as con:
        con.execute(f"UPDATE requests SET {cols} WHERE id=?", vals)
        con.commit()


def insert_request(**kwargs) -> int:
    """Insert a new row and return its auto-generated id."""
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join("?" for _ in kwargs)
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            f"INSERT INTO requests ({cols}) VALUES ({placeholders})",
            list(kwargs.values()),
        )
        con.commit()
        return cur.lastrowid


# ── Message formatters (HTML) ──────────────────────────────────────────────────
def rep_recap(r: dict) -> str:
    workflow = r.get("workflow") or "WSF"
    pallets_line = ""
    # For MOP/NP, pallets are only set when packaging is 1000 kg; omit line if blank
    if workflow == "WSF" or r.get("pallets"):
        pallets_line = f"🪵 Pallets: <code>{e(r['pallets'])}</code>\n"
    return (
        f"📋 <b>Price Request #{r['id']} [{e(workflow)}]</b>\n"
        f"👤 From: {e(r['rep_name'])}\n"
        f"─────────────────────\n"
        f"🧪 Product: <code>{e(r['product'])}</code>\n"
        f"📦 Packaging: <code>{e(r['packaging'])}</code>\n"
        f"{('🏷️ Labels: <code>' + e(r['labels']) + '</code>\n') if workflow == 'WSF' else ''}"
        f"{pallets_line}"
        f"⚖️ Volume: <code>{e(r['volume'])} mt</code>\n"
        f"📍 POD: <code>{e(r['pod'])}</code>\n"
        f"🚢 Basis: <code>{e(r['basis'])}</code>\n"
        f"💬 Comments: {e(r['rep_comments']) or '—'}\n"
        f"🕐 Filed: {e(r['created_at'])}"
    )


def _total_per_mt(r: dict) -> str:
    """Return the sum of Handling + THC + Freight + Extras + Labels cost as a
    formatted string, or '—' if a required component is missing / non-numeric.
    Labels cost is optional (blank for MOP/NP and for WSF requests where the
    rep didn't request Labels) and contributes 0 when absent."""
    try:
        total = sum(
            float(str(r.get(k) or "").replace(",", "."))
            for k in ("handling", "thc", "freight", "extras")
        )
        labels_cost_raw = str(r.get("labels_cost") or "").replace(",", ".")
        if labels_cost_raw:
            total += float(labels_cost_raw)
        # Format: drop trailing zeros but keep up to 2 decimal places
        return f"{total:.2f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return "—"


def _wsf_grand_total_per_mt(r: dict, subtotal: str) -> str:
    """WSF only: Subtotal/mt + whichever of STP tariff/mt or Novo tariff/mt
    applies (based on User L's POL selection) + Packaging/mt entered by
    User C. Returns '—' if the subtotal, the selected tariff, or the
    packaging cost isn't available/numeric."""
    if subtotal == "—":
        return "—"
    pol = r.get("pol")
    tariff_key = {"STP": "stp_tariff", "Novo": "novo_tariff"}.get(pol)
    if tariff_key is None:
        return "—"
    try:
        tariff_val = float(str(r.get(tariff_key) or "").replace(",", "."))
        packaging_val = float(str(r.get("packaging_mt") or "").replace(",", "."))
        grand_total = float(subtotal) + tariff_val + packaging_val
        return f"{grand_total:.2f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return "—"


def l_recap(r: dict) -> str:
    base = rep_recap(r)
    workflow = r.get("workflow") or "WSF"
    if r.get("service") == "Yes":
        subtotal = _total_per_mt(r)
        show_labels_cost = workflow == "WSF" and r.get("labels") == "Yes"
        labels_cost_line = (
            f"Labels cost/mt: <code>{e(r.get('labels_cost') or '0')}</code>\n" if show_labels_cost else ""
        )
        if workflow == "WSF":
            grand_total = _wsf_grand_total_per_mt(r, subtotal)
            total_lines = (
                f"<b>Subtotal/mt: <code>{e(subtotal)}</code></b>\n"
                f"Packaging/mt: <code>{e(r['packaging_mt'])}</code>\n"
                f"<b>Total/mt: <code>{e(grand_total)}</code></b>\n"
            )
        else:
            total_lines = f"<b>Total/mt: <code>{e(subtotal)}</code></b>\n"
        logistics = (
            f"\n─────────────────────\n"
            f"🚚 <b>Logistics (L)</b>\n"
            f"Service: Yes\n"
            f"POL: <code>{e(r['pol'])}</code>\n"
            f"Terminal: <code>{e(r['terminal']) or 'N/A'}</code>\n"
            f"Line: <code>{e(r['line']) or 'N/A'}</code>\n"
            f"Equipment: <code>{e(r['equipment']) or 'N/A'}</code>\n"
            f"Handling/mt: <code>{e(r['handling'])}</code>\n"
            f"THC/mt: <code>{e(r['thc'])}</code>\n"
            f"Freight/mt: <code>{e(r['freight'])}</code>\n"
            f"Extras/mt: <code>{e(r['extras'])}</code>\n"
            f"{labels_cost_line}"
            f"{total_lines}"
            f"Max payload: <code>{e(r['max_payload'])} mt</code>\n"
            f"Comments: {e(r['l_comments']) or '—'}"
        )
    else:
        logistics = "\n─────────────────────\n🚚 <b>Logistics (L)</b>\nService: No"
    return base + logistics


def c_answer_for_rep(r: dict) -> str:
    workflow = r.get("workflow") or "WSF"
    pallets_line = ""
    if workflow == "WSF" or r.get("pallets"):
        pallets_line = f"🪵 Pallets: <code>{e(r['pallets'])}</code>"
    lines = [
        f"✅ <b>Price Reply — Request #{r['id']} [{e(workflow)}]</b>\n",
        f"🧪 Product: <code>{e(r['product'])}</code>",
        f"📦 Packaging: <code>{e(r['packaging'])}</code>",
    ]
    if workflow == "WSF":
        lines.append(f"🏷️ Labels: <code>{e(r['labels'])}</code>")
    if pallets_line:
        lines.append(pallets_line)
    lines += [
        f"⚖️ Volume requested: <code>{e(r['volume'])} mt</code>",
        f"📍 POD: <code>{e(r['pod'])}</code>",
        f"🚢 Basis: <code>{e(r['basis'])}</code>",
    ]
    if r.get("service") == "No":
        lines.append("\n⛔ No logistics service available for this request.")
    else:
        if r.get("availability") == "Yes":
            lines += [
                "\n📊 <b>Pricing</b>",
                "Availability: ✅ Yes",
                f"Volume: <code>{e(r['c_volume'])} mt</code>",
                f"Price/mt: <code>{e(r['price'])} USD</code>",
                f"ETD: <code>{e(r['etd'])}</code>",
                f"Validity: <code>{e(r['validity'])}</code>",
                f"Comments: {e(r['c_comments']) or '—'}",
            ]

        else:
            lines.append("\n❌ Product not available at this time.")
    return "\n".join(lines)


def admin_stage_copy(stage: str, r: dict) -> str:
    workflow = r.get("workflow", "WSF")
    avail_label = (
        "🔔 <b>[ADMIN] Admin confirmed availability — forwarded to User L</b>"
        if workflow == "MOP/NP"
        else "🔔 <b>[ADMIN] User C confirmed availability — forwarded to User L</b>"
    )
    header = {
        "filed":   "🔔 <b>[ADMIN] New request filed</b>",
        "c_avail": avail_label,
        "l_done":  "🔔 <b>[ADMIN] User L completed recap</b>",
        "c_done":  "🔔 <b>[ADMIN] Price reply sent to representative</b>",
    }[stage]
    body = {
        "filed":   rep_recap(r),
        "c_avail": rep_recap(r),
        "l_done":  l_recap(r),
        "c_done":  c_answer_for_rep(r),
    }[stage]
    return header + "\n\n" + body


# ══════════════════════════════════════════════════════════════════════════════
# /edit — post-hoc correction of already-submitted fields
#
# Permission model:
#   - Rep may edit their own "rep" fields any time BEFORE the request is
#     'done' (i.e. before the final reply has already gone out).
#   - User L may edit their own "l" fields any time before 'done', once
#     they've actually submitted logistics (l_answered_at is set).
#   - User C may edit their own "c" fields (WSF only) any time before
#     'done', once they've actually submitted something.
#   - Admin has a blanket override: any field group, any request, at any
#     time — including after 'done' — which also covers Admin's own
#     MOP/NP pricing role. Editing a 'done' request re-sends the
#     corrected final reply to the rep instead of notifying a "holder".
#
# Whichever party is currently holding the request (per its status) gets
# a re-notification with the diff and a fresh recap; Admin gets an FYI
# copy of every edit it didn't itself make.
# ══════════════════════════════════════════════════════════════════════════════

EDIT_GROUP_LABELS = {
    "rep": "📋 Request details",
    "l":   "🚚 Logistics (User L)",
    "c":   "📊 Pricing (User C)",
}

# Comment fields are offered for editing even when still blank (adding a
# comment that was originally skipped is a legitimate correction); every
# other field is only offered once it actually holds a value.
_ALWAYS_EDITABLE_EVEN_IF_BLANK = {"rep_comments", "l_comments", "c_comments"}

# field key -> (group, display label, kind, options-getter)
# kind is one of: enum | num_pos | num_nonneg | date | text | text_optional
# 'pol' is resolved specially (enum for WSF, free text for MOP/NP) — see
# _resolve_field_kind — so its static kind/options here are unused placeholders.
FIELD_DEFS: dict[str, dict] = {
    "product":      {"group": "rep", "label": "Product",       "kind": "enum", "options": lambda r: (PRODUCTS if (r.get("workflow") or "WSF") == "WSF" else MOP_PRODUCTS)},
    "packaging":    {"group": "rep", "label": "Packaging",     "kind": "enum", "options": lambda r: (PACKAGINGS if (r.get("workflow") or "WSF") == "WSF" else MOP_PACKAGINGS)},
    "labels":       {"group": "rep", "label": "Labels",        "kind": "enum", "options": lambda r: LABELS},
    "pallets":      {"group": "rep", "label": "Pallets",       "kind": "enum", "options": lambda r: (PALLETS if (r.get("workflow") or "WSF") == "WSF" else MOP_PALLETS)},
    "volume":       {"group": "rep", "label": "Volume (mt)",   "kind": "num_pos"},
    "pod":          {"group": "rep", "label": "POD",           "kind": "text"},
    "basis":        {"group": "rep", "label": "Basis",         "kind": "enum", "options": lambda r: BASIS},
    "rep_comments": {"group": "rep", "label": "Comments",      "kind": "text_optional"},

    "pol":          {"group": "l", "label": "POL",             "kind": "enum", "options": lambda r: ["STP", "Novo"]},
    "terminal":     {"group": "l", "label": "Terminal",        "kind": "text_optional"},
    "line":         {"group": "l", "label": "Line",            "kind": "text_optional"},
    "equipment":    {"group": "l", "label": "Equipment",       "kind": "text_optional"},
    "handling":     {"group": "l", "label": "Handling/mt",     "kind": "num_nonneg"},
    "thc":          {"group": "l", "label": "THC/mt",          "kind": "num_nonneg"},
    "freight":      {"group": "l", "label": "Freight/mt",      "kind": "num_pos"},
    "extras":       {"group": "l", "label": "Extras/mt",       "kind": "num_nonneg"},
    "labels_cost":  {"group": "l", "label": "Labels cost/mt",  "kind": "num_nonneg"},
    "max_payload":  {"group": "l", "label": "Max payload (mt)", "kind": "num_pos"},
    "l_comments":   {"group": "l", "label": "Comments",        "kind": "text_optional"},

    "stp_tariff":   {"group": "c", "label": "STP tariff/mt",   "kind": "num_nonneg"},
    "novo_tariff":  {"group": "c", "label": "Novo tariff/mt",  "kind": "num_nonneg"},
    "packaging_mt": {"group": "c", "label": "Packaging/mt",    "kind": "num_nonneg"},
    "c_volume":     {"group": "c", "label": "Volume (mt)",     "kind": "num_pos"},
    "price":        {"group": "c", "label": "Price/mt",        "kind": "num_pos"},
    "etd":          {"group": "c", "label": "ETD",             "kind": "date"},
    "validity":     {"group": "c", "label": "Validity",        "kind": "date"},
    "c_comments":   {"group": "c", "label": "Comments",        "kind": "text_optional"},
}


def _fields_for_group(group: str, r: dict) -> list[str]:
    """Fields in `group` that are actually eligible for editing on `r` —
    comments always qualify, everything else only once it has a value."""
    out = []
    for key, meta in FIELD_DEFS.items():
        if meta["group"] != group:
            continue
        if key in _ALWAYS_EDITABLE_EVEN_IF_BLANK or r.get(key):
            out.append(key)
    return out


def _edit_allowed_groups(uid: int, r: dict) -> list[str]:
    """Which field-groups `uid` may currently edit on request `r`."""
    status = r["status"]
    workflow = r.get("workflow") or "WSF"
    candidates: list[str] = []

    if uid == ADMIN_ID:
        # Blanket override — any group, any status.
        candidates = ["rep", "l", "c"]
    else:
        # Reps cannot self-edit their submitted fields — if a rep-side
        # correction is needed, Admin makes it via the override above.
        if uid == USER_L_ID and r.get("l_answered_at") and status != "done":
            candidates.append("l")
        if workflow == "WSF" and uid == USER_C_ID and status != "done" and (r.get("c_avail_at") or r.get("c_answered_at")):
            candidates.append("c")

    # A group only counts if it actually has at least one field to offer.
    return [g for g in candidates if _fields_for_group(g, r)]


def _resolve_field_kind(field: str, r: dict) -> tuple[str, list[str] | None]:
    """Resolve a field's (kind, options) against the request's actual
    workflow — needed because 'pol' behaves differently for WSF vs MOP/NP."""
    if field == "pol" and (r.get("workflow") or "WSF") != "WSF":
        return "text", None
    meta = FIELD_DEFS[field]
    kind = meta["kind"]
    if kind == "enum":
        return "enum", meta["options"](r)
    return kind, None


def _kb_for_kind(kind: str, options: list[str] | None) -> ReplyKeyboardMarkup:
    if kind == "enum":
        cols = 2 if len(options) <= 4 else (4 if len(options) > 12 else 3)
        return kb(options, cols)
    if kind == "text_optional":
        return kb_clear()
    return ReplyKeyboardRemove()


def _prompt_for_kind(kind: str, label: str) -> str:
    return {
        "enum":          f"Select new <b>{label}</b>:",
        "num_pos":       f"Enter new <b>{label}</b> (positive number):",
        "num_nonneg":    f"Enter new <b>{label}</b> (0 or greater):",
        "date":          f"Enter new <b>{label}</b> (format DD-MM-YYYY):",
        "text":          f"Enter new <b>{label}</b>:",
        "text_optional": f"Enter new <b>{label}</b> (or tap Clear to blank it):",
    }[kind]


def _validate_edit_value(kind: str, text: str, options: list[str] | None) -> tuple[bool, str]:
    """Returns (ok, normalized_value)."""
    if kind == "enum":
        return (text in options), text
    if kind == "num_pos":
        return is_valid_number(text), text
    if kind == "num_nonneg":
        return is_valid_number_or_zero(text), text
    if kind == "date":
        try:
            datetime.strptime(text, "%d-%m-%Y")
            return True, text
        except ValueError:
            return False, text
    if kind == "text":
        return bool(text), text
    if kind == "text_optional":
        return True, ("" if text == CLEAR_BTN else text)
    return False, text


def _role_name(uid: int, r: dict) -> str:
    if uid == r["rep_id"]:
        return "Representative"
    if uid == USER_L_ID:
        return "User L"
    if uid == USER_C_ID:
        return "User C"
    if uid == ADMIN_ID:
        return "Admin"
    return "Unknown"


async def _notify_edit(
    ctx: ContextTypes.DEFAULT_TYPE, r_new: dict, editor_uid: int, label: str, old_value: str, new_value: str,
) -> None:
    """Re-notify whoever's currently holding the request (or the rep, if
    Admin corrected an already-'done' request) with a diff + fresh recap,
    plus an FYI copy to Admin unless Admin was the editor or recipient."""
    workflow = r_new.get("workflow") or "WSF"
    status = r_new["status"]
    editor_role = _role_name(editor_uid, r_new)
    diff_line = (
        f"✏️ <b>Correction — Request #{r_new['id']}</b>\n"
        f"{e(editor_role)} updated <b>{e(label)}</b>: "
        f"<s>{e(old_value or '—')}</s> → <b>{e(new_value or '—')}</b>"
    )

    holder = None
    if status == "done":
        # Only Admin's override reaches here (non-admin edits are blocked
        # once 'done') — the rep already has the final reply, so resend it.
        try:
            await ctx.bot.send_message(
                r_new["rep_id"],
                "🔧 <b>Your price reply was corrected</b>\n\n" + diff_line + "\n\n" + c_answer_for_rep(r_new),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Failed to send corrected reply to rep for Request #%s", r_new["id"])
    else:
        if status == "pending_C_avail":
            holder = ADMIN_ID if workflow == "MOP/NP" else USER_C_ID
            recap = rep_recap(r_new)
        elif status == "pending_L":
            holder = USER_L_ID
            recap = rep_recap(r_new) + (
                _c_tariffs_and_packaging_lines(r_new) if workflow == "WSF" else ""
            )
        elif status == "pending_C":
            holder = ADMIN_ID if workflow == "MOP/NP" else USER_C_ID
            recap = l_recap(r_new) if r_new.get("service") == "Yes" else rep_recap(r_new)
        else:
            recap = rep_recap(r_new)

        if holder and holder != editor_uid:
            try:
                await ctx.bot.send_message(
                    holder, diff_line + "\n\nUpdated details:\n\n" + recap, parse_mode="HTML",
                )
            except Exception:
                logger.exception("Failed to notify holder for Request #%s", r_new["id"])

    if editor_uid != ADMIN_ID and holder != ADMIN_ID:
        try:
            await ctx.bot.send_message(ADMIN_ID, "🔔 [ADMIN] " + diff_line, parse_mode="HTML")
        except Exception:
            logger.exception("Failed to send Admin FYI for Request #%s", r_new["id"])


# ── Email (Gmail API) helpers ────────────────────────────────────────────────────
def _email_html(title: str, body_text: str) -> str:
    """Wrap a Telegram-HTML recap string (already using <b>/<code> tags) into a
    minimal standalone HTML email body. Newlines are converted to <br> since
    email clients don't render bare \\n as line breaks."""
    body_html = body_text.replace("\n", "<br>\n")
    return (
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;"
        "font-size:14px;color:#222222;line-height:1.5;\">"
        f"<h2 style=\"margin:0 0 12px 0;\">{html.escape(title)}</h2>"
        f"<div>{body_html}</div>"
        "</body></html>"
    )


def _get_gmail_credentials() -> "Credentials | None":
    """Load OAuth credentials from the GCP_TOKEN_JSON env var (Railway) and
    refresh them if expired, using GCP_CREDENTIALS_JSON for the client_id/
    secret needed to refresh. Returns None if anything is missing or
    malformed — callers must treat that as "email unavailable", not raise."""
    if not GCP_TOKEN_JSON:
        logging.error("GCP_TOKEN_JSON is missing — cannot send email.")
        return None

    try:
        token_data = json.loads(GCP_TOKEN_JSON)
    except json.JSONDecodeError:
        logging.exception("GCP_TOKEN_JSON is not valid JSON — cannot send email.")
        return None

    # google-auth's Credentials.from_authorized_user_info() REQUIRES
    # client_id/client_secret to already be present in the dict, or it raises
    # ValueError outright — it will not fall back to anything else. A
    # standard token.json (from InstalledAppFlow) already includes them, but
    # if GCP_TOKEN_JSON was stored without them, backfill from
    # GCP_CREDENTIALS_JSON *before* constructing Credentials, not after.
    if ("client_id" not in token_data or "client_secret" not in token_data) and GCP_CREDENTIALS_JSON:
        try:
            creds_data = json.loads(GCP_CREDENTIALS_JSON)
            client_config = creds_data.get("installed", creds_data.get("web", {}))
            token_data.setdefault("client_id", client_config.get("client_id"))
            token_data.setdefault("client_secret", client_config.get("client_secret"))
        except json.JSONDecodeError:
            logging.exception("GCP_CREDENTIALS_JSON is not valid JSON.")

    try:
        # Use whatever scope(s) the token was actually granted (e.g. it may
        # be the broad "https://mail.google.com/" or the narrower
        # "gmail.send") rather than forcing GMAIL_SCOPES — a mismatch here
        # causes Google to reject the refresh with invalid_scope, even
        # though the granted scope is broad enough to cover sending.
        token_scopes = token_data.get("scopes") or GMAIL_SCOPES
        creds = Credentials.from_authorized_user_info(token_data, token_scopes)
    except ValueError:
        logging.exception(
            "GCP_TOKEN_JSON is missing client_id/client_secret and "
            "GCP_CREDENTIALS_JSON could not supply them — cannot send email."
        )
        return None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            logging.exception("Failed to refresh Gmail OAuth token.")
            return None

    return creds


def _send_email_sync(
    to_addr: str,
    subject: str,
    html_body: str,
    cc: list[str] | None = None,
    attachment_filename: str | None = None,
    attachment_bytes: bytes | None = None,
    attachment_mime: str = "text/csv",
) -> None:
    """Blocking Gmail API send — must be run off the event loop via
    asyncio.to_thread (see send_email_via_gmail). Raises on failure so the
    async wrapper can log it without ever propagating into the bot flow."""
    creds = _get_gmail_credentials()
    if creds is None:
        raise RuntimeError("No usable Gmail credentials.")

    if attachment_filename and attachment_bytes is not None:
        message = MIMEMultipart("mixed")
        message.attach(MIMEText(html_body, "html"))
        maintype, _, subtype = attachment_mime.partition("/")
        part = MIMEApplication(attachment_bytes, _subtype=subtype or "octet-stream")
        part.add_header("Content-Disposition", "attachment", filename=attachment_filename)
        message.attach(part)
    else:
        message = MIMEText(html_body, "html")

    message["to"] = to_addr
    message["subject"] = subject
    if cc:
        message["cc"] = ", ".join(cc)

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service = build("gmail", "v1", credentials=creds)
    service.users().messages().send(userId="me", body={"raw": raw_message}).execute()


async def send_email_via_gmail(
    to_addr: str,
    subject: str,
    html_body: str,
    cc: list[str] | None = None,
    attachment_filename: str | None = None,
    attachment_bytes: bytes | None = None,
    attachment_mime: str = "text/csv",
) -> None:
    """Fire-and-forget-style email send via the Gmail API.
    Never raises — errors are logged so the bot flow is never blocked or
    broken by an email delivery failure. The Gmail client library is
    synchronous, so the actual send runs in a worker thread."""
    if not EMAIL_ENABLED:
        return
    try:
        await asyncio.to_thread(
            _send_email_sync, to_addr, subject, html_body, cc,
            attachment_filename, attachment_bytes, attachment_mime,
        )
    except Exception:
        logging.exception("Failed to send email via Gmail API to %s", to_addr)


# Maps the internal subject_prefix used by each of the three WSF completion
# points to the human-facing "result" label used in the email header.
RESULT_LABELS: dict[str, str] = {
    "Price Reply": "Price Indication",
    "No Logistics Service": "No Service",
    "Product Not Available": "No Product",
}


def _header_safe(value) -> str:
    """Strip characters that could break or inject into an email header
    (e.g. stray CR/LF from free-text fields)."""
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


async def send_recap_email(r: dict, subject_prefix: str) -> None:
    """Send the price-recap email to the requesting rep, CC'ing Admin always
    and User C for WSF requests only, for one of the three final-completion
    points (full price, no availability, no logistics service).

    NOTE: c_availability / c_confirm_no_service / c_comments — the three
    callers of this function — are SHARED between the WSF (User C) and MOP/NP
    (Admin-as-pricer) flows, so this function fires for both workflows. User
    C must only ever be CC'd on WSF requests, never MOP/NP. Silently skips if
    email is disabled or the rep has no mapped address — this must never
    interrupt the Telegram flow."""
    if not EMAIL_ENABLED:
        return
    rep_email = REP_EMAILS.get(r["rep_id"])
    if not rep_email:
        logging.warning(
            "No email mapped for rep_id %s (REP_EMAILS in .env) — "
            "skipping recap email for Request #%s.",
            r["rep_id"], r["id"],
        )
        return

    result_label = RESULT_LABELS.get(subject_prefix, subject_prefix)
    subject = (
        f"Request #{r['id']} | Rep: {_header_safe(r.get('rep_name'))} | "
        f"Product: {_header_safe(r.get('product'))} | "
        f"Volume: {_header_safe(r.get('volume'))} mt | "
        f"POD: {_header_safe(r.get('pod'))} | Result: {result_label}"
    )
    html_body = _email_html(f"{subject_prefix} — Request #{r['id']}", c_answer_for_rep(r))

    workflow = r.get("workflow", "WSF")
    cc_addrs = []
    if ADMIN_EMAIL and ADMIN_EMAIL.lower() != rep_email.lower():
        cc_addrs.append(ADMIN_EMAIL)
    if (
        workflow != "MOP/NP"
        and USER_C_EMAIL
        and USER_C_EMAIL.lower() != rep_email.lower()
        and USER_C_EMAIL.lower() not in (a.lower() for a in cc_addrs)
    ):
        cc_addrs.append(USER_C_EMAIL)
    cc = cc_addrs or None

    await send_email_via_gmail(rep_email, subject, html_body, cc=cc)


# ── Auth helper ────────────────────────────────────────────────────────────────
async def deny(update: Update) -> None:
    await update.effective_message.reply_text(
        "⛔ You are not authorised to use this bot. "
        "Contact your administrator to be added to the whitelist."
    )


# ══════════════════════════════════════════════════════════════════════════════
# GENERAL COMMANDS
# ══════════════════════════════════════════════════════════════════════════════
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = uid_of(update)
    if uid not in KNOWN_IDS:
        await deny(update)
        return

    if uid == USER_L_ID:
        role = "User L (Logistics)"
        cmds = (
            "/pending — view requests awaiting your input\n"
            "/edit <id> — correct something you already submitted\n\n"
            "💡 When filling logistics, you can copy all fields from a similar past request "
            "instead of re-entering them — the option appears after you answer Service: Yes."
        )
    elif uid == USER_C_ID:
        role = "User C (Pricing)"
        cmds = (
            "/pending — view requests awaiting your input\n"
            "/edit <id> — correct something you already submitted\n"
            "/report — generate a summary + CSV report (WSF requests only)"
        )
    elif uid == ADMIN_ID:
        role = "Administrator / Sales Representative"
        cmds = (
            "/newrequest — file a price request\n"
            "/pending — view all open requests\n"
            "/edit <id> — correct any field on any request\n"
            "/report — generate a summary + CSV report by time range"
        )
    else:
        role = "Sales Representative"
        cmds = "/newrequest — file a price request\n/pending — view your requests"

    await update.message.reply_text(
        f"👋 Welcome to the <b>Price Indication Bot</b>!\n"
        f"Your role: <b>{role}</b>\n\n{cmds}",
        parse_mode="HTML",
    )


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = uid_of(update)
    if uid not in KNOWN_IDS:
        await deny(update)
        return

    if uid == USER_L_ID:
        text = (
            "📖 <b>Commands (Logistics):</b>\n\n"
            "/start — show your role\n"
            "/pending — requests awaiting your input\n"
            "/edit &lt;id&gt; — correct a logistics field you already submitted "
            "(any time before the request is done)\n"
            "/cancel — abort current operation\n\n"
            "💡 <b>Tip:</b> After answering <i>Service: Yes</i> on a request, if you've "
            "filled in logistics for a similar request before (same workflow), you'll be "
            "offered the option to copy all fields from it instead of re-entering everything."
        )
    elif uid == USER_C_ID:
        text = (
            "📖 <b>Commands (Pricing):</b>\n\n"
            "/start — show your role\n"
            "/pending — requests awaiting your input\n"
            "/edit &lt;id&gt; — correct a pricing field you already submitted "
            "(any time before the request is done)\n"
            "/report — pick a time range (Today / 7 days / 30 days / All time); "
            "sends a summary here and emails you a full CSV — WSF requests only\n"
            "/cancel — abort current operation"
        )
    elif uid == ADMIN_ID:
        text = (
            "📖 <b>Commands (Admin / Sales Rep):</b>\n\n"
            "/start — show your role\n"
            "/newrequest — file a new price request\n"
            "/pending — view all open requests\n"
            "/edit &lt;id&gt; — correct ANY field on ANY request, any time "
            "(including already-'done' requests — resends the corrected reply to the rep)\n"
            "/report — pick a time range (Today / 7 days / 30 days / All time); "
            "sends a summary here and emails you a full CSV of matching requests\n"
            "/cancel — abort current operation"
        )
    else:
        text = (
            "📖 <b>Commands (Sales Rep):</b>\n\n"
            "/start — show your role\n"
            "/newrequest — file a new price request\n"
            "/pending — view your submitted requests\n"
            "/cancel — abort current operation"
        )
    await update.message.reply_text(text, parse_mode="HTML")


async def pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = uid_of(update)
    if uid not in KNOWN_IDS:
        await deny(update)
        return

    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row

        if uid == USER_L_ID:
            rows = con.execute(
                "SELECT * FROM requests WHERE status='pending_L' ORDER BY id"
            ).fetchall()
            if not rows:
                await update.message.reply_text("✅ No pending requests for you.")
                return
            buttons = [
                [InlineKeyboardButton(
                    f"#{r['id']} — {r['product']}  {r['volume']} mt — {r['pod']}",
                    callback_data=f"L_{r['id']}",
                )]
                for r in rows
            ]
            await update.message.reply_text(
                "📋 <b>Pending requests for User L:</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif uid == USER_C_ID:
            # WSF only — MOP/NP requests are handled exclusively by Admin, never shown to User C
            rows = con.execute(
                "SELECT * FROM requests WHERE status IN ('pending_C_avail', 'pending_C') AND workflow = 'WSF' ORDER BY id"
            ).fetchall()
            if not rows:
                await update.message.reply_text("✅ No pending requests for you.")
                return
            buttons = [
                [InlineKeyboardButton(
                    f"#{r['id']} — {r['product']}  {r['volume']} mt — {r['pod']}",
                    callback_data=f"C_{r['id']}",
                )]
                for r in rows
            ]
            await update.message.reply_text(
                "📋 <b>Pending requests for User C:</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif uid == ADMIN_ID:
            rows = con.execute(
                "SELECT * FROM requests WHERE status != 'done' ORDER BY id"
            ).fetchall()
            if not rows:
                await update.message.reply_text("✅ No open requests.")
                return

            # MOP/NP requests awaiting Admin action (availability check or pricing)
            mop_actionable = [
                r for r in rows
                if r["workflow"] == "MOP/NP"
                and r["status"] in ("pending_C_avail", "pending_C")
            ]
            if mop_actionable:
                buttons = [
                    [InlineKeyboardButton(
                        f"#{r['id']} [MOP/NP] — {r['product']}  {r['volume']} mt — {r['pod']}",
                        callback_data=f"C_{r['id']}",
                    )]
                    for r in mop_actionable
                ]
                await update.message.reply_text(
                    "📋 <b>MOP/NP Requests awaiting your response:</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )

            # Full overview list for Admin
            txt = "<b>All Open Requests (Admin view):</b>\n\n"
            for r in rows:
                txt += (
                    f"• Request #{r['id']} [{e(r['workflow'])}] — {e(r['product'])} — "
                    f"Status: <code>{e(r['status'])}</code>\n"
                )
            await update.message.reply_text(txt, parse_mode="HTML")

        else:
            # Sales rep: show their own last 10 requests
            rows = con.execute(
                "SELECT * FROM requests WHERE rep_id=? ORDER BY id DESC LIMIT 10",
                (uid,),
            ).fetchall()
            if not rows:
                await update.message.reply_text(
                    "You have no requests yet. Use /newrequest to file one."
                )
                return
            txt = "<b>Your recent requests:</b>\n\n"
            for r in rows:
                label = {
                    "pending_C_avail": "⏳ Awaiting availability check",
                    "pending_L":       "⏳ Awaiting logistics",
                    "pending_C":       "⏳ Awaiting pricing",
                    "done":            "✅ Answered",
                }.get(r["status"], r["status"])
                txt += f"• Request #{r['id']} — {e(r['product'])} — {label}\n"
            await update.message.reply_text(txt, parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN REPORTS (on-demand, via /report)
# ══════════════════════════════════════════════════════════════════════════════
REPORT_RANGE_LABELS = {
    "today": "Today",
    "7d":    "Last 7 days",
    "30d":   "Last 30 days",
    "all":   "All time",
}

# Columns included in the detail-list CSV attachment, in order, with headers.
REPORT_CSV_COLUMNS = [
    ("id",            "ID"),
    ("created_at",    "Filed (UTC)"),
    ("workflow",      "Workflow"),
    ("rep_name",      "Sales Rep"),
    ("status",        "Status"),
    ("product",       "Product"),
    ("packaging",     "Packaging"),
    ("labels",        "Labels"),
    ("pallets",       "Pallets"),
    ("volume",        "Volume (mt)"),
    ("pod",           "POD"),
    ("basis",         "Basis"),
    ("availability",  "Availability"),
    ("price",         "Price/mt"),
    ("etd",           "ETD"),
    ("validity",      "Validity"),
    ("l_answered_at", "Logistics answered (UTC)"),
    ("c_answered_at", "Priced (UTC)"),
]


def _report_cutoff(range_key: str) -> str | None:
    """Return the created_at cutoff string for a range key, or None for 'all'."""
    now = datetime.utcnow()
    if range_key == "today":
        cutoff_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif range_key == "7d":
        cutoff_dt = now - timedelta(days=7)
    elif range_key == "30d":
        cutoff_dt = now - timedelta(days=30)
    else:
        return None
    return cutoff_dt.strftime("%Y-%m-%d %H:%M UTC")


def _fetch_report_rows(range_key: str, workflow_filter: str | None = None) -> list[dict]:
    cutoff = _report_cutoff(range_key)
    where_parts = []
    params: list[str] = []
    if cutoff is not None:
        where_parts.append("created_at >= ?")
        params.append(cutoff)
    if workflow_filter is not None:
        where_parts.append("workflow = ?")
        params.append(workflow_filter)
    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"SELECT * FROM requests {where_sql} ORDER BY id", params
        ).fetchall()
    return [dict(row) for row in rows]


def _build_report_summary_html(rows: list[dict], range_label: str, workflow_filter: str | None = None) -> str:
    total = len(rows)
    by_status: dict[str, int] = {}
    by_workflow: dict[str, int] = {}
    priced_volumes = []
    for r in rows:
        status = r.get("status") or "unknown"
        by_status[status] = by_status.get(status, 0) + 1
        workflow = r.get("workflow") or "WSF"
        by_workflow[workflow] = by_workflow.get(workflow, 0) + 1
        if r.get("status") == "done" and r.get("price"):
            try:
                priced_volumes.append(float(str(r["volume"]).replace(",", ".")))
            except (ValueError, TypeError):
                pass

    status_labels = {
        "pending_C_avail": "Awaiting availability check",
        "pending_L":       "Awaiting logistics",
        "pending_C":       "Awaiting pricing",
        "done":            "Answered (priced or closed)",
    }
    status_lines = "".join(
        f"  • {status_labels.get(s, s)}: <b>{n}</b>\n" for s, n in sorted(by_status.items())
    )
    # Only show the workflow breakdown when more than one workflow is present —
    # redundant when the report is already scoped to a single workflow.
    workflow_section = ""
    if len(by_workflow) > 1:
        workflow_lines = "".join(
            f"  • {e(w)}: <b>{n}</b>\n" for w, n in sorted(by_workflow.items())
        )
        workflow_section = f"<b>By workflow:</b>\n{workflow_lines}\n"
    volume_line = (
        f"📦 Total volume priced: <b>{sum(priced_volumes):.2f} mt</b> across {len(priced_volumes)} request(s)\n"
        if priced_volumes else ""
    )
    title_suffix = f" — {e(workflow_filter)} only" if workflow_filter else ""

    return (
        f"📊 <b>Pricing Bot Report — {e(range_label)}{title_suffix}</b>\n\n"
        f"Total requests: <b>{total}</b>\n\n"
        f"<b>By status:</b>\n{status_lines or '  —\n'}\n"
        f"{workflow_section}"
        f"{volume_line}"
        f"\nFull detail list attached as CSV."
    )


def _build_report_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([header for _, header in REPORT_CSV_COLUMNS])
    for r in rows:
        writer.writerow([r.get(key, "") or "" for key, _ in REPORT_CSV_COLUMNS])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens UTF-8 cleanly


async def report_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = uid_of(update)
    if uid not in (ADMIN_ID, USER_C_ID):
        await deny(update)
        return
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"report_{key}")]
        for key, label in REPORT_RANGE_LABELS.items()
    ]
    subtitle = "\n\n(Scoped to WSF requests only.)" if uid == USER_C_ID else ""
    await update.message.reply_text(
        f"📊 <b>Generate report</b>\n\nSelect a time range:{subtitle}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def report_range_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if uid not in (ADMIN_ID, USER_C_ID):
        return

    workflow_filter = "WSF" if uid == USER_C_ID else None
    recipient_chat_id = uid
    recipient_email = ADMIN_EMAIL if uid == ADMIN_ID else USER_C_EMAIL

    range_key = query.data.removeprefix("report_")
    range_label = REPORT_RANGE_LABELS.get(range_key, range_key)

    await query.edit_message_text(f"⏳ Generating report — {range_label}…")

    rows = _fetch_report_rows(range_key, workflow_filter=workflow_filter)
    if not rows:
        await query.edit_message_text(f"No requests found for: {range_label}")
        return

    summary_html = _build_report_summary_html(rows, range_label, workflow_filter=workflow_filter)
    csv_bytes = _build_report_csv(rows)

    # In-chat summary (Telegram) — always sent regardless of email config.
    await ctx.bot.send_message(recipient_chat_id, summary_html, parse_mode="HTML")

    if not recipient_email:
        env_hint = "ADMIN_EMAIL" if uid == ADMIN_ID else "USER_C_EMAIL"
        await query.edit_message_text(
            f"✅ Report generated ({range_label}) — summary sent above.\n"
            f"⚠️ {env_hint} isn't configured, so no email/CSV was sent."
        )
        return

    filename = f"pricing_report_{range_key}_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    await send_email_via_gmail(
        recipient_email,
        f"📊 Pricing Bot Report — {range_label}",
        summary_html,
        attachment_filename=filename,
        attachment_bytes=csv_bytes,
    )
    await query.edit_message_text(f"✅ Report sent to {recipient_email} — {range_label}.")


# ══════════════════════════════════════════════════════════════════════════════
# REPRESENTATIVE FLOW
# ══════════════════════════════════════════════════════════════════════════════
async def new_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = uid_of(update)
    if uid not in KNOWN_IDS:
        await deny(update)
        return ConversationHandler.END
    if uid in (USER_L_ID, USER_C_ID):
        await update.message.reply_text(
            "ℹ️ /newrequest is for Sales Representatives only."
        )
        return ConversationHandler.END

    ctx.user_data.clear()
    await update.message.reply_text(
        "📋 <b>New Price Request</b>\n\nStep 1/8 — Select <b>Workflow</b>:",
        parse_mode="HTML",
        reply_markup=kb(WORKFLOWS, 2),
    )
    return R_WORKFLOW


async def r_workflow(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text not in WORKFLOWS:
        await update.message.reply_text(
            "Please select a workflow:",
            reply_markup=kb(WORKFLOWS, 2),
        )
        return R_WORKFLOW
    ctx.user_data["workflow"] = update.message.text
    workflow = update.message.text

    if workflow == "WSF":
        await update.message.reply_text(
            "Step 2/8 — Select <b>Product</b>:",
            parse_mode="HTML",
            reply_markup=kb(PRODUCTS, 4),
        )
    else:  # MOP/NP
        await update.message.reply_text(
            "Step 2/8 — Select <b>Product</b>:",
            parse_mode="HTML",
            reply_markup=kb(MOP_PRODUCTS, 2),
        )
    return R_PRODUCT


async def r_product(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    workflow = ctx.user_data.get("workflow", "WSF")
    valid_products = PRODUCTS if workflow == "WSF" else MOP_PRODUCTS
    cols = 4 if workflow == "WSF" else 2
    if update.message.text not in valid_products:
        await update.message.reply_text(
            "Please select a valid product from the keyboard:",
            reply_markup=kb(valid_products, cols),
        )
        return R_PRODUCT
    ctx.user_data["product"] = update.message.text

    if workflow == "WSF":
        await update.message.reply_text(
            "Step 3/9 — Select <b>Packaging</b>:",
            parse_mode="HTML",
            reply_markup=kb(PACKAGINGS, 3),
        )
    else:
        await update.message.reply_text(
            "Step 3/8 — Select <b>Packaging</b>:",
            parse_mode="HTML",
            reply_markup=kb(MOP_PACKAGINGS, 3),
        )
    return R_PACKAGING


async def r_packaging(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    workflow = ctx.user_data.get("workflow", "WSF")
    if workflow == "WSF":
        valid_packagings = PACKAGINGS
        cols = 3
    else:
        valid_packagings = MOP_PACKAGINGS
        cols = 3

    if update.message.text not in valid_packagings:
        await update.message.reply_text(
            "Please select a valid packaging:",
            reply_markup=kb(valid_packagings, cols),
        )
        return R_PACKAGING
    ctx.user_data["packaging"] = update.message.text

    if workflow == "WSF":
        await update.message.reply_text(
            "Step 4/9 — <b>Labels?</b>",
            parse_mode="HTML",
            reply_markup=kb(LABELS, 2),
        )
        return R_LABELS
    else:
        # MOP/NP: pallets are relevant for bagged packaging (1000 kg, 900 kg, 50 kg),
        # but not for bulk/bulkcntr
        if update.message.text in ("1000 kg", "900 kg", "50 kg"):
            await update.message.reply_text(
                "Step 4/8 — Select <b>Pallets</b>:",
                parse_mode="HTML",
                reply_markup=kb(MOP_PALLETS, 2),
            )
            return R_PALLETS
        else:
            # Skip pallets for bulk/bulkcntr
            ctx.user_data["pallets"] = ""
            await update.message.reply_text(
                "Step 4/8 — Enter <b>Volume (mt)</b>:",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove(),
            )
            return R_VOLUME


async def r_labels(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """WSF only — Labels Yes/No, asked right after Packaging."""
    if update.message.text not in LABELS:
        await update.message.reply_text(
            "Please select a valid Labels option:",
            reply_markup=kb(LABELS, 2),
        )
        return R_LABELS
    ctx.user_data["labels"] = update.message.text
    await update.message.reply_text(
        "Step 5/9 — Select <b>Pallets</b>:",
        parse_mode="HTML",
        reply_markup=kb(PALLETS, 4),
    )
    return R_PALLETS


async def r_pallets(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    workflow = ctx.user_data.get("workflow", "WSF")
    valid_pallets = PALLETS if workflow == "WSF" else MOP_PALLETS
    cols = 4 if workflow == "WSF" else 2
    if update.message.text not in valid_pallets:
        await update.message.reply_text(
            "Please select a valid pallets option:",
            reply_markup=kb(valid_pallets, cols),
        )
        return R_PALLETS
    ctx.user_data["pallets"] = update.message.text
    step_label = "Step 6/9" if workflow == "WSF" else "Step 5/8"
    await update.message.reply_text(
        f"{step_label} — Enter <b>Volume (mt)</b>:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    return R_VOLUME


async def r_volume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_number(text):
        await update.message.reply_text(
            "⚠️ Please enter a valid positive number (e.g. 500 or 22.5):"
        )
        return R_VOLUME
    ctx.user_data["volume"] = text
    workflow = ctx.user_data.get("workflow", "WSF")
    step_label = "Step 7/9" if workflow == "WSF" else "Step 6/8"
    await update.message.reply_text(
        f"{step_label} — Enter <b>POD</b> (Port of Destination):",
        parse_mode="HTML",
    )
    return R_POD


async def r_pod(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["pod"] = update.message.text.strip()
    workflow = ctx.user_data.get("workflow", "WSF")
    step_label = "Step 8/9" if workflow == "WSF" else "Step 7/8"
    await update.message.reply_text(
        f"{step_label} — Select <b>Basis</b>:",
        parse_mode="HTML",
        reply_markup=kb(BASIS, 4),
    )
    return R_BASIS


async def r_basis(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text not in BASIS:
        await update.message.reply_text(
            "Please select a valid basis:",
            reply_markup=kb(BASIS, 4),
        )
        return R_BASIS
    ctx.user_data["basis"] = update.message.text
    workflow = ctx.user_data.get("workflow", "WSF")
    step_label = "Step 9/9" if workflow == "WSF" else "Step 8/8"
    await update.message.reply_text(
        f"{step_label} — Enter <b>Comments</b> or tap Skip:",
        parse_mode="HTML",
        reply_markup=kb_skip(),
    )
    return R_COMMENTS


async def r_comments(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    ctx.user_data["rep_comments"] = "" if text == SKIP_BTN else text

    d = ctx.user_data
    workflow = d.get("workflow", "WSF")
    pallets_line = ""
    if workflow == "WSF" or d.get("pallets"):
        pallets_line = f"🪵 Pallets: <code>{e(d['pallets'])}</code>\n"
    labels_line = f"🏷️ Labels: <code>{e(d['labels'])}</code>\n" if workflow == "WSF" else ""
    summary = (
        f"Please review your request:\n\n"
        f"🗂 Workflow: <code>{e(workflow)}</code>\n"
        f"🧪 Product: <code>{e(d['product'])}</code>\n"
        f"📦 Packaging: <code>{e(d['packaging'])}</code>\n"
        f"{labels_line}"
        f"{pallets_line}"
        f"⚖️ Volume: <code>{e(d['volume'])} mt</code>\n"
        f"📍 POD: <code>{e(d['pod'])}</code>\n"
        f"🚢 Basis: <code>{e(d['basis'])}</code>\n"
        f"💬 Comments: {e(d['rep_comments']) or '—'}"
    )
    await update.message.reply_text(
        summary + "\n\n<b>Confirm submission?</b>",
        parse_mode="HTML",
        reply_markup=kb(["✅ Confirm", "✗ Cancel"], 2),
    )
    return R_CONFIRM


async def r_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text != "✅ Confirm":
        await update.message.reply_text(
            "❌ Cancelled. Use /newrequest to start again.",
            reply_markup=ReplyKeyboardRemove(),
        )
        ctx.user_data.clear()
        return ConversationHandler.END

    u = update.effective_user
    d = ctx.user_data
    workflow = d.get("workflow", "WSF")
    req_id = insert_request(
        rep_id=u.id,
        rep_name=u.full_name,
        workflow=workflow,
        product=d["product"],
        packaging=d["packaging"],
        labels=d.get("labels", ""),
        pallets=d.get("pallets", ""),
        volume=d["volume"],
        pod=d["pod"],
        basis=d["basis"],
        rep_comments=d["rep_comments"],
        created_at=now_utc(),
        status="pending_C_avail",
    )
    r = get_request(req_id)

    await update.message.reply_text(
        f"✅ <b>Request #{req_id} submitted!</b>\n"
        f"We will get back to you with pricing.\n\n" + rep_recap(r),
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )

    if workflow == "MOP/NP":
        # MOP/NP: Admin takes the availability-check role entirely — User C is not involved
        await ctx.bot.send_message(
            ADMIN_ID,
            f"📥 <b>New MOP/NP Price Request #{req_id}</b>\n\n"
            f"Please validate <b>availability</b> for this request.\n"
            f"Use /pending to respond.\n\n" + rep_recap(r),
            parse_mode="HTML",
        )
        # No separate admin copy — Admin IS the actor for MOP/NP
    else:
        # WSF: standard routing to User C
        msg = await ctx.bot.send_message(
            USER_C_ID,
            f"📥 <b>New Price Request #{req_id}</b>\n\n"
            f"Please validate <b>availability</b> for this request.\n"
            f"Use /pending to respond.\n\n" + rep_recap(r),
            parse_mode="HTML",
        )
        update_request(req_id, c_msg_id=msg.message_id)
        await ctx.bot.send_message(ADMIN_ID, admin_stage_copy("filed", r), parse_mode="HTML")

    ctx.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# USER L FLOW
# Entry point: CallbackQueryHandler matching "L_<id>" from /pending buttons.
# This fixes the v1 bug where entry_points=[] meant the ConversationHandler
# state machine was never activated.
# ══════════════════════════════════════════════════════════════════════════════
async def l_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: User L taps an inline button produced by /pending."""
    query = update.callback_query
    await query.answer()

    if uid_of(update) != USER_L_ID:
        await query.message.reply_text("⛔ Not authorised.")
        return ConversationHandler.END

    req_id = int(query.data.split("_", 1)[1])
    r = get_request(req_id)

    if r is None:
        await query.message.reply_text("⚠️ Request not found.")
        return ConversationHandler.END
    if r["status"] != "pending_L":
        await query.message.reply_text(
            f"⚠️ Request #{req_id} is no longer pending your input (status: {r['status']})."
        )
        return ConversationHandler.END

    ctx.user_data.clear()
    ctx.user_data["l_req"] = req_id

    await query.message.reply_text(
        f"<b>Opening Request #{req_id}</b>\n\n" + rep_recap(r),
        parse_mode="HTML",
    )
    await query.message.reply_text(
        f"<b>Request #{req_id}</b>\n\n"
        f"Do you provide <b>Service</b> (logistics) for this request?",
        parse_mode="HTML",
        reply_markup=kb(["Yes", "No"], 2),
    )
    return L_SERVICE


async def l_service(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text not in ("Yes", "No"):
        await update.message.reply_text(
            "Please select Yes or No:", reply_markup=kb(["Yes", "No"], 2)
        )
        return L_SERVICE

    if update.message.text == "No":
        req_id = ctx.user_data["l_req"]
        update_request(req_id, service="No", l_answered_at=now_utc(), status="pending_C")
        r = get_request(req_id)
        workflow = r.get("workflow", "WSF")

        if workflow == "MOP/NP":
            await update.message.reply_text(
                f"✅ Saved. Request #{req_id} forwarded to Admin.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await ctx.bot.send_message(
                ADMIN_ID,
                f"📥 <b>MOP/NP Request #{req_id} — from User L</b>\n\n"
                f"Service: <b>No</b>\n"
                f"Please confirm reply to representative.\n"
                f"Use /pending to respond.\n\n" + rep_recap(r),
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"✅ Saved. Request #{req_id} forwarded to User C.",
                reply_markup=ReplyKeyboardRemove(),
            )
            msg = await ctx.bot.send_message(
                USER_C_ID,
                f"📥 <b>Request #{req_id} — from User L</b>\n\n"
                f"Service: <b>No</b>\n"
                f"Please confirm reply to representative.\n"
                f"Use /pending to respond.\n\n" + rep_recap(r),
                parse_mode="HTML",
            )
            update_request(req_id, c_msg_id=msg.message_id)
        # Only send the admin overview copy for WSF — for MOP/NP, Admin is the actor
        # and already received the direct message above
        if workflow != "MOP/NP":
            await ctx.bot.send_message(ADMIN_ID, admin_stage_copy("l_done", r), parse_mode="HTML")
        ctx.user_data.clear()
        return ConversationHandler.END

    # Service = Yes → collect logistics details
    r = get_request(ctx.user_data["l_req"])
    workflow = r.get("workflow", "WSF")

    candidates = get_recent_l_requests(workflow, exclude_id=ctx.user_data["l_req"])
    if candidates:
        await update.message.reply_text(
            "You've filled in logistics for similar requests before.\n"
            "Copy all fields from a previous request, or fill in fresh?",
            reply_markup=kb(["✍️ Fill manually", "📋 Copy from previous request"], 1),
        )
        return L_COPY_CHOICE

    await _ask_pol(update, workflow)
    return L_POL


async def _ask_pol(update: Update, workflow: str) -> None:
    if workflow == "WSF":
        # WSF only: POL is a selector (STP/Novo) — the choice also determines
        # which tariff/mt (entered earlier by User C) feeds the Total/mt calc.
        await update.message.reply_text(
            "Select <b>POL</b> (Port of Loading):",
            parse_mode="HTML",
            reply_markup=kb(["STP", "Novo"], 2),
        )
    else:
        await update.message.reply_text(
            "Enter <b>POL</b> (Port of Loading):",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )


COPY_CANCEL_BTN = "✗ Cancel — fill manually"


def _copy_candidate_label(c: dict) -> str:
    date = (c.get("l_answered_at") or "")[:10]
    return f"#{c['id']} — {c.get('product', '')} / {c.get('pod', '')} ({date})"


async def l_copy_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    r = get_request(ctx.user_data["l_req"])
    workflow = r.get("workflow", "WSF")

    if text == "✍️ Fill manually":
        await _ask_pol(update, workflow)
        return L_POL

    if text == "📋 Copy from previous request":
        candidates = get_recent_l_requests(workflow, exclude_id=ctx.user_data["l_req"])
        labels = {_copy_candidate_label(c): c["id"] for c in candidates}
        ctx.user_data["l_copy_candidates"] = labels
        await update.message.reply_text(
            "Select a request to copy logistics data from:",
            reply_markup=kb(list(labels.keys()) + [COPY_CANCEL_BTN], 1),
        )
        return L_COPY_SELECT

    await update.message.reply_text(
        "Please choose an option:",
        reply_markup=kb(["✍️ Fill manually", "📋 Copy from previous request"], 1),
    )
    return L_COPY_CHOICE


async def l_copy_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    r = get_request(ctx.user_data["l_req"])
    workflow = r.get("workflow", "WSF")

    if text == COPY_CANCEL_BTN:
        ctx.user_data.pop("l_copy_candidates", None)
        await _ask_pol(update, workflow)
        return L_POL

    labels = ctx.user_data.get("l_copy_candidates", {})
    source_id = labels.get(text)
    if source_id is None:
        await update.message.reply_text(
            "Please select one of the requests from the keyboard:",
            reply_markup=kb(list(labels.keys()) + [COPY_CANCEL_BTN], 1),
        )
        return L_COPY_SELECT

    source = get_request(source_id)
    d = ctx.user_data
    d["l_copy_mode"] = True
    d["l_pol"] = source.get("pol") or ""
    d["l_terminal"] = source.get("terminal") or ""
    d["l_line"] = source.get("line") or ""
    d["l_equipment"] = source.get("equipment") or ""
    d["l_handling"] = source.get("handling") or ""
    d["l_thc"] = source.get("thc") or ""
    d["l_freight"] = source.get("freight") or ""
    d["l_extras"] = source.get("extras") or ""
    d["l_maxpayload"] = source.get("max_payload") or ""

    needs_labels = workflow == "WSF" and r.get("labels") == "Yes"
    if needs_labels:
        source_labels_cost = source.get("labels_cost") or ""
        if source_labels_cost:
            d["l_labels_cost"] = source_labels_cost
        else:
            await update.message.reply_text(
                f"Source request #{source_id} didn't have a Labels cost/mt saved.\n"
                f"Enter <b>Labels cost/mt</b> (USD, e.g. 0 or 12.50):",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove(),
            )
            return L_LABELS_COST

    await update.message.reply_text(
        _l_copy_review_text(d, r),
        parse_mode="HTML",
        reply_markup=kb(["✅ Use these values", "✍️ Fill manually instead"], 1),
    )
    return L_COPY_REVIEW


def _l_copy_review_text(d: dict, target: dict) -> str:
    workflow = target.get("workflow") or "WSF"
    needs_labels = workflow == "WSF" and target.get("labels") == "Yes"
    lines = [
        "📋 <b>Review copied logistics data:</b>\n",
        f"POL: <code>{e(d.get('l_pol'))}</code>",
        f"Terminal: <code>{e(d.get('l_terminal')) or 'N/A'}</code>",
        f"Line: <code>{e(d.get('l_line')) or 'N/A'}</code>",
        f"Equipment: <code>{e(d.get('l_equipment')) or 'N/A'}</code>",
        f"Handling/mt: <code>{e(d.get('l_handling'))}</code>",
        f"THC/mt: <code>{e(d.get('l_thc'))}</code>",
        f"Freight/mt: <code>{e(d.get('l_freight'))}</code>",
        f"Extras/mt: <code>{e(d.get('l_extras'))}</code>",
    ]
    if needs_labels:
        lines.append(f"Labels cost/mt: <code>{e(d.get('l_labels_cost'))}</code>")
    lines.append(f"Max payload: <code>{e(d.get('l_maxpayload'))} mt</code>")
    return "\n".join(lines)


async def l_copy_review(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    r = get_request(ctx.user_data["l_req"])
    workflow = r.get("workflow", "WSF")

    if text == "✅ Use these values":
        await update.message.reply_text(
            "Enter <b>Comments</b> (or tap Skip):",
            parse_mode="HTML",
            reply_markup=kb_skip(),
        )
        return L_COMMENTS

    if text == "✍️ Fill manually instead":
        for k in (
            "l_pol", "l_terminal", "l_line", "l_equipment", "l_handling",
            "l_thc", "l_freight", "l_extras", "l_labels_cost", "l_maxpayload",
            "l_copy_mode", "l_copy_candidates",
        ):
            ctx.user_data.pop(k, None)
        await _ask_pol(update, workflow)
        return L_POL

    await update.message.reply_text(
        "Please choose an option:",
        reply_markup=kb(["✅ Use these values", "✍️ Fill manually instead"], 1),
    )
    return L_COPY_REVIEW


async def l_pol(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    r = get_request(ctx.user_data["l_req"])
    workflow = r.get("workflow", "WSF")

    if workflow == "WSF":
        if update.message.text not in ("STP", "Novo"):
            await update.message.reply_text(
                "Please select POL:", reply_markup=kb(["STP", "Novo"], 2)
            )
            return L_POL
        ctx.user_data["l_pol"] = update.message.text
    else:
        ctx.user_data["l_pol"] = update.message.text.strip()

    await update.message.reply_text(
        "Enter <b>Terminal</b> (or tap Skip if N/A):",
        parse_mode="HTML",
        reply_markup=kb_skip(),
    )
    return L_TERMINAL


async def l_terminal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    ctx.user_data["l_terminal"] = "" if text == SKIP_BTN else text
    await update.message.reply_text(
        "Enter <b>Line</b> (or tap Skip if N/A):",
        parse_mode="HTML",
        reply_markup=kb_skip(),
    )
    return L_LINE


async def l_line(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    ctx.user_data["l_line"] = "" if text == SKIP_BTN else text
    await update.message.reply_text(
        "Enter <b>Equipment</b> (or tap Skip if N/A):",
        parse_mode="HTML",
        reply_markup=kb_skip(),
    )
    return L_EQUIPMENT


async def l_equipment(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    ctx.user_data["l_equipment"] = "" if text == SKIP_BTN else text
    await update.message.reply_text(
        "Enter <b>Handling/mt</b> (USD, e.g. 12 or 12.50):",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    return L_HANDLING


async def l_handling(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_number_or_zero(text):
        await update.message.reply_text(
            "⚠️ Please enter a valid number of 0 or greater (e.g. 0, 12 or 12.50):"
        )
        return L_HANDLING
    ctx.user_data["l_handling"] = text
    await update.message.reply_text("Enter <b>THC/mt</b> (USD):", parse_mode="HTML")
    return L_THC


async def l_thc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_number_or_zero(text):
        await update.message.reply_text("⚠️ Please enter a valid number of 0 or greater:")
        return L_THC
    ctx.user_data["l_thc"] = text
    await update.message.reply_text("Enter <b>Freight/mt</b> (USD):", parse_mode="HTML")
    return L_FREIGHT


async def l_freight(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_number(text):
        await update.message.reply_text("⚠️ Please enter a valid positive number:")
        return L_FREIGHT
    ctx.user_data["l_freight"] = text
    await update.message.reply_text("Enter <b>Extras/mt</b> (USD):", parse_mode="HTML")
    return L_EXTRAS


async def l_extras(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_number_or_zero(text):
        await update.message.reply_text("⚠️ Please enter a valid number of 0 or greater:")
        return L_EXTRAS
    ctx.user_data["l_extras"] = text

    r = get_request(ctx.user_data["l_req"])
    workflow = r.get("workflow", "WSF")
    if workflow == "WSF" and r.get("labels") == "Yes":
        await update.message.reply_text(
            "Enter <b>Labels cost/mt</b> (USD, e.g. 0 or 12.50):", parse_mode="HTML"
        )
        return L_LABELS_COST

    await update.message.reply_text("Enter <b>Max payload</b> (mt):", parse_mode="HTML")
    return L_MAXPAYLOAD


async def l_labels_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """WSF only, and only when the rep requested Labels — cost/mt, 0 or any
    positive number, added into the logistics subtotal. Also reached mid-copy
    when the source request being copied didn't have this saved."""
    text = update.message.text.strip()
    if not is_valid_number_or_zero(text):
        await update.message.reply_text("⚠️ Please enter a valid number of 0 or greater:")
        return L_LABELS_COST
    ctx.user_data["l_labels_cost"] = text

    if ctx.user_data.get("l_copy_mode"):
        r = get_request(ctx.user_data["l_req"])
        await update.message.reply_text(
            _l_copy_review_text(ctx.user_data, r),
            parse_mode="HTML",
            reply_markup=kb(["✅ Use these values", "✍️ Fill manually instead"], 1),
        )
        return L_COPY_REVIEW

    await update.message.reply_text("Enter <b>Max payload</b> (mt):", parse_mode="HTML")
    return L_MAXPAYLOAD


async def l_maxpayload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_number(text):
        await update.message.reply_text("⚠️ Please enter a valid positive number:")
        return L_MAXPAYLOAD
    ctx.user_data["l_maxpayload"] = text
    await update.message.reply_text(
        "Enter <b>Comments</b> (or tap Skip):",
        parse_mode="HTML",
        reply_markup=kb_skip(),
    )
    return L_COMMENTS


async def l_comments(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    comments = "" if text == SKIP_BTN else text
    d = ctx.user_data
    req_id = d["l_req"]

    update_request(
        req_id,
        service="Yes",
        pol=d["l_pol"],
        terminal=d["l_terminal"],
        line=d["l_line"],
        equipment=d["l_equipment"],
        handling=d["l_handling"],
        thc=d["l_thc"],
        freight=d["l_freight"],
        extras=d["l_extras"],
        labels_cost=d.get("l_labels_cost", ""),
        max_payload=d["l_maxpayload"],
        l_comments=comments,
        l_answered_at=now_utc(),
        status="pending_C",
    )
    r = get_request(req_id)
    workflow = r.get("workflow", "WSF")

    if workflow == "MOP/NP":
        await update.message.reply_text(
            f"✅ Saved. Request #{req_id} forwarded to Admin.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await ctx.bot.send_message(
            ADMIN_ID,
            f"📥 <b>MOP/NP Request #{req_id} — Updated by User L</b>\n\n"
            f"Please add pricing.\n"
            f"Use /pending to respond.\n\n" + l_recap(r),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"✅ Saved. Request #{req_id} forwarded to User C.",
            reply_markup=ReplyKeyboardRemove(),
        )
        msg = await ctx.bot.send_message(
            USER_C_ID,
            f"📥 <b>Request #{req_id} — Updated by User L</b>\n\n"
            f"Please add pricing.\n"
            f"Use /pending to respond.\n\n" + l_recap(r),
            parse_mode="HTML",
        )
        update_request(req_id, c_msg_id=msg.message_id)
    # Only send the admin overview copy for WSF — for MOP/NP, Admin is the actor
    # and already received the direct message above (sending again would duplicate it)
    if workflow != "MOP/NP":
        await ctx.bot.send_message(ADMIN_ID, admin_stage_copy("l_done", r), parse_mode="HTML")
    ctx.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# USER C FLOW
# Entry point: CallbackQueryHandler matching "C_<id>" from /pending buttons.
# ══════════════════════════════════════════════════════════════════════════════
async def c_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: User C (WSF) or Admin (MOP/NP) taps an inline button from /pending."""
    query = update.callback_query
    await query.answer()

    uid = uid_of(update)
    req_id = int(query.data.split("_", 1)[1])
    r = get_request(req_id)

    if r is None:
        await query.message.reply_text("⚠️ Request not found.")
        return ConversationHandler.END

    workflow = r.get("workflow", "WSF")

    # Access control: MOP/NP requests are handled by Admin; WSF by User C
    if workflow == "MOP/NP" and uid != ADMIN_ID:
        await query.message.reply_text("⛔ Not authorised.")
        return ConversationHandler.END
    if workflow != "MOP/NP" and uid != USER_C_ID:
        await query.message.reply_text("⛔ Not authorised.")
        return ConversationHandler.END
    if r["status"] not in ("pending_C_avail", "pending_C"):
        await query.message.reply_text(
            f"⚠️ Request #{req_id} is no longer pending your input (status: {r['status']})."
        )
        return ConversationHandler.END

    ctx.user_data.clear()
    ctx.user_data["c_req"] = req_id

    # ── Stage 1: availability check (request just arrived from rep) ────────────
    if r["status"] == "pending_C_avail":
        await query.message.reply_text(
            f"<b>Opening Request #{req_id}</b>\n\n" + rep_recap(r),
            parse_mode="HTML",
        )
        await query.message.reply_text(
            f"<b>Request #{req_id}</b>\n\nIs the product <b>Available</b>?",
            parse_mode="HTML",
            reply_markup=kb(["Yes", "No"], 2),
        )
        return C_AVAILABILITY

    # ── Stage 2: after User L has responded (pending_C) ───────────────────────
    recap = l_recap(r) if r.get("service") == "Yes" else rep_recap(r)
    await query.message.reply_text(
        f"<b>Opening Request #{req_id}</b>\n\n" + recap,
        parse_mode="HTML",
    )

    if r.get("service") == "No":
        await query.message.reply_text(
            f"<b>Request #{req_id}</b> — Logistics: No service.\n\n"
            f"Send the 'no service' reply to the representative now?",
            parse_mode="HTML",
            reply_markup=kb(["Yes, send reply", "Cancel"], 2),
        )
        return C_CONFIRM_NO_SERVICE

    # Service = Yes → go straight to pricing (availability already confirmed)
    await query.message.reply_text(
        f"<b>Request #{req_id}</b> — Availability confirmed ✅\n\nEnter <b>Volume</b> (mt):",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    return C_VOLUME


async def c_confirm_no_service(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    req_id = ctx.user_data["c_req"]
    if update.message.text == "Yes, send reply":
        update_request(req_id, c_answered_at=now_utc(), status="done")
        r = get_request(req_id)
        await ctx.bot.send_message(r["rep_id"], c_answer_for_rep(r), parse_mode="HTML")
        await send_recap_email(r, "No Logistics Service")
        await update.message.reply_text(
            f"✅ Reply sent to representative for Request #{req_id}.",
            reply_markup=ReplyKeyboardRemove(),
        )
        # Only notify Admin for WSF (Admin is the actor for MOP/NP)
        if r.get("workflow", "WSF") != "MOP/NP":
            await ctx.bot.send_message(ADMIN_ID, admin_stage_copy("c_done", r), parse_mode="HTML")
    else:
        await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())

    ctx.user_data.clear()
    return ConversationHandler.END


def _c_tariffs_and_packaging_lines(r: dict) -> str:
    """WSF only: STP tariff/mt, Novo tariff/mt, and Packaging/mt as entered
    by User C, appended to the recap forwarded to User L so User L can see
    these figures before adding logistics."""
    return (
        f"\n─────────────────────\n"
        f"💰 <b>Tariffs / Packaging (User C)</b>\n"
        f"STP tariff/mt: <code>{e(r['stp_tariff'])}</code>\n"
        f"Novo tariff/mt: <code>{e(r['novo_tariff'])}</code>\n"
        f"Packaging/mt: <code>{e(r['packaging_mt'])}</code>"
    )


async def _confirm_availability_and_forward_to_l(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, req_id: int, **extra_fields
) -> None:
    """Mark availability confirmed and forward the request to User L.
    Shared by the MOP/NP path (immediate) and the WSF path (after the
    STP/Novo tariff and Packaging/mt steps) — extra_fields carries
    stp_tariff/novo_tariff/packaging_mt for WSF, and is empty for MOP/NP."""
    update_request(req_id, availability="Yes", c_avail_at=now_utc(), status="pending_L", **extra_fields)
    r = get_request(req_id)
    workflow = r.get("workflow", "WSF")
    await update.message.reply_text(
        f"✅ Availability confirmed. Request #{req_id} forwarded to User L.",
        reply_markup=ReplyKeyboardRemove(),
    )
    extra_section = _c_tariffs_and_packaging_lines(r) if workflow == "WSF" else ""
    msg = await ctx.bot.send_message(
        USER_L_ID,
        f"📥 <b>Request #{req_id} — Availability confirmed by {'Admin' if workflow == 'MOP/NP' else 'User C'}</b>\n\n"
        f"Please review and add logistics.\n"
        f"Use /pending to respond.\n\n" + rep_recap(r) + extra_section,
        parse_mode="HTML",
    )
    update_request(req_id, l_msg_id=msg.message_id)
    # Only send admin copy for WSF (for MOP/NP, Admin is the actor)
    if workflow != "MOP/NP":
        await ctx.bot.send_message(ADMIN_ID, admin_stage_copy("c_avail", r), parse_mode="HTML")


async def c_availability(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text not in ("Yes", "No"):
        await update.message.reply_text(
            "Please select Yes or No:", reply_markup=kb(["Yes", "No"], 2)
        )
        return C_AVAILABILITY

    req_id = ctx.user_data["c_req"]

    if update.message.text == "No":
        update_request(req_id, availability="No", c_answered_at=now_utc(), status="done")
        r = get_request(req_id)
        await ctx.bot.send_message(r["rep_id"], c_answer_for_rep(r), parse_mode="HTML")
        await send_recap_email(r, "Product Not Available")
        await update.message.reply_text(
            f"✅ Reply sent to representative for Request #{req_id}.",
            reply_markup=ReplyKeyboardRemove(),
        )
        # Only notify Admin for WSF (Admin is the actor for MOP/NP)
        if r.get("workflow", "WSF") != "MOP/NP":
            await ctx.bot.send_message(ADMIN_ID, admin_stage_copy("c_done", r), parse_mode="HTML")
        ctx.user_data.clear()
        return ConversationHandler.END

    # Availability = Yes
    r = get_request(req_id)
    workflow = r.get("workflow", "WSF")

    if workflow == "WSF":
        # WSF only: collect STP tariff/mt and Novo tariff/mt before forwarding to User L
        await update.message.reply_text(
            "Enter <b>STP tariff/mt</b> (USD, e.g. 0 or 12.50):",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return C_STP_TARIFF

    # MOP/NP → forward to User L for logistics immediately (unchanged)
    await _confirm_availability_and_forward_to_l(update, ctx, req_id)
    ctx.user_data.clear()
    return ConversationHandler.END


async def c_stp_tariff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_number_or_zero(text):
        await update.message.reply_text(
            "⚠️ Please enter a valid number of 0 or greater (e.g. 0, 12 or 12.50):"
        )
        return C_STP_TARIFF
    ctx.user_data["stp_tariff"] = text
    await update.message.reply_text(
        "Enter <b>Novo tariff/mt</b> (USD, e.g. 0 or 12.50):", parse_mode="HTML"
    )
    return C_NOVO_TARIFF


async def c_novo_tariff(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_number_or_zero(text):
        await update.message.reply_text(
            "⚠️ Please enter a valid number of 0 or greater (e.g. 0, 12 or 12.50):"
        )
        return C_NOVO_TARIFF

    ctx.user_data["novo_tariff"] = text
    await update.message.reply_text(
        "Enter <b>Packaging/mt</b> (USD, e.g. 0 or 12.50):", parse_mode="HTML"
    )
    return C_PACKAGING_MT


async def c_packaging_mt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_number_or_zero(text):
        await update.message.reply_text(
            "⚠️ Please enter a valid number of 0 or greater (e.g. 0, 12 or 12.50):"
        )
        return C_PACKAGING_MT

    req_id = ctx.user_data["c_req"]
    await _confirm_availability_and_forward_to_l(
        update, ctx, req_id,
        stp_tariff=ctx.user_data["stp_tariff"],
        novo_tariff=ctx.user_data["novo_tariff"],
        packaging_mt=text,
    )
    ctx.user_data.clear()
    return ConversationHandler.END


async def c_volume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_number(text):
        await update.message.reply_text(
            "⚠️ Please enter a valid positive number (e.g. 500 or 22.5):"
        )
        return C_VOLUME
    ctx.user_data["c_volume"] = text
    await update.message.reply_text("Enter <b>Price/mt</b> (USD):", parse_mode="HTML")
    return C_PRICE


async def c_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_number(text):
        await update.message.reply_text(
            "⚠️ Please enter a valid positive number (e.g. 320 or 320.50):"
        )
        return C_PRICE
    ctx.user_data["c_price"] = text
    await update.message.reply_text(
        "Enter <b>ETD</b> — Estimated Time of Departure (format DD-MM-YYYY):",
        parse_mode="HTML",
    )
    return C_ETD


async def c_etd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        datetime.strptime(text, "%d-%m-%Y")
    except ValueError:
        await update.message.reply_text(
            "⚠️ Please use the format DD-MM-YYYY (e.g. 15-07-2025):"
        )
        return C_ETD
    ctx.user_data["c_etd"] = text
    await update.message.reply_text(
        "Enter <b>Validity</b> (format DD-MM-YYYY):", parse_mode="HTML"
    )
    return C_VALIDITY


async def c_validity(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        datetime.strptime(text, "%d-%m-%Y")
    except ValueError:
        await update.message.reply_text(
            "⚠️ Please use the format DD-MM-YYYY (e.g. 30-07-2025):"
        )
        return C_VALIDITY
    ctx.user_data["c_validity"] = text
    await update.message.reply_text(
        "Enter <b>Comments</b> (or tap Skip):",
        parse_mode="HTML",
        reply_markup=kb_skip(),
    )
    return C_COMMENTS


async def c_comments(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    comments = "" if text == SKIP_BTN else text
    d = ctx.user_data
    req_id = d["c_req"]

    update_request(
        req_id,
        c_volume=d["c_volume"],
        price=d["c_price"],
        etd=d["c_etd"],
        validity=d["c_validity"],
        c_comments=comments,
        c_answered_at=now_utc(),
        status="done",
    )
    r = get_request(req_id)
    await ctx.bot.send_message(r["rep_id"], c_answer_for_rep(r), parse_mode="HTML")
    await send_recap_email(r, "Price Reply")
    await update.message.reply_text(
        f"✅ Price reply sent to representative for Request #{req_id}.",
        reply_markup=ReplyKeyboardRemove(),
    )
    # Only notify Admin for WSF (Admin is the actor for MOP/NP)
    if r.get("workflow", "WSF") != "MOP/NP":
        await ctx.bot.send_message(ADMIN_ID, admin_stage_copy("c_done", r), parse_mode="HTML")
    ctx.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# /edit FLOW — group picker → field picker → new-value entry
# ══════════════════════════════════════════════════════════════════════════════
async def edit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = uid_of(update)
    if uid not in KNOWN_IDS:
        await deny(update)
        return ConversationHandler.END

    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(
            "Usage: <code>/edit &lt;request_id&gt;</code> — e.g. <code>/edit 42</code>",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    req_id = int(ctx.args[0])
    r = get_request(req_id)
    if r is None:
        await update.message.reply_text(f"⚠️ Request #{req_id} not found.")
        return ConversationHandler.END

    groups = _edit_allowed_groups(uid, r)
    if not groups:
        await update.message.reply_text(
            f"There's nothing on Request #{req_id} that you're able to edit right now "
            f"(either nothing's been submitted yet, or the request is already done — "
            f"contact Admin if it needs a correction)."
        )
        return ConversationHandler.END

    ctx.user_data.clear()
    ctx.user_data["edit_req_id"] = req_id

    buttons = [[InlineKeyboardButton(EDIT_GROUP_LABELS[g], callback_data=f"editgrp_{g}")] for g in groups]
    await update.message.reply_text(
        f"✏️ <b>Editing Request #{req_id}</b>\n\nWhat would you like to edit?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_GROUP


async def edit_group_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    uid = uid_of(update)
    group = query.data.split("_", 1)[1]
    req_id = ctx.user_data.get("edit_req_id")
    r = get_request(req_id) if req_id else None

    if r is None or group not in _edit_allowed_groups(uid, r):
        await query.message.reply_text(
            "⚠️ This request has moved on and can no longer be edited by you here "
            "— contact Admin if it still needs a correction."
        )
        ctx.user_data.clear()
        return ConversationHandler.END

    fields = _fields_for_group(group, r)
    ctx.user_data["edit_group"] = group

    buttons = []
    for field in fields:
        label = FIELD_DEFS[field]["label"]
        current = r.get(field) or "—"
        buttons.append([InlineKeyboardButton(f"{label}: {current}", callback_data=f"editfld_{group}_{field}")])
    await query.message.reply_text(
        f"Select a field to correct in <b>{e(EDIT_GROUP_LABELS[group])}</b>:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_FIELD


async def edit_field_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    uid = uid_of(update)
    _, group, field = query.data.split("_", 2)
    req_id = ctx.user_data.get("edit_req_id")
    r = get_request(req_id) if req_id else None

    if r is None or group not in _edit_allowed_groups(uid, r) or field not in _fields_for_group(group, r):
        await query.message.reply_text(
            "⚠️ This request has moved on and can no longer be edited by you here "
            "— contact Admin if it still needs a correction."
        )
        ctx.user_data.clear()
        return ConversationHandler.END

    kind, options = _resolve_field_kind(field, r)
    ctx.user_data["edit_field"] = field
    ctx.user_data["edit_kind"] = kind
    ctx.user_data["edit_options"] = options
    label = FIELD_DEFS[field]["label"]
    old_value = r.get(field) or "—"

    await query.message.reply_text(
        f"Current <b>{e(label)}</b>: <code>{e(old_value)}</code>\n\n" + _prompt_for_kind(kind, label),
        parse_mode="HTML",
        reply_markup=_kb_for_kind(kind, options),
    )
    return EDIT_VALUE


async def edit_value_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    field = ctx.user_data.get("edit_field")
    group = ctx.user_data.get("edit_group")
    kind = ctx.user_data.get("edit_kind")
    options = ctx.user_data.get("edit_options")
    req_id = ctx.user_data.get("edit_req_id")
    uid = uid_of(update)

    if not all([field, group, kind, req_id]):
        await update.message.reply_text("⚠️ Edit session expired — use /edit <request_id> to start again.")
        ctx.user_data.clear()
        return ConversationHandler.END

    r = get_request(req_id)
    if r is None or group not in _edit_allowed_groups(uid, r):
        await update.message.reply_text(
            "⚠️ This request has moved on and can no longer be edited by you here "
            "— contact Admin if it still needs a correction.",
            reply_markup=ReplyKeyboardRemove(),
        )
        ctx.user_data.clear()
        return ConversationHandler.END

    ok, new_value = _validate_edit_value(kind, text, options)
    if not ok:
        label = FIELD_DEFS[field]["label"]
        await update.message.reply_text(
            "⚠️ That's not a valid value. " + _prompt_for_kind(kind, label),
            parse_mode="HTML",
            reply_markup=_kb_for_kind(kind, options),
        )
        return EDIT_VALUE

    label = FIELD_DEFS[field]["label"]
    old_value = r.get(field) or ""
    if new_value == old_value:
        await update.message.reply_text(
            f"No change — {label} is already that.", reply_markup=ReplyKeyboardRemove(),
        )
        ctx.user_data.clear()
        return ConversationHandler.END

    update_request(req_id, **{field: new_value})
    r_new = get_request(req_id)

    await update.message.reply_text(
        f"✅ Updated <b>{e(label)}</b> for Request #{req_id}: "
        f"<s>{e(old_value or '—')}</s> → <b>{e(new_value or '—')}</b>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await _notify_edit(ctx, r_new, uid, label, old_value, new_value)

    ctx.user_data.clear()
    return ConversationHandler.END


# ── 24 h reminder job (throttled) ─────────────────────────────────────────────
async def send_reminders(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs every hour. Sends one reminder per request per 24 h to User L or C.
    Uses last_reminded_*_at columns to prevent spamming after the first reminder.
    Note: sqlite3 is synchronous; for high-traffic bots consider aiosqlite.
    """
    cutoff = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M UTC")

    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row

        # Remind C (availability check): created > 24 h ago AND not yet reminded recently
        rows_c_avail = con.execute("""
            SELECT * FROM requests
            WHERE  status = 'pending_C_avail'
              AND  created_at <= ?
              AND  (last_reminded_c_at IS NULL OR last_reminded_c_at <= ?)
        """, (cutoff, cutoff)).fetchall()

        for row in rows_c_avail:
            r = dict(row)
            workflow = r.get("workflow", "WSF")
            target_id = ADMIN_ID if workflow == "MOP/NP" else USER_C_ID
            actor_label = "Admin" if workflow == "MOP/NP" else "User C"
            try:
                await ctx.bot.send_message(
                    target_id,
                    f"⏰ <b>Reminder</b> — Request #{r['id']} [{workflow}] is awaiting your <b>availability check</b> for 24 h!\n\n"
                    f"Use /pending to respond.\n\n" + rep_recap(r),
                    parse_mode="HTML",
                )
                con.execute(
                    "UPDATE requests SET last_reminded_c_at=? WHERE id=?",
                    (now_utc(), r["id"]),
                )
                con.commit()
            except Exception as exc:
                logger.error(f"Reminder {actor_label}_avail error for request #{r['id']}: {exc}")

        # Remind L: created > 24 h ago AND (never reminded OR last reminder > 24 h ago)
        rows_l = con.execute("""
            SELECT * FROM requests
            WHERE  status = 'pending_L'
              AND  created_at <= ?
              AND  (last_reminded_l_at IS NULL OR last_reminded_l_at <= ?)
        """, (cutoff, cutoff)).fetchall()

        for row in rows_l:
            r = dict(row)
            try:
                await ctx.bot.send_message(
                    USER_L_ID,
                    f"⏰ <b>Reminder</b> — Request #{r['id']} has been waiting 24 h!\n\n"
                    f"Use /pending to respond.\n\n" + rep_recap(r),
                    parse_mode="HTML",
                )
                con.execute(
                    "UPDATE requests SET last_reminded_l_at=? WHERE id=?",
                    (now_utc(), r["id"]),
                )
                con.commit()
            except Exception as exc:
                logger.error(f"Reminder L error for request #{r['id']}: {exc}")

        # Remind C (pricing): l_answered > 24 h ago AND (never reminded OR last reminder > 24 h ago)
        rows_c = con.execute("""
            SELECT * FROM requests
            WHERE  status = 'pending_C'
              AND  l_answered_at <= ?
              AND  (last_reminded_c_at IS NULL OR last_reminded_c_at <= ?)
        """, (cutoff, cutoff)).fetchall()

        for row in rows_c:
            r = dict(row)
            workflow = r.get("workflow", "WSF")
            target_id = ADMIN_ID if workflow == "MOP/NP" else USER_C_ID
            try:
                recap = l_recap(r) if r.get("service") == "Yes" else rep_recap(r)
                await ctx.bot.send_message(
                    target_id,
                    f"⏰ <b>Reminder</b> — Request #{r['id']} [{workflow}] has been waiting 24 h!\n\n"
                    f"Use /pending to respond.\n\n" + recap,
                    parse_mode="HTML",
                )
                con.execute(
                    "UPDATE requests SET last_reminded_c_at=? WHERE id=?",
                    (now_utc(), r["id"]),
                )
                con.commit()
            except Exception as exc:
                logger.error(f"Reminder {'Admin' if workflow == 'MOP/NP' else 'C'} error for request #{r['id']}: {exc}")


# ── /cancel ────────────────────────────────────────────────────────────────────
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ── Catch-all for unauthorised users ──────────────────────────────────────────
async def unauthorized_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await deny(update)


# ── Global error handler ──────────────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches any exception raised by a handler that would otherwise be
    logged by python-telegram-bot's default logger and silently swallowed
    from the user's point of view. Logs the full traceback, tells the
    user something went wrong (best-effort), and alerts Admin so a stuck
    request doesn't go unnoticed."""
    logger.error("Unhandled exception while processing update:", exc_info=ctx.error)

    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong processing that. "
                "Please try again, or use /cancel and start over."
            )
        except Exception:
            logger.exception("Failed to notify user after handler error.")

    try:
        await ctx.bot.send_message(
            ADMIN_ID,
            f"🚨 <b>Bot error</b>\n<code>{e(repr(ctx.error))}</code>",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Failed to notify Admin after handler error.")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main() -> None:
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # ── Representative conversation ────────────────────────────────────────────
    rep_conv = ConversationHandler(
        entry_points=[CommandHandler("newrequest", new_request)],
        states={
            R_WORKFLOW:  [MessageHandler(TEXT_FILTER, r_workflow)],
            R_PRODUCT:   [MessageHandler(TEXT_FILTER, r_product)],
            R_PACKAGING: [MessageHandler(TEXT_FILTER, r_packaging)],
            R_LABELS:    [MessageHandler(TEXT_FILTER, r_labels)],
            R_PALLETS:   [MessageHandler(TEXT_FILTER, r_pallets)],
            R_VOLUME:    [MessageHandler(TEXT_FILTER, r_volume)],
            R_POD:       [MessageHandler(TEXT_FILTER, r_pod)],
            R_BASIS:     [MessageHandler(TEXT_FILTER, r_basis)],
            R_COMMENTS:  [MessageHandler(TEXT_FILTER, r_comments)],
            R_CONFIRM:   [MessageHandler(TEXT_FILTER, r_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_user=True,
    )

    # ── User L conversation ────────────────────────────────────────────────────
    # Entry point is the inline button callback — fixes the v1 architecture bug.
    l_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(l_entry, pattern=r"^L_\d+$")],
        states={
            L_SERVICE:    [MessageHandler(TEXT_FILTER, l_service)],
            L_COPY_CHOICE: [MessageHandler(TEXT_FILTER, l_copy_choice)],
            L_COPY_SELECT: [MessageHandler(TEXT_FILTER, l_copy_select)],
            L_COPY_REVIEW: [MessageHandler(TEXT_FILTER, l_copy_review)],
            L_POL:        [MessageHandler(TEXT_FILTER, l_pol)],
            L_TERMINAL:   [MessageHandler(TEXT_FILTER, l_terminal)],
            L_LINE:       [MessageHandler(TEXT_FILTER, l_line)],
            L_EQUIPMENT:  [MessageHandler(TEXT_FILTER, l_equipment)],
            L_HANDLING:   [MessageHandler(TEXT_FILTER, l_handling)],
            L_THC:        [MessageHandler(TEXT_FILTER, l_thc)],
            L_FREIGHT:    [MessageHandler(TEXT_FILTER, l_freight)],
            L_EXTRAS:     [MessageHandler(TEXT_FILTER, l_extras)],
            L_LABELS_COST: [MessageHandler(TEXT_FILTER, l_labels_cost)],
            L_MAXPAYLOAD: [MessageHandler(TEXT_FILTER, l_maxpayload)],
            L_COMMENTS:   [MessageHandler(TEXT_FILTER, l_comments)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_user=True,
    )

    # ── User C conversation ────────────────────────────────────────────────────
    c_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(c_entry, pattern=r"^C_\d+$")],
        states={
            C_CONFIRM_NO_SERVICE: [MessageHandler(TEXT_FILTER, c_confirm_no_service)],
            C_AVAILABILITY:       [MessageHandler(TEXT_FILTER, c_availability)],
            C_STP_TARIFF:         [MessageHandler(TEXT_FILTER, c_stp_tariff)],
            C_NOVO_TARIFF:        [MessageHandler(TEXT_FILTER, c_novo_tariff)],
            C_PACKAGING_MT:       [MessageHandler(TEXT_FILTER, c_packaging_mt)],
            C_VOLUME:             [MessageHandler(TEXT_FILTER, c_volume)],
            C_PRICE:              [MessageHandler(TEXT_FILTER, c_price)],
            C_ETD:                [MessageHandler(TEXT_FILTER, c_etd)],
            C_VALIDITY:           [MessageHandler(TEXT_FILTER, c_validity)],
            C_COMMENTS:           [MessageHandler(TEXT_FILTER, c_comments)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_user=True,
    )

    # ── /edit conversation ──────────────────────────────────────────────────────
    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("edit", edit_start)],
        states={
            EDIT_GROUP: [CallbackQueryHandler(edit_group_selected, pattern=r"^editgrp_(rep|l|c)$")],
            EDIT_FIELD: [CallbackQueryHandler(edit_field_selected, pattern=r"^editfld_(rep|l|c)_\w+$")],
            EDIT_VALUE: [MessageHandler(TEXT_FILTER, edit_value_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_user=True,
    )

    # Register handlers — order matters: conversations first, catch-all last
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("help",   help_command))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CallbackQueryHandler(report_range_selected, pattern=r"^report_(today|7d|30d|all)$"))
    app.add_handler(rep_conv)
    app.add_handler(l_conv)
    app.add_handler(c_conv)
    app.add_handler(edit_conv)

    # Catch-all: anyone not in KNOWN_IDS gets a polite rejection
    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.User(user_id=list(KNOWN_IDS)),
            unauthorized_handler,
        )
    )

    # Global error handler — catches anything an individual handler doesn't
    app.add_error_handler(error_handler)

    # Reminder job: runs every hour, only sends if 24 h have elapsed since last reminder
    app.job_queue.run_repeating(send_reminders, interval=3600, first=60)

    logger.info(f"Bot started. Authorised IDs: {KNOWN_IDS}")
    app.run_polling()


if __name__ == "__main__":
    main()
