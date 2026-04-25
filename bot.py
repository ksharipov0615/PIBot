"""
Price Indication Bot
Workflow: Representative → User L → User C → Representative
Admin receives copies at each completed stage.
"""

import logging
import sqlite3
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, filters, ContextTypes
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
USER_L_ID = int(os.getenv("USER_L_ID"))
USER_C_ID = int(os.getenv("USER_C_ID"))
ADMIN_ID  = int(os.getenv("ADMIN_ID"))

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = "requests.db"

# ── ConversationHandler states ────────────────────────────────────────────────
# Representative flow
R_PRODUCT, R_PACKAGING, R_PALLETS, R_VOLUME, R_POD, R_BASIS, R_COMMENTS = range(7)

# User L flow
L_SERVICE, L_POL, L_TERMINAL, L_LINE, L_EQUIPMENT, \
L_HANDLING, L_THC, L_FREIGHT, L_EXTRAS, L_MAXPAYLOAD, L_COMMENTS = range(10, 21)

# User C flow
C_AVAILABILITY, C_VOLUME, C_PRICE, C_ETD, C_VALIDITY, C_COMMENTS = range(30, 36)
C_CONFIRM_NO_SERVICE = 36  # used when Service=No

# ── Keyboard options ──────────────────────────────────────────────────────────
PRODUCTS   = ["SNI","SNA","PNA","NKS44","NKS43","NKSM","UMP","FeedU","TechU",
               "CNC","CNCM","CNCB","MAP","MKP","NPK11","NPK13","NPK15","NPK18",
               "NPK19","NPK20","NPK3","NPK12","NPK157","AD5","AD13","AD18","AD20"]
PACKAGINGS = ["22.7 kg","25 kg","50 kg","500 kg","800 kg","850 kg","900 kg","1000 kg"]
PALLETS    = ["Default","No","1L","2L"]
BASIS      = ["CIF","CFR","DAP","CPT","CIP","FOB","FCA"]

def chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

def kb(options, cols=3):
    return ReplyKeyboardMarkup(list(chunked(options, cols)), one_time_keyboard=True, resize_keyboard=True)

# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_id INTEGER,
            rep_name TEXT,
            status TEXT DEFAULT 'pending_L',
            -- Rep fields
            product TEXT, packaging TEXT, pallets TEXT,
            volume TEXT, pod TEXT, basis TEXT, rep_comments TEXT,
            -- L fields
            service TEXT, pol TEXT, terminal TEXT, line TEXT,
            equipment TEXT, handling TEXT, thc TEXT, freight TEXT,
            extras TEXT, max_payload TEXT, l_comments TEXT,
            -- C fields
            availability TEXT, c_volume TEXT, price TEXT,
            etd TEXT, validity TEXT, c_comments TEXT,
            -- Timestamps
            created_at TEXT, l_answered_at TEXT, c_answered_at TEXT,
            -- Message IDs for reminders
            l_msg_id INTEGER, c_msg_id INTEGER
        )
    """)
    con.commit()
    con.close()

def db():
    return sqlite3.connect(DB_PATH)

def get_request(req_id):
    con = db()
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    con.close()
    return dict(r) if r else None

def update_request(req_id, **kwargs):
    cols = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [req_id]
    con = db()
    con.execute(f"UPDATE requests SET {cols} WHERE id=?", vals)
    con.commit()
    con.close()

# ── Recap formatters ──────────────────────────────────────────────────────────
def rep_recap(r):
    return (
        f"📋 *Price Request #{r['id']}*\n"
        f"👤 From: {r['rep_name']}\n"
        f"─────────────────────\n"
        f"🧪 Product: `{r['product']}`\n"
        f"📦 Packaging: `{r['packaging']}`\n"
        f"🪵 Pallets: `{r['pallets']}`\n"
        f"⚖️ Volume: `{r['volume']} mt`\n"
        f"📍 POD: `{r['pod']}`\n"
        f"🚢 Basis: `{r['basis']}`\n"
        f"💬 Comments: {r['rep_comments'] or '—'}\n"
        f"🕐 Filed: {r['created_at']}"
    )

def l_recap(r):
    base = rep_recap(r)
    if r.get('service') == 'Yes':
        logistics = (
            f"\n─────────────────────\n"
            f"🚚 *Logistics (L)*\n"
            f"Service: Yes\n"
            f"POL: `{r['pol']}`\n"
            f"Terminal: `{r['terminal']}`\n"
            f"Line: `{r['line']}`\n"
            f"Equipment: `{r['equipment']}`\n"
            f"Handling/mt: `{r['handling']}`\n"
            f"THC/mt: `{r['thc']}`\n"
            f"Freight/cntr: `{r['freight']}`\n"
            f"Extras/mt: `{r['extras']}`\n"
            f"Max payload: `{r['max_payload']}`\n"
            f"Comments: {r['l_comments'] or '—'}"
        )
    else:
        logistics = f"\n─────────────────────\n🚚 *Logistics (L)*\nService: No"
    return base + logistics

def c_answer_for_rep(r):
    """Final message sent to rep — logistics fields stripped."""
    lines = [
        f"✅ *Price Reply — Request #{r['id']}*\n",
        f"🧪 Product: `{r['product']}`",
        f"📦 Packaging: `{r['packaging']}`",
        f"🪵 Pallets: `{r['pallets']}`",
        f"⚖️ Volume requested: `{r['volume']} mt`",
        f"📍 POD: `{r['pod']}`",
        f"🚢 Basis: `{r['basis']}`",
    ]
    if r.get('service') == 'No':
        lines.append("\n⛔ No service available for this request.")
    else:
        avail = r.get('availability')
        if avail == 'Yes':
            lines += [
                f"\n📊 *Pricing*",
                f"Availability: ✅ Yes",
                f"Volume: `{r['c_volume']} mt`",
                f"Price/mt: `{r['price']}`",
                f"ETD: `{r['etd']}`",
                f"Validity: `{r['validity']}`",
                f"Comments: {r['c_comments'] or '—'}",
            ]
        else:
            lines.append("\n❌ Product not available at this time.")
    return "\n".join(lines)

def admin_stage_copy(stage, r):
    header = {
        'filed': "🔔 *[ADMIN] New request filed*",
        'l_done': "🔔 *[ADMIN] User L completed recap*",
        'c_done': "🔔 *[ADMIN] User C sent reply to representative*",
    }[stage]
    if stage == 'filed':
        return header + "\n\n" + rep_recap(r)
    elif stage == 'l_done':
        return header + "\n\n" + l_recap(r)
    else:
        return header + "\n\n" + c_answer_for_rep(r)

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == USER_L_ID:
        role = "User L (Logistics)"
    elif uid == USER_C_ID:
        role = "User C (Pricing)"
    elif uid == ADMIN_ID:
        role = "Administrator"
    else:
        role = "Sales Representative"
    await update.message.reply_text(
        f"👋 Welcome to the *Price Indication Bot*!\nYour role: *{role}*\n\n"
        f"Use /newrequest to file a price request.\n"
        f"Use /pending to see pending requests awaiting your input.",
        parse_mode="Markdown"
    )

# ── /help ─────────────────────────────────────────────────────────────────────
async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == USER_L_ID:
        text = (
            "📖 *Your commands (Logistics):*\n\n"
            "/start — Show your role\n"
            "/pending — View requests awaiting your input\n"
            "/cancel — Abort current operation"
        )
    elif uid == USER_C_ID:
        text = (
            "📖 *Your commands (Pricing):*\n\n"
            "/start — Show your role\n"
            "/pending — View requests awaiting your input\n"
            "/cancel — Abort current operation"
        )
    elif uid == ADMIN_ID:
        text = (
            "📖 *Your commands (Admin):*\n\n"
            "/start — Show your role\n"
            "/newrequest — File a new price request\n"
            "/pending — View all open requests\n"
            "/cancel — Abort current operation"
        )
    else:
        text = (
            "📖 *Your commands (Sales Rep):*\n\n"
            "/start — Show your role\n"
            "/newrequest — File a new price request\n"
            "/pending — View your submitted requests\n"
            "/cancel — Abort current operation"
        )
    await update.message.reply_text(text, parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# REPRESENTATIVE FLOW
# ══════════════════════════════════════════════════════════════════════════════
async def new_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in (USER_L_ID, USER_C_ID, ADMIN_ID):
        await update.message.reply_text("This command is for sales representatives only.")
        return ConversationHandler.END
    ctx.user_data.clear()
    await update.message.reply_text("📋 *New Price Request*\n\nStep 1/7 — Select *Product*:", parse_mode="Markdown",
                                    reply_markup=kb(PRODUCTS, 4))
    return R_PRODUCT

async def r_product(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text not in PRODUCTS:
        await update.message.reply_text("Please select a valid product:", reply_markup=kb(PRODUCTS, 4))
        return R_PRODUCT
    ctx.user_data['product'] = update.message.text
    await update.message.reply_text("Step 2/7 — Select *Packaging*:", parse_mode="Markdown",
                                    reply_markup=kb(PACKAGINGS, 3))
    return R_PACKAGING

async def r_packaging(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text not in PACKAGINGS:
        await update.message.reply_text("Please select a valid packaging:", reply_markup=kb(PACKAGINGS, 3))
        return R_PACKAGING
    ctx.user_data['packaging'] = update.message.text
    await update.message.reply_text("Step 3/7 — Select *Pallets*:", parse_mode="Markdown",
                                    reply_markup=kb(PALLETS, 4))
    return R_PALLETS

async def r_pallets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text not in PALLETS:
        await update.message.reply_text("Please select a valid pallets option:", reply_markup=kb(PALLETS, 4))
        return R_PALLETS
    ctx.user_data['pallets'] = update.message.text
    await update.message.reply_text("Step 4/7 — Enter *Volume (mt)* — digits only:",
                                    parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return R_VOLUME

async def r_volume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.strip().isdigit():
        await update.message.reply_text("⚠️ Digits only, no decimals. Enter Volume (mt):")
        return R_VOLUME
    ctx.user_data['volume'] = update.message.text.strip()
    await update.message.reply_text("Step 5/7 — Enter *POD* (Port of Destination):", parse_mode="Markdown")
    return R_POD

async def r_pod(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['pod'] = update.message.text.strip()
    await update.message.reply_text("Step 6/7 — Select *Basis*:", parse_mode="Markdown",
                                    reply_markup=kb(BASIS, 4))
    return R_BASIS

async def r_basis(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text not in BASIS:
        await update.message.reply_text("Please select a valid basis:", reply_markup=kb(BASIS, 4))
        return R_BASIS
    ctx.user_data['basis'] = update.message.text
    await update.message.reply_text("Step 7/7 — Enter *Comments* (or type 'none'):",
                                    parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return R_COMMENTS

async def r_comments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    comments = update.message.text.strip()
    if comments.lower() == 'none':
        comments = ''
    ctx.user_data['rep_comments'] = comments

    u = update.effective_user
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    con = db()
    cur = con.execute("""
        INSERT INTO requests (rep_id, rep_name, product, packaging, pallets,
            volume, pod, basis, rep_comments, created_at, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,'pending_L')
    """, (u.id, u.full_name,
          ctx.user_data['product'], ctx.user_data['packaging'], ctx.user_data['pallets'],
          ctx.user_data['volume'], ctx.user_data['pod'], ctx.user_data['basis'],
          comments, now))
    req_id = cur.lastrowid
    con.commit()
    con.close()

    r = get_request(req_id)

    # Confirm to rep
    await update.message.reply_text(
        f"✅ *Request #{req_id} submitted!*\nWe'll get back to you with pricing.\n\n" + rep_recap(r),
        parse_mode="Markdown"
    )

    # Send to User L
    msg = await ctx.bot.send_message(
        USER_L_ID,
        f"📥 *New Price Request #{req_id}*\n\nPlease review and add logistics.\nUse /pending to respond.\n\n" + rep_recap(r),
        parse_mode="Markdown"
    )
    update_request(req_id, l_msg_id=msg.message_id)

    # Admin copy
    await ctx.bot.send_message(ADMIN_ID, admin_stage_copy('filed', r), parse_mode="Markdown")

    ctx.user_data.clear()
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# USER L FLOW
# ══════════════════════════════════════════════════════════════════════════════
async def l_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Called when User L picks a request to answer."""
    req_id = int(ctx.user_data.get('current_req_id'))
    r = get_request(req_id)
    ctx.user_data['l_req'] = req_id
    await update.message.reply_text(
        f"*Request #{req_id}*\n\nDo you provide *Service* for this request?",
        parse_mode="Markdown",
        reply_markup=kb(["Yes", "No"], 2)
    )
    return L_SERVICE

async def l_service(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text not in ("Yes", "No"):
        await update.message.reply_text("Please select Yes or No:", reply_markup=kb(["Yes","No"],2))
        return L_SERVICE
    ctx.user_data['l_service'] = update.message.text
    if update.message.text == "No":
        # No service — save and forward to C
        req_id = ctx.user_data['l_req']
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        update_request(req_id, service='No', l_answered_at=now, status='pending_C')
        r = get_request(req_id)
        await update.message.reply_text(f"✅ Saved. Request #{req_id} forwarded to User C.", reply_markup=ReplyKeyboardRemove())
        msg = await ctx.bot.send_message(USER_C_ID, f"📥 *Request #{req_id} — from User L*\n\nService: No\nPlease confirm reply to representative.\n\n" + rep_recap(r), parse_mode="Markdown")
        update_request(req_id, c_msg_id=msg.message_id)
        await ctx.bot.send_message(ADMIN_ID, admin_stage_copy('l_done', r), parse_mode="Markdown")
        return ConversationHandler.END
    await update.message.reply_text("Enter *POL* (Port of Loading):", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return L_POL

async def l_pol(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['l_pol'] = update.message.text.strip()
    await update.message.reply_text("Enter *Terminal*:", parse_mode="Markdown")
    return L_TERMINAL

async def l_terminal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['l_terminal'] = update.message.text.strip()
    await update.message.reply_text("Enter *Line*:", parse_mode="Markdown")
    return L_LINE

async def l_line(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['l_line'] = update.message.text.strip()
    await update.message.reply_text("Enter *Equipment*:", parse_mode="Markdown")
    return L_EQUIPMENT

async def l_equipment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['l_equipment'] = update.message.text.strip()
    await update.message.reply_text("Enter *Handling/mt* — digits only:", parse_mode="Markdown")
    return L_HANDLING

async def l_handling(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.strip().isdigit():
        await update.message.reply_text("⚠️ Digits only. Enter Handling/mt:")
        return L_HANDLING
    ctx.user_data['l_handling'] = update.message.text.strip()
    await update.message.reply_text("Enter *THC/mt* — digits only:", parse_mode="Markdown")
    return L_THC

async def l_thc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.strip().isdigit():
        await update.message.reply_text("⚠️ Digits only. Enter THC/mt:")
        return L_THC
    ctx.user_data['l_thc'] = update.message.text.strip()
    await update.message.reply_text("Enter *Freight/cntr* — digits only:", parse_mode="Markdown")
    return L_FREIGHT

async def l_freight(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.strip().isdigit():
        await update.message.reply_text("⚠️ Digits only. Enter Freight/cntr:")
        return L_FREIGHT
    ctx.user_data['l_freight'] = update.message.text.strip()
    await update.message.reply_text("Enter *Extras/mt* — digits only:", parse_mode="Markdown")
    return L_EXTRAS

async def l_extras(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.strip().isdigit():
        await update.message.reply_text("⚠️ Digits only. Enter Extras/mt:")
        return L_EXTRAS
    ctx.user_data['l_extras'] = update.message.text.strip()
    await update.message.reply_text("Enter *Max payload* — digits only:", parse_mode="Markdown")
    return L_MAXPAYLOAD

async def l_maxpayload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.strip().isdigit():
        await update.message.reply_text("⚠️ Digits only. Enter Max payload:")
        return L_MAXPAYLOAD
    ctx.user_data['l_maxpayload'] = update.message.text.strip()
    await update.message.reply_text("Enter *Comments* (or type 'none'):", parse_mode="Markdown")
    return L_COMMENTS

async def l_comments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    comments = update.message.text.strip()
    if comments.lower() == 'none':
        comments = ''
    req_id = ctx.user_data['l_req']
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    update_request(req_id,
        service='Yes',
        pol=ctx.user_data['l_pol'],
        terminal=ctx.user_data['l_terminal'],
        line=ctx.user_data['l_line'],
        equipment=ctx.user_data['l_equipment'],
        handling=ctx.user_data['l_handling'],
        thc=ctx.user_data['l_thc'],
        freight=ctx.user_data['l_freight'],
        extras=ctx.user_data['l_extras'],
        max_payload=ctx.user_data['l_maxpayload'],
        l_comments=comments,
        l_answered_at=now,
        status='pending_C'
    )
    r = get_request(req_id)
    await update.message.reply_text(f"✅ Saved. Request #{req_id} forwarded to User C.", reply_markup=ReplyKeyboardRemove())
    msg = await ctx.bot.send_message(USER_C_ID,
        f"📥 *Request #{req_id} — Updated by User L*\n\nPlease add pricing.\nUse /pending to respond.\n\n" + l_recap(r),
        parse_mode="Markdown"
    )
    update_request(req_id, c_msg_id=msg.message_id)
    await ctx.bot.send_message(ADMIN_ID, admin_stage_copy('l_done', r), parse_mode="Markdown")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# USER C FLOW
# ══════════════════════════════════════════════════════════════════════════════
async def c_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    req_id = int(ctx.user_data.get('current_req_id'))
    r = get_request(req_id)
    ctx.user_data['c_req'] = req_id

    if r['service'] == 'No':
        await update.message.reply_text(
            f"*Request #{req_id}* — Service is No.\n\nSend reply to representative?",
            parse_mode="Markdown",
            reply_markup=kb(["Yes, send reply", "Cancel"], 2)
        )
        return C_CONFIRM_NO_SERVICE

    await update.message.reply_text(
        f"*Request #{req_id}*\n\nIs the product *Available*?",
        parse_mode="Markdown",
        reply_markup=kb(["Yes", "No"], 2)
    )
    return C_AVAILABILITY

async def c_confirm_no_service(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "Yes, send reply":
        req_id = ctx.user_data['c_req']
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        update_request(req_id, c_answered_at=now, status='done')
        r = get_request(req_id)
        reply = c_answer_for_rep(r)
        await ctx.bot.send_message(r['rep_id'], reply, parse_mode="Markdown")
        await update.message.reply_text(f"✅ Reply sent to representative for Request #{req_id}.", reply_markup=ReplyKeyboardRemove())
        await ctx.bot.send_message(ADMIN_ID, admin_stage_copy('c_done', r), parse_mode="Markdown")
    else:
        await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def c_availability(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.text not in ("Yes", "No"):
        await update.message.reply_text("Please select Yes or No:", reply_markup=kb(["Yes","No"],2))
        return C_AVAILABILITY
    ctx.user_data['c_availability'] = update.message.text
    if update.message.text == "No":
        req_id = ctx.user_data['c_req']
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        update_request(req_id, availability='No', c_answered_at=now, status='done')
        r = get_request(req_id)
        await ctx.bot.send_message(r['rep_id'], c_answer_for_rep(r), parse_mode="Markdown")
        await update.message.reply_text(f"✅ Reply sent to representative for Request #{req_id}.", reply_markup=ReplyKeyboardRemove())
        await ctx.bot.send_message(ADMIN_ID, admin_stage_copy('c_done', r), parse_mode="Markdown")
        return ConversationHandler.END
    await update.message.reply_text("Enter *Volume,mt* — digits only:", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return C_VOLUME

async def c_volume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.strip().isdigit():
        await update.message.reply_text("⚠️ Digits only. Enter Volume,mt:")
        return C_VOLUME
    ctx.user_data['c_volume'] = update.message.text.strip()
    await update.message.reply_text("Enter *Price/mt*:", parse_mode="Markdown")
    return C_PRICE

async def c_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['c_price'] = update.message.text.strip()
    await update.message.reply_text("Enter *ETD* (Estimated Time of Departure):", parse_mode="Markdown")
    return C_ETD

async def c_etd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['c_etd'] = update.message.text.strip()
    await update.message.reply_text("Enter *Validity*:", parse_mode="Markdown")
    return C_VALIDITY

async def c_validity(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['c_validity'] = update.message.text.strip()
    await update.message.reply_text("Enter *Comments* (or type 'none'):", parse_mode="Markdown")
    return C_COMMENTS

async def c_comments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    comments = update.message.text.strip()
    if comments.lower() == 'none':
        comments = ''
    req_id = ctx.user_data['c_req']
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    update_request(req_id,
        availability='Yes',
        c_volume=ctx.user_data['c_volume'],
        price=ctx.user_data['c_price'],
        etd=ctx.user_data['c_etd'],
        validity=ctx.user_data['c_validity'],
        c_comments=comments,
        c_answered_at=now,
        status='done'
    )
    r = get_request(req_id)
    await ctx.bot.send_message(r['rep_id'], c_answer_for_rep(r), parse_mode="Markdown")
    await update.message.reply_text(f"✅ Price reply sent to representative for Request #{req_id}.", reply_markup=ReplyKeyboardRemove())
    await ctx.bot.send_message(ADMIN_ID, admin_stage_copy('c_done', r), parse_mode="Markdown")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# /pending — list open requests for L or C
# ══════════════════════════════════════════════════════════════════════════════
async def pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    con = db()
    con.row_factory = sqlite3.Row

    if uid == USER_L_ID:
        rows = con.execute("SELECT * FROM requests WHERE status='pending_L' ORDER BY id").fetchall()
        if not rows:
            await update.message.reply_text("✅ No pending requests for you.")
            con.close()
            return
        buttons = [[InlineKeyboardButton(f"Request #{r['id']} — {r['product']} {r['volume']}mt — {r['pod']}", callback_data=f"L_{r['id']}")] for r in rows]
        await update.message.reply_text("📋 *Pending requests for User L:*", parse_mode="Markdown",
                                        reply_markup=InlineKeyboardMarkup(buttons))

    elif uid == USER_C_ID:
        rows = con.execute("SELECT * FROM requests WHERE status='pending_C' ORDER BY id").fetchall()
        if not rows:
            await update.message.reply_text("✅ No pending requests for you.")
            con.close()
            return
        buttons = [[InlineKeyboardButton(f"Request #{r['id']} — {r['product']} {r['volume']}mt — {r['pod']}", callback_data=f"C_{r['id']}")] for r in rows]
        await update.message.reply_text("📋 *Pending requests for User C:*", parse_mode="Markdown",
                                        reply_markup=InlineKeyboardMarkup(buttons))

    elif uid == ADMIN_ID:
        rows = con.execute("SELECT * FROM requests WHERE status != 'done' ORDER BY id").fetchall()
        if not rows:
            await update.message.reply_text("✅ No open requests.")
            con.close()
            return
        txt = "*Open Requests (Admin view):*\n\n"
        for r in rows:
            txt += f"• Request #{r['id']} — {r['product']} — Status: `{r['status']}`\n"
        await update.message.reply_text(txt, parse_mode="Markdown")
    else:
        # Rep sees their own requests
        rep_id = uid
        rows = con.execute("SELECT * FROM requests WHERE rep_id=? ORDER BY id DESC LIMIT 10", (rep_id,)).fetchall()
        if not rows:
            await update.message.reply_text("You have no requests yet. Use /newrequest.")
            con.close()
            return
        txt = "*Your recent requests:*\n\n"
        for r in rows:
            status_label = {"pending_L":"⏳ Awaiting logistics","pending_C":"⏳ Awaiting pricing","done":"✅ Answered"}.get(r['status'], r['status'])
            txt += f"• Request #{r['id']} — {r['product']} — {status_label}\n"
        await update.message.reply_text(txt, parse_mode="Markdown")

    con.close()

# ── Inline button callback (L or C picks a request) ──────────────────────────
async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # e.g. "L_12" or "C_5"
    role, req_id = data.split("_")
    ctx.user_data['current_req_id'] = req_id
    r = get_request(int(req_id))

    # Send recap so user sees it, then trigger the conversation
    if role == "L":
        await query.message.reply_text(f"Opening Request #{req_id}...\n\n" + rep_recap(r), parse_mode="Markdown")
        await l_start(query, ctx)
    elif role == "C":
        recap = l_recap(r) if r.get('service') == 'Yes' else rep_recap(r)
        await query.message.reply_text(f"Opening Request #{req_id}...\n\n" + recap, parse_mode="Markdown")
        await c_start(query, ctx)

# ── 24h reminder job ──────────────────────────────────────────────────────────
async def send_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    cutoff = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M UTC")
    con = db()
    con.row_factory = sqlite3.Row
    # Remind L
    rows_l = con.execute(
        "SELECT * FROM requests WHERE status='pending_L' AND created_at <= ?", (cutoff,)
    ).fetchall()
    for r in rows_l:
        r = dict(r)
        try:
            await ctx.bot.send_message(USER_L_ID,
                f"⏰ *Reminder* — Request #{r['id']} has been waiting 24h!\n\nUse /pending to respond.\n\n" + rep_recap(r),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Reminder error L: {e}")

    # Remind C
    rows_c = con.execute(
        "SELECT * FROM requests WHERE status='pending_C' AND l_answered_at <= ?", (cutoff,)
    ).fetchall()
    for r in rows_c:
        r = dict(r)
        try:
            recap = l_recap(r) if r.get('service') == 'Yes' else rep_recap(r)
            await ctx.bot.send_message(USER_C_ID,
                f"⏰ *Reminder* — Request #{r['id']} has been waiting 24h!\n\nUse /pending to respond.\n\n" + recap,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Reminder error C: {e}")
    con.close()

# ── cancel ─────────────────────────────────────────────────────────────────────
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Rep conversation
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
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True
    )

    # User L conversation — triggered from inline button
    l_conv = ConversationHandler(
        entry_points=[],   # triggered programmatically via button_callback
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
        per_user=True
    )

    # User C conversation — triggered from inline button
    c_conv = ConversationHandler(
        entry_points=[],
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
        per_user=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(rep_conv)
    app.add_handler(l_conv)
    app.add_handler(c_conv)
    app.add_handler(CallbackQueryHandler(button_callback))

    # Reminder job every hour, checks for 24h+ unanswered
    app.job_queue.run_repeating(send_reminders, interval=3600, first=60)

    logger.info("Bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()
