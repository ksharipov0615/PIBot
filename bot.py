"""
Price Indication Bot — v2
Workflow: Sales Representative → User L (Logistics) → User C (Pricing) → Representative
Admin receives copies at each completed stage.

Fixes over v1:
  - ConversationHandler architecture fixed: L and C handlers now have proper
    inline-button entry points (entry_points=[] was the root cause of both
    conversations never progressing past the first message)
  - Whitelist of authorised Sales Rep IDs loaded from REP_IDS in .env
  - HTML parse mode throughout with html.escape() on all user-supplied data
  - Decimal support for all numeric fields (float validation, not isdigit)
  - ETD and Validity now validated as DD-MM-YYYY dates
  - Price/mt validated as a positive number
  - Confirmation/review step before Rep submission (new R_CONFIRM state)
  - "— Skip —" button for optional fields (comments, terminal, line, equipment)
  - Reminder throttling: last_reminded_l_at / last_reminded_c_at columns
    prevent repeated hourly spam after the 24 h mark
  - DB connections use context managers (no silent connection leaks)
  - Startup validation of required environment variables (clear error on boot)
  - callback_data split with maxsplit=1 (safe for any req_id value)
  - allow_reentry=True on all ConversationHandlers
  - Admin and L/C users blocked from /newrequest with clear message
  - Catch-all handler tells unauthorised users they are not allowed
  - ctx.user_data.clear() called consistently on conversation end
  - Status guard in l_entry and c_entry (request must still be pending)
"""

import html
import logging
import sqlite3
import os
from datetime import datetime, timedelta

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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = "requests.db"

# ── ConversationHandler states ─────────────────────────────────────────────────
# Representative flow (0–7)
R_PRODUCT, R_PACKAGING, R_PALLETS, R_VOLUME, R_POD, R_BASIS, R_COMMENTS, R_CONFIRM = range(8)

# User L flow (10–20)
(
    L_SERVICE, L_POL, L_TERMINAL, L_LINE, L_EQUIPMENT,
    L_HANDLING, L_THC, L_FREIGHT, L_EXTRAS, L_MAXPAYLOAD, L_COMMENTS,
) = range(10, 21)

# User C flow (30–36)
C_AVAILABILITY, C_VOLUME, C_PRICE, C_ETD, C_VALIDITY, C_COMMENTS = range(30, 36)
C_CONFIRM_NO_SERVICE = 36

# ── Keyboard option lists ──────────────────────────────────────────────────────
PRODUCTS = [
    "SNI", "SNA", "PNA", "NKS44", "NKS43", "NKSM", "UMP", "FeedU", "TechU",
    "CNC", "CNCM", "CNCB", "MAP", "MKP", "NPK11", "NPK13", "NPK15", "NPK18",
    "NPK19", "NPK20", "NPK3", "NPK12", "NPK157", "AD5", "AD13", "AD18", "AD20",
]
PACKAGINGS = ["22.7 kg", "25 kg", "50 kg", "500 kg", "800 kg", "850 kg", "900 kg", "1000 kg"]
PALLETS    = ["Default", "No", "1L", "2L"]
BASIS      = ["CIF", "CFR", "DAP", "CPT", "CIP", "FOB", "FCA"]
SKIP_BTN   = "— Skip —"


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


# ── Utility helpers ────────────────────────────────────────────────────────────
def e(value) -> str:
    """HTML-escape any user-supplied value before embedding in messages."""
    return html.escape(str(value or ""))


def is_valid_number(text: str) -> bool:
    """Accept positive integers and decimals (e.g. 500, 22.5, 12.50)."""
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
        con.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                rep_id              INTEGER,
                rep_name            TEXT,
                status              TEXT DEFAULT 'pending_L',
                -- Rep fields
                product             TEXT,
                packaging           TEXT,
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
                max_payload         TEXT,
                l_comments          TEXT,
                -- C fields
                availability        TEXT,
                c_volume            TEXT,
                price               TEXT,
                etd                 TEXT,
                validity            TEXT,
                c_comments          TEXT,
                -- Timestamps
                created_at          TEXT,
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
        ]:
            if col not in existing_cols:
                con.execute(f"ALTER TABLE requests ADD COLUMN {col} {typedef}")
        con.commit()


def get_request(req_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    return dict(row) if row else None


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
    return (
        f"📋 <b>Price Request #{r['id']}</b>\n"
        f"👤 From: {e(r['rep_name'])}\n"
        f"─────────────────────\n"
        f"🧪 Product: <code>{e(r['product'])}</code>\n"
        f"📦 Packaging: <code>{e(r['packaging'])}</code>\n"
        f"🪵 Pallets: <code>{e(r['pallets'])}</code>\n"
        f"⚖️ Volume: <code>{e(r['volume'])} mt</code>\n"
        f"📍 POD: <code>{e(r['pod'])}</code>\n"
        f"🚢 Basis: <code>{e(r['basis'])}</code>\n"
        f"💬 Comments: {e(r['rep_comments']) or '—'}\n"
        f"🕐 Filed: {e(r['created_at'])}"
    )


def l_recap(r: dict) -> str:
    base = rep_recap(r)
    if r.get("service") == "Yes":
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
            f"Freight/cntr: <code>{e(r['freight'])}</code>\n"
            f"Extras/mt: <code>{e(r['extras'])}</code>\n"
            f"Max payload: <code>{e(r['max_payload'])} mt</code>\n"
            f"Comments: {e(r['l_comments']) or '—'}"
        )
    else:
        logistics = "\n─────────────────────\n🚚 <b>Logistics (L)</b>\nService: No"
    return base + logistics


def c_answer_for_rep(r: dict) -> str:
    lines = [
        f"✅ <b>Price Reply — Request #{r['id']}</b>\n",
        f"🧪 Product: <code>{e(r['product'])}</code>",
        f"📦 Packaging: <code>{e(r['packaging'])}</code>",
        f"🪵 Pallets: <code>{e(r['pallets'])}</code>",
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
    header = {
        "filed":  "🔔 <b>[ADMIN] New request filed</b>",
        "l_done": "🔔 <b>[ADMIN] User L completed recap</b>",
        "c_done": "🔔 <b>[ADMIN] User C sent reply to representative</b>",
    }[stage]
    body = {
        "filed":  rep_recap(r),
        "l_done": l_recap(r),
        "c_done": c_answer_for_rep(r),
    }[stage]
    return header + "\n\n" + body


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
        cmds = "/pending — view requests awaiting your input"
    elif uid == USER_C_ID:
        role = "User C (Pricing)"
        cmds = "/pending — view requests awaiting your input"
    elif uid == ADMIN_ID:
        role = "Administrator"
        cmds = "/pending — view all open requests"
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
            "/cancel — abort current operation"
        )
    elif uid == USER_C_ID:
        text = (
            "📖 <b>Commands (Pricing):</b>\n\n"
            "/start — show your role\n"
            "/pending — requests awaiting your input\n"
            "/cancel — abort current operation"
        )
    elif uid == ADMIN_ID:
        text = (
            "📖 <b>Commands (Admin):</b>\n\n"
            "/start — show your role\n"
            "/pending — view all open requests\n"
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
            rows = con.execute(
                "SELECT * FROM requests WHERE status='pending_C' ORDER BY id"
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
            txt = "<b>Open Requests (Admin view):</b>\n\n"
            for r in rows:
                txt += (
                    f"• Request #{r['id']} — {e(r['product'])} — "
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
                    "pending_L": "⏳ Awaiting logistics",
                    "pending_C": "⏳ Awaiting pricing",
                    "done":      "✅ Answered",
                }.get(r["status"], r["status"])
                txt += f"• Request #{r['id']} — {e(r['product'])} — {label}\n"
            await update.message.reply_text(txt, parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
# REPRESENTATIVE FLOW
# ══════════════════════════════════════════════════════════════════════════════
async def new_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = uid_of(update)
    if uid not in KNOWN_IDS:
        await deny(update)
        return ConversationHandler.END
    if uid in (USER_L_ID, USER_C_ID, ADMIN_ID):
        await update.message.reply_text(
            "ℹ️ /newrequest is for Sales Representatives only."
        )
        return ConversationHandler.END

    ctx.user_data.clear()
    await update.message.reply_text(
        "📋 <b>New Price Request</b>\n\nStep 1/7 — Select <b>Product</b>:",
        parse_mode="HTML",
        reply_markup=kb(PRODUCTS, 4),
    )
    return R_PRODUCT


async def r_product(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text not in PRODUCTS:
        await update.message.reply_text(
            "Please select a valid product from the keyboard:",
            reply_markup=kb(PRODUCTS, 4),
        )
        return R_PRODUCT
    ctx.user_data["product"] = update.message.text
    await update.message.reply_text(
        "Step 2/7 — Select <b>Packaging</b>:",
        parse_mode="HTML",
        reply_markup=kb(PACKAGINGS, 3),
    )
    return R_PACKAGING


async def r_packaging(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text not in PACKAGINGS:
        await update.message.reply_text(
            "Please select a valid packaging:",
            reply_markup=kb(PACKAGINGS, 3),
        )
        return R_PACKAGING
    ctx.user_data["packaging"] = update.message.text
    await update.message.reply_text(
        "Step 3/7 — Select <b>Pallets</b>:",
        parse_mode="HTML",
        reply_markup=kb(PALLETS, 4),
    )
    return R_PALLETS


async def r_pallets(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text not in PALLETS:
        await update.message.reply_text(
            "Please select a valid pallets option:",
            reply_markup=kb(PALLETS, 4),
        )
        return R_PALLETS
    ctx.user_data["pallets"] = update.message.text
    await update.message.reply_text(
        "Step 4/7 — Enter <b>Volume (mt)</b>:",
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
    await update.message.reply_text(
        "Step 5/7 — Enter <b>POD</b> (Port of Destination):",
        parse_mode="HTML",
    )
    return R_POD


async def r_pod(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["pod"] = update.message.text.strip()
    await update.message.reply_text(
        "Step 6/7 — Select <b>Basis</b>:",
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
    await update.message.reply_text(
        "Step 7/7 — Enter <b>Comments</b> or tap Skip:",
        parse_mode="HTML",
        reply_markup=kb_skip(),
    )
    return R_COMMENTS


async def r_comments(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    ctx.user_data["rep_comments"] = "" if text == SKIP_BTN else text

    d = ctx.user_data
    summary = (
        f"Please review your request:\n\n"
        f"🧪 Product: <code>{e(d['product'])}</code>\n"
        f"📦 Packaging: <code>{e(d['packaging'])}</code>\n"
        f"🪵 Pallets: <code>{e(d['pallets'])}</code>\n"
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
    req_id = insert_request(
        rep_id=u.id,
        rep_name=u.full_name,
        product=d["product"],
        packaging=d["packaging"],
        pallets=d["pallets"],
        volume=d["volume"],
        pod=d["pod"],
        basis=d["basis"],
        rep_comments=d["rep_comments"],
        created_at=now_utc(),
        status="pending_L",
    )
    r = get_request(req_id)

    await update.message.reply_text(
        f"✅ <b>Request #{req_id} submitted!</b>\n"
        f"We will get back to you with pricing.\n\n" + rep_recap(r),
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )

    msg = await ctx.bot.send_message(
        USER_L_ID,
        f"📥 <b>New Price Request #{req_id}</b>\n\n"
        f"Please review and add logistics.\n"
        f"Use /pending to respond.\n\n" + rep_recap(r),
        parse_mode="HTML",
    )
    update_request(req_id, l_msg_id=msg.message_id)
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
        await ctx.bot.send_message(ADMIN_ID, admin_stage_copy("l_done", r), parse_mode="HTML")
        ctx.user_data.clear()
        return ConversationHandler.END

    # Service = Yes → collect logistics details
    await update.message.reply_text(
        "Enter <b>POL</b> (Port of Loading):",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    return L_POL


async def l_pol(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
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
    if not is_valid_number(text):
        await update.message.reply_text(
            "⚠️ Please enter a valid positive number (e.g. 12 or 12.50):"
        )
        return L_HANDLING
    ctx.user_data["l_handling"] = text
    await update.message.reply_text("Enter <b>THC/mt</b> (USD):", parse_mode="HTML")
    return L_THC


async def l_thc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_number(text):
        await update.message.reply_text("⚠️ Please enter a valid positive number:")
        return L_THC
    ctx.user_data["l_thc"] = text
    await update.message.reply_text("Enter <b>Freight/cntr</b> (USD):", parse_mode="HTML")
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
    if not is_valid_number(text):
        await update.message.reply_text("⚠️ Please enter a valid positive number:")
        return L_EXTRAS
    ctx.user_data["l_extras"] = text
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
        max_payload=d["l_maxpayload"],
        l_comments=comments,
        l_answered_at=now_utc(),
        status="pending_C",
    )
    r = get_request(req_id)

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
    await ctx.bot.send_message(ADMIN_ID, admin_stage_copy("l_done", r), parse_mode="HTML")
    ctx.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# USER C FLOW
# Entry point: CallbackQueryHandler matching "C_<id>" from /pending buttons.
# ══════════════════════════════════════════════════════════════════════════════
async def c_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: User C taps an inline button produced by /pending."""
    query = update.callback_query
    await query.answer()

    if uid_of(update) != USER_C_ID:
        await query.message.reply_text("⛔ Not authorised.")
        return ConversationHandler.END

    req_id = int(query.data.split("_", 1)[1])
    r = get_request(req_id)

    if r is None:
        await query.message.reply_text("⚠️ Request not found.")
        return ConversationHandler.END
    if r["status"] != "pending_C":
        await query.message.reply_text(
            f"⚠️ Request #{req_id} is no longer pending your input (status: {r['status']})."
        )
        return ConversationHandler.END

    ctx.user_data.clear()
    ctx.user_data["c_req"] = req_id

    recap = l_recap(r) if r.get("service") == "Yes" else rep_recap(r)
    await query.message.reply_text(
        f"<b>Opening Request #{req_id}</b>\n\n" + recap,
        parse_mode="HTML",
    )

    if r["service"] == "No":
        await query.message.reply_text(
            f"<b>Request #{req_id}</b> — Logistics: No service.\n\n"
            f"Send the 'no service' reply to the representative now?",
            parse_mode="HTML",
            reply_markup=kb(["Yes, send reply", "Cancel"], 2),
        )
        return C_CONFIRM_NO_SERVICE

    await query.message.reply_text(
        f"<b>Request #{req_id}</b>\n\nIs the product <b>Available</b>?",
        parse_mode="HTML",
        reply_markup=kb(["Yes", "No"], 2),
    )
    return C_AVAILABILITY


async def c_confirm_no_service(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    req_id = ctx.user_data["c_req"]
    if update.message.text == "Yes, send reply":
        update_request(req_id, c_answered_at=now_utc(), status="done")
        r = get_request(req_id)
        await ctx.bot.send_message(r["rep_id"], c_answer_for_rep(r), parse_mode="HTML")
        await update.message.reply_text(
            f"✅ Reply sent to representative for Request #{req_id}.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await ctx.bot.send_message(ADMIN_ID, admin_stage_copy("c_done", r), parse_mode="HTML")
    else:
        await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())

    ctx.user_data.clear()
    return ConversationHandler.END


async def c_availability(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text not in ("Yes", "No"):
        await update.message.reply_text(
            "Please select Yes or No:", reply_markup=kb(["Yes", "No"], 2)
        )
        return C_AVAILABILITY

    if update.message.text == "No":
        req_id = ctx.user_data["c_req"]
        update_request(req_id, availability="No", c_answered_at=now_utc(), status="done")
        r = get_request(req_id)
        await ctx.bot.send_message(r["rep_id"], c_answer_for_rep(r), parse_mode="HTML")
        await update.message.reply_text(
            f"✅ Reply sent to representative for Request #{req_id}.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await ctx.bot.send_message(ADMIN_ID, admin_stage_copy("c_done", r), parse_mode="HTML")
        ctx.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text(
        "Enter <b>Volume</b> (mt, e.g. 500 or 22.5):",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    return C_VOLUME


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
        availability="Yes",
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
    await update.message.reply_text(
        f"✅ Price reply sent to representative for Request #{req_id}.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await ctx.bot.send_message(ADMIN_ID, admin_stage_copy("c_done", r), parse_mode="HTML")
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

        # Remind C: l_answered > 24 h ago AND (never reminded OR last reminder > 24 h ago)
        rows_c = con.execute("""
            SELECT * FROM requests
            WHERE  status = 'pending_C'
              AND  l_answered_at <= ?
              AND  (last_reminded_c_at IS NULL OR last_reminded_c_at <= ?)
        """, (cutoff, cutoff)).fetchall()

        for row in rows_c:
            r = dict(row)
            try:
                recap = l_recap(r) if r.get("service") == "Yes" else rep_recap(r)
                await ctx.bot.send_message(
                    USER_C_ID,
                    f"⏰ <b>Reminder</b> — Request #{r['id']} has been waiting 24 h!\n\n"
                    f"Use /pending to respond.\n\n" + recap,
                    parse_mode="HTML",
                )
                con.execute(
                    "UPDATE requests SET last_reminded_c_at=? WHERE id=?",
                    (now_utc(), r["id"]),
                )
                con.commit()
            except Exception as exc:
                logger.error(f"Reminder C error for request #{r['id']}: {exc}")


# ── /cancel ────────────────────────────────────────────────────────────────────
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ── Catch-all for unauthorised users ──────────────────────────────────────────
async def unauthorized_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await deny(update)


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
            R_PRODUCT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, r_product)],
            R_PACKAGING: [MessageHandler(filters.TEXT & ~filters.COMMAND, r_packaging)],
            R_PALLETS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, r_pallets)],
            R_VOLUME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, r_volume)],
            R_POD:       [MessageHandler(filters.TEXT & ~filters.COMMAND, r_pod)],
            R_BASIS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, r_basis)],
            R_COMMENTS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, r_comments)],
            R_CONFIRM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, r_confirm)],
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
            L_SERVICE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, l_service)],
            L_POL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, l_pol)],
            L_TERMINAL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, l_terminal)],
            L_LINE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, l_line)],
            L_EQUIPMENT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, l_equipment)],
            L_HANDLING:   [MessageHandler(filters.TEXT & ~filters.COMMAND, l_handling)],
            L_THC:        [MessageHandler(filters.TEXT & ~filters.COMMAND, l_thc)],
            L_FREIGHT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, l_freight)],
            L_EXTRAS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, l_extras)],
            L_MAXPAYLOAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, l_maxpayload)],
            L_COMMENTS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, l_comments)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_user=True,
    )

    # ── User C conversation ────────────────────────────────────────────────────
    c_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(c_entry, pattern=r"^C_\d+$")],
        states={
            C_CONFIRM_NO_SERVICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_confirm_no_service)],
            C_AVAILABILITY:       [MessageHandler(filters.TEXT & ~filters.COMMAND, c_availability)],
            C_VOLUME:             [MessageHandler(filters.TEXT & ~filters.COMMAND, c_volume)],
            C_PRICE:              [MessageHandler(filters.TEXT & ~filters.COMMAND, c_price)],
            C_ETD:                [MessageHandler(filters.TEXT & ~filters.COMMAND, c_etd)],
            C_VALIDITY:           [MessageHandler(filters.TEXT & ~filters.COMMAND, c_validity)],
            C_COMMENTS:           [MessageHandler(filters.TEXT & ~filters.COMMAND, c_comments)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_user=True,
    )

    # Register handlers — order matters: conversations first, catch-all last
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("help",   help_command))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(rep_conv)
    app.add_handler(l_conv)
    app.add_handler(c_conv)

    # Catch-all: anyone not in KNOWN_IDS gets a polite rejection
    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.User(user_id=list(KNOWN_IDS)),
            unauthorized_handler,
        )
    )

    # Reminder job: runs every hour, only sends if 24 h have elapsed since last reminder
    app.job_queue.run_repeating(send_reminders, interval=3600, first=60)

    logger.info(f"Bot started. Authorised IDs: {KNOWN_IDS}")
    app.run_polling()


if __name__ == "__main__":
    main()
