import os
import re
import io
import json
import time
import base64
import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncpg
import aiohttp

try:
    from anthropic import AsyncAnthropic
except Exception:  # package not installed -> AI intake simply stays disabled
    AsyncAnthropic = None

# Load a local .env if present (does not override real env vars on the host).
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ============================================================
# CONFIG / ENV
# ============================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0") or "0")
# The shop owner: tickets can be escalated ("forwarded") to this role, after
# which the customer is locked out and the owner sends the final logins.
# Falls back to the server owner / admins if unset.
OWNER_ROLE_ID = int(os.getenv("OWNER_ROLE_ID", "0") or "0")

CLAIM_CATEGORY_ID = int(os.getenv("CLAIM_CATEGORY_ID", "0") or "0")
CUSTOM_CATEGORY_ID = int(os.getenv("CUSTOM_CATEGORY_ID", "0") or "0")
SUPPORT_CATEGORY_ID = int(os.getenv("SUPPORT_CATEGORY_ID", "0") or "0")

TICKET_LOG_CHANNEL_ID = int(os.getenv("TICKET_LOG_CHANNEL_ID", "0") or "0")
# Where ticket transcripts land — and where staff-only order notices ("buy this
# listing and deliver it") are posted, so the customer never sees them inside
# their own ticket. Falls back to the ticket log channel when unset.
TRANSCRIPT_CHANNEL_ID = int(os.getenv("TRANSCRIPT_CHANNEL_ID", "0") or "0") or TICKET_LOG_CHANNEL_ID

# Reviews/vouches channel: when a customer posts here, their open ticket
# auto-closes and they receive the customer role.
REPS_CHANNEL_ID = int(os.getenv("REPS_CHANNEL_ID", "1485262743492624578") or "0")
# Role granted to a customer once they leave a review.
CUSTOMER_ROLE_ID = int(os.getenv("CUSTOMER_ROLE_ID", "1485241355629367420") or "0")
# Terms of Service channel — linked again at card checkout.
TOS_CHANNEL_ID = int(os.getenv("TOS_CHANNEL_ID", "1485235773606199326") or "0")

# --- Automation / housekeeping ---
# Auto-close tickets idle for this many hours (0 = never auto-close).
IDLE_CLOSE_HOURS = float(os.getenv("IDLE_CLOSE_HOURS", "48") or "48")
# Hours after a delivery before we DM the buyer a review reminder (0 = off).
REVIEW_REMINDER_HOURS = float(os.getenv("REVIEW_REMINDER_HOURS", "12") or "12")
# Low-stock alerting: warn when valid stock in a game drops below this count.
LOW_STOCK_THRESHOLD = int(os.getenv("LOW_STOCK_THRESHOLD", "2") or "2")
LOW_STOCK_CATEGORIES = [c.strip() for c in
                        os.getenv("LOW_STOCK_CATEGORIES", "fortnite,valorant").split(",") if c.strip()]
# Optional role granted with /compensation (e.g. a discount role).
COMPENSATION_ROLE_ID = int(os.getenv("COMPENSATION_ROLE_ID", "0") or "0")
# Warranty text shown on delivery and in replacement tickets.
WARRANTY_TEXT = os.getenv("WARRANTY_TEXT", "24-hour warranty & replacement support")

# Replacement / warranty policy (from the shop's Terms of Service). Shown to the
# customer and used by the replacement assistant to judge eligibility.
REPLACEMENT_RULES = (
    "• Replacements are **ONLY** for **invalid** or **banned** accounts.\n"
    "• **No refunds.**\n"
    "• **No review = no warranty** — if you didn't leave a review, your warranty is void.\n"
    "• **Full-Access accounts:** a **recording is REQUIRED** (before receiving, logging in, "
    "securing, recovery ticket, first match). No recording = no replacement.\n"
    "• False info / false reviews may be refused and can result in a timeout."
)

STATUS_ROTATE_SECONDS = int(os.getenv("STATUS_ROTATE_SECONDS", "15") or "15")
DELETE_COUNTDOWN_SECONDS = int(os.getenv("DELETE_COUNTDOWN_SECONDS", "5") or "5")

AF_BLUE = 0x1E90FF

# Image assets are bundled with the bot and attached to each message so Discord
# hosts them itself. External links (Imgur, etc.) often refuse to embed in
# Discord, so we ship the PNGs next to bot.py and reference them via
# attachment://. A direct image URL can still override each via env var.
LOGO_FILENAME = "af_logo_black.png"
BANNER_FILENAME = "af_tickets.png"

# Resolve asset paths absolutely (relative to this file), so the files are found
# regardless of the process working directory — which often differs from the
# project folder on hosts / worker processes.
ASSET_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(ASSET_DIR, LOGO_FILENAME)
BANNER_PATH = os.path.join(ASSET_DIR, BANNER_FILENAME)

AF_LOGO_URL = os.getenv("AF_LOGO_URL", "")     # optional direct-URL override
AF_BANNER_URL = os.getenv("AF_BANNER_URL", "")  # optional direct-URL override

# ============================================================
# AUTO-DELIVERY / AI / INTEGRATIONS CONFIG
# Every feature below is OFF until its env vars are set, so the bot keeps
# behaving exactly as before until you opt in.
# ============================================================
# --- Claude (Anthropic) AI intake assistant ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "claude-sonnet-5")  # set claude-opus-4-8 for max quality
AI_ENABLED = bool(ANTHROPIC_API_KEY) and AsyncAnthropic is not None
# Card payments carry a surcharge: customer pays account price × this multiplier.
CARD_TAX_MULTIPLIER = float(os.getenv("CARD_TAX_MULTIPLIER", "1.20") or "1.20")

# --- SellAuth (payment verification — the trusted proof) ---
SELLAUTH_API_KEY = os.getenv("SELLAUTH_API_KEY", "")
SELLAUTH_SHOP_ID = os.getenv("SELLAUTH_SHOP_ID", "")
SELLAUTH_API_BASE = os.getenv("SELLAUTH_API_BASE", "https://api.sellauth.com/v1").rstrip("/")
# Invoice statuses that count as "paid". SellAuth uses "completed" for a finished order.
SELLAUTH_PAID_STATUSES = {
    s.strip().lower()
    for s in os.getenv("SELLAUTH_PAID_STATUSES", "completed,paid").split(",")
    if s.strip()
}
SELLAUTH_ENABLED = bool(SELLAUTH_API_KEY and SELLAUTH_SHOP_ID)

# --- LZT.market (your account stock source) ---
LZT_API_TOKEN = os.getenv("LZT_API_TOKEN", "")
LZT_USER_ID = os.getenv("LZT_USER_ID", "")  # your numeric lzt.market user id (for /user/{id}/orders)
LZT_API_BASE = os.getenv("LZT_API_BASE", "https://api.lzt.market").rstrip("/")
LZT_ENABLED = bool(LZT_API_TOKEN)

# Customer-facing category name -> LZT.market category_id (verified against the live API).
# Valorant accounts live under the "riot" category (13).
LZT_CATEGORY_IDS = {
    "fortnite": 9,
    "valorant": 13,
    "riot": 13,
    "steam": 1,
    "socialclub": 7,
    "llm": 6,
}
# Owned-account tag titles on LZT.market that mean "do NOT deliver this".
LZT_BAD_TAGS = {"invalid", "resold"}

# Customer-facing category -> LZT.market search URL slug (for browsing listings to resell).
# Not limited to Valorant/Fortnite — the AI can shop any of these on LZT.
LZT_MARKET_SLUGS = {
    "valorant": "riot",     # riot category holds Valorant + LoL
    "lol": "riot",
    "fortnite": "fortnite",
    "steam": "steam",
    "rockstar": "socialclub",   # GTA V / RDR2 / Social Club
    "gta": "socialclub",
    "socialclub": "socialclub",
    "epicgames": "epicgames",
    "epic": "epicgames",
    "minecraft": "minecraft",
    "roblox": "roblox",
    "ea": "origin",
    "origin": "origin",
    "ubisoft": "uplay",
    "uplay": "uplay",
    "battlenet": "battlenet",
    "chatgpt": "chatgpt",
    "supercell": "supercell",
    "telegram": "telegram",
    "discord": "discord",
    "tiktok": "tiktok",
    "genshin": "mihoyo",
    "mihoyo": "mihoyo",
    "tarkov": "escape-from-tarkov",
    "wot": "world-of-tanks",
    "spotify": "spotify",
}
# Nicer display labels for the categories above.
LZT_GAME_LABELS = {
    "valorant": "Valorant", "lol": "League of Legends", "fortnite": "Fortnite",
    "steam": "Steam", "rockstar": "Rockstar / GTA", "gta": "GTA", "socialclub": "Rockstar",
    "epicgames": "Epic Games", "epic": "Epic Games", "minecraft": "Minecraft", "roblox": "Roblox",
    "ea": "EA App", "origin": "EA / Origin", "ubisoft": "Ubisoft", "uplay": "Ubisoft",
    "battlenet": "Battle.net", "chatgpt": "ChatGPT", "supercell": "Supercell",
    "telegram": "Telegram", "discord": "Discord", "tiktok": "TikTok", "genshin": "Genshin Impact",
    "mihoyo": "miHoYo", "tarkov": "Escape from Tarkov", "wot": "World of Tanks", "spotify": "Spotify",
}


def game_label(key: str) -> str:
    return LZT_GAME_LABELS.get((key or "").lower(), (key or "Account").title())
# Resale markup: we sell to the customer at >= this multiple of the LZT source price.
RESALE_MULTIPLIER = float(os.getenv("RESALE_MULTIPLIER", "2.5") or "2.5")
# Where "buy this now to restock" alerts go. If unset, the alert DMs the staff who triggered it.
RESTOCK_CHANNEL_ID = int(os.getenv("RESTOCK_CHANNEL_ID", "0") or "0")

# --- Email (inbox) letter reading: fetch verification letters via IMAP using the
# account's email:pass, so we never need to hand out an LZT email link. ---
EMAIL_LETTERS_ENABLED = os.getenv("EMAIL_LETTERS_ENABLED", "1").lower() not in ("0", "false", "no", "")
# Wall-clock budget for one inbox read. Some mailboxes hold huge letters and a
# full scan can outlast Discord's interaction window, so we return what we have.
EMAIL_FETCH_BUDGET_SECONDS = float(os.getenv("EMAIL_FETCH_BUDGET_SECONDS", "45") or "45")
# Fallback IMAP host when the email domain isn't in the map below (else imap.<domain>).
IMAP_HOST_DEFAULT = os.getenv("IMAP_HOST_DEFAULT", "")
# Known email-domain → IMAP host (the providers commonly attached to shop accounts).
IMAP_HOSTS = {
    "rambler.ru": "imap.rambler.ru", "myrambler.ru": "imap.rambler.ru",
    "autorambler.ru": "imap.rambler.ru", "ro.ru": "imap.rambler.ru", "lenta.ru": "imap.rambler.ru",
    "mail.ru": "imap.mail.ru", "internet.ru": "imap.mail.ru", "bk.ru": "imap.mail.ru",
    "inbox.ru": "imap.mail.ru", "list.ru": "imap.mail.ru",
    "gmx.com": "imap.gmx.com", "gmx.net": "imap.gmx.net", "gmx.de": "imap.gmx.net", "gmx.us": "imap.gmx.com",
    # Outlook / Microsoft (note: consumer accounts often need an app password for IMAP).
    "outlook.com": "outlook.office365.com", "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com", "hotmail.co.uk": "outlook.office365.com",
    "outlook.fr": "outlook.office365.com", "outlook.de": "outlook.office365.com",
    "gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
    # Firstmail — many resold domains all route through imap.firstmail.ltd.
    "firstmail.ltd": "imap.firstmail.ltd", "firstmail.com": "imap.firstmail.ltd",
    "fmailler.com": "imap.firstmail.ltd", "fmailler.ltd": "imap.firstmail.ltd",
    "dfirstmail.com": "imap.firstmail.ltd", "firstmail.co": "imap.firstmail.ltd",
    "vfirstmail.com": "imap.firstmail.ltd", "sfirstmail.com": "imap.firstmail.ltd",
    # Notletters.
    "notletters.com": "imap.notletters.com", "notletters.net": "imap.notletters.com",
    "yahoo.com": "imap.mail.yahoo.com",
}
# Any domains containing these substrings map to the given IMAP host (catch-all for
# providers that sell mailboxes across many random domains, e.g. Firstmail).
IMAP_HOST_SUBSTR = {
    "firstmail": "imap.firstmail.ltd",
    "notletters": "imap.notletters.com",
}
# Extra domain→host mappings from env, e.g. IMAP_EXTRA_HOSTS="dom1.com=imap.x.com,dom2.io=mail.y.io"
for _pair in os.getenv("IMAP_EXTRA_HOSTS", "").split(","):
    if "=" in _pair:
        _d, _h = _pair.split("=", 1)
        if _d.strip() and _h.strip():
            IMAP_HOSTS[_d.strip().lower()] = _h.strip()

# --- NowPayments (crypto checkout for custom orders) ---
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_API_BASE = os.getenv("NOWPAYMENTS_API_BASE", "https://api.nowpayments.io/v1").rstrip("/")
NOWPAYMENTS_PRICE_CURRENCY = os.getenv("NOWPAYMENTS_PRICE_CURRENCY", "eur").lower()
NOWPAYMENTS_ENABLED = bool(NOWPAYMENTS_API_KEY)
# Coins customers can pay with -> NowPayments pay_currency ticker.
CRYPTO_CHOICES = {"LTC": "ltc", "SOL": "sol", "BTC": "btc", "ETH": "eth"}
# NowPayments statuses that mean the customer has paid.
NP_PAID_STATUSES = {"confirmed", "sending", "finished", "partially_paid"}

# --- Delivery policy ---
# You chose "AI gathers + staff approves": payment is verified automatically but a
# human clicks Approve before any account leaves stock. Set AUTO_DELIVER=1 to skip
# the human step once SellAuth confirms payment.
AUTO_DELIVER = os.getenv("AUTO_DELIVER", "0") not in ("0", "false", "False", "")
# Require a verified SellAuth order before delivery is even offered to staff.
REQUIRE_SELLAUTH = os.getenv("REQUIRE_SELLAUTH", "1") not in ("0", "false", "False", "")

# --- Card payment example images (shown in /card) ---
# Bundled locally and attached so they always render (Imgur often refuses to embed
# directly in Discord). Env vars can override with direct image URLs if preferred.
CARD_PROOF_PAYMENT_FILENAME = "card_proof_payment.png"
CARD_PROOF_EMAIL_FILENAME = "card_proof_email.png"
CARD_PROOF_PAYMENT_PATH = os.path.join(ASSET_DIR, CARD_PROOF_PAYMENT_FILENAME)
CARD_PROOF_EMAIL_PATH = os.path.join(ASSET_DIR, CARD_PROOF_EMAIL_FILENAME)
CARD_PROOF_PAYMENT_URL = os.getenv("CARD_PROOF_PAYMENT_URL", "")
CARD_PROOF_EMAIL_URL = os.getenv("CARD_PROOF_EMAIL_URL", "")

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if AI_ENABLED else None
# Channels currently being processed by the AI, so two quick messages don't double-fire.
ai_locks: set[int] = set()

# AI shopping: the last set of accounts shown in a ticket, so a customer can
# pick "the 2nd one" and we know which listing that maps to.
# channel_id -> [{"item_id", "price", "title", "game"}], most recent shown first.
shop_offers: dict[int, list[dict]] = {}

# --- Account delivery cooldown (per staff member) ---
# A single staff member may release at most one account every
# DELIVERY_COOLDOWN_SECONDS, so an accidental double-click or a rushed hand-out
# can't fire twice back to back. Kept in memory (resets on restart, which is
# fine — the cooldown is only meant to smooth out rapid-fire deliveries).
DELIVERY_COOLDOWN_SECONDS = int(os.getenv("DELIVERY_COOLDOWN_SECONDS", "300") or "300")
_staff_delivery_cooldown: dict[int, datetime] = {}


def logo_ref() -> str:
    return AF_LOGO_URL or f"attachment://{LOGO_FILENAME}"


def banner_ref() -> str:
    return AF_BANNER_URL or f"attachment://{BANNER_FILENAME}"


def embed_files(include_banner: bool = False) -> list[discord.File]:
    """Fresh File objects to attach alongside an embed. Single-use, so build new
    ones for every send. Skipped when an env URL override is set or file missing."""
    files: list[discord.File] = []
    if not AF_LOGO_URL and os.path.exists(LOGO_PATH):
        files.append(discord.File(LOGO_PATH, filename=LOGO_FILENAME))
    if include_banner and not AF_BANNER_URL and os.path.exists(BANNER_PATH):
        files.append(discord.File(BANNER_PATH, filename=BANNER_FILENAME))
    return files

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL")
if not GUILD_ID:
    raise RuntimeError("Missing GUILD_ID")


# ============================================================
# DISCORD BOT
# ============================================================
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

db_pool: asyncpg.Pool | None = None


# ============================================================
# DATABASE SCHEMA
# ============================================================
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ticket_counters (
  kind TEXT PRIMARY KEY,
  next_num INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
  channel_id BIGINT PRIMARY KEY,
  guild_id BIGINT NOT NULL,
  owner_id BIGINT NOT NULL,
  kind TEXT NOT NULL,
  ticket_num INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_activity TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  claimed_by BIGINT NULL,
  first_staff_response_seconds INTEGER NULL,
  control_message_id BIGINT NULL,
  last_footer_text TEXT NULL,
  last_topic_text TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_open_owner_kind
ON tickets (guild_id, owner_id, kind)
WHERE status='open';

CREATE INDEX IF NOT EXISTS idx_status
ON tickets (status);

ALTER TABLE tickets ADD COLUMN IF NOT EXISTS claimed_by BIGINT NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS first_staff_response_seconds INTEGER NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS control_message_id BIGINT NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS last_footer_text TEXT NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS last_topic_text TEXT NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS ai_handled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS verified_order_id TEXT NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS verified_product TEXT NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS reserved_market_item_id BIGINT NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS restock_alerted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS crypto_payment_id TEXT NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS crypto_amount NUMERIC NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS crypto_paid BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS forwarded_to_owner BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS forwarded_by BIGINT NULL;
-- AI checkout: set when the customer is expected to upload payment/replacement
-- proof, so an uploaded attachment is treated as proof (→ forward to owner).
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS awaiting_proof BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS checkout_method TEXT NULL;
-- Kill-switch: when TRUE, all AI automation (intake, shopping, proof auto-forward)
-- is off for this ticket so staff can take over manually.
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS ai_disabled BOOLEAN NOT NULL DEFAULT FALSE;
-- Exact amount the customer must pay for the current checkout (used to verify proof).
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS checkout_total NUMERIC NULL;
-- Replacement proof progress: NULL/'payment' → 'video' → forwarded.
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS proof_stage TEXT NULL;
-- How many times unclear/invalid proof was uploaded (to escalate to staff).
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS proof_attempts INTEGER NOT NULL DEFAULT 0;
-- Set once the "buy this account and deliver it" notice has been posted in the
-- transcript channel, so payment + forward don't post it twice.
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS order_notice_sent BOOLEAN NOT NULL DEFAULT FALSE;

-- Record of every account released, so a SellAuth order can never be used twice.
CREATE TABLE IF NOT EXISTS deliveries (
  id SERIAL PRIMARY KEY,
  channel_id BIGINT NOT NULL,
  owner_id BIGINT NOT NULL,
  product TEXT NULL,
  order_id TEXT NULL,
  lzt_item_id BIGINT NULL,
  delivered_by BIGINT NULL,
  delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_order
ON deliveries (order_id) WHERE order_id IS NOT NULL;
-- Price charged (for /sales revenue) and whether we've sent the review reminder.
ALTER TABLE deliveries ADD COLUMN IF NOT EXISTS price NUMERIC NULL;
ALTER TABLE deliveries ADD COLUMN IF NOT EXISTS review_reminded BOOLEAN NOT NULL DEFAULT FALSE;

-- Compensation grants logged from /compensation.
CREATE TABLE IF NOT EXISTS compensations (
  id SERIAL PRIMARY KEY,
  guild_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  amount TEXT NULL,
  reason TEXT NULL,
  granted_by BIGINT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def ensure_db() -> None:
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with db_pool.acquire() as con:
            await con.execute(SCHEMA_SQL)
            await con.execute("ALTER TABLE tickets DROP COLUMN IF EXISTS priority")


async def db_fetchrow(q: str, *args):
    assert db_pool is not None
    async with db_pool.acquire() as con:
        return await con.fetchrow(q, *args)


async def db_fetch(q: str, *args):
    assert db_pool is not None
    async with db_pool.acquire() as con:
        return await con.fetch(q, *args)


async def db_execute(q: str, *args):
    assert db_pool is not None
    async with db_pool.acquire() as con:
        return await con.execute(q, *args)


async def get_next_ticket_num(kind: str) -> int:
    assert db_pool is not None
    async with db_pool.acquire() as con:
        row = await con.fetchrow(
            """
            INSERT INTO ticket_counters(kind, next_num)
            VALUES ($1, 1)
            ON CONFLICT (kind) DO UPDATE
            SET next_num = ticket_counters.next_num + 1
            RETURNING next_num;
            """,
            kind
        )
        return int(row["next_num"])


# ============================================================
# SELLAUTH — payment verification
# ============================================================
async def sellauth_get_invoice(order_id: str) -> dict:
    """Look up a SellAuth invoice by its id. Returns a normalized dict:
    {ok, found, paid, status, amount, email, items, error}."""
    out = {
        "ok": False, "found": False, "paid": False,
        "status": None, "amount": None, "email": None, "items": [], "error": None,
    }
    if not SELLAUTH_ENABLED:
        out["error"] = "SellAuth not configured"
        return out

    oid = re.sub(r"[^A-Za-z0-9_\-]", "", (order_id or "").strip())
    if not oid:
        out["error"] = "empty order id"
        return out

    url = f"{SELLAUTH_API_BASE}/shops/{SELLAUTH_SHOP_ID}/invoices/{oid}"
    headers = {"Authorization": f"Bearer {SELLAUTH_API_KEY}", "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(url, headers=headers) as resp:
                text = await resp.text()
                if resp.status == 404:
                    out["ok"] = True  # request succeeded; the invoice just doesn't exist
                    out["error"] = "invoice not found"
                    return out
                if resp.status >= 400:
                    out["error"] = f"HTTP {resp.status}: {text[:200]}"
                    return out
                data = json.loads(text)
    except Exception as e:
        out["error"] = f"request failed: {e}"
        return out

    # SellAuth may wrap the object as {"invoice": {...}} or return it flat.
    inv = data.get("invoice", data) if isinstance(data, dict) else {}
    status = str(inv.get("status", "")).lower()
    paid_usd = inv.get("paid_usd")
    completed = bool(inv.get("completed_at"))

    out.update(
        ok=True,
        found=True,
        status=status or None,
        amount=inv.get("price_usd") or inv.get("price") or inv.get("total"),
        email=inv.get("email"),
        items=inv.get("items") or [],
    )
    # "paid" if status is in the allow-list, or SellAuth already marked it completed/paid_usd.
    out["paid"] = (
        status in SELLAUTH_PAID_STATUSES
        or completed
        or (paid_usd is not None and float(paid_usd or 0) > 0)
    )
    return out


# ============================================================
# LZT.MARKET — your account stock
# ============================================================
def _lzt_headers() -> dict:
    return {
        "Authorization": f"Bearer {LZT_API_TOKEN}",
        "Accept": "application/json",
    }


async def lzt_list_owned(page: int = 1, category_id: int | None = None) -> dict:
    """List accounts you've purchased on LZT.market (your sellable stock).
    Pass category_id to filter server-side (e.g. 9=Fortnite, 13=Riot/Valorant).
    Returns {ok, items: [{item_id, title, price, item_state, category, tags}], total, has_next, error}."""
    out = {"ok": False, "items": [], "total": None, "has_next": False, "error": None}
    if not LZT_ENABLED:
        out["error"] = "stock source not configured"
        return out
    if not LZT_USER_ID:
        out["error"] = "LZT_USER_ID not set (your numeric lzt.market user id)"
        return out

    url = f"{LZT_API_BASE}/user/{LZT_USER_ID}/orders"
    params: dict = {"page": page}
    if category_id is not None:
        params["category_id"] = category_id
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(url, headers=_lzt_headers(), params=params) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    out["error"] = f"HTTP {resp.status}: {text[:200]}"
                    return out
                data = json.loads(text)
    except Exception as e:
        out["error"] = f"request failed: {e}"
        return out

    items = data.get("items") or data.get("orders") or []
    out.update(
        ok=True,
        items=[
            {
                "item_id": it.get("item_id") or it.get("id"),
                "title": it.get("title") or it.get("title_en") or "Account",
                "price": it.get("price"),
                "item_state": it.get("item_state") or it.get("status"),
                "category": (it.get("category") or {}).get("category_name"),
                "tags": [t.get("title") for t in (it.get("tags") or {}).values() if isinstance(t, dict)],
            }
            for it in items if isinstance(it, dict)
        ],
        total=data.get("totalItems"),
        has_next=bool(data.get("hasNextPage")),
    )
    return out


def _item_is_valid(item: dict) -> bool:
    """True when an owned item has no 'invalid'/'resold' tag — i.e. safe to deliver."""
    tags = {str(t).lower() for t in (item.get("tags") or [])}
    return not (tags & LZT_BAD_TAGS)


async def _delivered_item_ids() -> set[int]:
    """LZT item IDs already delivered to a customer (never hand the same account out twice)."""
    rows = await db_fetch("SELECT lzt_item_id FROM deliveries WHERE lzt_item_id IS NOT NULL")
    return {int(r["lzt_item_id"]) for r in rows if r["lzt_item_id"]}


async def lzt_find_valid(category: str, max_pages: int = 10) -> dict:
    """Walk your purchases for one category, keeping only VALID accounts
    (excludes the 'invalid' and 'resold' tags). `category` is a name like
    'fortnite' or 'valorant'. Returns {ok, items, error}."""
    out = {"ok": False, "items": [], "error": None}
    cid = LZT_CATEGORY_IDS.get(category.lower())
    if cid is None:
        out["error"] = f"unknown category '{category}'"
        return out
    valid: list[dict] = []
    for pg in range(1, max_pages + 1):
        res = await lzt_list_owned(page=pg, category_id=cid)
        if not res["ok"]:
            out["error"] = res["error"]
            return out
        valid.extend(it for it in res["items"] if _item_is_valid(it))
        if not res.get("has_next"):
            break
    out.update(ok=True, items=valid)
    return out


# ============================================================
# LZT.MARKET — BROWSE LISTINGS TO BUY & RESELL (dropshipping)
# ============================================================
LZT_SITE = "https://lzt.market"


async def _lzt_get(path: str, params: dict | None = None) -> dict:
    """Raw authenticated GET against the LZT.market API. Returns {ok, data, error}."""
    out = {"ok": False, "data": None, "error": None}
    if not LZT_ENABLED:
        out["error"] = "stock source not configured"
        return out
    url = f"{LZT_API_BASE}{path}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(url, headers=_lzt_headers(), params=params or {}) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    out["error"] = f"HTTP {resp.status}: {text[:200]}"
                    return out
                out.update(ok=True, data=json.loads(text))
    except Exception as e:
        out["error"] = f"request failed: {e}"
    return out


def _spent_metric(category: str, item: dict) -> int:
    """How much the previous owner spent on an account, in the game's currency:
    Valorant → VP value of the skin inventory; Fortnite → V-Bucks spent in shop.
    Used to rank market listings so the customer gets the richest account their
    budget allows."""
    c = category.lower()
    if c == "valorant":
        return int(item.get("riot_valorant_inventory_value") or 0)
    if c == "fortnite":
        return sum(int(item.get(f"fortnite_shop_{k}_cost") or 0)
                   for k in ("skins", "pickaxes", "dances", "gliders"))
    # Other games have no universal "spent" metric — rank by price (richest first).
    return int(float(item.get("price") or 0) * 100)


async def lzt_search_market(category: str, budget: float | None = None,
                            count: int = 3, pool: int | None = None,
                            cheapest: bool = False) -> dict:
    """Search live LZT.market listings we can buy & resell within a budget, ranked
    by how much the previous owner spent (VP for Valorant, V-Bucks for Fortnite) so
    the customer gets the richest account their budget allows.
    `category` is 'valorant' or 'fortnite'. `budget` is the customer's max spend (EUR);
    we cap the source price at budget / RESALE_MULTIPLIER. `pool` overrides how many
    ranked items to return (used when we still need to filter by skin). Returns
    {ok, items, error}."""
    out = {"ok": False, "items": [], "error": None}
    slug = LZT_MARKET_SLUGS.get(category.lower())
    if not slug:
        out["error"] = f"unknown category '{category}'"
        return out
    # For a specific request (a named skin/game) pull cheapest-first so the customer
    # gets the cheapest account that matches; otherwise pull the richest first.
    params: dict = {"order_by": "price_to_up" if cheapest else "price_to_down"}
    if budget and budget > 0:
        params["pmax"] = round(budget / RESALE_MULTIPLIER, 2)
    res = await _lzt_get(f"/{slug}", params)
    if not res["ok"]:
        out["error"] = res["error"]
        return out
    items = (res["data"] or {}).get("items") or []
    if cheapest:
        # Cheapest matching account first (within budget).
        items.sort(key=lambda it: float(it.get("price") or 1e9))
    else:
        # Richest first — most invested in the account for the budget.
        items.sort(key=lambda it: _spent_metric(category, it), reverse=True)
    limit = pool if pool else max(1, min(count, 5))
    out.update(ok=True, items=items[:max(1, limit)])
    return out


async def lzt_item_detail(item_id: str | int) -> dict:
    """Full listing detail (skins, image preview links, etc). Returns {ok, item, error}."""
    iid = re.sub(r"[^0-9]", "", str(item_id or "").strip())
    if not iid:
        return {"ok": False, "item": None, "error": "invalid item id"}
    res = await _lzt_get(f"/{iid}")
    if not res["ok"]:
        return {"ok": False, "item": None, "error": res["error"]}
    data = res["data"] or {}
    return {"ok": True, "item": data.get("item", data), "error": None}


def _resale_price(item: dict) -> tuple[float, float]:
    """(source_price, resale_price) in EUR, resale rounded up to the next whole euro."""
    src = float(item.get("price") or 0)
    resale = src * RESALE_MULTIPLIER
    resale = float(int(resale) + (1 if resale > int(resale) else 0)) if resale else 0.0
    return src, resale


def _preview_image(item: dict, kind: str) -> str | None:
    """Direct (JWT-signed, embeddable) composite image URL for skins/weapons, if present."""
    links = (item.get("imagePreviewLinks") or {}).get("direct") or {}
    return links.get(kind)


_FN_RARITY = {
    "legendary": "🟠", "epic": "🟣", "rare": "🔵", "uncommon": "🟢",
    "common": "⚪", "marvel": "🔴", "dc": "🔷", "icon": "🟦", "gaming": "🟦",
    "starwars": "🟡", "frozen": "🧊", "lava": "🔥", "shadow": "⬛", "slurp": "💧",
}


def _valorant_skin_names(item: dict) -> list[str]:
    """Best-effort list of Valorant skin names from an LZT item.
    LZT exposes the skin list under a few different keys depending on the
    endpoint, so try each and pull a readable name from every entry."""
    def _name(entry) -> str | None:
        if isinstance(entry, str):
            return entry.strip() or None
        if isinstance(entry, dict):
            for k in ("title", "name", "localizedName", "displayName", "skin_name", "displayIcon"):
                v = entry.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return None

    for key in ("valorantSkins", "valorant_skins", "riotValorantSkins",
                "riot_valorant_skins", "valorantInventory", "valorant_inventory",
                "riot_valorant_inventory"):
        val = item.get(key)
        entries = None
        if isinstance(val, list):
            entries = val
        elif isinstance(val, dict):
            # Some payloads nest the list under a 'skins'/'items' field.
            for sub in ("skins", "items", "weaponSkins", "WeaponSkins"):
                if isinstance(val.get(sub), list):
                    entries = val[sub]
                    break
            if entries is None:
                entries = list(val.values())
        if entries:
            names = [n for n in (_name(e) for e in entries) if n]
            if names:
                return names
    return []


# ---- Valorant skin UUID → readable name resolution --------------------------
# LZT returns Valorant skins as content UUIDs (e.g. "5b43d27b-..."), not names.
# We resolve them via valorant-api.com (skins + skin levels + chromas cover every
# UUID variant) and cache the map in memory after the first lookup.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_VAL_SKIN_MAP: dict[str, str] | None = None
_val_skin_map_lock = asyncio.Lock()
VALORANT_API_ENDPOINTS = (
    "https://valorant-api.com/v1/weapons/skins",
    "https://valorant-api.com/v1/weapons/skinlevels",
    "https://valorant-api.com/v1/weapons/skinchromas",
)


async def _valorant_skin_map() -> dict[str, str]:
    global _VAL_SKIN_MAP
    if _VAL_SKIN_MAP is not None:
        return _VAL_SKIN_MAP
    async with _val_skin_map_lock:
        if _VAL_SKIN_MAP is not None:
            return _VAL_SKIN_MAP
        m: dict[str, str] = {}
        for url in VALORANT_API_ENDPOINTS:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
                    async with s.get(url) as r:
                        if r.status != 200:
                            continue
                        data = await r.json()
                for entry in (data.get("data") or []):
                    uid = str(entry.get("uuid") or "").lower()
                    name = entry.get("displayName")
                    if uid and name:
                        m.setdefault(uid, str(name))
            except Exception as e:
                print("valorant-api fetch failed:", e)
        # Cache only a non-empty result so a transient failure can retry later.
        if m:
            _VAL_SKIN_MAP = m
        return m


def _dedup_keep_order(names: list[str]) -> list[str]:
    seen, out = set(), []
    for n in names:
        key = n.lower()
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


async def _resolve_valorant_skins(item: dict) -> list[str]:
    """Valorant skin names for an account, resolving UUIDs to readable names.
    Unresolvable UUIDs are dropped (we never show raw IDs to customers)."""
    raw = _valorant_skin_names(item)
    if not raw:
        return []
    if not any(_UUID_RE.match(str(x)) for x in raw):
        return _dedup_keep_order([str(x) for x in raw])
    mapping = await _valorant_skin_map()
    out: list[str] = []
    for x in raw:
        x = str(x)
        if _UUID_RE.match(x):
            nm = mapping.get(x.lower())
            if nm:
                out.append(nm)
        else:
            out.append(x)
    return _dedup_keep_order(out)


# ---- Fortnite cosmetic lists (skins, pickaxes, emotes, gliders, …) -----------
# Candidate LZT keys per cosmetic type; each entry is a dict with a title (like
# fortniteSkins) or a plain string. We probe several names defensively.
_FN_COSMETIC_KEYS: dict[str, list[str]] = {
    "Skins":       ["fortniteSkins", "fortnite_skins", "fortniteOutfits", "fortnite_outfits"],
    "Pickaxes":    ["fortnitePickaxes", "fortnitePickaxe", "fortnite_pickaxes",
                    "fortniteHarvestingTools", "fortnite_harvesting_tools",
                    "fortnitePickaxes_list", "fortnitePickaxeList"],
    "Emotes":      ["fortniteDances", "fortniteEmotes", "fortnite_dances", "fortnite_emotes",
                    "fortniteEmote"],
    "Gliders":     ["fortniteGliders", "fortniteGlider", "fortnite_gliders"],
    "Back Blings": ["fortniteBackpacks", "fortniteBackblings", "fortnite_backpacks",
                    "fortniteBackBlings"],
}


def _fn_cosmetic_names(item: dict, keys: list[str]) -> list[str]:
    for k in keys:
        v = item.get(k)
        if isinstance(v, list) and v:
            names = []
            for s in v:
                if isinstance(s, dict):
                    n = s.get("title") or s.get("name")
                    if n:
                        names.append(str(n))
                elif isinstance(s, str) and s.strip():
                    names.append(s.strip())
            if names:
                return names
    return []


async def build_cosmetic_sections(category: str, item: dict) -> dict[str, list[str]]:
    """Full, categorised cosmetic lists for an account, for the paginator/filter.
    Valorant → {'Skins': [...]}; Fortnite → {'Skins': [...], 'Pickaxes': [...], …}."""
    if category.lower() == "valorant":
        skins = await _resolve_valorant_skins(item)
        return {"Skins": skins} if skins else {}
    sections: dict[str, list[str]] = {}
    for label, keys in _FN_COSMETIC_KEYS.items():
        names = _fn_cosmetic_names(item, keys)
        if names:
            sections[label] = names
    return sections


# Community nicknames → official in-game name substrings. When a customer asks
# for a cosmetic by its common name, we match any of the real names it maps to.
# (e.g. "FNCS pickaxe" → every "Axe of Champions" variant: I, II, III, …)
_COSMETIC_ALIASES: list[tuple[list[str], list[str]]] = [
    (["fncs", "axe of champion", "champion axe", "champions axe", "fncs axe", "fncs pickaxe",
      "fncs pick"], ["axe of champions"]),
    (["renegade"], ["renegade raider"]),
    (["aerial assault", "aat"], ["aerial assault trooper"]),
    (["skull trooper"], ["skull trooper"]),
    (["ghoul"], ["ghoul trooper"]),
    (["black knight"], ["black knight"]),
    (["reaper", "john wick"], ["the reaper", "reaper"]),
    (["take the l", "take the elle"], ["take the l"]),
    (["floss"], ["floss"]),
    (["merry mint", "mint axe", "minty", "minty pickaxe", "minty axe"], ["merry mint axe"]),
    (["star wand", "star wand pickaxe"], ["star wand"]),
    (["candy axe"], ["candy axe"]),
    (["reaper pickaxe", "harvesting tool"], ["reaper"]),
    (["gaia", "gaia vengeance", "gaias vengeance", "gaia's vengeance"], ["gaia"]),
    # Steam-game shorthands → substrings of the official titles.
    (["gta", "gta v", "gta 5", "grand theft"], ["grand theft auto"]),
    (["cs2", "cs go", "csgo", "counter strike", "counter-strike"], ["counter-strike"]),
    (["rdr2", "rdr", "red dead"], ["red dead redemption"]),
    (["cod", "call of duty", "modern warfare", "warzone"], ["call of duty"]),
    (["pubg"], ["pubg", "playerunknown"]),
    (["apex"], ["apex legends"]),
    (["r6", "rainbow six", "siege"], ["rainbow six"]),
    (["rocket league", "rl"], ["rocket league"]),
]


def _expand_skin_query(term: str) -> list[str]:
    """Expand a requested cosmetic term into official-name substrings to match."""
    t = (term or "").strip().lower()
    if not t:
        return []
    out = [t]
    for triggers, targets in _COSMETIC_ALIASES:
        if any(trig in t or t in trig for trig in triggers):
            out.extend(targets)
    seen, res = set(), []
    for c in out:
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            res.append(c)
    return res


def _cosmetic_matches(owned_lower: list[str], term: str) -> bool:
    """True if the account owns a cosmetic matching the requested term (via aliases)."""
    for cand in _expand_skin_query(term):
        if any(cand in n for n in owned_lower):
            return True
    return False


def _steam_game_names(item: dict) -> list[str]:
    """Best-effort list of the game titles on a Steam account listing."""
    names: list[str] = []
    for key in ("steam_full_games", "steamGames", "steam_games", "games"):
        v = item.get(key)
        entries = None
        if isinstance(v, dict):
            sub = v.get("list")
            entries = sub if isinstance(sub, (list, dict)) else v
        elif isinstance(v, list):
            entries = v
        if entries:
            it = entries.values() if isinstance(entries, dict) else entries
            for g in it:
                if isinstance(g, dict):
                    n = g.get("title") or g.get("name") or g.get("appName")
                    if n:
                        names.append(str(n))
                elif isinstance(g, str) and g.strip():
                    names.append(g.strip())
            if names:
                return names
    return names


async def _searchable_names(game: str, item: dict) -> list[str]:
    """Names to match a customer's request against, per game:
    Valorant/Fortnite → cosmetics; Steam → game titles; anything else → the title.
    The listing title is always appended — LZT titles often name rare items
    (e.g. "…Axe of Champions…"), so we still match when the cosmetic list is thin."""
    g = (game or "").lower()
    names: list[str] = []
    if g in ("valorant", "fortnite"):
        sections = await build_cosmetic_sections(g, item)
        names = [n for grp in sections.values() for n in grp]
    elif g in ("steam",):
        names = _steam_game_names(item)
    title = item.get("title") or item.get("title_en") or ""
    if title:
        names.append(str(title))
    return names


_REGION_ALIASES = {
    "eu": ["eu", "europe"], "na": ["na", "north america"], "ap": ["ap", "asia"],
    "kr": ["kr", "korea"], "br": ["br", "brazil"], "latam": ["latam", "latin"],
    "tr": ["tr", "turkey"], "mena": ["mena", "middle east"],
}


def _region_matches(item: dict, region: str | None) -> bool:
    """True if a Valorant/Riot listing is in the requested region (best effort)."""
    if not region:
        return True
    hay = " ".join(str(item.get(k) or "") for k in
                   ("valorantRegionPhrase", "riot_valorant_region", "region")).lower()
    if not hay.strip():
        return True  # region unknown on the listing → don't exclude it
    keys = _REGION_ALIASES.get(region.lower(), [region.lower()])
    return any(k in hay for k in keys)


_GENERIC_STAT_FIELDS = [
    # (label, list-of-candidate-keys) — shown when present on the listing.
    ("Level", ["steam_level", "account_level", "level"]),
    ("Games", ["steam_game_count", "steam_full_games_count", "game_count"]),
    ("Balance", ["steam_balance", "balance", "account_balance"]),
    ("Country", ["steam_country", "account_country", "country"]),
    ("Last seen", ["account_last_activity", "steam_last_activity"]),
]


def _generic_account_embed(category: str, item: dict) -> discord.Embed:
    """A category-agnostic account card for any LZT game (Steam, Rockstar, EA, …).
    Uses the listing's own title + whatever common stat fields are present."""
    label = game_label(category)
    if category.lower() in ("generic", "account") or label.lower() == "generic":
        cat = item.get("category") or {}
        label = str(cat.get("category_name") or cat.get("title") or "Account")
    title = item.get("title") or item.get("title_en") or f"{label} Account"
    e = discord.Embed(title=f"🎮  {label} Account",
                      description=str(title)[:600], color=AF_BLUE)
    shown = 0
    for name, keys in _GENERIC_STAT_FIELDS:
        val = next((item.get(k) for k in keys if item.get(k) not in (None, "", 0)), None)
        if val is not None:
            e.add_field(name=name, value=str(val)[:64], inline=True)
            shown += 1
        if shown >= 5:
            break
    return e


def market_account_embed(category: str, item: dict, image_name: str | None = None,
                         valorant_skin_names: list[str] | None = None) -> discord.Embed:
    """Customer-facing embed describing one account: stats + skins image + price.
    Never exposes the sourcing marketplace. `image_name` is an attachment:// filename."""
    src, resale = _resale_price(item)

    if category.lower() == "valorant":
        skins = item.get("riot_valorant_skin_count") or 0
        vp_inv = item.get("riot_valorant_inventory_value") or 0
        vp_wallet = item.get("riot_valorant_wallet_vp") or 0
        rank = item.get("valorantRankTitle") or "Unrated"
        level = item.get("riot_valorant_level") or 0
        region = item.get("valorantRegionPhrase") or item.get("riot_valorant_region") or "—"
        agents = item.get("riot_valorant_agent_count") or 0
        knives = item.get("riot_valorant_knife_count") or 0
        e = discord.Embed(
            title=f"🔫  Valorant Account — {skins} Skins",
            description=f"**Rank:** {rank}  •  **Level:** {level}  •  **Region:** {region}",
            color=GUIDE_RIOT_COLOR,
        )
        e.add_field(name="🎨 Skins", value=str(skins), inline=True)
        e.add_field(name="💎 Skin Value", value=f"{vp_inv:,} VP", inline=True)
        e.add_field(name="🪙 VP Balance", value=f"{vp_wallet:,} VP", inline=True)
        e.add_field(name="🧍 Agents", value=str(agents), inline=True)
        e.add_field(name="🔪 Knives", value=str(knives), inline=True)
        # List the actual skins (resolved to names) so buyers see exactly what
        # they're getting. The full list is browsable via the "View all skins" button.
        skin_names = valorant_skin_names if valorant_skin_names is not None else _valorant_skin_names(item)
        if skin_names:
            shown = skin_names[:15]
            skin_list = "\n".join(f"🔫 {n}" for n in shown)
            more = len(skin_names) - len(shown)
            if more > 0:
                skin_list += f"\n…and **{more}** more — tap **View all skins** below"
            e.add_field(name="🎨 Skin List", value=skin_list[:1024], inline=False)
    elif category.lower() == "fortnite":
        skins = item.get("fortnite_skin_count") or 0
        vbucks = item.get("fortnite_balance") or 0
        spent = sum(int(item.get(f"fortnite_shop_{k}_cost") or 0)
                    for k in ("skins", "pickaxes", "dances", "gliders"))
        fn_skins = item.get("fortniteSkins") or []
        names = []
        for s in fn_skins[:18]:
            emoji = _FN_RARITY.get(str(s.get("rarity", "")).lower(), "▫️")
            names.append(f"{emoji} {s.get('title')}")
        more = len(fn_skins) - len(names)
        skin_list = "\n".join(names) if names else "—"
        if more > 0:
            skin_list += f"\n…and **{more}** more — tap **View all skins** below"
        e = discord.Embed(
            title=f"🎮  Fortnite Account — {skins} Skins",
            description=f"**V-Bucks:** {vbucks:,}  •  **V-Bucks spent in shop:** {spent:,}",
            color=GUIDE_EPIC_COLOR,
        )
        e.add_field(name="🎨 Skins", value=skin_list[:1024], inline=False)
    else:  # any other LZT category — render generically from the listing
        e = _generic_account_embed(category, item)

    e.add_field(name="💶 Price", value=f"**€{resale:.0f}**", inline=True)
    if image_name:
        e.set_image(url=f"attachment://{image_name}")
    e.set_footer(text="AF SERVICES • Prices include warranty & setup support")
    return e


async def _fetch_bytes(url: str) -> bytes | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(url) as r:
                return await r.read() if r.status == 200 else None
    except Exception:
        return None


async def build_account_message(category: str, item: dict, idx: int = 0):
    """(embed, file|None) for one account. The skin preview is re-hosted through Discord
    so the customer never sees the source marketplace URL."""
    kind = "weapons" if category.lower() == "valorant" else "skins"
    url = _preview_image(item, kind)
    file = None
    image_name = None
    if url:
        data = await _fetch_bytes(url)
        if data:
            image_name = f"account_{idx}.png"
            file = discord.File(io.BytesIO(data), filename=image_name)
    val_skins = await _resolve_valorant_skins(item) if category.lower() == "valorant" else None
    embed = market_account_embed(category, item, image_name=image_name,
                                 valorant_skin_names=val_skins)
    return embed, file


# ============================================================
# COSMETICS PAGINATOR — browse every skin/pickaxe/emote on an account
# ============================================================
def _account_item_id(item: dict) -> str:
    return re.sub(r"[^0-9]", "", str(item.get("item_id") or item.get("id") or ""))


class CosmeticsPager(discord.ui.View):
    """Ephemeral, paged view of an account's full cosmetic inventory.
    Fortnite gets a category selector (Skins / Pickaxes / Emotes / …)."""
    PAGE_SIZE = 20

    def __init__(self, category: str, title: str, sections: dict[str, list[str]]):
        super().__init__(timeout=600)
        self.category = category.lower()
        self.title = title
        self.sections = sections or {}
        self.labels = list(self.sections.keys()) or ["Skins"]
        self.sec = 0
        self.page = 0

        self.prev_btn = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary)
        self.prev_btn.callback = self._prev
        self.next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary)
        self.next_btn.callback = self._next
        self.add_item(self.prev_btn)
        self.add_item(self.next_btn)

        if len(self.labels) > 1:
            self.selector = discord.ui.Select(
                placeholder="Choose a category…",
                options=[discord.SelectOption(label=f"{l} ({len(self.sections[l])})"[:100], value=str(i))
                         for i, l in enumerate(self.labels)],
            )
            self.selector.callback = self._select
            self.add_item(self.selector)

    def _cur(self) -> list[str]:
        return self.sections.get(self.labels[self.sec], [])

    def _page_count(self) -> int:
        n = len(self._cur())
        return max(1, (n + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def render(self) -> discord.Embed:
        pages = self._page_count()
        self.page = max(0, min(self.page, pages - 1))
        lst = self._cur()
        start = self.page * self.PAGE_SIZE
        chunk = lst[start:start + self.PAGE_SIZE]
        label = self.labels[self.sec]
        emoji = "🔫" if self.category == "valorant" else "🎨"
        numbered = [f"`{start + i + 1:>3}.` {emoji} {name}" for i, name in enumerate(chunk)]
        e = discord.Embed(
            title=f"{self.title} — {label}",
            description="\n".join(numbered) if numbered else "—",
            color=GUIDE_RIOT_COLOR if self.category == "valorant" else GUIDE_EPIC_COLOR,
        )
        self.prev_btn.disabled = self.next_btn.disabled = (pages <= 1)
        e.set_footer(text=f"{label}: {len(lst)} total • Page {self.page + 1}/{pages}")
        return e

    async def _refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.render(), view=self)

    async def _prev(self, interaction: discord.Interaction):
        self.page = (self.page - 1) % self._page_count()
        await self._refresh(interaction)

    async def _next(self, interaction: discord.Interaction):
        self.page = (self.page + 1) % self._page_count()
        await self._refresh(interaction)

    async def _select(self, interaction: discord.Interaction):
        self.sec = int(self.selector.values[0])
        self.page = 0
        await self._refresh(interaction)


class ViewSkinsButton(discord.ui.DynamicItem[discord.ui.Button],
                      template=r"af_skins:(?P<item_id>\d+):(?P<cat>[a-z]+)"):
    """A persistent button attached to account embeds that opens the full,
    paginated cosmetic list for that account (ephemeral, per viewer)."""
    def __init__(self, item_id: int, cat: str, label: str = "🎨 View all skins"):
        self.item_id = int(item_id)
        self.cat = cat
        super().__init__(
            discord.ui.Button(label=label, style=discord.ButtonStyle.primary,
                              custom_id=f"af_skins:{item_id}:{cat}")
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["item_id"]), match["cat"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        det = await lzt_item_detail(self.item_id)
        if not det["ok"]:
            await interaction.followup.send(
                f"⚠️ Couldn't load this account's cosmetics: `{det['error']}`", ephemeral=True)
            return
        item = det["item"] or {}
        sections = await build_cosmetic_sections(self.cat, item)
        if not sections:
            await interaction.followup.send(
                "No detailed cosmetic list is available for this account.", ephemeral=True)
            return
        title = "Valorant Account" if self.cat == "valorant" else "Fortnite Account"
        pager = CosmeticsPager(self.cat, title, sections)
        await interaction.followup.send(embed=pager.render(), view=pager, ephemeral=True)


def skins_view_for(items: list[dict], category: str) -> discord.ui.View | None:
    """A View with one 'View all skins' button per account (max 5)."""
    view = discord.ui.View(timeout=None)
    added = 0
    for i, it in enumerate(items):
        iid = _account_item_id(it)
        if not iid:
            continue
        label = "🎨 View all skins" if len(items) == 1 else f"🎨 Skins #{i + 1}"
        view.add_item(ViewSkinsButton(int(iid), category.lower(), label=label))
        added += 1
        if added >= 5:
            break
    return view if added else None


async def notify_restock(triggered_by: discord.abc.User | None, guild: discord.Guild,
                         item: dict, ticket: discord.TextChannel | None) -> str:
    """Alert the owner that a sold account must be re-bought on LZT ASAP. Returns a status string."""
    src, resale = _resale_price(item)
    iid = item.get("item_id") or item.get("id")
    listing = f"{LZT_SITE}/{iid}/"
    title = item.get("title") or item.get("title_en") or "Account"
    e = discord.Embed(
        title="🛒  RESTOCK NOW — Account Sold",
        description=(
            f"A customer paid for this account. **Buy the exact listing below ASAP** before it's gone.\n\n"
            f"**{str(title)[:200]}**"
        ),
        color=0xE74C3C,
        url=listing,
    )
    e.add_field(name="💸 Your cost", value=f"€{src:.2f}", inline=True)
    e.add_field(name="💶 Sold for", value=f"€{resale:.0f}", inline=True)
    e.add_field(name="🆔 Item ID", value=f"`{iid}`", inline=True)
    e.add_field(name="🔗 Buy now", value=f"[Open listing]({listing})", inline=False)
    if ticket:
        e.add_field(name="🎟️ Ticket", value=ticket.mention, inline=False)
    e.set_footer(text="AF SERVICES • Restock alert")

    target = await resolve_text_channel(guild, RESTOCK_CHANNEL_ID)
    if target is None:
        # No dedicated restock channel → fall back to the transcript channel, which
        # is staff-only, so the alert is never lost (and never hits the ticket).
        target = await get_transcript_channel(guild)
    if target is not None:
        await target.send(embed=e)
        return f"Restock alert posted in {target.mention}."
    if triggered_by is not None:
        try:
            await triggered_by.send(embed=e)
            return "Restock alert sent to your DMs."
        except discord.Forbidden:
            return "⚠️ Couldn't DM you and no RESTOCK_CHANNEL_ID is set — enable DMs or set the channel."
    return "⚠️ No RESTOCK_CHANNEL_ID set — restock alert could not be delivered."


# ============================================================
# NOWPAYMENTS — crypto checkout (LTC / SOL / BTC / ETH)
# ============================================================
def _np_headers() -> dict:
    return {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}


async def np_create_payment(amount: float, pay_currency: str, order_id: str,
                            description: str) -> dict:
    """Create a NowPayments crypto payment. Returns {ok, payment_id, pay_address,
    pay_amount, pay_currency, error}."""
    out = {"ok": False, "payment_id": None, "pay_address": None,
           "pay_amount": None, "pay_currency": pay_currency, "error": None}
    if not NOWPAYMENTS_ENABLED:
        out["error"] = "crypto payments not configured"
        return out
    body = {
        "price_amount": round(float(amount), 2),
        "price_currency": NOWPAYMENTS_PRICE_CURRENCY,
        "pay_currency": pay_currency,
        "order_id": order_id,
        "order_description": description[:200],
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
            async with s.post(f"{NOWPAYMENTS_API_BASE}/payment",
                              headers=_np_headers(), json=body) as r:
                text = await r.text()
                if r.status >= 400:
                    out["error"] = f"HTTP {r.status}: {text[:200]}"
                    return out
                data = json.loads(text)
    except Exception as e:
        out["error"] = f"request failed: {e}"
        return out
    out.update(
        ok=True,
        payment_id=str(data.get("payment_id")),
        pay_address=data.get("pay_address"),
        pay_amount=data.get("pay_amount"),
        pay_currency=(data.get("pay_currency") or pay_currency),
    )
    return out


async def np_get_payment(payment_id: str) -> dict:
    """Look up a NowPayments payment. Returns {ok, status, paid, error}."""
    out = {"ok": False, "status": None, "paid": False, "error": None}
    if not NOWPAYMENTS_ENABLED:
        out["error"] = "crypto payments not configured"
        return out
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(f"{NOWPAYMENTS_API_BASE}/payment/{payment_id}",
                             headers=_np_headers()) as r:
                text = await r.text()
                if r.status >= 400:
                    out["error"] = f"HTTP {r.status}: {text[:200]}"
                    return out
                data = json.loads(text)
    except Exception as e:
        out["error"] = f"request failed: {e}"
        return out
    status = str(data.get("payment_status") or "").lower()
    out.update(ok=True, status=status, paid=status in NP_PAID_STATUSES)
    return out


def _extract_email_access(item: dict, login_data: dict) -> tuple[str | None, str | None, str | None]:
    """Best-effort (email_login, email_password, email_raw) for the account's
    inbox, so the buyer can pull verification codes. LZT exposes this under a few
    different keys depending on the listing, so probe each."""
    email_login = email_password = email_raw = None
    blob = (item.get("emailLoginData") or item.get("email_login_data")
            or login_data.get("emailLoginData") or login_data.get("email_login_data"))
    if isinstance(blob, str) and blob.strip():
        email_raw = blob.strip()
        if ":" in email_raw:
            email_login, _, email_password = email_raw.partition(":")
    elif isinstance(blob, dict):
        email_login = blob.get("login") or blob.get("email")
        email_password = blob.get("password")
        email_raw = blob.get("raw")
    # Fallbacks on flat item / loginData fields.
    email_login = (email_login or item.get("email") or item.get("email_login")
                   or login_data.get("email") or login_data.get("email_login"))
    email_password = (email_password or item.get("email_password")
                      or login_data.get("email_password"))
    if not email_raw and email_login and email_password:
        email_raw = f"{email_login}:{email_password}"
    return email_login, email_password, email_raw


async def lzt_get_credentials(item_id: str | int) -> dict:
    """Fetch the login credentials for one owned LZT item, including email (inbox)
    access when the listing provides it.
    Returns {ok, login, password, raw, email_login, email_password, email_raw, title, error}."""
    out = {"ok": False, "login": None, "password": None, "raw": None,
           "email_login": None, "email_password": None, "email_raw": None,
           "title": None, "error": None}
    if not LZT_ENABLED:
        out["error"] = "stock source not configured"
        return out

    iid = re.sub(r"[^0-9]", "", str(item_id or "").strip())
    if not iid:
        out["error"] = "invalid item id"
        return out

    url = f"{LZT_API_BASE}/{iid}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(url, headers=_lzt_headers()) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    out["error"] = f"HTTP {resp.status}: {text[:200]}"
                    return out
                data = json.loads(text)
    except Exception as e:
        out["error"] = f"request failed: {e}"
        return out

    item = data.get("item", data) if isinstance(data, dict) else {}
    login_data = item.get("loginData") or item.get("login_data") or {}
    login = login_data.get("login") or item.get("account_login")
    password = login_data.get("password") or item.get("account_password")
    raw = login_data.get("raw") or (f"{login}:{password}" if login and password else None)

    if not (login or raw):
        out["error"] = "no credentials returned (item may not be owned, or token lacks scope)"
        return out

    email_login, email_password, email_raw = _extract_email_access(item, login_data)
    out.update(ok=True, login=login, password=password, raw=raw,
               email_login=email_login, email_password=email_password, email_raw=email_raw,
               title=item.get("title") or item.get("title_en"))
    return out


# ============================================================
# EMAIL LETTERS — read the account inbox over IMAP (email:pass)
# ============================================================
def _imap_host_for(address: str) -> str:
    domain = address.split("@")[-1].strip().lower()
    if domain in IMAP_HOSTS:
        return IMAP_HOSTS[domain]
    for needle, host in IMAP_HOST_SUBSTR.items():
        if needle in domain:
            return host
    return IMAP_HOST_DEFAULT or f"imap.{domain}"


def _decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    from email.header import decode_header
    out = ""
    for txt, enc in decode_header(value):
        if isinstance(txt, bytes):
            try:
                out += txt.decode(enc or "utf-8", "replace")
            except Exception:
                out += txt.decode("utf-8", "replace")
        else:
            out += txt
    return out


def _html_to_text(html_src: str) -> str:
    """Readable plain text from an HTML mail body.

    Verification mails are almost always HTML, so a naive tag strip leaves the
    <style>/<script> blocks behind — which buries the code in CSS noise and
    breaks code detection. Drop the non-visible blocks, keep the line breaks the
    layout implies, and decode entities."""
    import html as _html

    text = re.sub(r"(?is)<(script|style|head|title)[^>]*>.*?</\1>", " ", html_src)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|table|li|h[1-6]|blockquote)\s*>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n• ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    # Collapse runs of spaces/tabs, then runs of blank lines, keeping the layout.
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _email_body_text(msg) -> str:
    """Plain-text body of an email.Message (HTML converted as a fallback)."""
    def _decode(part) -> str:
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, "replace")
        except (LookupError, UnicodeDecodeError):
            return payload.decode("utf-8", "replace")

    def _is_attachment(part) -> bool:
        return (part.get_content_disposition() or "").lower() == "attachment"

    if msg.is_multipart():
        plain = html_body = ""
        for part in msg.walk():
            if part.get_content_maintype() == "multipart" or _is_attachment(part):
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain" and not plain:
                plain = _decode(part)
            elif ctype == "text/html" and not html_body:
                html_body = _decode(part)
        # Some senders ship an empty text/plain part next to the real HTML one.
        if plain.strip():
            return plain
        return _html_to_text(html_body) if html_body else ""
    body = _decode(msg)
    if msg.get_content_type() == "text/html":
        body = _html_to_text(body)
    return body


_CODE_KEYWORDS = ("code", "verification", "verify", "confirm", "otp", "one-time",
                  "one time", "security", "passcode", "pin", "access code", "auth",
                  "код", "подтвержд")  # incl. RU for rambler/mail.ru


def _find_verification_code(text: str) -> str | None:
    """Best-effort verification code: prefer a 4-8 char code that sits near a
    'code/verification/OTP' keyword; avoid years and unrelated long numbers."""
    if not text:
        return None
    # An explicit "code: 123456" style label wins outright — it's the one place a
    # sender states the code unambiguously.
    labelled = re.search(
        r"(?:code|otp|passcode|pin|код)\s*(?:is|:|-|=|\u2014)?\s*\**\s*([A-Z0-9]{4,8})\b",
        text, re.IGNORECASE)
    if labelled and re.search(r"\d", labelled.group(1)):
        return labelled.group(1).upper()

    low = text.lower()
    best = None  # (score, position, code)
    # Numeric codes, possibly grouped like 123-456 or 123 456. Bounded by non-digits
    # only (so a trailing period / end-of-sentence doesn't reject the code).
    for m in re.finditer(r"(?<!\d)(\d[\d \-]{2,9}\d)(?!\d)", text):
        digits = re.sub(r"\D", "", m.group(1))
        if not (4 <= len(digits) <= 8):
            continue
        score = {6: 4, 8: 3, 5: 2, 4: 1, 7: 1}.get(len(digits), 0)
        if len(digits) == 4 and 1900 <= int(digits) <= 2099:
            score -= 4  # looks like a year
        # A keyword on either side counts: "your code is 123456" and
        # "123456 is your verification code" are both common.
        before = low[max(0, m.start() - 45):m.start()]
        after = low[m.end():m.end() + 45]
        if any(k in before for k in _CODE_KEYWORDS):
            score += 6
        elif any(k in after for k in _CODE_KEYWORDS):
            score += 5
        cand = (score, m.start(), digits)
        if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[1] < best[1]):
            best = cand
    if best and best[0] > 0:
        return best[2]
    if labelled:
        return labelled.group(1).upper()
    return best[2] if best else None


# Folder names that hold junk — verification letters land there constantly, so the
# reader must look past INBOX or the buyer never sees their code.
_JUNK_FOLDER_HINTS = ("junk", "spam", "bulk", "нежелательная", "спам")


def _imap_mailboxes(M) -> list[str]:
    """INBOX first, then any junk/spam mailbox the server exposes."""
    boxes = ["INBOX"]
    try:
        typ, data = M.list()
    except Exception:
        return boxes
    if typ != "OK" or not data:
        return boxes
    for line in data:
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        if not isinstance(line, str) or ")" not in line:
            continue
        flags = line[:line.find(")")].lower()
        # `(\HasNoChildren \Junk) "/" "Junk Email"` → name is the last token,
        # which may or may not be quoted.
        tokens = [q or bare for q, bare in
                  re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', line[line.find(")") + 1:])]
        name = tokens[-1] if tokens else ""
        if not name or name.upper() == "INBOX":
            continue
        if "\\junk" in flags or any(h in name.lower() for h in _JUNK_FOLDER_HINTS):
            boxes.append(name)
    return boxes[:3]


def _fetch_emails_sync(host: str, address: str, password: str, limit: int) -> dict:
    """Blocking IMAP fetch of the newest `limit` letters (inbox + junk folders).
    Run via to_thread."""
    import imaplib
    from email import message_from_bytes
    from email.utils import parsedate_to_datetime

    deadline = time.monotonic() + EMAIL_FETCH_BUDGET_SECONDS
    M = imaplib.IMAP4_SSL(host, 993, timeout=25)
    try:
        M.login(address, password)
        messages: list[dict] = []
        selected_any = False
        boxes = _imap_mailboxes(M)
        for box in boxes:
            if time.monotonic() > deadline:
                break
            # Junk folders only need a few of the newest letters; the inbox is the
            # one that has to be complete, and every extra fetch costs a round trip.
            per_box = limit if box.upper() == "INBOX" else min(limit, 6)
            try:
                # readonly + BODY.PEEK keeps every letter unread, so the buyer still
                # sees it as new in webmail after we've listed it here.
                typ, _ = M.select(f'"{box}"', readonly=True)
                if typ != "OK":
                    continue
                selected_any = True
                typ, data = M.search(None, "ALL")
                if typ != "OK":
                    continue
                ids = (data[0].split() if data and data[0] else [])[-per_box:]
            except Exception as e:
                print(f"Email folder {box} unreadable:", e)
                continue
            for i in reversed(ids):
                if time.monotonic() > deadline:
                    break
                try:
                    typ, md = M.fetch(i, "(BODY.PEEK[])")
                    if typ != "OK" or not md or not md[0] or not isinstance(md[0], tuple):
                        continue
                    msg = message_from_bytes(md[0][1])
                    body = _email_body_text(msg)
                    subject = _decode_mime_header(msg.get("Subject"))
                    raw_date = (msg.get("Date") or "").strip()
                    try:
                        when = parsedate_to_datetime(raw_date)
                        if when.tzinfo is None:
                            when = when.replace(tzinfo=timezone.utc)
                    except Exception:
                        when = None
                    messages.append({
                        "from": _decode_mime_header(msg.get("From"))[:80],
                        "subject": subject[:140] or "(no subject)",
                        "date": (when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                                 if when else raw_date[:40]),
                        "ts": when.timestamp() if when else 0.0,
                        "folder": box,
                        "snippet": re.sub(r"\s+", " ", body).strip()[:300],
                        "body": body.strip()[:3500],
                        "code": _find_verification_code(body) or _find_verification_code(subject),
                    })
                except Exception as e:
                    print(f"Email fetch skipped one letter in {box}:", e)
        if not selected_any:
            raise RuntimeError("no readable mailbox on this account")
        messages.sort(key=lambda m: m["ts"], reverse=True)
        return {"ok": True, "messages": messages[:max(limit, 10)]}
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _inbox_error(host: str, err: Exception) -> str:
    """Turn an IMAP failure into something the reader can act on."""
    msg = str(err).strip().strip("'\"") or type(err).__name__
    low = msg.lower()
    if isinstance(err, asyncio.TimeoutError) or "timed out" in low or "timeout" in low:
        return f"{host} didn't respond in time — try again in a moment"
    if any(k in low for k in ("authenticationfailed", "authentication", "invalid credentials",
                              "login failed", "logon failure", "password")):
        return (f"{host} rejected the login — the email password is wrong, or the provider "
                f"needs IMAP switched on / an app password")
    if any(k in low for k in ("getaddrinfo", "name or service not known", "nodename",
                              "no such host", "name does not resolve")):
        return (f"no IMAP server found for this domain (tried {host}) — map it with "
                f"IMAP_EXTRA_HOSTS")
    if any(k in low for k in ("refused", "unreachable", "reset", "ssl", "certificate")):
        return f"couldn't connect to {host} ({msg[:80]})"
    return f"{host}: {msg[:150]}"


async def read_inbox(address: str, password: str, limit: int = 15) -> dict:
    """Read the newest letters (inbox + junk) for a given email:pass. Returns
    {ok, address, messages, error}."""
    out = {"ok": False, "address": address, "messages": [], "error": None}
    if not EMAIL_LETTERS_ENABLED:
        out["error"] = "email reading is disabled"
        return out
    address = (address or "").strip()
    password = (password or "").strip()
    if not (address and password and "@" in address):
        out["error"] = "missing or malformed email:pass"
        return out
    out["address"] = address
    host = _imap_host_for(address)
    try:
        res = await asyncio.wait_for(
            asyncio.to_thread(_fetch_emails_sync, host, address, password, max(1, min(limit, 30))),
            timeout=EMAIL_FETCH_BUDGET_SECONDS + 30)
        out.update(ok=res["ok"], messages=res.get("messages", []))
    except Exception as e:
        out["error"] = _inbox_error(host, e)
        print(f"Inbox read failed for {address} via {host}: {type(e).__name__}: {e}")
    return out


async def fetch_account_emails(item_id: str | int, limit: int = 15) -> dict:
    """Resolve an owned account's email:pass from LZT, then read its inbox."""
    if not EMAIL_LETTERS_ENABLED:
        return {"ok": False, "address": None, "messages": [], "error": "email reading is disabled"}
    creds = await lzt_get_credentials(item_id)
    if not creds["ok"]:
        return {"ok": False, "address": None, "messages": [], "error": creds["error"]}
    address = creds.get("email_login")
    password = creds.get("email_password")
    if not (address and password) and creds.get("email_raw") and ":" in str(creds["email_raw"]):
        address, _, password = str(creds["email_raw"]).partition(":")
    if not (address and password):
        return {"ok": False, "address": None, "messages": [],
                "error": "this account has no email (inbox) access"}
    return await read_inbox(address, password, limit=limit)


# Email domain → (provider name, webmail URL) for the "how to get codes" guide.
EMAIL_PROVIDERS = {
    "rambler.ru": ("Rambler", "https://mail.rambler.ru"),
    "myrambler.ru": ("Rambler", "https://mail.rambler.ru"),
    "autorambler.ru": ("Rambler", "https://mail.rambler.ru"),
    "ro.ru": ("Rambler", "https://mail.rambler.ru"),
    "lenta.ru": ("Rambler", "https://mail.rambler.ru"),
    "mail.ru": ("Mail.ru", "https://e.mail.ru"),
    "bk.ru": ("Mail.ru", "https://e.mail.ru"),
    "inbox.ru": ("Mail.ru", "https://e.mail.ru"),
    "list.ru": ("Mail.ru", "https://e.mail.ru"),
    "internet.ru": ("Mail.ru", "https://e.mail.ru"),
    "gmx.com": ("GMX", "https://www.gmx.com"),
    "gmx.net": ("GMX", "https://www.gmx.net"),
    "gmx.de": ("GMX", "https://www.gmx.net"),
    "outlook.com": ("Outlook", "https://outlook.live.com"),
    "hotmail.com": ("Outlook", "https://outlook.live.com"),
    "live.com": ("Outlook", "https://outlook.live.com"),
    "gmail.com": ("Gmail", "https://mail.google.com"),
    "firstmail.ltd": ("Firstmail", "https://firstmail.ltd"),
    "firstmail.com": ("Firstmail", "https://firstmail.ltd"),
    "notletters.com": ("Notletters", "https://notletters.com"),
    "yahoo.com": ("Yahoo", "https://mail.yahoo.com"),
}
# Substring → (provider name, webmail) for the guide, matching many resold domains.
EMAIL_PROVIDER_SUBSTR = {
    "firstmail": ("Firstmail", "https://firstmail.ltd"),
    "notletters": ("Notletters", "https://notletters.com"),
}


def _email_provider(address: str) -> tuple[str, str | None]:
    domain = (address or "").split("@")[-1].strip().lower()
    if domain in EMAIL_PROVIDERS:
        return EMAIL_PROVIDERS[domain]
    for needle, info in EMAIL_PROVIDER_SUBSTR.items():
        if needle in domain:
            return info
    return (domain or "Email", None)


def email_provider_guide_embed(address: str) -> discord.Embed:
    name, url = _email_provider(address)
    body = (
        f"Your account's email is a **{name}** inbox (`{address}`).\n\n"
        "**To get login / verification codes:**\n"
        "1️⃣ Tap **📧 Read email letters** on your delivery above — it shows **all** letters "
        "from the inbox **and the spam folder**, and pulls out the code automatically. "
        "No external site needed.\n"
    )
    if url:
        body += (f"2️⃣ Or log in directly at **{url}** using the email & password from your "
                 "delivery.\n")
    body += ("\n💡 Codes arrive as new letters — request the code, wait a few seconds, then hit "
             "**🔄 Refresh**. Reading here keeps your letters unread.")
    e = discord.Embed(title="📧  How to get your email codes", description=body, color=AF_BLUE)
    e.set_footer(text="AF SERVICES • Email access")
    return e


class EmailPager(discord.ui.View):
    """LZT-style inbox reader: paginated list of ALL letters + open any one in full."""
    PAGE = 8

    def __init__(self, address: str, messages: list[dict], item_id: int | None = None,
                 password: str | None = None):
        super().__init__(timeout=900)
        self.address = address
        self.messages = messages[:30]
        self.item_id = item_id
        self.password = password
        self.page = 0
        self.notice = ""

        self.prev_btn = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary)
        self.prev_btn.callback = self._prev
        self.next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary)
        self.next_btn.callback = self._next
        self.refresh_btn = discord.ui.Button(label="🔄 Refresh", style=discord.ButtonStyle.primary)
        self.refresh_btn.callback = self._refresh_btn
        self.add_item(self.prev_btn)
        self.add_item(self.next_btn)
        self.add_item(self.refresh_btn)

        # The selector is re-filled per page in render(), so every letter can be
        # opened — a single 25-option list would hide letters 26+.
        self.selector = discord.ui.Select(placeholder="Open a letter to read it in full…",
                                          options=[discord.SelectOption(label="—", value="-1")])
        self.selector.callback = self._open
        self.add_item(self.selector)

    def _pages(self) -> int:
        return max(1, (len(self.messages) + self.PAGE - 1) // self.PAGE)

    def render(self) -> discord.Embed:
        pages = self._pages()
        self.page = max(0, min(self.page, pages - 1))
        start = self.page * self.PAGE
        chunk = self.messages[start:start + self.PAGE]

        desc = f"Inbox: `{self.address}` • **{len(self.messages)}** letter(s)"
        if self.notice:
            desc += f"\n{self.notice}"
        e = discord.Embed(title="📧  Email Letters", color=AF_BLUE, description=desc)
        if not self.messages:
            e.description += ("\n\nNo letters yet (inbox and spam are both empty) — request the "
                              "code again, wait a few seconds, then tap 🔄 Refresh.")
        for i, m in enumerate(chunk):
            code = f" • code `{m['code']}`" if m.get("code") else ""
            folder = m.get("folder") or ""
            spam = " • ⚠️ spam" if folder and folder.upper() != "INBOX" else ""
            name = f"{start + i + 1}. ✉️ {m.get('subject') or '(no subject)'}"[:256]
            val = (f"{(m.get('from') or '')[:70]} • *{m.get('date', '')}*{code}{spam}\n"
                   f"{m.get('snippet', '')[:150]}")
            e.add_field(name=name, value=val[:1024] or "—", inline=False)

        if chunk:
            self.selector.options = [
                discord.SelectOption(
                    label=f"{start + i + 1}. {(m.get('subject') or '(no subject)')[:80]}",
                    description=(m.get("from") or "")[:90] or None,
                    value=str(start + i))
                for i, m in enumerate(chunk)]
            self.selector.placeholder = "Open a letter to read it in full…"
            self.selector.disabled = False
        else:
            self.selector.options = [discord.SelectOption(label="No letters yet", value="-1")]
            self.selector.placeholder = "No letters to open yet"
            self.selector.disabled = True

        self.prev_btn.disabled = self.next_btn.disabled = (pages <= 1)
        e.set_footer(text=f"Page {self.page + 1}/{pages} • pick a letter below to read it fully")
        return e

    async def _prev(self, interaction: discord.Interaction):
        self.page = (self.page - 1) % self._pages()
        await interaction.response.edit_message(embed=self.render(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = (self.page + 1) % self._pages()
        await interaction.response.edit_message(embed=self.render(), view=self)

    async def _refresh_btn(self, interaction: discord.Interaction):
        await interaction.response.defer()
        res = None
        if self.item_id:
            res = await fetch_account_emails(self.item_id)
        elif self.address and self.password:
            res = await read_inbox(self.address, self.password)
        if res and res["ok"]:
            newview = EmailPager(res["address"] or self.address, res["messages"],
                                 self.item_id, self.password)
            newview.page = self.page
        else:
            # Keep the letters we already have on screen and say why it failed,
            # instead of silently redrawing the same list.
            newview = self
            newview.notice = (f"⚠️ Refresh failed: `{res['error']}`"
                              if res and res.get("error") else
                              "⚠️ Refresh needs the email password — re-run `/email`.")
        await interaction.edit_original_response(embed=newview.render(), view=newview)

    async def _open(self, interaction: discord.Interaction):
        idx = int(self.selector.values[0])
        if not (0 <= idx < len(self.messages)):
            await interaction.response.send_message(
                "That letter is gone — tap 🔄 Refresh.", ephemeral=True)
            return
        m = self.messages[idx]
        body = (m.get("body") or m.get("snippet") or "").strip() or "(empty letter)"
        e = discord.Embed(title=f"✉️  {m.get('subject') or '(no subject)'}"[:256],
                          description=body[:4000], color=AF_BLUE)
        e.add_field(name="From", value=(m.get("from") or "—")[:200], inline=True)
        if m.get("date"):
            e.add_field(name="Date", value=m["date"], inline=True)
        if m.get("code"):
            e.add_field(name="Code", value=f"`{m['code']}`", inline=True)
        folder = m.get("folder") or ""
        if folder and folder.upper() != "INBOX":
            e.add_field(name="Folder", value=f"{folder} (spam)", inline=True)
        e.set_footer(text="AF SERVICES • Email letter")
        await interaction.response.send_message(embed=e, ephemeral=True)


class ReadMailButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"af_mail:(?P<item_id>\d+)"):
    """Persistent button on a delivery message → reads the account's inbox letters."""
    def __init__(self, item_id: int):
        self.item_id = int(item_id)
        super().__init__(discord.ui.Button(
            label="📧 Read email letters", style=discord.ButtonStyle.secondary,
            custom_id=f"af_mail:{item_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["item_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        res = await fetch_account_emails(self.item_id)
        if not res["ok"]:
            await interaction.followup.send(
                f"⚠️ Couldn't read the email letters: `{res['error']}`\n"
                "You can still log in to the mailbox with the email:pass from your delivery, "
                "or read it here with `/email your@mail.com:password`.", ephemeral=True)
            return
        pager = EmailPager(res["address"], res["messages"], self.item_id)
        await interaction.followup.send(embed=pager.render(), view=pager, ephemeral=True)


# ============================================================
# DELIVERY — idempotent account release into a ticket
# ============================================================
async def order_already_delivered(order_id: str | None) -> bool:
    if not order_id:
        return False
    row = await db_fetchrow("SELECT 1 FROM deliveries WHERE order_id=$1", order_id)
    return row is not None


def credentials_embed(product: str | None, order_id: str | None,
                      login: str | None, password: str | None, raw: str | None,
                      email_login: str | None = None, email_password: str | None = None,
                      email_raw: str | None = None) -> discord.Embed:
    body = "Here is your account. **Tap the hidden field to reveal it**, then secure it immediately.\n\n"
    if login:
        body += f"**Login:** ||{login}||\n"
    if password:
        body += f"**Password:** ||{password}||\n"
    if raw and not (login and password):
        body += f"**Account:** ||{raw}||\n"

    has_email = bool(email_login or email_raw)
    if has_email:
        body += "\n**📧 Email (inbox) access — for verification codes:**\n"
        if email_login:
            body += f"**Email:** ||{email_login}||\n"
        if email_password:
            body += f"**Email password:** ||{email_password}||\n"
        if email_raw and not (email_login and email_password):
            body += f"**Email login:** ||{email_raw}||\n"

    body += "\nFollow **/guide** to lock the account to you (change email/password, enable 2FA)."
    if WARRANTY_TEXT:
        body += f"\n🛡️ **Warranty:** {WARRANTY_TEXT}."

    e = discord.Embed(title="📦  Your Account — Delivered", description=body, color=0x2ECC71)
    if product:
        e.add_field(name="Product", value=product, inline=True)
    if order_id:
        e.add_field(name="Order", value=f"`{order_id}`", inline=True)
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES • Thank you for your purchase")
    return e


async def deliver_account(
    channel: discord.TextChannel,
    owner: discord.abc.User,
    lzt_item_id: str | int,
    product: str | None,
    order_id: str | None,
    delivered_by: int | None,
) -> bool:
    """Pull credentials from LZT for the given item and deliver them into the ticket.
    Guards against re-using an order. Returns True on success."""
    # Per-staff delivery cooldown: block a staff member from releasing another
    # account until DELIVERY_COOLDOWN_SECONDS have passed since their last one.
    if delivered_by:
        last = _staff_delivery_cooldown.get(delivered_by)
        if last is not None:
            elapsed = (utcnow() - last).total_seconds()
            if elapsed < DELIVERY_COOLDOWN_SECONDS:
                remaining = int(DELIVERY_COOLDOWN_SECONDS - elapsed)
                mins, secs = divmod(remaining, 60)
                await channel.send(
                    f"⏳ <@{delivered_by}> is on delivery cooldown — please wait "
                    f"**{mins}m {secs:02d}s** before releasing another account."
                )
                return False

    if order_id and await order_already_delivered(order_id):
        await channel.send("⚠️ That order has already been used for a delivery — staff will review.")
        return False

    creds = await lzt_get_credentials(lzt_item_id)
    if not creds["ok"]:
        await channel.send(f"⚠️ Couldn't load that account from stock: `{creds['error']}`")
        return False

    # Price charged for this order (for /sales), if we recorded one at checkout.
    trow = await db_fetchrow("SELECT checkout_total FROM tickets WHERE channel_id=$1", channel.id)
    price = float(trow["checkout_total"]) if trow and trow["checkout_total"] is not None else None

    # Record first (unique index on order_id makes a double-deliver fail loudly).
    try:
        await db_execute(
            """INSERT INTO deliveries(channel_id, owner_id, product, order_id, lzt_item_id, delivered_by, price)
               VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            channel.id, owner.id, product, order_id, int(re.sub(r"[^0-9]", "", str(lzt_item_id)) or 0),
            delivered_by, price,
        )
    except asyncpg.UniqueViolationError:
        await channel.send("⚠️ That order was already delivered (duplicate blocked).")
        return False

    embed = credentials_embed(product or creds.get("title"), order_id,
                              creds["login"], creds["password"], creds["raw"],
                              creds.get("email_login"), creds.get("email_password"),
                              creds.get("email_raw"))
    # If the account ships with email access, offer a "Read email letters" button
    # so the buyer can pull verification codes without any external link.
    mail_view = None
    has_email = bool(creds.get("email_login") or creds.get("email_raw"))
    iid_int = int(re.sub(r"[^0-9]", "", str(lzt_item_id)) or 0)
    if EMAIL_LETTERS_ENABLED and has_email and iid_int:
        mail_view = discord.ui.View(timeout=None)
        mail_view.add_item(ReadMailButton(iid_int))
    send_kwargs = {"content": owner.mention, "embed": embed, "files": embed_files()}
    if mail_view is not None:
        send_kwargs["view"] = mail_view
    await channel.send(**send_kwargs)
    # Tell the buyer how to read codes for this specific email provider.
    if EMAIL_LETTERS_ENABLED and has_email:
        addr = creds.get("email_login") or (str(creds.get("email_raw") or "").split(":")[0])
        if addr:
            try:
                await channel.send(embed=email_provider_guide_embed(addr))
            except Exception as e:
                print("Email guide post failed:", e)
    try:
        await owner.send(embed=embed)  # also DM the buyer as a backup copy
    except Exception:
        pass

    await db_execute("UPDATE tickets SET ai_handled=TRUE WHERE channel_id=$1", channel.id)

    log_ch = await get_log_channel(channel.guild)
    if log_ch:
        le = discord.Embed(title="✅ Account Delivered", color=0x2ECC71)
        le.add_field(name="Buyer", value=f"{owner.mention} (`{owner.id}`)", inline=False)
        le.add_field(name="Product", value=product or creds.get("title") or "—", inline=True)
        le.add_field(name="Item", value=str(lzt_item_id), inline=True)
        le.add_field(name="Order", value=f"`{order_id}`" if order_id else "—", inline=True)
        if delivered_by:
            le.add_field(name="Released by", value=f"<@{delivered_by}>", inline=False)
        le.add_field(name="Channel", value=channel.mention, inline=False)
        await log_ch.send(embed=le)

    # Confirmed purchase → send the security guide + replacement policy so the buyer
    # knows how to secure the account and how replacements work.
    try:
        await post_purchase_followup(channel, owner, product or creds.get("title"), creds.get("title"))
    except Exception as e:
        print("post-purchase guide failed:", e)

    # Start the staff member's cooldown only after a successful release, so a
    # failed attempt (bad item id, etc.) never locks them out.
    if delivered_by:
        _staff_delivery_cooldown[delivered_by] = utcnow()
    return True


# ============================================================
# HELPERS
# ============================================================
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def safe_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name[:40] if name else "ticket"


def kind_label(kind: str) -> str:
    return {
        "claim": "Claim Order",
        "custom": "Custom Order",
        "support": "Issues/Help",
    }.get(kind, "Support")


def kind_prefix(kind: str) -> str:
    return {
        "claim": "claim",
        "custom": "custom",
        "support": "support",
    }.get(kind, "support")


def kind_emoji(kind: str) -> str:
    return {
        "claim": "🛒",
        "custom": "🛍️",
        "support": "🎫",
    }.get(kind, "🎫")


def category_for_kind(kind: str) -> int:
    if kind == "claim":
        return CLAIM_CATEGORY_ID
    if kind == "custom":
        return CUSTOM_CATEGORY_ID
    return SUPPORT_CATEGORY_ID


def get_staff_role(guild: discord.Guild) -> discord.Role | None:
    return guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None


def get_owner_role(guild: discord.Guild) -> discord.Role | None:
    return guild.get_role(OWNER_ROLE_ID) if OWNER_ROLE_ID else None


def is_owner_member(member: discord.Member) -> bool:
    """True for the shop owner: the dedicated owner role if configured,
    otherwise the server owner / administrators."""
    if member.guild and member.id == member.guild.owner_id:
        return True
    role = get_owner_role(member.guild)
    if role is not None:
        return role in member.roles
    # No owner role configured → admins act as the owner.
    return member.guild_permissions.administrator


def is_staff(member: discord.Member) -> bool:
    # Server owner and administrators always count as staff so the bot
    # owner is never locked out, even without the explicit staff role.
    if member.guild and member.id == member.guild.owner_id:
        return True
    if member.guild_permissions.administrator:
        return True
    # The shop owner has every staff privilege (delivering, closing, etc.).
    if is_owner_member(member):
        return True
    role = get_staff_role(member.guild)
    return (role in member.roles) if role else False


async def resolve_text_channel(guild: discord.Guild, channel_id: int) -> discord.TextChannel | None:
    if not channel_id:
        return None
    ch = guild.get_channel(channel_id)
    if isinstance(ch, discord.TextChannel):
        return ch
    try:
        fetched = await bot.fetch_channel(channel_id)
        return fetched if isinstance(fetched, discord.TextChannel) else None
    except Exception:
        return None


async def get_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return await resolve_text_channel(guild, TICKET_LOG_CHANNEL_ID)


async def get_transcript_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """The transcript channel — transcripts plus every staff-only order notice go
    here instead of into the customer's ticket."""
    return await resolve_text_channel(guild, TRANSCRIPT_CHANNEL_ID)


def make_channel_name(kind: str, owner: discord.abc.User, num: int) -> str:
    return f"{kind_emoji(kind)}-{kind_prefix(kind)}-{safe_name(owner.name)}-{num:04d}"


def sanitize_channel_rename(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^\w\-\u0080-\uffff]", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:90] if text else "renamed-ticket"


async def is_ticket_channel(channel_id: int) -> bool:
    row = await db_fetchrow("SELECT 1 FROM tickets WHERE channel_id=$1", channel_id)
    return row is not None


async def hide_ticket_from_other_staff(channel: discord.TextChannel, claimer: discord.Member) -> None:
    staff_role = get_staff_role(channel.guild)
    if staff_role:
        await channel.set_permissions(staff_role, overwrite=discord.PermissionOverwrite(view_channel=False))

    await channel.set_permissions(
        claimer,
        overwrite=discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    )


# ============================================================
# EMBEDS
# ============================================================
def panel_embed() -> discord.Embed:
    e = discord.Embed(
        title="AF SERVICES Tickets",
        description=(
            "Do you require assistance with anything? If so,\n"
            "please open a ticket and our support team will answer your queries.\n\n"
            "**What can we help with?**\n"
            "• Claim Order\n"
            "• Custom Order\n"
            "• Issues/Help\n\n"
            "Please be precise and straight forward with your query."
        ),
        color=AF_BLUE,
    )
    e.set_author(name="AF SERVICES Support System")
    e.set_thumbnail(url=logo_ref())
    e.set_image(url=banner_ref())
    e.set_footer(text="Support Team | AF SERVICES")
    return e


def ticket_embed(
    kind: str,
    owner_mention: str,
    claimed_by_mention: str | None,
    first_staff_seconds: int | None,
    footer_text: str | None
) -> discord.Embed:
    title = kind_label(kind)
    claimed_line = claimed_by_mention or "This ticket has not been claimed."

    e = discord.Embed(
        title=title,
        description=(
            "Thank you for contacting us.\n"
            "Please describe your request clearly.\n\n"
            "**Claimed by**\n"
            f"{claimed_line}\n\n"
            f"**Owner:** {owner_mention}"
        ),
        color=AF_BLUE,
    )
    if first_staff_seconds is not None:
        mins = first_staff_seconds // 60
        secs = first_staff_seconds % 60
        e.add_field(name="First staff response", value=f"{mins}m {secs}s", inline=False)

    e.set_author(name="AF SERVICES Tickets")
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text=footer_text or "AF SERVICES")
    return e


def make_proof_checklist_embed(kind: str) -> discord.Embed:
    """Shown to the customer on open so they know exactly what to provide before
    staff can forward the ticket to the owner for the final hand-off."""
    if kind == "claim":
        e = discord.Embed(
            title="📋  Before we can process your claim",
            description=(
                "To verify your order and hand it off for delivery, please provide the "
                "following. Staff will review, then forward the ticket to the owner who "
                "releases your account.\n" + DIVIDER
            ),
            color=AF_BLUE,
        )
        e.add_field(name="1️⃣ Proof of payment",
                    value="Your **order ID / invoice ID** (preferred) or a screenshot of the "
                          "payment & confirmation email.", inline=False)
        e.add_field(name="2️⃣ What you bought",
                    value="The **product name** exactly as listed at checkout.", inline=False)
        e.add_field(name="3️⃣ Requirements met",
                    value="Confirm you've followed everything in the account's **description / "
                          "listing requirements**.", inline=False)
    else:  # support / replacement
        e = discord.Embed(
            title="📋  Before we can process your replacement / issue",
            description=(
                (f"🛡️ **Warranty:** {WARRANTY_TEXT}.\n\n" if WARRANTY_TEXT else "")
                + "So we can verify eligibility and hand off to the owner, please provide the "
                "following.\n" + DIVIDER
            ),
            color=AF_BLUE,
        )
        e.add_field(name="1️⃣ Proof of purchase",
                    value="Your original **order ID** or purchase proof.", inline=False)
        e.add_field(name="2️⃣ The issue",
                    value="Describe the problem clearly (what happened, when).", inline=False)
        e.add_field(name="3️⃣ Recording (Full-Access accounts)",
                    value="A **video** is REQUIRED — record **before receiving** the account, "
                          "**logging in**, **securing it** (email/password/2FA), making the "
                          "**recovery ticket**, and **entering your first match**. "
                          "**No recording = no replacement.**", inline=False)
        e.add_field(name="⚠️ Replacement policy",
                    value=REPLACEMENT_RULES, inline=False)
    e.set_author(name="AF SERVICES • Verification")
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="Staff will review your proof, then forward this ticket to the owner.")
    return e


# ============================================================
# ORDER NOTICES — staff-only, always in the transcript channel
# ============================================================
async def post_order_to_fulfil(
    ticket: discord.TextChannel,
    reason: str,
    amount: float | None = None,
    force: bool = False,
) -> bool:
    """Tell the owner which account to buy and deliver for this ticket.

    Always posted in the transcript channel, never in the ticket: the notice
    contains the source listing and what it costs us, which the customer must
    not see. Fires once per ticket unless `force`. Returns True when the notice
    was posted."""
    row = await db_fetchrow(
        "SELECT owner_id, reserved_market_item_id, checkout_total, checkout_method, "
        "verified_product, verified_order_id, order_notice_sent FROM tickets WHERE channel_id=$1",
        ticket.id,
    )
    if not row:
        return False
    if row["order_notice_sent"] and not force:
        return False

    target = await get_transcript_channel(ticket.guild)
    if target is None:
        print("Order notice skipped: set TRANSCRIPT_CHANNEL_ID or TICKET_LOG_CHANNEL_ID")
        return False

    total = amount
    if total is None and row["checkout_total"] is not None:
        total = float(row["checkout_total"])

    e = discord.Embed(
        title="🛍️  Order to Fulfil — Buy & Deliver",
        description=f"{reason}\n{DIVIDER}",
        color=0x2ECC71,
    )
    e.add_field(name="🎟️ Ticket", value=ticket.mention, inline=True)
    e.add_field(name="👤 Customer", value=f"<@{int(row['owner_id'])}>", inline=True)
    if total is not None:
        e.add_field(name="💶 Customer paid",
                    value=f"€{total:.2f} ({(row['checkout_method'] or '—').upper()})", inline=True)
    if row["verified_order_id"]:
        e.add_field(name="🧾 Order", value=f"`{row['verified_order_id']}`", inline=True)

    reserved = row["reserved_market_item_id"]
    if reserved:
        iid = int(reserved)
        listing = f"{LZT_SITE}/{iid}/"
        title = row["verified_product"]
        det = await lzt_item_detail(iid)
        if det["ok"]:
            item = det["item"] or {}
            title = item.get("title") or item.get("title_en") or title
            src, resale = _resale_price(item)
            e.add_field(name="💸 Your cost", value=f"€{src:.2f}", inline=True)
            e.add_field(name="💰 Sells for", value=f"€{resale:.0f}", inline=True)
        e.add_field(name="📦 Product", value=str(title or "Account")[:1000], inline=False)
        e.add_field(name="🆔 Account to buy",
                    value=f"`{iid}` — [open listing]({listing})", inline=False)
        e.add_field(name="🚀 Then deliver it",
                    value=f"Run `/deliver {iid}` inside {ticket.mention} once you own it.",
                    inline=False)
    else:
        e.add_field(
            name="📦 Product",
            value=str(row["verified_product"]
                      or "No listing was reserved — open the ticket to see what the "
                         "customer ordered, then deliver with `/deliver <item id>`.")[:1000],
            inline=False)
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES • Owner action required")

    owner_role = get_owner_role(ticket.guild)
    try:
        await target.send(content=owner_role.mention if owner_role else None,
                          embed=e, files=embed_files())
    except Exception as ex:
        print("Order notice failed:", ex)
        return False
    await db_execute("UPDATE tickets SET order_notice_sent=TRUE WHERE channel_id=$1", ticket.id)
    return True


# ============================================================
# FORWARD-TO-OWNER FLOW
# ============================================================
async def forward_ticket_flow(
    channel: discord.TextChannel,
    forwarded_by: discord.Member | None,
) -> tuple[bool, str]:
    """Escalate a ticket to the shop owner: lock the customer out of typing, grant
    the owner access, and ping them to send the final logins. Staff can still type.
    `forwarded_by` is the staff member who forwarded, or None when the AI assistant
    forwards automatically. Returns (ok, ephemeral_status_message)."""
    row = await db_fetchrow(
        "SELECT owner_id, kind, status, forwarded_to_owner FROM tickets WHERE channel_id=$1",
        channel.id,
    )
    if not row:
        return False, "This is not a ticket channel."
    if row["status"] != "open":
        return False, "This ticket isn't open."
    if row["forwarded_to_owner"]:
        return False, "This ticket has already been forwarded to the owner."

    owner_role = get_owner_role(channel.guild)
    customer = channel.guild.get_member(int(row["owner_id"]))

    # Lock the customer out of typing (they can still read the channel).
    if customer is not None:
        try:
            await channel.set_permissions(
                customer,
                overwrite=discord.PermissionOverwrite(
                    view_channel=True, send_messages=False, read_message_history=True),
                reason="Ticket forwarded to owner — customer locked",
            )
        except Exception as e:
            print("Customer lock failed:", e)

    # Make sure the owner role can see and write in the ticket.
    if owner_role is not None:
        try:
            await channel.set_permissions(
                owner_role,
                overwrite=discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True),
                reason="Ticket forwarded to owner",
            )
        except Exception as e:
            print("Owner grant failed:", e)

    await db_execute(
        "UPDATE tickets SET forwarded_to_owner=TRUE, forwarded_by=$1, "
        "last_footer_text=$2, last_activity=NOW() WHERE channel_id=$3",
        (forwarded_by.id if forwarded_by else None),
        "AF SERVICES • Forwarded to owner", channel.id,
    )
    try:
        await refresh_ticket_control_message(channel)
    except Exception:
        pass

    is_replacement = row["kind"] in ("support",)
    action_line = (
        "send the **replacement logins** to the customer."
        if is_replacement else
        "release the **account / logins** to the customer."
    )
    ping = owner_role.mention if owner_role else (
        f"<@{channel.guild.owner_id}>" if channel.guild.owner_id else "@owner"
    )
    customer_mention = customer.mention if customer else f"<@{int(row['owner_id'])}>"
    actor = forwarded_by.mention if forwarded_by else "The assistant"

    e = discord.Embed(
        title="📨  Ticket Forwarded to Owner",
        description=(
            f"{actor} forwarded this ticket to the owner.\n\n"
            f"**Customer:** {customer_mention} — now **locked** (read-only).\n"
            f"**Owner:** please review the proof above and {action_line}"
        ),
        color=0x9B59B6,
    )
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES • Owner hand-off")
    await channel.send(content=ping, embed=e)

    # Which listing to buy and deliver goes to the transcript channel only — the
    # customer must never see the source listing or our cost inside their ticket.
    await post_order_to_fulfil(
        channel,
        f"{actor} forwarded this ticket to the owner — {action_line.capitalize()}",
    )

    log_ch = await get_log_channel(channel.guild)
    if log_ch:
        le = discord.Embed(title="📨 Ticket Forwarded to Owner", color=0x9B59B6)
        le.add_field(name="Ticket", value=channel.mention, inline=False)
        le.add_field(name="Forwarded by", value=actor, inline=True)
        le.add_field(name="Customer", value=customer_mention, inline=True)
        await log_ch.send(embed=le)

    return True, "✅ Ticket forwarded to the owner — the customer is now locked out of typing."


async def unforward_ticket_flow(
    channel: discord.TextChannel,
    unlocked_by: discord.Member,
) -> tuple[bool, str]:
    """Undo a forward: unlock the customer, clear the owner grant, and reset state.
    Returns (ok, ephemeral_status_message)."""
    row = await db_fetchrow(
        "SELECT owner_id, status, forwarded_to_owner FROM tickets WHERE channel_id=$1",
        channel.id,
    )
    if not row:
        return False, "This is not a ticket channel."
    if not row["forwarded_to_owner"]:
        return False, "This ticket isn't currently forwarded to the owner."

    customer = channel.guild.get_member(int(row["owner_id"]))
    if customer is not None:
        try:
            await channel.set_permissions(
                customer,
                overwrite=discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True),
                reason="Ticket un-forwarded — customer unlocked",
            )
        except Exception as e:
            print("Customer unlock failed:", e)

    owner_role = get_owner_role(channel.guild)
    if owner_role is not None:
        try:
            # Clear the explicit owner overwrite added on forward.
            await channel.set_permissions(owner_role, overwrite=None,
                                          reason="Ticket un-forwarded")
        except Exception as e:
            print("Owner overwrite clear failed:", e)

    await db_execute(
        "UPDATE tickets SET forwarded_to_owner=FALSE, forwarded_by=NULL, "
        "last_footer_text=$1, last_activity=NOW() WHERE channel_id=$2",
        "AF SERVICES", channel.id,
    )
    try:
        await refresh_ticket_control_message(channel)
    except Exception:
        pass

    customer_mention = customer.mention if customer else f"<@{int(row['owner_id'])}>"
    await channel.send(
        f"🔓 {unlocked_by.mention} un-forwarded this ticket — {customer_mention} can type again."
    )
    return True, "✅ Ticket un-forwarded — the customer can type again."


# ============================================================
# TRANSCRIPT
# ============================================================
async def build_formatted_transcript(channel: discord.TextChannel) -> bytes:
    lines: list[str] = []

    ticket_row = await db_fetchrow(
        "SELECT owner_id, kind, ticket_num, claimed_by, created_at FROM tickets WHERE channel_id=$1",
        channel.id,
    )

    claimed_by_text = "Unclaimed"
    if ticket_row and ticket_row["claimed_by"]:
        claimed_member = channel.guild.get_member(int(ticket_row["claimed_by"]))
        claimed_by_text = (
            f"{claimed_member} ({claimed_member.id})"
            if claimed_member else
            f"{int(ticket_row['claimed_by'])}"
        )

    lines.append(f"Transcript for: {channel.name}")
    lines.append(f"Channel ID: {channel.id}")
    if ticket_row:
        lines.append(f"Ticket kind: {ticket_row['kind']}")
        lines.append(f"Ticket number: {ticket_row['ticket_num']}")
        lines.append(f"Ticket owner ID: {ticket_row['owner_id']}")
        lines.append(f"Claimed by: {claimed_by_text}")
        lines.append(f"Created at: {ticket_row['created_at'].isoformat()}")
    lines.append(f"Generated: {utcnow().isoformat()}")
    lines.append("=" * 80)

    async for msg in channel.history(limit=None, oldest_first=True):
        ts = msg.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        author = f"{msg.author} ({msg.author.id})"
        content = (msg.content or "").replace("\r", "")

        lines.append(f"[{ts}] {author}")
        if content.strip():
            for line in content.split("\n"):
                lines.append(f"  {line}")

        if msg.attachments:
            lines.append("  Attachments:")
            for a in msg.attachments:
                lines.append(f"    - {a.url}")

        if msg.embeds:
            lines.append(f"  Embeds: {len(msg.embeds)}")

        lines.append("-" * 80)

    lines.append("END OF TRANSCRIPT")
    return ("\n".join(lines)).encode("utf-8", errors="replace")


async def send_transcript_txt(
    guild: discord.Guild,
    ticket_channel: discord.TextChannel,
    close_reason: str,
    closed_by: str,
    claimed_by: str,
    data: bytes | None = None,
) -> bool:
    log_ch = await get_transcript_channel(guild)
    if not log_ch:
        return False

    try:
        if data is None:
            data = await build_formatted_transcript(ticket_channel)
        f = discord.File(io.BytesIO(data), filename=f"{ticket_channel.name}.txt")
        await log_ch.send(
            content=(
                f"🧾 **Ticket Transcript**\n"
                f"Channel: `{ticket_channel.name}`\n"
                f"Closed by: {closed_by}\n"
                f"Claimed by: {claimed_by}\n"
                f"Reason: {close_reason}"
            ),
            file=f
        )
        return True
    except Exception as e:
        print("Transcript send failed:", e)
        return False


async def send_transcript_to_owner(
    guild: discord.Guild,
    ticket_channel: discord.TextChannel,
    owner_id: int,
    close_reason: str,
    data: bytes,
) -> bool:
    """DM the person who opened the ticket a copy of its transcript on close."""
    owner = guild.get_member(owner_id)
    if owner is None:
        try:
            owner = await bot.fetch_user(owner_id)
        except Exception:
            return False
    try:
        f = discord.File(io.BytesIO(data), filename=f"{ticket_channel.name}.txt")
        e = discord.Embed(
            title="🧾  Your Ticket Transcript",
            description=(
                f"Your ticket **`{ticket_channel.name}`** has been closed.\n"
                f"A full transcript is attached for your records.\n\n"
                f"**Reason:** {close_reason}"
            ),
            color=AF_BLUE,
        )
        e.set_thumbnail(url=logo_ref())
        e.set_footer(text="AF SERVICES • Thank you for reaching out")
        await owner.send(embed=e, file=f)
        return True
    except discord.Forbidden:
        return False
    except Exception as ex:
        print("Owner transcript DM failed:", ex)
        return False


# ============================================================
# CUSTOM IDS
# ============================================================
PANEL_SELECT_CID = "af_panel_select"


def cid_close(channel_id: int) -> str:
    return f"af_close:{channel_id}"


def cid_claim(channel_id: int) -> str:
    return f"af_claim:{channel_id}"


def cid_forward(channel_id: int) -> str:
    return f"af_forward:{channel_id}"


# ============================================================
# CLOSE MODAL
# ============================================================
class CloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    reason = discord.ui.TextInput(
        label="Close reason",
        style=discord.TextStyle.long,
        required=True,
        max_length=400,
        placeholder="Example: Delivered / Resolved / Duplicate / etc."
    )

    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Invalid context.", ephemeral=True)
            return
        # Staff can always close; the ticket owner may also close their own ticket.
        ticket_row = await db_fetchrow(
            "SELECT owner_id FROM tickets WHERE channel_id=$1", self.channel_id
        )
        is_owner = bool(ticket_row) and int(ticket_row["owner_id"]) == interaction.user.id
        if not (is_staff(interaction.user) or is_owner):
            await interaction.response.send_message(
                "Only staff or the person who opened this ticket can close it.", ephemeral=True)
            return

        ch = interaction.guild.get_channel(self.channel_id)
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("Ticket channel not found.", ephemeral=True)
            return

        await interaction.response.send_message("Closing ticket...", ephemeral=True)
        await close_ticket_flow(
            channel=ch,
            closed_by=f"{interaction.user} ({interaction.user.id})",
            reason=str(self.reason.value)
        )


# ============================================================
# TICKET CONTROLS VIEW
# ============================================================
class TicketControlView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

        close_btn = discord.ui.Button(
            label="Close Ticket",
            style=discord.ButtonStyle.danger,
            emoji="🔒",
            custom_id=cid_close(channel_id),
        )
        close_btn.callback = self._close_callback
        self.add_item(close_btn)

        claim_btn = discord.ui.Button(
            label="Claim",
            style=discord.ButtonStyle.success,
            emoji="✋",
            custom_id=cid_claim(channel_id),
        )
        claim_btn.callback = self._claim_callback
        self.add_item(claim_btn)

        forward_btn = discord.ui.Button(
            label="Forward to Owner",
            style=discord.ButtonStyle.primary,
            emoji="📨",
            custom_id=cid_forward(channel_id),
        )
        forward_btn.callback = self._forward_callback
        self.add_item(forward_btn)

    async def _close_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CloseReasonModal(self.channel_id))

    async def _forward_callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Invalid context.", ephemeral=True)
            return
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "Only staff can forward tickets to the owner.", ephemeral=True)
            return
        ch = interaction.guild.get_channel(self.channel_id)
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("Ticket channel not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok, msg = await forward_ticket_flow(ch, interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

    async def _claim_callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Invalid context.", ephemeral=True)
            return
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)
            return

        row = await db_fetchrow(
            "SELECT owner_id, kind, ticket_num, claimed_by, status FROM tickets WHERE channel_id=$1",
            self.channel_id
        )
        if not row or row["status"] != "open":
            await interaction.response.send_message("Ticket not found or not open.", ephemeral=True)
            return
        if row["claimed_by"] is not None:
            await interaction.response.send_message("This ticket is already claimed.", ephemeral=True)
            return

        await db_execute(
            "UPDATE tickets SET claimed_by=$1, last_activity=NOW() WHERE channel_id=$2",
            interaction.user.id, self.channel_id
        )

        ch = interaction.guild.get_channel(self.channel_id)
        if isinstance(ch, discord.TextChannel):
            try:
                await hide_ticket_from_other_staff(ch, interaction.user)
            except Exception as e:
                print("Permission update failed:", e)

            await ch.send(f"✅ Ticket claimed by {interaction.user.mention}.")
            await refresh_ticket_control_message(ch)

        await interaction.response.send_message("✅ Ticket claimed.", ephemeral=True)


# ============================================================
# PANEL SELECT VIEW
# ============================================================
class TicketPanelSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Claim Order", value="claim", description="Claim your order", emoji="🛒"),
            discord.SelectOption(label="Custom Order", value="custom", description="Browse & buy an account", emoji="🛍️"),
            discord.SelectOption(label="Issues/Help", value="support", description="Get help", emoji="🎫"),
        ]
        super().__init__(
            placeholder="Select a category...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=PANEL_SELECT_CID,
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        guild = interaction.guild
        user = interaction.user
        kind = self.values[0]

        # One open ticket per person, across every category. They must close
        # their current ticket before opening a new one of any kind.
        existing = await db_fetchrow(
            "SELECT channel_id FROM tickets WHERE guild_id=$1 AND owner_id=$2 AND status='open'",
            guild.id, user.id
        )
        if existing:
            ch = guild.get_channel(int(existing["channel_id"]))
            if isinstance(ch, discord.TextChannel):
                await interaction.response.send_message(
                    f"You already have an open ticket: {ch.mention}\n"
                    "Please close it before opening another one.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "You already have an open ticket — please close it before opening another.",
                    ephemeral=True)
            return

        cat_id = category_for_kind(kind)
        category = guild.get_channel(cat_id)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("Ticket category not configured correctly.", ephemeral=True)
            return

        num = await get_next_ticket_num(kind)
        channel_name = make_channel_name(kind, user, num)

        staff_role = get_staff_role(guild)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason="Ticket created"
        )

        await db_execute(
            """
            INSERT INTO tickets(channel_id, guild_id, owner_id, kind, ticket_num, status)
            VALUES ($1,$2,$3,$4,$5,'open')
            """,
            channel.id, guild.id, user.id, kind, num
        )

        embed = ticket_embed(
            kind=kind,
            owner_mention=user.mention,
            claimed_by_mention=None,
            first_staff_seconds=None,
            footer_text="AF SERVICES • Status: Waiting for staff"
        )
        msg = await channel.send(content=user.mention, embed=embed, view=TicketControlView(channel.id), files=embed_files())

        await db_execute(
            "UPDATE tickets SET control_message_id=$1 WHERE channel_id=$2",
            msg.id, channel.id
        )

        bot.add_view(TicketControlView(channel.id), message_id=msg.id)

        if kind == "custom" and AI_ENABLED:
            await channel.send(embed=make_buy_intro_embed())
        elif kind in ("claim", "support"):
            # Tell the customer up front what proof they must provide so staff can
            # verify and forward the ticket to the owner for the final hand-off.
            await channel.send(embed=make_proof_checklist_embed(kind))

        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketPanelSelect())


# ============================================================
# STAFF CHECK + COMMANDS
# ============================================================
def staff_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return is_staff(interaction.user)
    return app_commands.check(predicate)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Without this handler, a failed check (e.g. staff-only) silently drops the
    # interaction and Discord shows "Application did not respond".
    if isinstance(error, app_commands.CheckFailure):
        msg = "🚫 You don't have permission to use this command — staff only."
    else:
        msg = f"⚠️ Something went wrong while running this command:\n```{error}```"
        print("App command error:", repr(error))

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception as e:
        print("Failed to deliver error message:", e)


@bot.tree.command(name="ticket_panel", description="Post the AF SERVICES ticket panel.")
@staff_only()
async def ticket_panel(interaction: discord.Interaction):
    await interaction.response.send_message(embed=panel_embed(), view=TicketPanelView(), files=embed_files(include_banner=True))


@bot.tree.command(name="close", description="Close the current ticket.")
@app_commands.describe(reason="Reason for closing this ticket")
async def close_command(interaction: discord.Interaction, reason: str):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Use this in a ticket channel.", ephemeral=True)
        return

    ticket_row = await db_fetchrow(
        "SELECT owner_id FROM tickets WHERE channel_id=$1", interaction.channel.id
    )
    if not ticket_row:
        await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
        return

    # Staff can close any ticket; the ticket owner can close their own.
    is_owner = isinstance(interaction.user, discord.Member) and \
        int(ticket_row["owner_id"]) == interaction.user.id
    is_staff_member = isinstance(interaction.user, discord.Member) and is_staff(interaction.user)
    if not (is_owner or is_staff_member):
        await interaction.response.send_message(
            "Only staff or the person who opened this ticket can close it.", ephemeral=True)
        return

    await interaction.response.send_message("Closing ticket...", ephemeral=True)
    await close_ticket_flow(
        channel=interaction.channel,
        closed_by=f"{interaction.user} ({interaction.user.id})",
        reason=reason,
    )


@bot.tree.command(name="forward",
                  description="Forward this ticket to the owner (locks the customer, owner sends logins).")
@staff_only()
async def forward_command(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) \
            or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Use this in a ticket channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok, msg = await forward_ticket_flow(interaction.channel, interaction.user)
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="unforward",
                  description="Undo a forward: unlock the customer and reset the ticket.")
@staff_only()
async def unforward_command(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) \
            or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Use this in a ticket channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok, msg = await unforward_ticket_flow(interaction.channel, interaction.user)
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="ai",
                  description="Turn the AI assistant on/off for THIS ticket (kill-switch to take over).")
@staff_only()
@app_commands.describe(state="Turn the AI off to take over manually, or back on")
@app_commands.choices(state=[
    app_commands.Choice(name="off (staff take over)", value="off"),
    app_commands.Choice(name="on (resume AI)", value="on"),
])
async def ai_command(interaction: discord.Interaction, state: app_commands.Choice[str]):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Use this in a ticket channel.", ephemeral=True)
        return
    row = await db_fetchrow("SELECT 1 FROM tickets WHERE channel_id=$1", interaction.channel.id)
    if not row:
        await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
        return
    disabled = state.value == "off"
    await db_execute(
        "UPDATE tickets SET ai_disabled=$1 WHERE channel_id=$2",
        disabled, interaction.channel.id)
    if disabled:
        await interaction.response.send_message(
            "🤖 AI turned **off** for this ticket — staff have taken over.", ephemeral=True)
        await interaction.channel.send(
            f"🤖 The assistant has been switched off by {interaction.user.mention} — "
            "a staff member will help you from here.")
    else:
        await interaction.response.send_message(
            "🤖 AI turned **back on** for this ticket.", ephemeral=True)


@bot.tree.command(name="rename", description="Rename the current ticket channel.")
@staff_only()
@app_commands.describe(text="New channel name")
async def rename_command(interaction: discord.Interaction, text: str):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Use this in a ticket channel.", ephemeral=True)
        return

    if not await is_ticket_channel(interaction.channel.id):
        await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
        return

    new_name = sanitize_channel_rename(text)
    try:
        await interaction.channel.edit(name=new_name)
        await interaction.response.send_message(f"✅ Channel renamed to `{new_name}`.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Rename failed: {e}", ephemeral=True)


@bot.tree.command(name="purge", description="Close tickets in bulk.")
@staff_only()
@app_commands.describe(target="Use 'all' to close all open tickets")
@app_commands.choices(target=[app_commands.Choice(name="all", value="all")])
async def purge_command(interaction: discord.Interaction, target: app_commands.Choice[str]):
    if not interaction.guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    if target.value != "all":
        await interaction.response.send_message("Invalid purge target.", ephemeral=True)
        return

    rows = await db_fetch(
        "SELECT channel_id FROM tickets WHERE guild_id=$1 AND status='open'",
        interaction.guild.id,
    )
    if not rows:
        await interaction.response.send_message("No open tickets found.", ephemeral=True)
        return

    await interaction.response.send_message(f"Closing {len(rows)} open ticket(s)...", ephemeral=True)

    for row in rows:
        ch = interaction.guild.get_channel(int(row["channel_id"]))
        if isinstance(ch, discord.TextChannel):
            try:
                await close_ticket_flow(
                    channel=ch,
                    closed_by=f"{interaction.user} ({interaction.user.id})",
                    reason="Bulk purge: all open tickets",
                )
                await asyncio.sleep(1)
            except Exception as e:
                print(f"Failed to purge channel {ch.id}: {e}")


@bot.tree.command(name="ticket_stats", description="Show ticket stats overview (server).")
@staff_only()
async def ticket_stats(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Use in a server.", ephemeral=True)
        return

    totals = await db_fetchrow(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open,
          SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed,
          SUM(CASE WHEN status='deleted' THEN 1 ELSE 0 END) AS deleted
        FROM tickets
        WHERE guild_id=$1
        """,
        guild.id
    )

    by_kind = await db_fetch(
        """
        SELECT kind,
               COUNT(*) AS total,
               SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open
        FROM tickets
        WHERE guild_id=$1
        GROUP BY kind
        ORDER BY kind
        """,
        guild.id
    )

    avg_resp = await db_fetchrow(
        """
        SELECT AVG(first_staff_response_seconds)::float AS avg_first_response
        FROM tickets
        WHERE guild_id=$1 AND first_staff_response_seconds IS NOT NULL
        """,
        guild.id
    )

    avg_seconds = avg_resp["avg_first_response"]
    avg_text = "N/A" if avg_seconds is None else f"{int(avg_seconds) // 60}m {int(avg_seconds) % 60}s"

    e = discord.Embed(title="AF SERVICES • Ticket Stats", color=AF_BLUE)
    e.set_thumbnail(url=logo_ref())
    e.add_field(name="Total tickets", value=str(totals["total"]), inline=True)
    e.add_field(name="Open", value=str(totals["open"] or 0), inline=True)
    e.add_field(name="Closed", value=str(totals["closed"] or 0), inline=True)
    e.add_field(name="Deleted", value=str(totals["deleted"] or 0), inline=True)
    e.add_field(name="Avg first staff response", value=avg_text, inline=False)

    lines = []
    for r in by_kind:
        lines.append(f"**{kind_label(r['kind'])}**: total {r['total']}, open {r['open'] or 0}")
    e.add_field(name="By category", value="\n".join(lines) if lines else "No data", inline=False)

    await interaction.response.send_message(embed=e, ephemeral=True, files=embed_files())


# ============================================================
# STOCK / DELIVERY / VERIFICATION COMMANDS
# ============================================================
@bot.tree.command(name="stock", description="List the accounts in your stock.")
@staff_only()
@app_commands.describe(page="Page number (default 1)")
async def lzt_stock_command(interaction: discord.Interaction, page: int = 1):
    await interaction.response.defer(ephemeral=True)
    res = await lzt_list_owned(page=max(1, page))
    if not res["ok"]:
        await interaction.followup.send(f"⚠️ Stock error: `{res['error']}`", ephemeral=True)
        return
    if not res["items"]:
        await interaction.followup.send("No owned accounts found on this page.", ephemeral=True)
        return

    lines = []
    for it in res["items"][:25]:
        price = f" • {it['price']}" if it.get("price") is not None else ""
        state = f" • `{it['item_state']}`" if it.get("item_state") else ""
        lines.append(f"`{it['item_id']}` — {str(it['title'])[:60]}{price}{state}")

    e = discord.Embed(
        title="📦  Your Stock",
        description="\n".join(lines),
        color=AF_BLUE,
    )
    total = f" • total {res['total']}" if res.get("total") is not None else ""
    e.set_footer(text=f"AF SERVICES • Page {max(1, page)}{total} • Use the item ID with /deliver")
    await interaction.followup.send(embed=e, ephemeral=True)


LZT_CATEGORY_CHOICES = [
    app_commands.Choice(name="Fortnite", value="fortnite"),
    app_commands.Choice(name="Valorant", value="valorant"),
    app_commands.Choice(name="Steam", value="steam"),
    app_commands.Choice(name="Social Club", value="socialclub"),
]


@bot.tree.command(name="find_account", description="List VALID accounts you own in a category (excludes Invalid & Resold).")
@staff_only()
@app_commands.describe(category="Game category the customer wants")
@app_commands.choices(category=LZT_CATEGORY_CHOICES)
async def find_account_command(interaction: discord.Interaction, category: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    res = await lzt_find_valid(category.value)
    if not res["ok"]:
        await interaction.followup.send(f"⚠️ Stock error: `{res['error']}`", ephemeral=True)
        return

    used = await _delivered_item_ids()
    items = [it for it in res["items"]
             if int(re.sub(r"[^0-9]", "", str(it["item_id"])) or 0) not in used]
    if not items:
        await interaction.followup.send(
            f"No **valid** {category.name} accounts available in your purchases "
            f"(all are tagged Invalid/Resold or already delivered).", ephemeral=True)
        return

    lines = []
    for it in items[:25]:
        price = f" • {it['price']}" if it.get("price") is not None else ""
        lines.append(f"`{it['item_id']}` — {str(it['title'])[:60]}{price}")
    e = discord.Embed(
        title=f"✅  Valid {category.name} Stock",
        description="\n".join(lines),
        color=0x2ECC71,
    )
    e.set_footer(text=f"AF SERVICES • {len(items)} valid • excludes Invalid/Resold/already-delivered "
                      f"• deliver with /deliver_next or /deliver")
    await interaction.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="deliver_next",
                  description="Auto-pick the next VALID account in a category and deliver it into this ticket.")
@staff_only()
@app_commands.describe(category="Game category the customer wants")
@app_commands.choices(category=LZT_CATEGORY_CHOICES)
async def deliver_next_command(interaction: discord.Interaction, category: app_commands.Choice[str]):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Use this in a ticket channel.", ephemeral=True)
        return
    row = await db_fetchrow(
        "SELECT owner_id, verified_order_id, verified_product FROM tickets WHERE channel_id=$1",
        interaction.channel.id,
    )
    if not row:
        await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    res = await lzt_find_valid(category.value)
    if not res["ok"]:
        await interaction.followup.send(f"⚠️ Stock error: `{res['error']}`", ephemeral=True)
        return

    used = await _delivered_item_ids()
    candidates = [it for it in res["items"]
                  if int(re.sub(r"[^0-9]", "", str(it["item_id"])) or 0) not in used]
    if not candidates:
        await interaction.followup.send(
            f"❌ No **valid** {category.name} accounts left to deliver "
            f"(all tagged Invalid/Resold or already delivered).", ephemeral=True)
        return

    pick = candidates[0]
    owner = interaction.guild.get_member(int(row["owner_id"])) or await bot.fetch_user(int(row["owner_id"]))
    await interaction.followup.send(
        f"📦 Delivering valid {category.name} account `{pick['item_id']}` — "
        f"{str(pick['title'])[:60]}...", ephemeral=True)
    await deliver_account(
        channel=interaction.channel,
        owner=owner,
        lzt_item_id=pick["item_id"],
        product=row["verified_product"] or category.name,
        order_id=row["verified_order_id"],
        delivered_by=interaction.user.id,
    )


MARKET_CATEGORY_CHOICES = [
    app_commands.Choice(name="Valorant", value="valorant"),
    app_commands.Choice(name="Fortnite", value="fortnite"),
    app_commands.Choice(name="Steam", value="steam"),
    app_commands.Choice(name="Rockstar / GTA", value="rockstar"),
    app_commands.Choice(name="Epic Games", value="epicgames"),
    app_commands.Choice(name="EA App", value="ea"),
    app_commands.Choice(name="Ubisoft", value="ubisoft"),
    app_commands.Choice(name="Battle.net", value="battlenet"),
    app_commands.Choice(name="Minecraft", value="minecraft"),
    app_commands.Choice(name="Roblox", value="roblox"),
    app_commands.Choice(name="League of Legends", value="lol"),
    app_commands.Choice(name="Genshin Impact", value="genshin"),
    app_commands.Choice(name="ChatGPT", value="chatgpt"),
    app_commands.Choice(name="Supercell", value="supercell"),
]


MARKET_MAX_SCAN = 20  # cap on detail lookups when filtering by skin


@bot.tree.command(name="market",
                  description="Browse accounts available to buy on any supported game, within a budget.")
@app_commands.describe(
    category="Game the customer wants",
    budget="Customer's max budget in EUR (optional — we find accounts that fit)",
    count="How many accounts to show (1-5, default 3)",
    skins="Require specific skin(s)/game(s). Comma-separated (e.g. reaver, prime / GTA V, CS2).",
    region="Optional region filter (e.g. EU, NA, AP) — Valorant/Riot.",
)
@app_commands.choices(category=MARKET_CATEGORY_CHOICES)
async def market_command(interaction: discord.Interaction, category: app_commands.Choice[str],
                         budget: float | None = None, count: int = 3,
                         skins: str | None = None, region: str | None = None):
    await interaction.response.defer()
    count = max(1, min(count, 5))
    wanted = [s.strip() for s in re.split(r"[,\n]+", skins) if s.strip()] if skins else []
    region = (region or "").strip().lower() or None

    # When filtering we scan a bigger pool; a specific request pulls cheapest-first.
    specific = bool(wanted or region)
    pool = MARKET_SCAN_DEEP if specific else count
    res = await lzt_search_market(category.value, budget=budget, count=count, pool=pool,
                                  cheapest=specific)
    if not res["ok"]:
        await interaction.followup.send(f"⚠️ Stock error: `{res['error']}`", ephemeral=True)
        return
    if not res["items"]:
        await interaction.followup.send(
            f"No {category.name} accounts found"
            + (f" under €{budget:.0f}." if budget else "."), ephemeral=True)
        return

    # Scan deeply and rank: full matches first (cheapest), else best partial matches.
    chosen, full_match = await scan_and_rank(category.value, res["items"], wanted, region, count)

    cosmetic_game = category.value in ("valorant", "fortnite")
    if not chosen:
        msg = f"No {category.name} accounts matched"
        if wanted:
            msg += f" **{', '.join(wanted)}**"
        if region:
            msg += f" in region **{region.upper()}**"
        if budget:
            msg += f" under €{budget:.0f}"
        await interaction.followup.send(msg + ".", ephemeral=True)
        return
    partial = bool(wanted) and not full_match

    embeds, files = [], []
    for i, item in enumerate(chosen):
        embed, file = await build_account_message(category.value, item, i)
        embeds.append(embed)
        if file:
            files.append(file)

    if partial:
        header = f"🛍️ **{category.name} accounts** — closest matches (no single account had everything)"
    elif specific:
        header = f"🛍️ **{category.name} accounts** — cheapest match first"
    elif category.value == "valorant":
        header = f"🛍️ **{category.name} accounts available** — highest **VP spent** first"
    elif category.value == "fortnite":
        header = f"🛍️ **{category.name} accounts available** — highest **V-Bucks spent** first"
    else:
        header = f"🛍️ **{category.name} accounts available** — best value first"
    if wanted:
        header += f" · matching **{', '.join(wanted)}**"
    if region:
        header += f" · region **{region.upper()}**"
    if budget:
        header += f" · within a **€{budget:.0f}** budget"

    view = skins_view_for(chosen, category.value) if cosmetic_game else None
    kwargs = {"content": header, "embeds": embeds[:10], "files": files}
    if view is not None:
        kwargs["view"] = view
    await interaction.followup.send(**kwargs)


@bot.tree.command(name="account_info",
                  description="Show full details (skins, value, price) for one account.")
@app_commands.describe(item_id="Account listing/item ID")
async def account_info_command(interaction: discord.Interaction, item_id: str):
    await interaction.response.defer()
    det = await lzt_item_detail(item_id)
    if not det["ok"]:
        await interaction.followup.send(f"⚠️ Stock error: `{det['error']}`", ephemeral=True)
        return
    item = det["item"] or {}
    cid = (item.get("category") or {}).get("category_id") or item.get("category_id")
    # Render Valorant/Fortnite richly; every other LZT category renders generically.
    category = "valorant" if cid == 13 else "fortnite" if cid == 9 else "generic"
    embed, file = await build_account_message(category, item, 0)
    view = skins_view_for([item], category) if category in ("valorant", "fortnite") else None
    kwargs = {"embed": embed, "files": [file] if file else []}
    if view is not None:
        kwargs["view"] = view
    await interaction.followup.send(**kwargs)


@bot.tree.command(name="reserve",
                  description="Reserve an account for this ticket — auto-alerts you to buy it once paid.")
@staff_only()
@app_commands.describe(item_id="The account listing the customer chose")
async def reserve_command(interaction: discord.Interaction, item_id: str):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Use this in a ticket channel.", ephemeral=True)
        return
    row = await db_fetchrow("SELECT 1 FROM tickets WHERE channel_id=$1", interaction.channel.id)
    if not row:
        await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    det = await lzt_item_detail(item_id)
    if not det["ok"]:
        await interaction.followup.send(f"⚠️ Stock error: `{det['error']}`", ephemeral=True)
        return
    item = det["item"] or {}
    iid = int(re.sub(r"[^0-9]", "", str(item.get("item_id") or item_id)) or 0)
    src, resale = _resale_price(item)
    await db_execute(
        "UPDATE tickets SET reserved_market_item_id=$1, restock_alerted=FALSE, "
        "order_notice_sent=FALSE WHERE channel_id=$2",
        iid, interaction.channel.id,
    )
    await interaction.followup.send(
        f"📌 Reserved listing `{iid}` for this ticket — **{str(item.get('title') or 'Account')[:80]}**\n"
        f"Cost €{src:.2f} → sells for €{resale:.0f}. "
        f"You'll get a restock alert automatically once the buyer's payment is verified.",
        ephemeral=True,
    )


@bot.tree.command(name="restock",
                  description="Alert yourself to re-buy a sold account ASAP.")
@staff_only()
@app_commands.describe(item_id="The account item ID the customer bought")
async def restock_command(interaction: discord.Interaction, item_id: str):
    if not interaction.guild:
        await interaction.response.send_message("Use this inside the server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    det = await lzt_item_detail(item_id)
    if not det["ok"]:
        await interaction.followup.send(f"⚠️ Stock error: `{det['error']}`", ephemeral=True)
        return
    ticket = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
    status = await notify_restock(interaction.user, interaction.guild, det["item"] or {}, ticket)
    await interaction.followup.send(f"✅ {status}", ephemeral=True)


@bot.tree.command(name="email",
                  description="Read email letters / verification codes — paste your email:pass (or item ID for staff).")
@app_commands.describe(account="Your email:pass  (e.g. bob@rambler.ru:pass123)  — or an account item ID (staff)",
                       count="How many letters (1-30)")
async def email_command(interaction: discord.Interaction, account: str, count: int = 10):
    if not EMAIL_LETTERS_ENABLED:
        await interaction.response.send_message("Email reading is disabled.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    acc = account.strip()
    limit = max(1, min(count, 30))
    item_id_for_refresh: int | None = None
    password_for_refresh: str | None = None

    # email:pass — anyone can read an inbox they already have the creds for.
    # Buyers paste it with any separator (`:`, a space, `|`), and the password
    # itself may contain colons, so match the address and keep the rest as-is.
    creds = re.match(r"^(?P<addr>[^\s:|,]+@[^\s:|,]+?)\s*[:|,\s]\s*(?P<pw>\S.*)$", acc)
    if creds:
        address = creds.group("addr").strip()
        password = creds.group("pw").strip()
        password_for_refresh = password
        res = await read_inbox(address, password, limit=limit)
    else:
        # Item-ID lookup pulls creds from stock → staff only (don't let anyone
        # read arbitrary accounts' inboxes by guessing IDs).
        if not (isinstance(interaction.user, discord.Member) and is_staff(interaction.user)):
            await interaction.followup.send(
                "Paste your **email:pass** (e.g. `bob@rambler.ru:pass123`) to read your letters. "
                "Reading by item ID is staff-only.", ephemeral=True)
            return
        res = await fetch_account_emails(acc, limit=limit)
        item_id_for_refresh = int(re.sub(r"[^0-9]", "", acc) or 0) or None

    if not res["ok"]:
        await interaction.followup.send(
            f"⚠️ Couldn't read the inbox: `{res['error']}`\n"
            "Double-check the **email password** (not the game password) and that it's pasted "
            "as `email:password`.", ephemeral=True)
        return
    pager = EmailPager(res["address"], res["messages"], item_id_for_refresh, password_for_refresh)
    await interaction.followup.send(embed=pager.render(), view=pager, ephemeral=True)


# ============================================================
# CRYPTO CHECKOUT VIEWS (NowPayments) — posted into a ticket
# ============================================================
class CryptoSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Select a cryptocurrency to pay with…",
            min_values=1, max_values=1, custom_id="af_crypto_select",
            options=[discord.SelectOption(label=lbl, value=tk) for lbl, tk in CRYPTO_CHOICES.items()],
        )

    async def callback(self, interaction: discord.Interaction):
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("Use this inside the ticket.", ephemeral=True)
            return
        row = await db_fetchrow("SELECT crypto_amount FROM tickets WHERE channel_id=$1", ch.id)
        if not row or row["crypto_amount"] is None:
            await interaction.response.send_message(
                "No price is set yet — staff must run `/crypto` first.", ephemeral=True)
            return
        amount = float(row["crypto_amount"])
        coin = self.values[0]
        await interaction.response.defer()
        pay = await np_create_payment(amount, coin, order_id=f"ticket-{ch.id}",
                                      description=f"AF SERVICES order ({ch.name})")
        if not pay["ok"]:
            await interaction.followup.send(
                f"⚠️ Couldn't create the {coin.upper()} payment: `{pay['error']}`", ephemeral=True)
            return
        await db_execute(
            "UPDATE tickets SET crypto_payment_id=$1, crypto_paid=FALSE WHERE channel_id=$2",
            pay["payment_id"], ch.id)
        e = discord.Embed(
            title=f"💎  Send {str(pay['pay_currency']).upper()} to Pay €{amount:.2f}",
            description=("Send **exactly** the amount below to this address. "
                         f"Sending less will not confirm.\n{DIVIDER}"),
            color=AF_BLUE,
        )
        e.add_field(name="Amount to send",
                    value=f"`{pay['pay_amount']} {str(pay['pay_currency']).upper()}`", inline=False)
        e.add_field(name="Address", value=f"`{pay['pay_address']}`", inline=False)
        e.set_footer(text="After sending, click ‘I’ve Paid — Check’ below.")
        await interaction.followup.send(embed=e, view=CryptoCheckView())


class CryptoPayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CryptoSelect())


class CryptoCheckView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="I've Paid — Check", style=discord.ButtonStyle.success,
                       emoji="✅", custom_id="af_crypto_check")
    async def check(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("Use this inside the ticket.", ephemeral=True)
            return
        row = await db_fetchrow(
            "SELECT crypto_payment_id, crypto_paid, owner_id, ai_disabled, crypto_amount "
            "FROM tickets WHERE channel_id=$1", ch.id)
        if not row or not row["crypto_payment_id"]:
            await interaction.response.send_message("No crypto payment is in progress here.", ephemeral=True)
            return
        if row["crypto_paid"]:
            await interaction.response.send_message("✅ This order is already marked paid.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        st = await np_get_payment(row["crypto_payment_id"])
        if not st["ok"]:
            await interaction.followup.send(f"⚠️ Couldn't check the payment: `{st['error']}`", ephemeral=True)
            return
        if not st["paid"]:
            await interaction.followup.send(
                f"⏳ Not confirmed yet (status: `{st['status']}`). Give it a minute and check again.",
                ephemeral=True)
            return
        await db_execute("UPDATE tickets SET crypto_paid=TRUE, awaiting_proof=FALSE WHERE channel_id=$1", ch.id)
        await interaction.followup.send("✅ Payment confirmed — thank you!", ephemeral=True)
        owner = ch.guild.get_member(int(row["owner_id"])) or await bot.fetch_user(int(row["owner_id"]))
        await ch.send(
            embed=discord.Embed(
                title="✅  Crypto Payment Confirmed",
                description=f"{owner.mention}'s crypto payment is confirmed. Handing off to the owner to deliver.",
                color=0x2ECC71),
        )
        paid_amount = float(row["crypto_amount"]) if row["crypto_amount"] is not None else None
        # The account we now have to buy and hand over goes to the transcript
        # channel only — the customer stays in the dark about the source listing.
        await post_order_to_fulfil(
            ch, "💎 **Crypto payment confirmed** — buy this account and deliver it.",
            amount=paid_amount)
        await maybe_fire_restock(ch)
        # Owner always delivers → lock the customer and forward to the owner,
        # unless staff have taken the ticket over via the AI kill-switch.
        if not row["ai_disabled"]:
            await forward_ticket_flow(ch, None)


@bot.tree.command(name="crypto",
                  description="Post a crypto (LTC/SOL/BTC/ETH) payment option in this ticket.")
@staff_only()
@app_commands.describe(amount="Price in EUR (optional — defaults to the reserved account's price)")
async def crypto_command(interaction: discord.Interaction, amount: float | None = None):
    if not NOWPAYMENTS_ENABLED:
        await interaction.response.send_message(
            "⚠️ Crypto payments aren't configured (set `NOWPAYMENTS_API_KEY`).", ephemeral=True)
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Use this in a ticket channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    row = await db_fetchrow(
        "SELECT reserved_market_item_id FROM tickets WHERE channel_id=$1", interaction.channel.id)
    if not row:
        await interaction.followup.send("This is not a ticket channel.", ephemeral=True)
        return
    price = amount
    if price is None:
        if not row["reserved_market_item_id"]:
            await interaction.followup.send(
                "No account reserved — run `/reserve` first, or pass an amount: `/crypto 25`.",
                ephemeral=True)
            return
        det = await lzt_item_detail(row["reserved_market_item_id"])
        if not det["ok"]:
            await interaction.followup.send(
                f"⚠️ Couldn't load the reserved account: `{det['error']}`", ephemeral=True)
            return
        _, price = _resale_price(det["item"] or {})
    if not price or price <= 0:
        await interaction.followup.send("Couldn't determine a price.", ephemeral=True)
        return
    await post_crypto_checkout(interaction.channel, price)
    await interaction.followup.send("✅ Crypto checkout posted.", ephemeral=True)


@bot.tree.command(name="verify_order", description="Check a SellAuth order/invoice and its payment status.")
@staff_only()
@app_commands.describe(order_id="The SellAuth invoice / order ID")
async def verify_order_command(interaction: discord.Interaction, order_id: str):
    await interaction.response.defer(ephemeral=True)
    res = await sellauth_get_invoice(order_id)
    if not res["ok"]:
        await interaction.followup.send(f"⚠️ SellAuth error: `{res['error']}`", ephemeral=True)
        return
    if not res["found"]:
        await interaction.followup.send(f"❌ Order `{order_id}` not found on SellAuth.", ephemeral=True)
        return

    already = await order_already_delivered(order_id)
    e = discord.Embed(
        title="🔎  SellAuth Order",
        color=0x2ECC71 if res["paid"] else 0xE67E22,
    )
    e.add_field(name="Order", value=f"`{order_id}`", inline=True)
    e.add_field(name="Status", value=f"`{res.get('status')}`", inline=True)
    e.add_field(name="Paid", value="✅ Yes" if res["paid"] else "❌ Not yet", inline=True)
    if res.get("amount") is not None:
        e.add_field(name="Amount", value=str(res["amount"]), inline=True)
    if res.get("email"):
        e.add_field(name="Email", value=str(res["email"]), inline=True)
    if already:
        e.add_field(name="⚠️ Already delivered", value="This order was already used.", inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="deliver", description="Manually release an account into this ticket.")
@staff_only()
@app_commands.describe(
    item_id="Item ID to deliver (see /stock)",
    order_id="Optional SellAuth order ID (for the record / duplicate-protection)",
    product="Optional product label",
)
async def deliver_command(interaction: discord.Interaction, item_id: str,
                          order_id: str | None = None, product: str | None = None):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Use this in a ticket channel.", ephemeral=True)
        return

    row = await db_fetchrow(
        "SELECT owner_id, verified_order_id, verified_product FROM tickets WHERE channel_id=$1",
        interaction.channel.id,
    )
    if not row:
        await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
        return

    owner = interaction.guild.get_member(int(row["owner_id"]))
    if owner is None:
        owner = await bot.fetch_user(int(row["owner_id"]))

    await interaction.response.send_message("📦 Delivering account...", ephemeral=True)
    await deliver_account(
        channel=interaction.channel,
        owner=owner,
        lzt_item_id=item_id,
        product=product or row["verified_product"],
        order_id=order_id or row["verified_order_id"],
        delivered_by=interaction.user.id,
    )


# ============================================================
# GUIDE COMMAND
# ============================================================
GUIDE_STEAM_COLOR = 0x00ADEF
GUIDE_RIOT_COLOR  = 0xFF4655
GUIDE_EPIC_COLOR  = AF_BLUE

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━"


def make_steam_guide_embed() -> discord.Embed:
    e = discord.Embed(
        title="🎮  Steam — Account Security Guide",
        description=f"Follow these steps to properly secure your Steam account.\n{DIVIDER}",
        color=GUIDE_STEAM_COLOR,
    )
    steps = [
        ("1️⃣  Change Profile Info",
         "Update your **username**, **profile picture**, **display name**, and **country**."),
        ("2️⃣  Link Phone Number (2FA)",
         "Go to account settings and **link your phone number** to enable Two-Factor Authentication."),
        ("3️⃣  Enable Steam Guard",
         "Activate **Steam Guard Mobile Authenticator** for maximum account protection."),
        ("4️⃣  Block All Friends",
         "**Block every existing friend** on the account to cut off the previous owner's access."),
        ("5️⃣  Make Profile Private",
         "Go to **Privacy Settings** and set your profile to **Private** across all categories."),
    ]
    for name, value in steps:
        e.add_field(name=name, value=value, inline=False)
    e.set_author(name="AF SERVICES • Account Guides")
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES | Steam Guide")
    return e


def make_riot_guide_embed() -> discord.Embed:
    e = discord.Embed(
        title="⚔️  Riot Games / Valorant — Account Security Guide",
        description=f"Follow these steps to properly secure your Riot Games / Valorant account.\n{DIVIDER}",
        color=GUIDE_RIOT_COLOR,
    )
    steps = [
        ("1️⃣  Change Name & Username",
         "Update both the **in-game display name** and your **Riot username** in account settings."),
        ("2️⃣  Add a Google Account (Required!)",
         "🔗 **Add a Google account to the Valorant/Riot account** in your sign-in settings.\n"
         "Use a **Google account you fully control** — this locks the account to you, and it's "
         "**the proof we require for a replacement**: if anything goes wrong you must show that you "
         "can **no longer log in** with that Google account."),
        ("3️⃣  Block All Friends",
         "Go through your **friends list** and **block every contact**."),
        ("4️⃣  Wait 2–3 Days",
         "⏳ **Do not change the password yet.** Wait **2–3 days** before doing so."),
        ("5️⃣  Add 2FA (If Needed for Ranked)",
         "Enable **Two-Factor Authentication** if it is required to participate in ranked modes."),
        ("6️⃣  Check Riot Support — Close Active Tickets",
         "Visit **Riot Support** and check for **active tickets**.\n"
         "If any are open, reply with:\n"
         "> *\"I have dealt with the problem, you can close, I won't need it for now.\"*"),
        ("♻️  Need a Replacement Later?",
         "Always **log in with the same email / Google account** you linked here.\n"
         "Tap **♻️ Need a replacement?** below to see exactly what to record as proof."),
    ]
    for name, value in steps:
        e.add_field(name=name, value=value, inline=False)
    e.set_author(name="AF SERVICES • Account Guides")
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES | Riot Games Guide")
    return e


def make_riot_replacement_embed() -> discord.Embed:
    """Shown when a buyer taps 'Need a replacement?' — the proof their video must contain."""
    e = discord.Embed(
        title="♻️  Riot / Valorant — How to Claim a Replacement",
        description=(
            "To receive a replacement you **must** provide **one uncut video** as proof.\n"
            "Make sure every item below is clearly visible in that single recording.\n"
            f"{DIVIDER}"
        ),
        color=GUIDE_RIOT_COLOR,
    )
    e.add_field(
        name="🔑  Use the SAME Google account",
        value="On camera, attempt to sign in using the **exact Google account you added** to the "
              "Valorant account when you secured it. Show that **Google email on screen** first.",
        inline=False,
    )
    e.add_field(
        name="🚫  Prove the Google login no longer works",
        value="The key proof: show that signing in with **that Google account no longer gives you "
              "access** to the Valorant account — it's been removed / changed and you're **locked out**.",
        inline=False,
    )
    e.add_field(
        name="🎥  One continuous, uncut video",
        value="• **No cuts, no edits, no pauses** — a single take.\n"
              "• Record your **full screen** (not a cropped/phone-camera clip).\n"
              "• Show the **current date** on screen (e.g. open a clock/date site).",
        inline=False,
    )
    e.add_field(
        name="📨  Submit it",
        value="Open a **ticket** here and attach the video. Staff will verify and issue your replacement.",
        inline=False,
    )
    e.set_author(name="AF SERVICES • Replacement Policy")
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES | Riot Games Replacement")
    return e


class RiotGuideView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Need a replacement?",
        style=discord.ButtonStyle.danger,
        custom_id="af_riot_replacement",
        emoji="♻️",
    )
    async def replacement_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=make_riot_replacement_embed(),
            ephemeral=True,
        )


def make_replacement_policy_embed() -> discord.Embed:
    """Game-agnostic replacement policy, sent to every buyer after a confirmed purchase."""
    e = discord.Embed(
        title="♻️  Replacement Policy — Please Read",
        description=(
            "If something is wrong with your account, you're covered — here's how it works.\n"
            f"{DIVIDER}"
        ),
        color=0x2ECC71,
    )
    e.add_field(
        name="✅ You qualify for a replacement if",
        value="The account is **dead on arrival** or stops working before you've changed the "
              "login details — as long as you followed the security guide.",
        inline=False,
    )
    e.add_field(
        name="🎥 What you must provide",
        value="**One uncut video** (no cuts/edits) that:\n"
              "• Logs in **live** using the **same email / login** we delivered\n"
              "• Shows the **error or lockout** clearly\n"
              "• Shows your **full screen** and the **current date**",
        inline=False,
    )
    e.add_field(
        name="🚫 Not covered",
        value="Issues caused **after** you changed the email/password without following the guide, "
              "or claims made without the video proof above.",
        inline=False,
    )
    e.add_field(
        name="📨 How to claim",
        value="Open a ticket here with your video and order ID — staff will verify and replace it fast.",
        inline=False,
    )
    e.set_author(name="AF SERVICES • Replacement Policy")
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES | Warranty & Replacements")
    return e


def detect_game(*texts: str | None) -> str | None:
    """Best-effort game detection from a product label / account title."""
    blob = " ".join(t for t in texts if t).lower()
    if any(k in blob for k in ("valorant", "riot", "league")):
        return "valorant"
    if any(k in blob for k in ("fortnite", "epic")):
        return "fortnite"
    if "steam" in blob:
        return "steam"
    return None


async def post_purchase_followup(channel: discord.TextChannel, owner: discord.abc.User,
                                 product: str | None, title: str | None) -> None:
    """After a confirmed delivery, send the matching security guide + replacement policy."""
    game = detect_game(product, title)
    if game == "valorant":
        guides = [make_riot_guide_embed()]
    elif game == "fortnite":
        guides = [make_epic_guide_embed()]
    elif game == "steam":
        guides = [make_steam_guide_embed()]
    else:
        guides = []
    embeds = guides + [make_replacement_policy_embed()]

    view = RiotGuideView() if game == "valorant" else None
    await channel.send(
        content=(f"{owner.mention} ✅ **Purchase confirmed!** Please read the security guide and our "
                 f"replacement policy below — securing the account correctly keeps your warranty valid."),
        embeds=embeds[:10],
        view=view,
        files=embed_files(),
    )


def make_epic_guide_embed() -> discord.Embed:
    e = discord.Embed(
        title="🎯  Epic Games — Account Security Guide",
        description=f"Follow these steps to properly secure an Epic Games / Fortnite account.\n{DIVIDER}",
        color=GUIDE_EPIC_COLOR,
    )
    steps = [
        ("1️⃣  Change User ID",
         "Update the **account display name / user ID** if the option is available in settings."),
        ("2️⃣  Create a Fake Recovery Ticket",
         "Submit a recovery ticket via [Epic ID Recovery](https://www.epicgames.com/id/login/recovery/help) "
         "to lock in account recovery access."),
        ("3️⃣  Block All Friends & Connections",
         "**Block every friend and linked connection** on the account.\n"
         "You can use [FishStick FN](https://t.me/fishstickfn) for assistance."),
        ("4️⃣  Download Account PDF",
         "Download the **PDF with your account information** from "
         "[Account Settings](https://www.epicgames.com/id/login?redirect_uri=https%3A%2F%2Fwww.epicgames.com%2Faccount%2Fpersonal&prompt=select_account&display=guided)."),
        ("5️⃣  Waiting Periods",
         "⏳ Wait at least **8 days** before any event/tournament *(if different country or region)*.\n"
         "⏳ Wait **2 weeks** before making **ANY purchases**."),
        ("6️⃣  V-Bucks Accounts — Use All Refund Credits",
         "💰 **USE ALL REFUND CREDITS** — earn more V-Bucks and greatly reduce the risk of a skin revert."),
        ("7️⃣  No 2FA on Ramblers",
         "🚫 **DO NOT add 2FA** to any ramblers!"),
    ]
    for name, value in steps:
        e.add_field(name=name, value=value, inline=False)
    e.set_author(name="AF SERVICES • Account Guides")
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES | Epic Games Guide")
    return e


def make_chatgpt_guide_embed() -> discord.Embed:
    e = discord.Embed(
        title="🤖  ChatGPT / OpenAI — Account Security Guide",
        description=(f"Follow these steps to make the account fully yours. Do everything from "
                     f"**your own browser** while logged in.\n{DIVIDER}"),
        color=0x10A37F,
    )
    steps = [
        ("1️⃣  Log in & open Settings",
         "Sign in at [chatgpt.com](https://chatgpt.com), then open **Settings** → **Account**."),
        ("2️⃣  Change the email to YOUR email",
         "Update the account email to one **you** control, and confirm it via the verification "
         "link sent to your inbox (use the email access included with your delivery to grab any "
         "code, then switch it to your own address)."),
        ("3️⃣  Change the password",
         "Set a **new, unique password** you haven't used elsewhere."),
        ("4️⃣  Enable your own 2FA",
         "Turn on **Multi-Factor Authentication** with **your own** authenticator app so only you "
         "can log in."),
        ("5️⃣  Log out other sessions",
         "In Settings → **Security**, use **Log out of all devices** to remove any previous sessions."),
    ]
    for name, value in steps:
        e.add_field(name=name, value=value, inline=False)
    e.set_author(name="AF SERVICES • Account Guides")
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES | ChatGPT Guide")
    return e


@bot.tree.command(name="guide", description="View the account security guide for a game platform.")
@app_commands.describe(game="Select the game platform")
@app_commands.choices(game=[
    app_commands.Choice(name="Steam", value="steam"),
    app_commands.Choice(name="Riot Games", value="riot_game"),
    app_commands.Choice(name="Epic Games", value="epic_games"),
    app_commands.Choice(name="ChatGPT / OpenAI", value="chatgpt"),
])
async def guide_command(interaction: discord.Interaction, game: app_commands.Choice[str]):
    if game.value == "steam":
        embed = make_steam_guide_embed()
        await interaction.response.send_message(embed=embed, files=embed_files())
    elif game.value == "riot_game":
        embed = make_riot_guide_embed()
        await interaction.response.send_message(
            embed=embed, view=RiotGuideView(), files=embed_files()
        )
    elif game.value == "chatgpt":
        embed = make_chatgpt_guide_embed()
        await interaction.response.send_message(embed=embed, files=embed_files())
    else:
        embed = make_epic_guide_embed()
        await interaction.response.send_message(embed=embed, files=embed_files())


# ============================================================
# STATUS / SALES / ORDERS / COMPENSATION / PROOF OVERRIDE
# ============================================================
def _mask_secret(s: str) -> str:
    if not s:
        return "—"
    return f"{s[:7]}…{s[-3:]}" if len(s) > 12 else "set"


@bot.tree.command(name="aistatus", description="Show the bot's live AI / integration config (staff).")
@staff_only()
async def aistatus_command(interaction: discord.Interaction):
    e = discord.Embed(title="🤖  Bot Status", color=AF_BLUE)
    e.add_field(name="AI",
                value=(f"{'🟢 ON' if AI_ENABLED else '🔴 OFF'} • model `{AI_MODEL}`\n"
                       f"key `{_mask_secret(ANTHROPIC_API_KEY)}`"), inline=False)
    e.add_field(name="LZT.market",
                value=(f"{'🟢 ON' if LZT_ENABLED else '🔴 OFF'} • user `{LZT_USER_ID or '—'}`\n"
                       f"token `{_mask_secret(LZT_API_TOKEN)}`"), inline=False)
    e.add_field(name="NowPayments", value="🟢 ON" if NOWPAYMENTS_ENABLED else "🔴 OFF", inline=True)
    e.add_field(name="SellAuth", value="🟢 ON" if SELLAUTH_ENABLED else "🔴 OFF", inline=True)
    e.add_field(name="Owner role",
                value=(f"<@&{OWNER_ROLE_ID}>" if OWNER_ROLE_ID else "—"), inline=True)
    e.add_field(
        name="Reps • Customer role • TOS",
        value=(f"{'<#'+str(REPS_CHANNEL_ID)+'>' if REPS_CHANNEL_ID else '—'} • "
               f"{'<@&'+str(CUSTOMER_ROLE_ID)+'>' if CUSTOMER_ROLE_ID else '—'} • "
               f"{'<#'+str(TOS_CHANNEL_ID)+'>' if TOS_CHANNEL_ID else '—'}"), inline=False)
    e.add_field(
        name="Automation",
        value=(f"idle-close **{IDLE_CLOSE_HOURS:g}h** • review reminder **{REVIEW_REMINDER_HOURS:g}h** • "
               f"low-stock **<{LOW_STOCK_THRESHOLD}** • card tax **×{CARD_TAX_MULTIPLIER}** • "
               f"markup **×{RESALE_MULTIPLIER}**"), inline=False)
    e.set_footer(text="AF SERVICES • Status")
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="sales", description="Delivery & revenue stats (staff).")
@staff_only()
@app_commands.describe(days="Look back this many days (default 30)")
async def sales_command(interaction: discord.Interaction, days: int = 30):
    days = max(1, min(days, 365))
    await interaction.response.defer(ephemeral=True)
    rows = await db_fetch(
        "SELECT product, price FROM deliveries WHERE delivered_at > NOW() - make_interval(days => $1)",
        days)
    total = len(rows)
    revenue = sum(float(r["price"]) for r in rows if r["price"] is not None)
    priced = sum(1 for r in rows if r["price"] is not None)
    by_product: dict[str, int] = {}
    for r in rows:
        p = (r["product"] or "Account").strip()[:40]
        by_product[p] = by_product.get(p, 0) + 1
    top = sorted(by_product.items(), key=lambda kv: kv[1], reverse=True)[:8]

    e = discord.Embed(title=f"📈  Sales — last {days} day(s)", color=0x2ECC71)
    e.add_field(name="Delivered", value=str(total), inline=True)
    e.add_field(name="Revenue", value=f"€{revenue:,.2f}", inline=True)
    e.add_field(name="Avg / order",
                value=(f"€{revenue/priced:,.2f}" if priced else "—"), inline=True)
    if priced < total:
        e.add_field(name="Note", value=f"Revenue covers **{priced}/{total}** orders with a recorded price.",
                    inline=False)
    if top:
        e.add_field(name="Top products",
                    value="\n".join(f"**{n}×** {name}" for name, n in top), inline=False)
    e.set_footer(text="AF SERVICES • Sales")
    await interaction.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="myorders", description="See your past orders with us.")
async def myorders_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = await db_fetch(
        "SELECT product, price, delivered_at FROM deliveries WHERE owner_id=$1 "
        "ORDER BY delivered_at DESC LIMIT 25", interaction.user.id)
    if not rows:
        await interaction.followup.send("You don't have any orders on record yet.", ephemeral=True)
        return
    lines = []
    for r in rows:
        when = r["delivered_at"].strftime("%Y-%m-%d")
        price = f" — €{float(r['price']):.2f}" if r["price"] is not None else ""
        lines.append(f"• **{r['product'] or 'Account'}** — {when}{price}")
    e = discord.Embed(title="🧾  Your Orders", description="\n".join(lines)[:4000], color=AF_BLUE)
    e.set_footer(text=f"AF SERVICES • {len(rows)} order(s)")
    await interaction.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="verifyproof",
                  description="Override: accept the proof and forward this ticket to the owner (staff).")
@staff_only()
async def verifyproof_command(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel) \
            or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Use this in a ticket channel.", ephemeral=True)
        return
    row = await db_fetchrow("SELECT 1 FROM tickets WHERE channel_id=$1", interaction.channel.id)
    if not row:
        await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await db_execute("UPDATE tickets SET awaiting_proof=FALSE WHERE channel_id=$1", interaction.channel.id)
    await interaction.channel.send(
        f"✅ Proof manually accepted by {interaction.user.mention}.")
    ok, msg = await forward_ticket_flow(interaction.channel, interaction.user)
    await interaction.followup.send(msg, ephemeral=True)


# ============================================================
# FORTNITE FAKE TICKET GUIDE
# ============================================================
def make_fortnite_faketicket_embed() -> discord.Embed:
    e = discord.Embed(
        title="🎟️  Fortnite — How to Make a Recovery Ticket",
        description=(
            "**⚠️ There can only be 1 recovery ticket per account — be quick!**\n"
            "Submit before the previous owner gets the chance.\n\n"
            f"🔗 **Recovery Form:** [epicgames.com/id/login/recovery/help](https://www.epicgames.com/id/login/recovery/help)\n\n"
            "**Before you start:**\n"
            "🌐 Open a **VPN**\n"
            "📧 Get a temp email at **[tempmail.ninja](https://tempmail.ninja/)**\n"
            f"{DIVIDER}"
        ),
        color=0x00D4FF,
    )
    steps = [
        ("1️⃣  New Email Address",
         "Enter a **new email address** that has **no existing Epic ID** linked — "
         "this becomes the new address for the account."),
        ("2️⃣  Account ID",
         "Get the **Account ID** from account settings.\n"
         "*(PDF access is **not** required for this step.)*"),
        ("3️⃣  Current Email on the Account",
         "Enter the **current email address** linked to the account *(the one you already have access to)*."),
        ("4️⃣  Display Name",
         "Enter the **display name** exactly as shown in account settings."),
        ("5️⃣  Personal Details",
         "• **Name:** Guess using **first and last letters** — *accuracy not required, just be fast*\n"
         "• **Country:** Shown in account settings\n"
         "• **City:** Any guess works *(doesn't need to be accurate)*"),
        ("6️⃣  Connected Accounts",
         "Select that you **haven't connected any accounts**. *(Skip this question)*"),
        ("7️⃣  Payment Methods",
         "Select that you **haven't used a card**. *(Skip this question)*"),
        ("8️⃣  Support Message",
         "Use the following message:\n"
         "> *\"Hello, I recently moved to [your country] and saw that I no longer have access to my email. "
         "I am making this request so I can regain access.\"*"),
    ]
    for name, value in steps:
        e.add_field(name=name, value=value, inline=False)
    e.add_field(
        name="🔄  Ticket Declined?",
        value="**Redo the ticket immediately** — resubmit after every decline.",
        inline=False,
    )
    e.set_author(name="AF SERVICES • Account Guides")
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES | Fortnite Recovery Guide")
    return e


@bot.tree.command(name="fortnite_faketicketguide", description="How to make a recovery ticket for a Fortnite account.")
async def fortnite_faketicketguide_command(interaction: discord.Interaction):
    await interaction.response.send_message(embed=make_fortnite_faketicket_embed(), files=embed_files())


# ============================================================
# CARD PAYMENT GUIDE
# ============================================================
class PaidProofModal(discord.ui.Modal, title="Submit Payment Proof"):
    screenshot = discord.ui.TextInput(
        label="Payment Page Screenshot URL",
        style=discord.TextStyle.short,
        required=True,
        placeholder="https://i.imgur.com/... or any image URL",
        max_length=500,
    )
    email_proof = discord.ui.TextInput(
        label="Email Confirmation Screenshot URL",
        style=discord.TextStyle.short,
        required=False,
        placeholder="https://i.imgur.com/... (optional but recommended)",
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Submit this inside the server.", ephemeral=True)
            return

        log_ch = await get_log_channel(interaction.guild)

        e = discord.Embed(
            title="💳  Payment Proof Received",
            color=0x2ECC71,
        )
        e.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        e.add_field(
            name="Submitted by",
            value=f"{interaction.user.mention} (`{interaction.user.id}`)",
            inline=False,
        )
        e.add_field(name="📸  Payment Screenshot", value=self.screenshot.value, inline=False)
        if self.email_proof.value:
            e.add_field(name="📧  Email Confirmation", value=self.email_proof.value, inline=False)
        e.set_footer(text=f"AF SERVICES | {utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

        if log_ch:
            await log_ch.send(embed=e)

        await interaction.response.send_message(
            "✅ **Payment proof submitted!** A staff member will review it shortly.",
            ephemeral=True,
        )


class CardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Paid",
        style=discord.ButtonStyle.success,
        custom_id="af_card_paid",
        emoji="✅",
    )
    async def paid_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PaidProofModal())


def card_proof_payment_ref() -> str:
    return CARD_PROOF_PAYMENT_URL or f"attachment://{CARD_PROOF_PAYMENT_FILENAME}"


def card_proof_email_ref() -> str:
    return CARD_PROOF_EMAIL_URL or f"attachment://{CARD_PROOF_EMAIL_FILENAME}"


def card_proof_files() -> list[discord.File]:
    """Fresh File objects for the two card example screenshots. Single-use."""
    files: list[discord.File] = []
    if not CARD_PROOF_PAYMENT_URL and os.path.exists(CARD_PROOF_PAYMENT_PATH):
        files.append(discord.File(CARD_PROOF_PAYMENT_PATH, filename=CARD_PROOF_PAYMENT_FILENAME))
    if not CARD_PROOF_EMAIL_URL and os.path.exists(CARD_PROOF_EMAIL_PATH):
        files.append(discord.File(CARD_PROOF_EMAIL_PATH, filename=CARD_PROOF_EMAIL_FILENAME))
    return files


def make_card_guide_embed() -> discord.Embed:
    e = discord.Embed(
        title="💳  How to Purchase Using Card",
        description=f"Follow the steps below to complete your purchase.\n{DIVIDER}",
        color=0x2ECC71,
    )
    steps = [
        ("1️⃣  Visit the Store",
         "Head over to **[accsforge.fun](https://www.accsforge.fun/)**."),
        ("2️⃣  Select the 1€ Option",
         "Choose the **1 EURO** option — it is the first product listed."),
        ("3️⃣  Set the Quantity",
         "Set the quantity to the **price of your product**, then add **20% on top** to cover taxes.\n"
         "*(Example: product costs €10 → set quantity to **12**)*"),
        ("4️⃣  Add to Cart & Checkout",
         "Add the product to your cart, click the **cart icon**, and proceed through to payment."),
        ("5️⃣  Save Your Payment Proof",
         "📸 Take a screenshot of:\n"
         "• The **payment confirmation page**\n"
         "• The **email confirmation** received after payment\n\n"
         "Upload both to an image host *(e.g. Imgur)* and click **✅ Paid** below to submit."),
    ]
    for name, value in steps:
        e.add_field(name=name, value=value, inline=False)
    e.set_author(name="AF SERVICES • Payment Guide")
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES | Card Payment Guide")
    return e


def make_card_embeds() -> list[discord.Embed]:
    """The card guide plus the two example screenshots buyers should reproduce."""
    main = make_card_guide_embed()

    ex_pay = discord.Embed(
        title="📸  Example — Payment Confirmation Page",
        description="Your payment-page screenshot should look like this:",
        color=0x2ECC71,
    )
    ex_pay.set_image(url=card_proof_payment_ref())

    ex_email = discord.Embed(
        title="📧  Example — Email Confirmation",
        description="And your confirmation email should look like this:",
        color=0x2ECC71,
    )
    ex_email.set_image(url=card_proof_email_ref())

    return [main, ex_pay, ex_email]


@bot.tree.command(name="card", description="How to purchase using a card payment method.")
async def card_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        embeds=make_card_embeds(),
        view=CardView(),
        files=embed_files() + card_proof_files(),
    )


# ============================================================
# STAFF APPROVAL / DELIVERY VIEW
# Posted in a claim ticket once payment is (or could not be) verified. Staff
# enters the LZT item id to release. custom_ids are static and the action reads
# the ticket channel directly, so the buttons survive bot restarts.
# ============================================================
APPROVE_DELIVER_CID = "af_approve_deliver"
REJECT_DELIVER_CID = "af_reject_deliver"


class ApproveDeliverModal(discord.ui.Modal, title="Approve & Deliver"):
    item_id = discord.ui.TextInput(
        label="Account item ID to deliver",
        style=discord.TextStyle.short,
        required=True,
        max_length=30,
        placeholder="e.g. 12345678  (see /stock)",
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("Use this in a ticket channel.", ephemeral=True)
            return

        row = await db_fetchrow(
            "SELECT owner_id, verified_order_id, verified_product FROM tickets WHERE channel_id=$1",
            ch.id,
        )
        if not row:
            await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
            return

        owner = ch.guild.get_member(int(row["owner_id"]))
        if owner is None:
            owner = await bot.fetch_user(int(row["owner_id"]))

        await interaction.response.send_message("📦 Delivering account...", ephemeral=True)
        await deliver_account(
            channel=ch,
            owner=owner,
            lzt_item_id=str(self.item_id.value),
            product=row["verified_product"],
            order_id=row["verified_order_id"],
            delivered_by=interaction.user.id,
        )


class DeliveryApprovalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Approve & Deliver", style=discord.ButtonStyle.success,
                       emoji="✅", custom_id=APPROVE_DELIVER_CID)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        await interaction.response.send_modal(ApproveDeliverModal())

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger,
                       emoji="🚫", custom_id=REJECT_DELIVER_CID)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        await interaction.response.send_message("Marked as rejected — no account released.", ephemeral=True)
        if isinstance(interaction.channel, discord.TextChannel):
            await interaction.channel.send(f"🚫 Delivery rejected by {interaction.user.mention}.")


async def post_delivery_approval(channel: discord.TextChannel, owner: discord.abc.User,
                                 product: str | None, order_id: str | None,
                                 verification: dict | None) -> None:
    """Drop the staff Approve/Reject panel into a claim ticket."""
    staff_role = get_staff_role(channel.guild)
    if verification and verification.get("paid"):
        status_line = f"✅ **Payment verified** via SellAuth (status: `{verification.get('status')}`)."
    elif verification and verification.get("ok") and not verification.get("found"):
        status_line = f"⚠️ Order `{order_id}` **not found** on SellAuth — verify manually."
    elif verification and verification.get("error"):
        status_line = f"⚠️ Could not auto-verify (`{verification.get('error')}`) — verify manually."
    else:
        status_line = "ℹ️ Manual verification (SellAuth not configured)."

    e = discord.Embed(
        title="🛒  Ready to Deliver — Staff Approval",
        description=(
            f"{status_line}\n\n"
            f"**Buyer:** {owner.mention}\n"
            f"**Product:** {product or 'unspecified'}\n"
            f"**Order ID:** {f'`{order_id}`' if order_id else 'not provided'}\n\n"
            "Click **Approve & Deliver** and enter the account item ID to release the account."
        ),
        color=AF_BLUE,
    )
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES • Staff action required")
    content = staff_role.mention if staff_role else None
    await channel.send(content=content, embed=e, view=DeliveryApprovalView())

    # Auto restock alert: if this ticket reserved a market account and payment is verified,
    # tell us to re-buy that exact listing ASAP (fires once per ticket).
    if verification and verification.get("paid"):
        await maybe_fire_restock(channel)


async def maybe_fire_restock(channel: discord.TextChannel) -> None:
    """Fire the restock alert once if the ticket has a reserved, not-yet-alerted market item."""
    row = await db_fetchrow(
        "SELECT reserved_market_item_id, restock_alerted FROM tickets WHERE channel_id=$1",
        channel.id,
    )
    if not row or not row["reserved_market_item_id"] or row["restock_alerted"]:
        return
    det = await lzt_item_detail(row["reserved_market_item_id"])
    if not det["ok"]:
        # Staff-facing problem → transcript channel, never the customer's ticket.
        target = await get_transcript_channel(channel.guild)
        if target:
            await target.send(
                f"⚠️ {channel.mention}: payment verified, but I couldn't load reserved "
                f"listing `{int(row['reserved_market_item_id'])}` for the restock alert: "
                f"`{det['error']}`")
        return
    await notify_restock(None, channel.guild, det["item"] or {}, channel)
    await db_execute("UPDATE tickets SET restock_alerted=TRUE WHERE channel_id=$1", channel.id)


# ============================================================
# AI INTAKE (Claude) — claim tickets
# ============================================================
AI_SYSTEM_PROMPT = (
    "You are the intake assistant for AF SERVICES, a Discord shop that resells gaming "
    "accounts. You are talking to a buyer inside a 'Claim Order' ticket — someone claiming "
    "an order they already paid for. Your goals, in order:\n"
    "1. Find out WHICH product/account they purchased (a short product name or key).\n"
    "2. Get their SellAuth ORDER ID / invoice ID. This is the only proof we trust — "
    "screenshots are easy to fake, so politely insist on the order ID.\n"
    "Be warm, concise, and human. Never reveal or promise specific account credentials "
    "yourself — once payment is confirmed, a staff member releases the account.\n\n"
    "Respond with ONLY a JSON object, no prose around it:\n"
    '{"reply": "<your message to the buyer>", "product": <string|null>, '
    '"order_id": <string|null>, "ready": <true only when you have BOTH a product and an order_id>}'
)


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


async def run_ai_intake(channel: discord.TextChannel, owner: discord.abc.User) -> None:
    if not (AI_ENABLED and anthropic_client):
        return

    # Build the conversation so far (buyer + bot), mapped to Anthropic roles.
    history: list[dict] = []
    async for m in channel.history(limit=25, oldest_first=True):
        if not m.content:
            continue
        role = "assistant" if (bot.user and m.author.id == bot.user.id) else "user"
        history.append({"role": role, "content": m.content[:1500]})
    while history and history[0]["role"] != "user":
        history.pop(0)
    if not history:
        return
    # Anthropic requires alternating roles — merge consecutive same-role turns.
    merged: list[dict] = []
    for h in history:
        if merged and merged[-1]["role"] == h["role"]:
            merged[-1]["content"] += "\n" + h["content"]
        else:
            merged.append(dict(h))

    try:
        resp = await anthropic_client.messages.create(
            model=AI_MODEL, max_tokens=600, system=AI_SYSTEM_PROMPT, messages=merged
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    except Exception as e:
        print("AI intake error:", e)
        return

    data = _extract_json(raw) or {}
    reply = data.get("reply") or "Could you tell me which product you bought and your order ID?"
    await channel.send(reply)

    product = (data.get("product") or None)
    order_id = (data.get("order_id") or None)
    if not (data.get("ready") and order_id):
        return

    # We have enough to attempt verification.
    if order_id and await order_already_delivered(order_id):
        await channel.send("⚠️ That order has already been delivered. A staff member will check.")
        return

    verification = None
    if REQUIRE_SELLAUTH and SELLAUTH_ENABLED:
        verification = await sellauth_get_invoice(order_id)
        if verification["ok"] and verification["found"] and not verification["paid"]:
            await channel.send(
                f"I found order `{order_id}` but it isn't marked paid yet "
                f"(status: `{verification.get('status')}`). Ping me once the payment completes."
            )
            return

    # Persist what we verified so the staff approval action can read it.
    await db_execute(
        "UPDATE tickets SET verified_order_id=$1, verified_product=$2, ai_handled=TRUE WHERE channel_id=$3",
        order_id, product, channel.id,
    )
    await post_delivery_approval(channel, owner, product, order_id, verification)


# ============================================================
# AI SHOPPING ASSISTANT — "Buy Account" tickets
# ============================================================
def make_buy_intro_embed() -> discord.Embed:
    e = discord.Embed(
        title="🛍️  What are you looking for?",
        description=(
            "Tell me what you'd like to buy and your **budget**, and I'll pull up matching "
            "accounts right away.\n\n"
            "We stock **Valorant, Fortnite, Steam, Rockstar/GTA, EA, Ubisoft, Battle.net, "
            "Minecraft, Roblox, LoL, Genshin, ChatGPT** and more.\n\n"
            "**For example:**\n"
            "> *\"A Valorant account with some skins, around €25\"*\n"
            "> *\"Steam account with a few games, budget €30\"*\n"
            "> *\"GTA V / Rockstar account, around €20\"*\n\n"
            f"{DIVIDER}\nJust type your request below 👇"
        ),
        color=AF_BLUE,
    )
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES • Account Shop")
    return e


# ---- AI checkout helpers ----------------------------------------------------
async def reserve_market_item(channel: discord.TextChannel, item: dict) -> tuple[int, float]:
    """Reserve a market listing to this ticket. Returns (item_id, resale_price)."""
    iid = int(_account_item_id(item) or 0)
    _, resale = _resale_price(item)
    await db_execute(
        "UPDATE tickets SET reserved_market_item_id=$1, restock_alerted=FALSE, "
        "order_notice_sent=FALSE WHERE channel_id=$2",
        iid, channel.id)
    return iid, resale


async def post_crypto_checkout(channel: discord.TextChannel, price: float) -> None:
    """Post the crypto (NowPayments) checkout for `price` EUR into the ticket."""
    await db_execute(
        "UPDATE tickets SET crypto_amount=$1, crypto_payment_id=NULL, crypto_paid=FALSE, "
        "checkout_method='crypto', checkout_total=$1, order_notice_sent=FALSE WHERE channel_id=$2",
        price, channel.id)
    e = discord.Embed(
        title="💳  Pay with Crypto",
        description=(f"**Total: €{price:.2f}**\n\nChoose a coin below to get your payment address.\n"
                     f"We accept **LTC, SOL, BTC, ETH**.\n{DIVIDER}"),
        color=AF_BLUE,
    )
    e.set_thumbnail(url=logo_ref())
    e.set_footer(text="AF SERVICES • Crypto Checkout")
    await channel.send(embed=e, view=CryptoPayView(), files=embed_files())


async def post_card_checkout(channel: discord.TextChannel, account_price: float) -> None:
    """Post the card guide + the exact amount to pay (account price × card tax),
    re-link the Terms of Service, and arm proof verification."""
    total = round(account_price * CARD_TAX_MULTIPLIER, 2)
    pct = int(round((CARD_TAX_MULTIPLIER - 1) * 100))
    await db_execute(
        "UPDATE tickets SET awaiting_proof=TRUE, checkout_method='card', "
        "checkout_total=$1, proof_attempts=0 WHERE channel_id=$2",
        total, channel.id)
    await channel.send(embeds=make_card_embeds(), view=CardView(),
                       files=embed_files() + card_proof_files())
    tos_line = (f"\n\n📜 By paying you agree to our **Terms of Service** — please read them: "
                f"<#{TOS_CHANNEL_ID}>") if TOS_CHANNEL_ID else ""
    e = discord.Embed(
        title="💳  Card Payment — Amount to Pay",
        description=(
            f"Account price: **€{account_price:.2f}**\n"
            f"Card fee (**+{pct}%**) included\n"
            f"**Total to pay by card: €{total:.2f}**\n\n"
            f"Pay the **exact** total above, then **upload a screenshot of your payment "
            f"confirmation AND your confirmation email** right here. I'll check that it says "
            f"*payment confirmed*, the amount matches, and the order IDs line up — then pass "
            f"your order to the owner.{tos_line}"),
        color=AF_BLUE,
    )
    e.set_footer(text="AF SERVICES • Card Checkout")
    await channel.send(embed=e)


MARKET_SCAN_DEEP = 40  # how many listings to inspect when filtering a specific request


async def scan_and_rank(game: str, items: list[dict], wanted: list[str],
                        region: str | None, count: int,
                        max_scan: int = MARKET_SCAN_DEEP) -> tuple[list[dict], bool]:
    """Scan listings (fetching detail), scoring by how many requested items each has.
    Returns (chosen, full_match): FULL matches (all requested items) first, cheapest;
    if none has everything, the best PARTIAL matches (most items, then cheapest)."""
    scored: list[tuple[int, float, dict]] = []
    scanned = 0
    need = len(wanted)
    for it in items:
        if scanned >= max_scan:
            break
        det = await lzt_item_detail(it.get("item_id") or it.get("id"))
        scanned += 1
        item = det["item"] if det["ok"] else it
        if region and not _region_matches(item, region):
            continue
        mc = 0
        if wanted:
            owned = [n.lower() for n in await _searchable_names(game, item)]
            mc = sum(1 for w in wanted if _cosmetic_matches(owned, w))
            if mc == 0:
                continue
        _, price = _resale_price(item)
        scored.append((mc, price, item))
        # Stop early once we have enough FULL matches (or enough items with no filter).
        if wanted and sum(1 for s in scored if s[0] == need) >= count:
            break
        if not wanted and len(scored) >= count:
            break
    if not scored:
        return [], False
    if wanted:
        full = [s for s in scored if s[0] == need]
        pool = full if full else scored
        pool.sort(key=lambda s: (-s[0], s[1]))  # most items matched, then cheapest
        return [s[2] for s in pool[:count]], bool(full)
    return [s[2] for s in scored[:count]], True


async def present_accounts(channel: discord.TextChannel, game: str, budget: float | None,
                           wanted: list[str], count: int = 3, region: str | None = None) -> int:
    """Search, filter, and post matching accounts; remember them for selection.
    A specific request (named item/region) is shown cheapest-first. Returns count shown."""
    region = (region or "").strip().lower() or None
    specific = bool(wanted or region)
    pool = MARKET_SCAN_DEEP if specific else count
    res = await lzt_search_market(game, budget=budget, count=count, pool=pool, cheapest=specific)
    if not res["ok"]:
        await channel.send(f"⚠️ I couldn't reach the stock right now (`{res['error']}`). "
                           f"A staff member will help you shortly.")
        return 0
    chosen, full_match = await scan_and_rank(game, res["items"], wanted, region, count)
    if not chosen:
        bits = []
        if wanted:
            bits.append("matching " + ", ".join(wanted))
        if region:
            bits.append(f"in {region.upper()}")
        extra = (" " + " ".join(bits)) if bits else ""
        await channel.send(
            f"I couldn't find a {game_label(game)} account{extra} within that budget right now. "
            f"Try a higher budget, or a staff member can source one for you.")
        return 0
    partial = bool(wanted) and not full_match

    embeds, files, offers = [], [], []
    for i, item in enumerate(chosen):
        embed, file = await build_account_message(game, item, i)
        embed.title = f"#{i + 1} • {embed.title}"  # number for easy selection
        embeds.append(embed)
        if file:
            files.append(file)
        iid = int(_account_item_id(item) or 0)
        _, price = _resale_price(item)
        offers.append({"item_id": iid, "price": price,
                       "title": item.get("title") or "Account", "game": game})
    shop_offers[channel.id] = offers

    view = skins_view_for(chosen, game) if game in ("valorant", "fortnite") else None
    if partial:
        lead = ("I couldn't find one account with **everything** you asked for within budget, so "
                "here are the **closest matches** (cheapest first) 👇")
    elif specific:
        lead = "Here are the cheapest matches 👇"
    else:
        lead = "Here are the best matches 👇"
    kwargs = {
        "content": f"{lead} Reply with the **number** of the one you want, "
                   "or ask me to adjust the budget.",
        "embeds": embeds[:10], "files": files,
    }
    if view is not None:
        kwargs["view"] = view
    await channel.send(**kwargs)
    return len(offers)


AI_SHOP_GAME_KEYS = sorted(set(LZT_MARKET_SLUGS.keys()))


def _build_ai_shop_prompt() -> str:
    games = ", ".join(f"{k} ({game_label(k)})" for k in AI_SHOP_GAME_KEYS)
    return (
        "You are the shopping assistant for AF SERVICES, a Discord shop that sells gaming and "
        "digital accounts sourced from a large marketplace. You talk to a customer in their "
        "ticket and drive the whole flow: understand what they want, show accounts, take their "
        "pick, and start checkout.\n\n"
        "SUPPORTED GAMES/PLATFORMS (use the KEY on the left as the \"game\" value):\n"
        f"{games}\n"
        "Map the customer's words to the closest key (e.g. 'GTA' or 'rockstar' → rockstar, "
        "'LoL' → lol, 'EA'/'FIFA'/'Origin' → ea, 'CoD'/'Overwatch' → battlenet). If they ask "
        "for something NOT in this list, politely say we don't currently stock it and list a few "
        "we do have.\n\n"
        "IMPORTANT RULES:\n"
        "- NEVER invent account details, skins, or prices. The system shows real accounts.\n"
        "- NEVER reveal or promise account logins/credentials — the OWNER always delivers those "
        "after payment. You only take the order up to payment/proof.\n"
        "- Card payments include a surcharge; the system computes the exact total — don't quote a "
        "card total yourself, just say card has a small fee and let the system post it.\n"
        "- 'skins' is for the specific things they want: Valorant/Fortnite cosmetics, OR Steam "
        "game titles (e.g. 'GTA V', 'CS2'). Put each requested item in the skins array.\n"
        "- When the customer names a cosmetic by a community NICKNAME, translate it to the "
        "OFFICIAL in-game name(s) in the skins array. Examples: 'FNCS pickaxe'/'FNCS axe' → "
        "'Axe of Champions' (matches every FNCS axe: I, II, III…); 'minty'/'mint axe' → "
        "'Merry Mint Axe'; 'renegade' → 'Renegade Raider'; 'AAT' → 'Aerial Assault Trooper'; "
        "'gaia' → 'Gaia's Vengeance'. Always put the official name in skins, not the nickname.\n"
        "- If a customer names a skin/pickaxe you do NOT clearly recognise or aren't confident of "
        "the official name for, DON'T guess and DON'T search — instead ask them for the EXACT "
        "in-game name (action \"none\"). Only search once you know the real cosmetic name.\n"
        "- If they mention a REGION (EU, NA, AP, KR, BR, LATAM, TR), put it in the region field.\n"
        "- When they ask for a SPECIFIC item/region, the system shows the CHEAPEST match for their "
        "budget — so never show an expensive account when a cheaper one fits.\n"
        "- Be warm, concise, human.\n\n"
        "Each turn, respond with ONLY a JSON object (no prose around it):\n"
        "{\n"
        '  "reply": "<message to the customer>",\n'
        '  "action": "none" | "search" | "select" | "checkout",\n'
        '  "game": "<one of the KEYS above>" | null,\n'
        '  "budget": <number|null>,          // EUR, for search\n'
        '  "skins": [<string>, ...] | null,  // specific cosmetics (val/fn) or Steam games\n'
        '  "region": "<EU|NA|AP|KR|BR|LATAM|TR>" | null,\n'
        '  "index": <number|null>,           // which shown account they picked (1-based)\n'
        '  "method": "crypto" | "card" | null // for checkout\n'
        "}\n\n"
        "Flow:\n"
        "1. Missing game or budget → action \"none\", ask for it.\n"
        "2. Have game + budget → action \"search\" (reply: tell them you're pulling options).\n"
        "3. They pick one (e.g. 'the 2nd', '#1') → action \"select\" with index (reply: confirm the "
        "pick, ask whether they want to pay by crypto or card).\n"
        "4. They choose a method → action \"checkout\" with method (reply: tell them the checkout is below).\n"
        "CRITICAL: Always use the customer's MOST RECENT budget and game. If they change the budget "
        "or game at ANY point — even after you've already shown options or they picked one (e.g. they "
        "first said €1.3 then say €20, or switch from Valorant to Steam) — treat it as a brand-new "
        "search: set action \"search\" with the UPDATED budget/game and show fresh matches. Never keep "
        "using an old budget.\n"
        "Only set action to search/select/checkout when that step is actually reached; otherwise \"none\"."
    )


AI_SHOP_PROMPT = _build_ai_shop_prompt()


async def run_ai_shopping(channel: discord.TextChannel, owner: discord.abc.User) -> None:
    if not (AI_ENABLED and anthropic_client):
        return

    history: list[dict] = []
    async for m in channel.history(limit=25, oldest_first=True):
        if not m.content:
            continue
        role = "assistant" if (bot.user and m.author.id == bot.user.id) else "user"
        history.append({"role": role, "content": m.content[:1500]})
    while history and history[0]["role"] != "user":
        history.pop(0)
    if not history:
        return
    merged: list[dict] = []
    for h in history:
        if merged and merged[-1]["role"] == h["role"]:
            merged[-1]["content"] += "\n" + h["content"]
        else:
            merged.append(dict(h))

    try:
        resp = await anthropic_client.messages.create(
            model=AI_MODEL, max_tokens=500, system=AI_SHOP_PROMPT, messages=merged
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    except Exception as e:
        print("AI shopping error:", e)
        return

    data = _extract_json(raw) or {}
    reply = data.get("reply")
    if reply:
        await channel.send(str(reply)[:1900])

    action = str(data.get("action") or "none").lower()

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    if action == "search":
        game = str(data.get("game") or "").lower()
        budget = _num(data.get("budget"))
        wanted = [str(s).strip() for s in (data.get("skins") or []) if str(s).strip()]
        region = str(data.get("region") or "").strip() or None
        if game in LZT_MARKET_SLUGS and budget and budget > 0:
            await present_accounts(channel, game, budget, wanted, region=region)

    elif action == "select":
        offers = shop_offers.get(channel.id) or []
        idx = data.get("index")
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = None
        if offers and idx and 1 <= idx <= len(offers):
            pick = offers[idx - 1]
            det = await lzt_item_detail(pick["item_id"])
            item = det["item"] if det["ok"] else {"price": pick["price"],
                                                  "item_id": pick["item_id"]}
            await reserve_market_item(channel, item)
        else:
            await channel.send("Which account would you like? Reply with its **number** "
                               "(e.g. `1`).")

    elif action == "checkout":
        method = str(data.get("method") or "").lower()
        row = await db_fetchrow(
            "SELECT reserved_market_item_id FROM tickets WHERE channel_id=$1", channel.id)
        if not row or not row["reserved_market_item_id"]:
            await channel.send("First let me know which account you'd like, then we'll set up payment.")
            return
        det = await lzt_item_detail(row["reserved_market_item_id"])
        if not det["ok"]:
            await channel.send("Hmm, I couldn't load that account just now — a staff member will help.")
            return
        _, price = _resale_price(det["item"] or {})
        if method == "crypto":
            await post_crypto_checkout(channel, price)
        elif method == "card":
            await post_card_checkout(channel, price)
        else:
            await channel.send("Would you like to pay by **crypto** or **card**?")


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")


def _message_has_video(message: discord.Message) -> bool:
    return any(
        (a.content_type or "").startswith("video")
        or a.filename.lower().endswith(_VIDEO_EXTS)
        for a in message.attachments)


async def _download_proof_images(message: discord.Message, limit: int = 5) -> list[tuple[bytes, str]]:
    """Return [(bytes, media_type)] for image attachments (for AI vision)."""
    out: list[tuple[bytes, str]] = []
    for a in message.attachments:
        ct = (a.content_type or "").lower()
        is_img = ct.startswith("image/") or a.filename.lower().endswith(_IMAGE_EXTS)
        if not is_img:
            continue
        data = await _fetch_bytes(a.url)
        if not data:
            continue
        media = ct if ct.startswith("image/") else "image/png"
        if media == "image/jpg":
            media = "image/jpeg"
        out.append((data, media))
        if len(out) >= limit:
            break
    return out


async def _ai_vision_json(system: str, text: str, images: list[tuple[bytes, str]]) -> dict:
    """One vision call over the given images; returns the parsed JSON (or {})."""
    if not (AI_ENABLED and anthropic_client):
        return {}
    content: list[dict] = []
    for data, media in images:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": media,
            "data": base64.b64encode(data).decode("ascii")}})
    content.append({"type": "text", "text": text})
    try:
        resp = await anthropic_client.messages.create(
            model=AI_MODEL, max_tokens=500, system=system,
            messages=[{"role": "user", "content": content}])
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    except Exception as e:
        print("AI vision error:", e)
        return {}
    return _extract_json(raw) or {}


async def verify_card_proof(images: list[tuple[bytes, str]], total: float) -> dict:
    """AI-check card payment proof. Returns the raw verdict fields."""
    system = ("You verify card-payment proof for a Discord gaming-account shop. Be strict but "
              "fair. Only report what is actually visible in the images — never assume.")
    text = (
        f"The customer must pay EXACTLY €{total:.2f} by card. They should provide (a) a payment "
        f"CONFIRMATION screenshot clearly showing success (e.g. 'Payment confirmed', 'Payment "
        f"successful', 'completed'), and (b) a confirmation EMAIL/receipt showing the amount. "
        f"From the image(s) provided, determine:\n"
        f"- has_confirmation: is a clear payment-SUCCESS screen shown?\n"
        f"- has_email: is a confirmation email/receipt shown?\n"
        f"- amount_paid: the exact amount actually paid (number only), or null if not visible\n"
        f"- ids_match: does an order/transaction/purchase ID appear on BOTH and match? "
        f"(true/false, or null if only one is present)\n"
        f"- unreadable: true if the image(s) are too blurry/cropped to read\n"
        f"- reason: ONE short sentence telling the customer what (if anything) to fix.\n"
        f'Respond with ONLY JSON: {{"has_confirmation": bool, "has_email": bool, '
        f'"amount_paid": number|null, "ids_match": bool|null, "unreadable": bool, "reason": "..."}}')
    return await _ai_vision_json(system, text, images)


async def verify_purchase_proof(images: list[tuple[bytes, str]]) -> dict:
    """AI-check that an image plausibly proves a past purchase (for replacements)."""
    system = ("You verify proof-of-purchase for a Discord gaming-account shop's replacement "
              "request. Only report what is actually visible.")
    text = ("Does the image clearly show a plausible proof of purchase — an order confirmation, "
            "payment receipt, invoice, or a visible order/transaction ID? "
            'Respond with ONLY JSON: {"is_proof": bool, "unreadable": bool, "reason": "<one short sentence>"}')
    return await _ai_vision_json(system, text, images)


async def _forward_with_note(channel: discord.TextChannel, note: str) -> None:
    await channel.send(note)
    await db_execute("UPDATE tickets SET awaiting_proof=FALSE WHERE channel_id=$1", channel.id)
    await forward_ticket_flow(channel, None)


AI_REPLACEMENT_PROMPT = (
    "You are the replacement/support assistant for AF SERVICES, a gaming-account shop. The "
    "customer is in a SUPPORT ticket, usually requesting a replacement. Enforce this policy "
    "strictly but kindly:\n"
    + REPLACEMENT_RULES.replace("**", "") + "\n\n"
    "Decide each turn:\n"
    "- If they want a REFUND, or their reason is clearly NOT 'the account is invalid or banned' "
    "(e.g. changed their mind, don't like it, want money back, buyer's remorse): politely explain "
    "we only replace invalid/banned accounts and don't refund, then set action \"close\".\n"
    "- If the account is invalid/banned (a valid reason): ask them to upload their proof of "
    "purchase AND the REQUIRED recording/video (for Full-Access accounts), then set action "
    "\"await_proof\".\n"
    "- If you still need more detail, ask for it with action \"none\".\n"
    "- NEVER promise a replacement yourself — the owner decides after reviewing the video.\n\n"
    "Respond with ONLY JSON: "
    '{"reply": "<message>", "action": "none"|"close"|"await_proof", "reason": "<short>"}'
)


async def run_ai_replacement(channel: discord.TextChannel, owner: discord.abc.User) -> None:
    """Conversational assistant for support/replacement tickets: enforces policy,
    guides the customer to upload proof, and closes clearly-ineligible requests."""
    if not (AI_ENABLED and anthropic_client):
        return
    history: list[dict] = []
    async for m in channel.history(limit=25, oldest_first=True):
        if not m.content:
            continue
        role = "assistant" if (bot.user and m.author.id == bot.user.id) else "user"
        history.append({"role": role, "content": m.content[:1500]})
    while history and history[0]["role"] != "user":
        history.pop(0)
    if not history:
        return
    merged: list[dict] = []
    for h in history:
        if merged and merged[-1]["role"] == h["role"]:
            merged[-1]["content"] += "\n" + h["content"]
        else:
            merged.append(dict(h))
    try:
        resp = await anthropic_client.messages.create(
            model=AI_MODEL, max_tokens=400, system=AI_REPLACEMENT_PROMPT, messages=merged)
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    except Exception as e:
        print("AI replacement error:", e)
        return
    data = _extract_json(raw) or {}
    reply = data.get("reply")
    if reply:
        await channel.send(str(reply)[:1900])
    if str(data.get("action") or "none").lower() == "close":
        await db_execute("UPDATE tickets SET awaiting_proof=FALSE WHERE channel_id=$1", channel.id)
        await close_ticket_flow(
            channel, closed_by="Auto (ineligible per policy)",
            reason=str(data.get("reason") or "Not eligible for replacement per policy"))


async def handle_proof_upload(message: discord.Message, row) -> None:
    """Verify uploaded proof before forwarding. Card: AI reads the payment screenshot +
    email (payment confirmed, exact amount, matching order IDs, not underpaid). Replacement:
    AI verifies the purchase image, then requires a video, then forwards (owner judges the video).
    Never forwards on unclear/insufficient proof."""
    ch = message.channel
    if not isinstance(ch, discord.TextChannel):
        return
    kind = row["kind"]
    has_video = _message_has_video(message)
    images = await _download_proof_images(message)

    async def _bump_attempts() -> int:
        n = int(row["proof_attempts"] or 0) + 1
        await db_execute("UPDATE tickets SET proof_attempts=$1 WHERE channel_id=$2", n, ch.id)
        return n

    # If the AI is unavailable, fall back to the old acknowledge-and-forward behavior.
    if not (AI_ENABLED and anthropic_client):
        await _forward_with_note(
            ch, "📎 Proof received. Please **wait for the owner** to review and finalize. 🙏")
        return

    # ---------------- CARD PAYMENT PROOF (custom/buy) ----------------
    if kind != "support":
        total = float(row["checkout_total"]) if row["checkout_total"] is not None else None
        if not images:
            await ch.send("Please upload a **screenshot of your payment confirmation** and your "
                          "**confirmation email** so I can verify the payment. 📸")
            return
        v = await verify_card_proof(images, total or 0.0)
        if not v:
            await ch.send("I couldn't read that clearly — please re-upload a **clear, full** "
                          "screenshot of your payment confirmation and email. 📸")
            return
        if v.get("unreadable"):
            n = await _bump_attempts()
            if n >= 2:
                await ch.send("I still can't read the proof clearly. A staff member will take a "
                              "look shortly — thank you for your patience. 🙏")
                return
            await ch.send("That image is too blurry/cropped to read — please upload a **clearer, "
                          "full-screen** screenshot. 📸")
            return
        if not v.get("has_confirmation"):
            await ch.send("I don't see a **payment confirmation** yet. Please upload a screenshot "
                          "that clearly shows your payment was **confirmed/successful**. ✅")
            return
        if not v.get("has_email"):
            await ch.send("Thanks! I also need your **confirmation email** (showing the amount) "
                          "to verify the order — please upload it too. 📧")
            return
        amount = v.get("amount_paid")
        try:
            amount = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount = None
        if total and amount is not None and amount + 0.009 < total:
            await ch.send(f"It looks like you paid **€{amount:.2f}**, but the total is "
                          f"**€{total:.2f}**. Please **top up the remaining €{total - amount:.2f}** "
                          f"and re-upload your proof. 🙏")
            return
        if v.get("ids_match") is False:
            await ch.send("The **order IDs** on your payment screenshot and email don't match — "
                          "please double-check and upload matching proof for this order. 🔎")
            return
        # All checks passed → forward to owner.
        await _forward_with_note(
            ch, "✅ Payment verified — amount and order details check out. Forwarding to the "
                "owner to deliver your account now. 🙏")
        return

    # ---------------- REPLACEMENT PROOF (support): payment → video → forward ----------------
    stage = row["proof_stage"] or "payment"
    if stage == "payment":
        if not images:
            await ch.send("First, please upload your **proof of purchase** (order confirmation / "
                          "payment receipt / order ID) as an image. 📸")
            return
        v = await verify_purchase_proof(images)
        if not v or v.get("unreadable"):
            n = await _bump_attempts()
            if n >= 2:
                await ch.send("I can't read that clearly — a staff member will review your "
                              "request shortly. 🙏")
                return
            await ch.send("That image is hard to read — please upload a **clearer** proof of "
                          "purchase. 📸")
            return
        if not v.get("is_proof"):
            await ch.send("That doesn't look like a proof of purchase. Please upload your "
                          "**order confirmation / receipt** for this account. 🧾")
            return
        await db_execute(
            "UPDATE tickets SET proof_stage='video', proof_attempts=0 WHERE channel_id=$1", ch.id)
        await ch.send("✅ Purchase verified. Now please upload a **video** clearly showing the "
                      "issue with the account, so the owner can review it. 🎥")
        return

    # stage == 'video'
    if not has_video:
        await ch.send("Almost there — please upload a **video** (not an image) that clearly shows "
                      "the issue, so the owner can review it. 🎥")
        return
    await _forward_with_note(
        ch, "✅ Video received. I've verified your purchase and forwarded your replacement request "
            "to the owner to review the video. Please wait for the owner. 🙏")


# ============================================================
# REFRESH CONTROL MESSAGE
# ============================================================
async def refresh_ticket_control_message(channel: discord.TextChannel):
    row = await db_fetchrow(
        """
        SELECT owner_id, kind, status, claimed_by,
               first_staff_response_seconds, control_message_id, last_footer_text
        FROM tickets WHERE channel_id=$1
        """,
        channel.id
    )
    if not row:
        return

    owner = channel.guild.get_member(int(row["owner_id"]))
    owner_mention = owner.mention if owner else f"<@{int(row['owner_id'])}>"

    claimed_by_mention = None
    if row["claimed_by"]:
        claimer = channel.guild.get_member(int(row["claimed_by"]))
        claimed_by_mention = claimer.mention if claimer else f"<@{int(row['claimed_by'])}>"

    footer_text = row["last_footer_text"] or "AF SERVICES"
    embed = ticket_embed(
        kind=str(row["kind"]),
        owner_mention=owner_mention,
        claimed_by_mention=claimed_by_mention,
        first_staff_seconds=row["first_staff_response_seconds"],
        footer_text=footer_text,
    )

    mid = row["control_message_id"]
    if not mid:
        return

    try:
        msg = await channel.fetch_message(int(mid))
        if str(row["status"]) == "open":
            await msg.edit(embed=embed, view=TicketControlView(channel.id))
            bot.add_view(TicketControlView(channel.id), message_id=msg.id)
        else:
            await msg.edit(embed=embed, view=None)
    except Exception:
        pass


# ============================================================
# CLOSE FLOW
# ============================================================
# A leading +rep / -rep tag (tolerant of a space: "+ rep", "-rep …").
_PLUS_REP_RE = re.compile(r"(?:^|[^\w])\+\s?rep\b", re.IGNORECASE)
_MINUS_REP_RE = re.compile(r"(?:^|[^\w])[-–—]\s?rep\b", re.IGNORECASE)

NEG_REVIEW_SYSTEM = (
    "You are a caring customer-support agent for AF SERVICES, a gaming-account shop. A customer "
    "just left a NEGATIVE review. Read it, briefly and specifically acknowledge their complaint, "
    "sincerely apologize, and invite them to open a support ticket (or reply here) so the team "
    "can make it right — mention we'd like to offer compensation appropriate to their issue. "
    "Warm and human, 2-3 sentences, no exact figures or over-promising."
)


async def _reply_negative_review(message: discord.Message) -> None:
    """Read a -rep and reply with an empathetic compensation-outreach message."""
    text = (message.content or "").strip()
    reply = None
    if AI_ENABLED and anthropic_client and text:
        try:
            resp = await anthropic_client.messages.create(
                model=AI_MODEL, max_tokens=250, system=NEG_REVIEW_SYSTEM,
                messages=[{"role": "user", "content": text[:1500]}])
            reply = "".join(b.text for b in resp.content
                            if getattr(b, "type", "") == "text").strip()
        except Exception as e:
            print("Negative-review AI reply failed:", e)
    if not reply:
        reply = ("We're really sorry your experience wasn't perfect. Please open a support ticket "
                 "or reply here so we can make it right — we'd like to offer you compensation "
                 "depending on the issue. 🙏")
    try:
        await message.reply(reply[:1900], mention_author=True)
    except Exception as e:
        print("Negative-review reply send failed:", e)


async def handle_review_post(message: discord.Message) -> None:
    """Handle a post in the reviews/reps channel.
    -rep → read it and reply offering compensation (keep the ticket OPEN).
    otherwise (a +rep or plain review) → ❤️ react (on +rep), grant the customer
    role, and auto-close the customer's open ticket(s)."""
    guild = message.guild
    member = message.author
    if guild is None or not isinstance(member, discord.Member):
        return

    content = message.content or ""
    is_plus = bool(_PLUS_REP_RE.search(content))
    is_minus = bool(_MINUS_REP_RE.search(content))

    # Negative review → only allowed if they actually contacted support via a ticket.
    if is_minus and not is_plus:
        has_ticket = await db_fetchrow(
            "SELECT 1 FROM tickets WHERE guild_id=$1 AND owner_id=$2 LIMIT 1",
            guild.id, member.id)
        if not has_ticket:
            # Never opened a ticket → remove the -rep and point them to support first.
            try:
                await message.delete()
            except Exception as e:
                print("Could not delete -rep message:", e)
            msg = ("Please open a **support ticket** first so we can look into your problem and "
                   "see if it's solvable — we'll do our best to make it right. Once support has "
                   "tried to help, you're welcome to leave your review. 🙏")
            try:
                await member.send(embed=discord.Embed(
                    title="Let's sort this out first", description=msg, color=AF_BLUE))
            except Exception:
                # DM closed — post a brief, self-deleting note in the channel instead.
                try:
                    await message.channel.send(f"{member.mention} {msg}", delete_after=30)
                except Exception:
                    pass
            return
        await _reply_negative_review(message)
        return

    # Positive review → heart it.
    if is_plus:
        try:
            await message.add_reaction("❤️")
        except Exception as e:
            print("Review react failed:", e)

    # Grant the customer role (skip staff — they don't need it).
    if CUSTOMER_ROLE_ID and not is_staff(member):
        role = guild.get_role(CUSTOMER_ROLE_ID)
        if role and role not in member.roles:
            try:
                await member.add_roles(role, reason="Left a review")
            except Exception as e:
                print("Customer role grant failed:", e)

    # Auto-close their open tickets (thanks them + saves/DMs the transcript).
    rows = await db_fetch(
        "SELECT channel_id FROM tickets WHERE guild_id=$1 AND owner_id=$2 AND status='open'",
        guild.id, member.id)
    for r in rows:
        ch = guild.get_channel(int(r["channel_id"]))
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send("🌟 Thanks for your review! Closing this ticket now.")
                await close_ticket_flow(
                    ch, closed_by=f"Auto-close (review by {member})",
                    reason="Customer left a review ✅")
            except Exception as e:
                print("Review auto-close failed:", e)


async def close_ticket_flow(channel: discord.TextChannel, closed_by: str, reason: str):
    row = await db_fetchrow(
        "SELECT status, claimed_by, owner_id FROM tickets WHERE channel_id=$1",
        channel.id,
    )
    if not row or row["status"] != "open":
        return

    await db_execute(
        "UPDATE tickets SET status='closed', last_activity=NOW() WHERE channel_id=$1",
        channel.id
    )

    claimed_by = "Unclaimed"
    if row["claimed_by"]:
        claimer = channel.guild.get_member(int(row["claimed_by"]))
        claimed_by = f"{claimer} ({claimer.id})" if claimer else str(int(row["claimed_by"]))

    # Build the transcript once, then send it to the staff log AND DM it to the
    # person who opened the ticket so they keep their own copy.
    transcript_data: bytes | None = None
    try:
        transcript_data = await build_formatted_transcript(channel)
    except Exception as e:
        print("Transcript build failed:", e)

    transcript_sent = await send_transcript_txt(
        guild=channel.guild,
        ticket_channel=channel,
        close_reason=reason,
        closed_by=closed_by,
        claimed_by=claimed_by,
        data=transcript_data,
    )

    owner_transcript_sent = False
    if transcript_data is not None:
        owner_transcript_sent = await send_transcript_to_owner(
            guild=channel.guild,
            ticket_channel=channel,
            owner_id=int(row["owner_id"]),
            close_reason=reason,
            data=transcript_data,
        )

    owner_line = (
        "📨 Transcript sent to you via DM."
        if owner_transcript_sent
        else "ℹ️ Couldn't DM you the transcript (check your DM privacy settings)."
    )
    base = (
        f"🔒 **Ticket Closed**\n"
        f"Closed by: {closed_by}\n"
        f"Claimed by: {claimed_by}\n"
        f"Reason: {reason}\n"
        f"{'✅ Transcript saved.' if transcript_sent else '⚠️ Transcript failed.'}\n"
        f"{owner_line}\n\n"
        f"🗑️ Deleting in {DELETE_COUNTDOWN_SECONDS} seconds..."
    )
    countdown_msg = await channel.send(base)

    for i in range(DELETE_COUNTDOWN_SECONDS - 1, 0, -1):
        await asyncio.sleep(1)
        try:
            await countdown_msg.edit(content=base.replace(
                f"Deleting in {DELETE_COUNTDOWN_SECONDS} seconds...",
                f"Deleting in {i} seconds..."
            ))
        except Exception:
            pass

    await asyncio.sleep(1)

    await db_execute("UPDATE tickets SET status='deleted' WHERE channel_id=$1", channel.id)
    try:
        await channel.delete(reason="Ticket closed and deleted.")
    except Exception as e:
        print("Channel delete failed:", e)


# ============================================================
# FIRST STAFF RESPONSE + ACTIVITY
# ============================================================
@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    if not isinstance(message.channel, discord.TextChannel):
        return

    # A customer posting in the reviews/reps channel → grant the customer role and
    # auto-close their open ticket(s).
    if REPS_CHANNEL_ID and message.channel.id == REPS_CHANNEL_ID:
        try:
            await handle_review_post(message)
        except Exception as e:
            print("Review handling failed:", e)
        await bot.process_commands(message)
        return

    row = await db_fetchrow(
        """SELECT created_at, first_staff_response_seconds, status, kind, owner_id,
                  ai_handled, claimed_by, forwarded_to_owner, awaiting_proof, ai_disabled,
                  checkout_total, proof_stage, proof_attempts
           FROM tickets WHERE channel_id=$1""",
        message.channel.id
    )
    if not row:
        await bot.process_commands(message)
        return

    await db_execute("UPDATE tickets SET last_activity=NOW() WHERE channel_id=$1", message.channel.id)

    is_member = isinstance(message.author, discord.Member)
    author_is_staff = is_member and is_staff(message.author)

    if row["status"] == "open" and author_is_staff:
        if row["first_staff_response_seconds"] is None:
            created_at: datetime = row["created_at"]
            seconds = int((utcnow() - created_at).total_seconds())
            await db_execute(
                "UPDATE tickets SET first_staff_response_seconds=$1 WHERE channel_id=$2",
                seconds, message.channel.id
            )
            await refresh_ticket_control_message(message.channel)

    # Proof upload → acknowledge + auto-forward to the owner. Applies to the
    # customer's own attachments in an open, not-yet-forwarded ticket:
    #   • support (replacement) → any attachment counts as proof
    #   • custom (buy) → only once a card checkout is awaiting proof
    if (
        row["status"] == "open"
        and not row["forwarded_to_owner"]
        and not row["ai_disabled"]
        and is_member
        and not author_is_staff
        and message.author.id == int(row["owner_id"])
        and message.attachments
        and message.channel.id not in ai_locks
        and (row["kind"] == "support"
             or (row["kind"] == "custom" and row["awaiting_proof"]))
    ):
        ai_locks.add(message.channel.id)
        try:
            async with message.channel.typing():
                await handle_proof_upload(message, row)
        except Exception as e:
            print("Proof handling failed:", e)
        finally:
            ai_locks.discard(message.channel.id)
        await bot.process_commands(message)
        return

    # AI intake: only in open claim tickets, only for the buyer's own messages,
    # and only until staff takes over (claimed) or an account is delivered.
    if (
        AI_ENABLED
        and row["status"] == "open"
        and row["kind"] == "claim"
        and not row["ai_handled"]
        and not row["forwarded_to_owner"]
        and not row["ai_disabled"]
        and is_member
        and not author_is_staff
        and message.author.id == int(row["owner_id"])
        and message.channel.id not in ai_locks
    ):
        ai_locks.add(message.channel.id)
        try:
            async with message.channel.typing():
                await run_ai_intake(message.channel, message.author)
        except Exception as e:
            print("AI intake failed:", e)
        finally:
            ai_locks.discard(message.channel.id)

    # AI shopping: in open "custom" tickets, help the buyer browse stock until staff claims it.
    if (
        AI_ENABLED
        and row["status"] == "open"
        and row["kind"] == "custom"
        and row["claimed_by"] is None
        and not row["forwarded_to_owner"]
        and not row["ai_disabled"]
        and is_member
        and not author_is_staff
        and message.author.id == int(row["owner_id"])
        and message.channel.id not in ai_locks
    ):
        ai_locks.add(message.channel.id)
        try:
            async with message.channel.typing():
                await run_ai_shopping(message.channel, message.author)
        except Exception as e:
            print("AI shopping failed:", e)
        finally:
            ai_locks.discard(message.channel.id)

    # AI replacement: in open "support" tickets, enforce policy and guide the
    # customer until staff claims it or it's forwarded to the owner.
    if (
        AI_ENABLED
        and row["status"] == "open"
        and row["kind"] == "support"
        and row["claimed_by"] is None
        and not row["forwarded_to_owner"]
        and not row["ai_disabled"]
        and is_member
        and not author_is_staff
        and message.author.id == int(row["owner_id"])
        and message.channel.id not in ai_locks
    ):
        ai_locks.add(message.channel.id)
        try:
            async with message.channel.typing():
                await run_ai_replacement(message.channel, message.author)
        except Exception as e:
            print("AI replacement failed:", e)
        finally:
            ai_locks.discard(message.channel.id)

    await bot.process_commands(message)


# ============================================================
# STATUS ROTATOR
# ============================================================
def compute_status_strings(claimed_by: int | None, tick: int) -> tuple[str, str]:
    states = [
        "Waiting for staff",
        "Processing",
        "AF SERVICES Support",
        "Please provide details",
    ]
    state = states[tick % len(states)]
    claimed = "Claimed" if claimed_by else "Unclaimed"
    footer = f"AF SERVICES • Status: {state} • {claimed}"
    topic = f"AF SERVICES Ticket | {claimed} | {state}"
    return footer, topic


@tasks.loop(seconds=STATUS_ROTATE_SECONDS)
async def status_rotator():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    rows = await db_fetch(
        """
        SELECT channel_id, claimed_by, last_footer_text, last_topic_text
        FROM tickets
        WHERE guild_id=$1 AND status='open'
        """,
        guild.id
    )

    tick = int(utcnow().timestamp() // STATUS_ROTATE_SECONDS)

    for r in rows:
        channel_id = int(r["channel_id"])
        ch = guild.get_channel(channel_id)
        if not isinstance(ch, discord.TextChannel):
            continue

        footer_text, topic_text = compute_status_strings(r["claimed_by"], tick)

        if (r["last_topic_text"] or "") != topic_text:
            try:
                await ch.edit(topic=topic_text)
                await db_execute("UPDATE tickets SET last_topic_text=$1 WHERE channel_id=$2", topic_text, channel_id)
            except Exception:
                pass

        if (r["last_footer_text"] or "") != footer_text:
            await db_execute("UPDATE tickets SET last_footer_text=$1 WHERE channel_id=$2", footer_text, channel_id)
            await refresh_ticket_control_message(ch)


# ============================================================
# BACKGROUND HOUSEKEEPING LOOPS
# ============================================================
@tasks.loop(minutes=30)
async def review_reminder_loop():
    """DM buyers a review reminder some hours after delivery (once)."""
    if REVIEW_REMINDER_HOURS <= 0:
        return
    rows = await db_fetch(
        "SELECT id, owner_id FROM deliveries WHERE review_reminded=FALSE "
        "AND delivered_at < NOW() - make_interval(hours => $1) "
        "AND delivered_at > NOW() - INTERVAL '3 days' LIMIT 20",
        max(1, int(REVIEW_REMINDER_HOURS)))
    reps = f"<#{REPS_CHANNEL_ID}>" if REPS_CHANNEL_ID else "our reviews channel"
    for r in rows:
        await db_execute("UPDATE deliveries SET review_reminded=TRUE WHERE id=$1", r["id"])
        try:
            user = await bot.fetch_user(int(r["owner_id"]))
            await user.send(embed=discord.Embed(
                title="⭐  Enjoying your account?",
                description=(f"If everything's working great, we'd love a quick **+rep** in "
                            f"{reps} — it really helps us out! 💙\n\nHaving any issues? Just open "
                            f"a ticket and we'll sort it."),
                color=AF_BLUE))
        except Exception as e:
            print("Review reminder DM failed:", e)


@tasks.loop(minutes=30)
async def idle_close_loop():
    """Auto-close tickets idle beyond IDLE_CLOSE_HOURS (skips forwarded ones)."""
    if IDLE_CLOSE_HOURS <= 0:
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    rows = await db_fetch(
        "SELECT channel_id FROM tickets WHERE guild_id=$1 AND status='open' "
        "AND forwarded_to_owner=FALSE AND last_activity < NOW() - make_interval(hours => $2) LIMIT 25",
        guild.id, max(1, int(IDLE_CLOSE_HOURS)))
    for r in rows:
        ch = guild.get_channel(int(r["channel_id"]))
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(f"💤 This ticket has been inactive for over "
                              f"**{int(IDLE_CLOSE_HOURS)}h** — closing it automatically. "
                              f"Open a new ticket any time!")
                await close_ticket_flow(ch, closed_by="Auto (inactivity)",
                                        reason=f"Inactive > {int(IDLE_CLOSE_HOURS)}h")
            except Exception as e:
                print("Idle auto-close failed:", e)


_low_stock_alerted: set[str] = set()


@tasks.loop(minutes=60)
async def low_stock_loop():
    """Alert staff when valid stock in a category drops below the threshold (once per dip)."""
    if not (LZT_ENABLED and LOW_STOCK_THRESHOLD > 0):
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    used = await _delivered_item_ids()
    target = guild.get_channel(RESTOCK_CHANNEL_ID) if RESTOCK_CHANNEL_ID else None
    if not isinstance(target, discord.TextChannel):
        target = await get_log_channel(guild)
    for cat in LOW_STOCK_CATEGORIES:
        res = await lzt_find_valid(cat)
        if not res["ok"]:
            continue
        avail = [it for it in res["items"]
                 if int(re.sub(r"[^0-9]", "", str(it["item_id"])) or 0) not in used]
        n = len(avail)
        if n < LOW_STOCK_THRESHOLD:
            if cat in _low_stock_alerted:
                continue
            _low_stock_alerted.add(cat)
            if isinstance(target, discord.TextChannel):
                await target.send(embed=discord.Embed(
                    title="📉  Low Stock Alert",
                    description=(f"Only **{n}** valid **{cat.title()}** account(s) left "
                                f"(threshold {LOW_STOCK_THRESHOLD}). Time to restock."),
                    color=0xE67E22))
        else:
            _low_stock_alerted.discard(cat)


# ============================================================
# READY
# ============================================================
@bot.event
async def on_ready():
    await ensure_db()

    try:
        gobj = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=gobj)
        await bot.tree.sync(guild=gobj)
    except Exception as e:
        print("Command sync error:", e)

    bot.add_view(TicketPanelView())
    bot.add_view(CardView())
    bot.add_view(RiotGuideView())
    bot.add_view(DeliveryApprovalView())
    bot.add_view(CryptoPayView())
    bot.add_view(CryptoCheckView())
    # Resolve dynamic buttons (View all skins / Read email letters) after restarts.
    try:
        bot.add_dynamic_items(ViewSkinsButton, ReadMailButton)
    except Exception as e:
        print("Dynamic item register failed:", e)

    rows = await db_fetch(
        "SELECT channel_id, control_message_id FROM tickets WHERE guild_id=$1 AND status='open' AND control_message_id IS NOT NULL",
        GUILD_ID
    )
    for r in rows:
        try:
            bot.add_view(TicketControlView(int(r["channel_id"])), message_id=int(r["control_message_id"]))
        except Exception:
            pass

    if not status_rotator.is_running():
        status_rotator.start()
    for loop in (review_reminder_loop, idle_close_loop, low_stock_loop):
        if not loop.is_running():
            loop.start()

    print(f"✅ Ticket bot online as {bot.user}")
    print(f"🖼️  Asset dir: {ASSET_DIR}")
    print(f"🖼️  Logo found:   {os.path.exists(LOGO_PATH)}  ({LOGO_PATH})")
    print(f"🖼️  Banner found: {os.path.exists(BANNER_PATH)}  ({BANNER_PATH})")
    if AF_LOGO_URL or AF_BANNER_URL:
        print(f"🖼️  URL overrides -> logo:{AF_LOGO_URL or '(none)'} banner:{AF_BANNER_URL or '(none)'}")
    print(f"🖼️  Card proof imgs -> payment:{os.path.exists(CARD_PROOF_PAYMENT_PATH)} email:{os.path.exists(CARD_PROOF_EMAIL_PATH)}")
    print(f"🤖 AI intake:      {'ON ('+AI_MODEL+')' if AI_ENABLED else 'OFF (set ANTHROPIC_API_KEY)'}")
    print(f"💳 SellAuth:       {'ON (shop '+SELLAUTH_SHOP_ID+')' if SELLAUTH_ENABLED else 'OFF (set SELLAUTH_API_KEY + SELLAUTH_SHOP_ID)'}")
    print(f"📦 LZT.market:     {'ON' if LZT_ENABLED else 'OFF (set LZT_API_TOKEN)'}{'' if LZT_USER_ID else ' [LZT_USER_ID missing]'}")
    print(f"🚚 Delivery:       {'AUTO' if AUTO_DELIVER else 'staff-approve'} • SellAuth required: {REQUIRE_SELLAUTH}")
    print(f"🛒 Resale/restock: x{RESALE_MULTIPLIER} markup • restock alerts → "
          f"{'channel '+str(RESTOCK_CHANNEL_ID) if RESTOCK_CHANNEL_ID else 'DM fallback (set RESTOCK_CHANNEL_ID)'}")
    print(f"💎 Crypto pay:     {'ON ('+NOWPAYMENTS_PRICE_CURRENCY+', LTC/SOL/BTC/ETH)' if NOWPAYMENTS_ENABLED else 'OFF (set NOWPAYMENTS_API_KEY)'}")
    print(f"🧾 Transcripts + order notices → "
          f"{'channel '+str(TRANSCRIPT_CHANNEL_ID) if TRANSCRIPT_CHANNEL_ID else 'OFF (set TRANSCRIPT_CHANNEL_ID or TICKET_LOG_CHANNEL_ID)'}")
    print(f"📧 Email letters:  {'ON' if EMAIL_LETTERS_ENABLED else 'OFF (EMAIL_LETTERS_ENABLED=0)'}")


bot.run(DISCORD_TOKEN)