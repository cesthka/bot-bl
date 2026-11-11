"""
================================================================================
  BOT BL — Blacklist cross-serveur
================================================================================
Hiérarchie : Buyer (4) > Sys (3) > Owner (2) > Whitelist (1)

  - Blacklist (WL+) : ban automatique sur tous les serveurs du bot + à l'arrivée
  - Blacklist TEMPORAIRE : &bl @user 7j raison  (30m, 12h, 7j, 2s...) -> levée auto
  - Super blacklist (Sys+) : retirable uniquement par Sys+ / rang supérieur à l'auteur
  - Limites anti-raid par rang : X blacklists par fenêtre de temps (panneau &limite)
  - Unban ciblé : on ne débannit QUE les bans posés par la blacklist
  - Synchronisation : au join d'un serveur + &blsync
  - Historique complet (&bllog), listes paginées, logs

Variables d'environnement : TOKEN, DATA_DIR
================================================================================
"""

import discord
from discord.ext import commands, tasks
import os
import re
import sys
import json
import math
import time
import asyncio
import sqlite3
import logging
import traceback
from datetime import datetime, timezone

# ========================= CONFIG =========================
BOT_TOKEN = os.environ.get("TOKEN")
if not BOT_TOKEN:
    print("[ERREUR CRITIQUE] La variable d'environnement TOKEN n'est pas définie.")
    sys.exit(1)

DEFAULT_BUYER_IDS = [142365250803466240, 1546586066407133308]
DEFAULT_PREFIX = "&"

DATA_DIR = os.environ.get("DATA_DIR")
if not DATA_DIR:
    print("[ERREUR CRITIQUE] DATA_DIR non défini. Configure DATA_DIR=/data dans Railway.")
    sys.exit(1)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bl_bot.db")

BAN_TAG = "[BL]"           # préfixe des raisons de ban posées par ce bot
REASON_MIN = 5
REASON_MAX = 500
MAX_TEMP_SECONDS = 365 * 86400

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%d/%m/%Y %H:%M:%S")
log = logging.getLogger("bl")

_prefix_cache = {"value": None}

# {rang: (max, fenêtre en heures)}
DEFAULT_BL_LIMITS = {"1": [5, 24], "2": [15, 24], "3": [50, 24], "4": [9999, 24]}


# ========================= DATABASE =========================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS ranks (user_id TEXT PRIMARY KEY, rank INTEGER NOT NULL)")
    c.execute("""CREATE TABLE IF NOT EXISTS blacklist (
        user_id TEXT PRIMARY KEY, added_by TEXT NOT NULL, reason TEXT, added_at TEXT NOT NULL, is_super INTEGER DEFAULT 0)""")
    for col, typ in (("ts", "INTEGER"), ("expires_at", "INTEGER"), ("guild_id", "TEXT")):
        try:
            c.execute(f"ALTER TABLE blacklist ADD COLUMN {col} {typ}")  # migration de l'ancienne base
        except sqlite3.OperationalError:
            pass
    c.execute("CREATE TABLE IF NOT EXISTS log_channels (guild_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS bl_actions (user_id TEXT NOT NULL, ts INTEGER NOT NULL)")
    c.execute("""CREATE TABLE IF NOT EXISTS bl_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, target_id TEXT NOT NULL, author_id TEXT NOT NULL,
        action TEXT NOT NULL, reason TEXT, ts INTEGER NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_hist_target ON bl_history (target_id, ts DESC)")
    c.execute("INSERT OR IGNORE INTO config VALUES ('prefix', ?)", (DEFAULT_PREFIX,))
    c.execute("INSERT OR REPLACE INTO config VALUES ('buyer_ids', ?)", (json.dumps([str(i) for i in DEFAULT_BUYER_IDS]),))
    conn.commit()
    conn.close()


def db_exec(sql, params=()):
    conn = get_db()
    cur = conn.execute(sql, params)
    conn.commit()
    rc = cur.rowcount
    conn.close()
    return rc


def db_all(sql, params=()):
    conn = get_db()
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows


def db_one(sql, params=()):
    rows = db_all(sql, params)
    return rows[0] if rows else None


_config_cache = {}


def get_config(key):
    if key not in _config_cache:
        row = db_one("SELECT value FROM config WHERE key = ?", (key,))
        _config_cache[key] = row["value"] if row else None
    return _config_cache[key]


def set_config(key, value):
    db_exec("INSERT OR REPLACE INTO config VALUES (?, ?)", (key, str(value)))
    _config_cache[key] = str(value)
    if key == "prefix":
        _prefix_cache["value"] = str(value)


def get_prefix_cached():
    if _prefix_cache["value"] is None:
        _prefix_cache["value"] = get_config("prefix") or DEFAULT_PREFIX
    return _prefix_cache["value"]


# ---- Rangs (cache) ----
_rank_cache = {}
_buyer_cache = set()


def load_ranks():
    _rank_cache.clear()
    for r in db_all("SELECT user_id, rank FROM ranks"):
        _rank_cache[r["user_id"]] = r["rank"]
    _buyer_cache.clear()
    try:
        _buyer_cache.update(str(i) for i in json.loads(get_config("buyer_ids") or "[]"))
    except (json.JSONDecodeError, TypeError):
        pass


def get_rank_db(user_id):
    uid = str(user_id)
    return 4 if uid in _buyer_cache else _rank_cache.get(uid, 0)


def set_rank_db(user_id, rank):
    uid = str(user_id)
    if rank == 0:
        db_exec("DELETE FROM ranks WHERE user_id = ?", (uid,))
        _rank_cache.pop(uid, None)
    else:
        db_exec("INSERT OR REPLACE INTO ranks VALUES (?, ?)", (uid, rank))
        _rank_cache[uid] = rank


def get_ranks_by_level(level):
    return [uid for uid, r in _rank_cache.items() if r == level]


# ---- Emojis personnalisables ----
DEFAULT_EMOJIS = {"help_home_title": "🔨", "help_cat_bl": "🔨", "help_cat_superbl": "⛔", "help_cat_rangs": "👑", "help_cat_system": "🛠️"}
EMOJI_LABELS = {"help_home_title": "Help · Titre accueil", "help_cat_bl": "Help · Catégorie Blacklist",
                "help_cat_superbl": "Help · Catégorie Super BL", "help_cat_rangs": "Help · Catégorie Rangs",
                "help_cat_system": "Help · Catégorie Système"}


def get_emojis():
    try:
        data = json.loads(get_config("emojis") or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    merged = dict(DEFAULT_EMOJIS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_EMOJIS})
    return merged


def get_emoji(key):
    return get_emojis().get(key, DEFAULT_EMOJIS.get(key, ""))


def set_emoji(key, value):
    e = get_emojis()
    e[key] = value
    set_config("emojis", json.dumps(e))


def reset_emojis():
    set_config("emojis", json.dumps(DEFAULT_EMOJIS))


# ---- Limites (max + fenêtre de temps par rang) ----

def get_bl_limits():
    merged = {k: list(v) for k, v in DEFAULT_BL_LIMITS.items()}
    try:
        data = json.loads(get_config("bl_limits") or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    for k, v in data.items():
        if k in merged:
            merged[k] = [int(v), 24] if isinstance(v, (int, float)) else [int(v[0]), int(v[1])]  # ancien format = nombre seul
    return merged


def get_bl_limit_for_rank(rank):
    m, h = get_bl_limits().get(str(rank), [0, 24])
    return m, h


def set_bl_limit_for_rank(rank, max_count, hours):
    limits = get_bl_limits()
    limits[str(rank)] = [int(max_count), int(hours)]
    set_config("bl_limits", json.dumps(limits))


def reset_bl_limits():
    set_config("bl_limits", json.dumps(DEFAULT_BL_LIMITS))


def record_bl_action(user_id):
    db_exec("INSERT INTO bl_actions VALUES (?, ?)", (str(user_id), int(time.time())))


def count_bl_actions(user_id, hours):
    cutoff = int(time.time()) - hours * 3600
    row = db_one("SELECT COUNT(*) AS c FROM bl_actions WHERE user_id = ? AND ts >= ?", (str(user_id), cutoff))
    return row["c"]


def oldest_action_in_window(user_id, hours):
    cutoff = int(time.time()) - hours * 3600
    row = db_one("SELECT MIN(ts) AS t FROM bl_actions WHERE user_id = ? AND ts >= ?", (str(user_id), cutoff))
    return row["t"]


def check_limit(author_id):
    """Retourne None si OK, sinon un embed d'erreur."""
    rank = get_rank_db(author_id)
    max_count, hours = get_bl_limit_for_rank(rank)
    used = count_bl_actions(author_id, hours)
    if used >= max_count:
        oldest = oldest_action_in_window(author_id, hours)
        retry = f"\nProchaine blacklist possible <t:{oldest + hours * 3600}:R>." if oldest else ""
        return error_embed("⛔ Limite de blacklist atteinte",
                           f"Ton rang (**{rank_name(rank)}**) est limité à **{max_count}** blacklist(s) par **{fmt_hours(hours)}**.\n"
                           f"Tu en as déjà fait **{used}**.{retry}")
    return None


# ---- Blacklist ----

def add_blacklist(user_id, added_by, reason, is_super=0, expires_at=None, guild_id=None):
    now = int(time.time())
    db_exec("INSERT OR REPLACE INTO blacklist (user_id, added_by, reason, added_at, is_super, ts, expires_at, guild_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(user_id), str(added_by), reason, datetime.now().strftime("%d/%m/%Y %Hh%M"), is_super, now,
             expires_at, str(guild_id) if guild_id else None))


def remove_blacklist(user_id):
    db_exec("DELETE FROM blacklist WHERE user_id = ?", (str(user_id),))


def get_blacklist_entry(user_id):
    return db_one("SELECT * FROM blacklist WHERE user_id = ?", (str(user_id),))


def get_all_blacklist(is_super=0):
    return db_all("SELECT * FROM blacklist WHERE is_super = ? ORDER BY COALESCE(ts, 0) DESC", (is_super,))


def add_history(target_id, author_id, action, reason=None):
    db_exec("INSERT INTO bl_history (target_id, author_id, action, reason, ts) VALUES (?, ?, ?, ?, ?)",
            (str(target_id), str(author_id), action, (reason or "")[:REASON_MAX], int(time.time())))


def get_log_channel(guild_id):
    row = db_one("SELECT channel_id FROM log_channels WHERE guild_id = ?", (str(guild_id),))
    return row["channel_id"] if row else None


def set_log_channel(guild_id, channel_id):
    db_exec("INSERT OR REPLACE INTO log_channels VALUES (?, ?)", (str(guild_id), str(channel_id)))


# ========================= HELPERS =========================

def rank_name(level):
    return {4: "Buyer", 3: "Sys", 2: "Owner", 1: "Whitelist", 0: "Aucun"}.get(level, "Aucun")


def has_min_rank(user_id, minimum):
    return get_rank_db(user_id) >= minimum


def embed_color():
    return 0x2b2d31


def success_embed(title, desc=""):
    return discord.Embed(title=title, description=desc, color=0x43b581)


def error_embed(title, desc=""):
    return discord.Embed(title=title, description=desc, color=0xf04747)


def info_embed(title, desc=""):
    return discord.Embed(title=title, description=desc, color=embed_color())


def denied(min_rank):
    if min_rank >= 4:
        return error_embed("❌ Permission refusée", "Seul le **Buyer** peut utiliser cette commande.")
    return error_embed("❌ Permission refusée", f"**{rank_name(min_rank)}+** requis.")


def _emoji_for_select(raw):
    try:
        return discord.PartialEmoji.from_str(raw) if raw else None
    except Exception:
        return None


def apply_thumbnail(em, guild):
    if guild and getattr(guild, "icon", None):
        try:
            em.set_thumbnail(url=guild.icon.url)
        except (AttributeError, TypeError):
            pass


def fmt_hours(h):
    if h % 24 == 0:
        d = h // 24
        return f"{d} jour" + ("s" if d > 1 else "")
    return f"{h} h"


DURATION_RE = re.compile(r"^(\d{1,4})\s*(m|min|mn|h|j|d|s|sem|w)$", re.IGNORECASE)
DURATION_UNITS = {"m": 60, "min": 60, "mn": 60, "h": 3600, "j": 86400, "d": 86400, "s": 604800, "sem": 604800, "w": 604800}


def parse_duration(token):
    """'30m', '12h', '7j', '2s' (semaines) -> secondes, sinon None."""
    m = DURATION_RE.match(token or "")
    if not m:
        return None
    secs = int(m.group(1)) * DURATION_UNITS[m.group(2).lower()]
    return secs if 0 < secs <= MAX_TEMP_SECONDS else None


def fmt_duration(secs):
    for unit, n in (("semaine", 604800), ("jour", 86400), ("heure", 3600), ("minute", 60)):
        if secs % n == 0:
            v = secs // n
            return f"{v} {unit}" + ("s" if v > 1 else "")
    return f"{secs // 60} minutes"


def entry_date(e):
    return f"<t:{e['ts']}:d>" if e.get("ts") else e.get("added_at", "?")


def entry_expiry(e):
    return f"<t:{e['expires_at']}:R>" if e.get("expires_at") else "définitive"


async def resolve_user_or_id(ctx, user_input):
    if not user_input:
        return None, None
    raw = user_input.strip()
    try:
        user_id = int(raw.strip("<@!>"))
    except ValueError:
        try:
            m = await commands.MemberConverter().convert(ctx, raw)
            return m, m.id
        except commands.CommandError:
            pass
        try:
            u = await commands.UserConverter().convert(ctx, raw)
            return u, u.id
        except commands.CommandError:
            return None, None
    if ctx.guild:
        member = ctx.guild.get_member(user_id)
        if member:
            return member, user_id
    try:
        return await bot.fetch_user(user_id), user_id
    except discord.NotFound:
        return None, user_id
    except discord.HTTPException as e:
        log.warning(f"resolve_user_or_id: fetch_user({user_id}) a échoué : {e}")
        return None, user_id


def format_user_display(display_obj, user_id):
    if display_obj is not None:
        return f"{display_obj.mention} (`{display_obj.id}`)"
    return f"<@{user_id}> (`{user_id}`) *(hors serveur)*"


# ========================= BOT SETUP =========================

init_db()
load_ranks()

intents = discord.Intents.all()


def get_prefix(bot, message):
    return get_prefix_cached()


bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)


@bot.check
async def _guild_only(ctx):
    return ctx.guild is not None


# ========================= LOGS =========================

async def send_log_embed(guild, em):
    if guild is None:
        return
    cid = get_log_channel(guild.id)
    ch = guild.get_channel(int(cid)) if cid else None
    if not ch:
        return
    try:
        await ch.send(embed=em, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        log.warning(f"send_log: échec d'envoi : {e}")


async def send_log(guild, action, author, target_display, target_id, reason=None, color=0xf04747, extra=None):
    em = discord.Embed(title=f"📋 {action}", color=color, timestamp=datetime.now(timezone.utc))
    em.add_field(name="Modérateur", value=f"{author.mention} (`{author.id}`)" if author else "*Automatique*", inline=True)
    em.add_field(name="Utilisateur", value=format_user_display(target_display, target_id), inline=True)
    if reason:
        em.add_field(name="Raison", value=reason[:1024], inline=False)
    if extra:
        em.add_field(name="Détail", value=extra[:1024], inline=False)
    await send_log_embed(guild, em)


# ========================= BAN / UNBAN CROSS-SERVEUR =========================

def _is_our_ban(reason):
    r = (reason or "").lower()
    return reason is not None and (r.startswith(BAN_TAG.lower()) or "blacklist" in r)


async def ban_everywhere(user_id, reason):
    """Bannit sur tous les serveurs. Retourne (succès, échecs)."""
    ok, failed = 0, []
    obj = discord.Object(id=int(user_id))
    full = f"{BAN_TAG} {reason}"[:512]
    for guild in bot.guilds:
        try:
            await guild.ban(obj, reason=full, delete_message_seconds=0)
            ok += 1
        except discord.HTTPException:
            failed.append(guild.name)
    return ok, failed


async def unban_everywhere(user_id, reason):
    """Débannit UNIQUEMENT là où le ban vient de la blacklist (un ban posé à la main reste)."""
    ok, kept = 0, 0
    obj = discord.Object(id=int(user_id))
    for guild in bot.guilds:
        try:
            ban = await guild.fetch_ban(obj)
        except discord.NotFound:
            continue
        except discord.HTTPException:
            continue
        if not _is_our_ban(ban.reason):
            kept += 1
            continue
        try:
            await guild.unban(obj, reason=f"{BAN_TAG} {reason}"[:512])
            ok += 1
        except discord.HTTPException:
            pass
    return ok, kept


async def sync_guild(guild):
    """Bannit les blacklistés encore présents sur ce serveur."""
    n = 0
    for e in db_all("SELECT user_id, reason FROM blacklist"):
        m = guild.get_member(int(e["user_id"]))
        if m is None:
            continue
        try:
            await guild.ban(m, reason=f"{BAN_TAG} Synchronisation blacklist | {e['reason'] or ''}"[:512], delete_message_seconds=0)
            n += 1
        except discord.HTTPException:
            pass
    return n


# ========================= EVENTS =========================

@bot.event
async def on_ready():
    log.info(f"Bot connecté : {bot.user} ({bot.user.id}) · prefix {get_prefix_cached()} · {len(bot.guilds)} serveur(s)")
    if not expiry_loop.is_running():
        expiry_loop.start()


@bot.event
async def on_guild_join(guild):
    n = await sync_guild(guild)
    log.info(f"Nouveau serveur {guild.name} : {n} blacklisté(s) banni(s).")


@bot.event
async def on_member_join(member):
    entry = get_blacklist_entry(member.id)
    if not entry:
        return
    try:
        await member.ban(reason=f"{BAN_TAG} Blacklisté a tenté de rejoindre | {entry['reason'] or ''}"[:512], delete_message_seconds=0)
        await send_log(member.guild, "Blacklisté bloqué à l'arrivée", None, member, member.id, entry["reason"],
                       extra=f"Blacklisté par <@{entry['added_by']}> · expire : {entry_expiry(entry)}")
    except discord.HTTPException as e:
        log.warning(f"on_member_join: ban échoué pour {member.id} : {e}")


@tasks.loop(seconds=60)
async def expiry_loop():
    """Lève les blacklists temporaires arrivées à échéance."""
    now = int(time.time())
    for e in db_all("SELECT * FROM blacklist WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)):
        remove_blacklist(e["user_id"])
        add_history(e["user_id"], bot.user.id if bot.user else 0, "expiration", "Blacklist temporaire expirée")
        ok, _kept = await unban_everywhere(e["user_id"], "Blacklist temporaire expirée")
        guild = bot.get_guild(int(e["guild_id"])) if e.get("guild_id") else None
        await send_log(guild, "Blacklist temporaire expirée", None, None, int(e["user_id"]), e["reason"], color=0x43b581,
                       extra=f"Posée par <@{e['added_by']}> · débanni de {ok} serveur(s)")


@expiry_loop.before_loop
async def _before_expiry():
    await bot.wait_until_ready()


# ========================= VUES =========================

class OwnedView(discord.ui.View):
    def __init__(self, author_id, guild=None, timeout=180):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.guild = guild
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(f"Ce menu n'est pas à toi. Fais `{get_prefix_cached()}help` pour voir le tien.",
                                                    ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class PagerView(OwnedView):
    """Liste paginée (évite l'embed trop long qui faisait planter &bl)."""

    def __init__(self, author_id, guild, title, lines, per_page=10, footer=None):
        super().__init__(author_id, guild, timeout=180)
        self.title, self.lines, self.per_page, self.footer = title, lines, per_page, footer
        self.page = 0
        self.pages = max(1, math.ceil(len(lines) / per_page))
        self._sync()

    def _sync(self):
        self.prev.disabled = self.page <= 0
        self.next.disabled = self.page >= self.pages - 1

    def embed(self):
        chunk = self.lines[self.page * self.per_page:(self.page + 1) * self.per_page]
        em = info_embed(self.title, "\n".join(chunk)[:4000] or "Aucun élément.")
        em.set_footer(text=f"Page {self.page + 1}/{self.pages}" + (f" · {self.footer}" if self.footer else ""))
        apply_thumbnail(em, self.guild)
        return em

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.pages - 1, self.page + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)


async def send_pager(ctx, title, lines, per_page=10, footer=None):
    view = PagerView(ctx.author.id, ctx.guild, title, lines, per_page, footer)
    view.message = await ctx.send(embed=view.embed(), view=view if view.pages > 1 else None,
                                  allowed_mentions=discord.AllowedMentions.none())


async def ask_value(interaction, author_id, prompt, timeout=60):
    await interaction.response.send_message(prompt)

    def check(m):
        return m.author.id == author_id and m.channel.id == interaction.channel.id

    try:
        reply = await bot.wait_for("message", check=check, timeout=timeout)
    except asyncio.TimeoutError:
        try:
            await interaction.edit_original_response(content="⏱️ Temps écoulé, aucune valeur reçue.")
        except discord.HTTPException:
            pass
        return None
    val = reply.content.strip()
    for coro in (interaction.delete_original_response(), reply.delete()):
        try:
            await coro
        except discord.HTTPException:
            pass
    return val


# ========================= HELP (filtré par rang) =========================

HELP_CATEGORIES = {
    "bl": {
        "label": "Blacklist", "title": "Blacklist",
        "subtitle": "Ban automatique sur tous les serveurs du bot, et à chaque tentative de retour.",
        "sections": [
            ("⚔️", "Sanctionner", [
                ("bl @user <raison>", "Blacklist définitive (raison min. 5 caractères)", 1),
                ("bl @user 7j <raison>", "Blacklist temporaire (30m, 12h, 7j, 2s…)", 1),
                ("unbl @user", "Retirer la blacklist", 1),
            ]),
            ("👁️", "Consulter", [
                ("bl", "Liste paginée de la blacklist", 1),
                ("blinfo @user", "Fiche d'un utilisateur", 1),
                ("bllog @user", "Historique (BL, unBL, expirations)", 1),
                ("quota", "Tes blacklists restantes", 1),
            ]),
        ],
    },
    "superbl": {
        "label": "Super BL", "title": "Super Blacklist",
        "subtitle": "Blacklist renforcée, définitive, retirable uniquement par Sys+.",
        "sections": [
            ("⛔", "Super blacklist (Sys+)", [
                ("superbl @user <raison>", "Super blacklist", 3),
                ("superbl", "Liste de la super blacklist", 3),
                ("unsuperbl @user", "Retirer la super blacklist", 3),
            ]),
            ("🔄", "Synchronisation (Sys+)", [
                ("blsync", "Rebannir les blacklistés présents sur les serveurs", 3),
            ]),
        ],
    },
    "rangs": {
        "label": "Rangs", "title": "Rangs",
        "subtitle": "Tu ne peux gérer que les rangs inférieurs au tien.",
        "sections": [
            ("📜", "Whitelist (Owner+)", [("wl [@user]", "Ajouter / voir la whitelist", 2), ("unwl @user", "Retirer", 2)]),
            ("👑", "Owner (Sys+)", [("owner [@user]", "Ajouter / voir les owners", 3), ("unowner @user", "Retirer", 3)]),
            ("⚙️", "Sys (Buyer)", [("sys [@user]", "Ajouter / voir les sys", 4), ("unsys @user", "Retirer", 4)]),
        ],
    },
    "system": {
        "label": "Système", "title": "Système",
        "subtitle": "Configuration du bot.",
        "sections": [
            ("⚙️", "Buyer only", [
                ("prefix [nouveau]", "Changer le prefix", 4),
                ("setlog #salon", "Salon de logs", 4),
                ("setemoji", "Personnaliser les emojis", 4),
                ("limite", "Limites de blacklist par rang et par période", 4),
                ("massban id1 id2 … | raison", "Bannir une liste d'IDs", 4),
            ]),
        ],
    },
}


def _accessible_sections(key, rank):
    out = []
    for emoji, title, items in HELP_CATEGORIES[key]["sections"]:
        vis = [(s, d) for (s, d, mr) in items if rank >= mr]
        if vis:
            out.append((emoji, title, vis))
    return out


def help_category_visible(key, rank):
    return bool(_accessible_sections(key, rank))


def build_bl_category_embed(key, rank, guild=None):
    p = get_prefix_cached()
    cat = HELP_CATEGORIES[key]
    em = discord.Embed(title=f"{get_emoji(f'help_cat_{key}')}  {cat['title']}", description=cat["subtitle"], color=embed_color())
    apply_thumbnail(em, guild)
    sections = _accessible_sections(key, rank)
    if not sections:
        em.add_field(name="⛔ Aucune commande accessible", value="Tu n'as pas les permissions pour cette catégorie.", inline=False)
    for s_emoji, s_title, items in sections:
        em.add_field(name=f"{s_emoji} {s_title}", value="\n".join(f"`{p}{s}` — {d}" for s, d in items), inline=False)
    if key == "bl":
        m, h = get_bl_limit_for_rank(rank)
        em.add_field(name="💡 Règles",
                     value=f"Tu peux uniquement blacklister un rang **inférieur** au tien.\n"
                           f"Ta limite : **{m}** blacklist(s) par **{fmt_hours(h)}**.", inline=False)
    return em


def build_bl_home_embed(rank, guild=None):
    em = discord.Embed(title=f"{get_emoji('help_home_title')}  Bot BL",
                       description=f"**Prefix :** `{get_prefix_cached()}` ・ **Ton rang :** {rank_name(rank)}\n\n"
                                   f"*Choisis une catégorie ci-dessous pour voir ses commandes.*", color=embed_color())
    apply_thumbnail(em, guild)
    descs = {"bl": "Blacklister, temporaire ou définitif", "superbl": "Blacklist renforcée (Sys+)",
             "rangs": "Whitelist, owners, sys", "system": "Config du bot"}
    for gname, keys in (("🔨 Blacklist", ["bl", "superbl"]), ("🛠️ Staff & Admin", ["rangs", "system"])):
        lines = [f"{get_emoji(f'help_cat_{k}')} **{HELP_CATEGORIES[k]['label']}** — {descs[k]}" for k in keys if help_category_visible(k, rank)]
        if lines:
            em.add_field(name=gname, value="\n".join(lines), inline=False)
    n, s = db_one("SELECT COUNT(*) AS n FROM blacklist WHERE is_super = 0")["n"], db_one("SELECT COUNT(*) AS n FROM blacklist WHERE is_super = 1")["n"]
    t = db_one("SELECT COUNT(*) AS n FROM blacklist WHERE expires_at IS NOT NULL")["n"]
    em.add_field(name="📊 Statut", value=f"**{n}** blacklisté(s) dont **{t}** temporaire(s) ・ **{s}** super BL ・ **{len(bot.guilds)}** serveur(s)",
                 inline=False)
    return em


class HelpDropdown(discord.ui.Select):
    def __init__(self, rank, guild=None):
        self.rank, self.guild = rank, guild
        options = [discord.SelectOption(label="Accueil", emoji="🏠", value="home")]
        for key, cat in HELP_CATEGORIES.items():
            if help_category_visible(key, rank):
                options.append(discord.SelectOption(label=cat["label"], value=key, emoji=_emoji_for_select(get_emoji(f"help_cat_{key}"))))
        super().__init__(placeholder="📂 Choisis une catégorie...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        em = build_bl_home_embed(self.rank, self.guild) if key == "home" else build_bl_category_embed(key, self.rank, self.guild)
        await interaction.response.edit_message(embed=em, view=self.view)


class HelpView(OwnedView):
    def __init__(self, author_id, rank, guild=None):
        super().__init__(author_id, guild, timeout=120)
        self.add_item(HelpDropdown(rank, guild))


@bot.command(name="help")
async def _help(ctx):
    rank = get_rank_db(ctx.author.id)
    if rank < 1:
        return
    view = HelpView(ctx.author.id, rank, ctx.guild)
    view.message = await ctx.send(embed=build_bl_home_embed(rank, ctx.guild), view=view)


# ========================= SYSTÈME =========================

@bot.command(name="prefix")
async def _prefix(ctx, new_prefix: str = None):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=denied(4))
    if not new_prefix:
        return await ctx.send(embed=info_embed("Prefix actuel", f"`{get_prefix_cached()}`"))
    set_config("prefix", new_prefix)
    await ctx.send(embed=success_embed("✅ Prefix modifié", f"Nouveau prefix : `{new_prefix}`"))


@bot.command(name="setlog")
async def _setlog(ctx, channel: discord.TextChannel = None):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=denied(4))
    if not channel:
        return await ctx.send(embed=error_embed("Argument manquant", "Mentionne un salon."))
    set_log_channel(ctx.guild.id, channel.id)
    await ctx.send(embed=success_embed("✅ Salon de logs défini", f"Les logs seront envoyés dans {channel.mention}."))


def build_emoji_embed(guild=None):
    e = get_emojis()
    em = discord.Embed(title="🎨 Emojis personnalisables",
                       description="Choisis un emoji à modifier dans le menu déroulant.\n"
                                   "Tu peux mettre un emoji classique (🔥) ou un emoji du serveur (`<:nom:id>`).", color=embed_color())
    em.add_field(name="🎙️ Help", value="\n".join(f"{e[k]} **{EMOJI_LABELS[k]}**" for k in DEFAULT_EMOJIS), inline=False)
    apply_thumbnail(em, guild)
    return em


class EmojiSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Choisis un emoji à modifier...", min_values=1, max_values=1, options=[
            discord.SelectOption(label=EMOJI_LABELS[k][:100], value=k, emoji=_emoji_for_select(get_emoji(k)), description=k[:100])
            for k in DEFAULT_EMOJIS])

    async def callback(self, interaction: discord.Interaction):
        key, panel, author_id, guild = self.values[0], interaction.message, self.view.author_id, self.view.guild
        val = await ask_value(interaction, author_id, f"📨 Envoie l'emoji pour **{EMOJI_LABELS[key]}** dans ce salon. *(60 secondes)*")
        if val:
            set_emoji(key, val)
        try:
            view = EmojiView(author_id, guild)
            await panel.edit(embed=build_emoji_embed(guild), view=view)
            view.message = panel
        except discord.HTTPException:
            pass


class EmojiView(OwnedView):
    def __init__(self, author_id, guild=None):
        super().__init__(author_id, guild)
        self.add_item(EmojiSelect())

    @discord.ui.button(label="Tout réinitialiser", style=discord.ButtonStyle.danger, emoji="♻️", row=1)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        reset_emojis()
        view = EmojiView(self.author_id, self.guild)
        await interaction.response.edit_message(embed=build_emoji_embed(self.guild), view=view)
        view.message = interaction.message


@bot.command(name="setemoji")
async def _setemoji(ctx, key: str = None, *, value: str = None):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=denied(4))
    if key is not None and value is not None:
        if key not in DEFAULT_EMOJIS:
            return await ctx.send(embed=error_embed("❌ Clé inconnue", "Clés valides :\n" + "\n".join(f"`{k}`" for k in DEFAULT_EMOJIS)))
        set_emoji(key, value.strip())
        return await ctx.send(embed=success_embed("✅ Emoji modifié", f"`{key}` → {value.strip()}"))
    view = EmojiView(ctx.author.id, ctx.guild)
    view.message = await ctx.send(embed=build_emoji_embed(ctx.guild), view=view)


# ---- &limite : max + période par rang (même panneau que l'Antifast) ----

RANK_LEVELS_EDIT = [(4, "Buyer"), (3, "Sys"), (2, "Owner"), (1, "Whitelist")]


def build_bl_limit_embed(guild=None):
    limits = get_bl_limits()
    em = discord.Embed(title="🛡️ Limites de blacklist",
                       description="Nombre maximum de blacklists **par période** et par rang.\n"
                                   "Choisis un rang, puis envoie `max heures` (ex : `5 24` = 5 BL par 24 h).\n"
                                   "*Protège contre un ban-all via un compte compromis ou un selfbot.*", color=embed_color())
    for lvl, name in RANK_LEVELS_EDIT:
        m, h = limits[str(lvl)]
        em.add_field(name=name, value=f"**{m}** / {fmt_hours(h)}", inline=True)
    apply_thumbnail(em, guild)
    return em


class BLLimitSelect(discord.ui.Select):
    def __init__(self):
        limits = get_bl_limits()
        super().__init__(placeholder="Choisis un rang à modifier...", min_values=1, max_values=1, options=[
            discord.SelectOption(label=name, value=str(lvl), description=f"Actuel : {limits[str(lvl)][0]} / {fmt_hours(limits[str(lvl)][1])}")
            for lvl, name in RANK_LEVELS_EDIT])

    async def callback(self, interaction: discord.Interaction):
        lvl = int(self.values[0])
        panel, author_id, guild = interaction.message, self.view.author_id, self.view.guild
        val = await ask_value(interaction, author_id,
                              f"📨 Envoie la limite pour **{rank_name(lvl)}** : `max heures` (ex : `5 24`). *(60 secondes)*")
        if val is None:
            return
        parts = val.split()
        if parts and parts[0].isdigit() and (len(parts) == 1 or (parts[1].isdigit() and 1 <= int(parts[1]) <= 720)):
            set_bl_limit_for_rank(lvl, int(parts[0]), int(parts[1]) if len(parts) > 1 else get_bl_limit_for_rank(lvl)[1])
        else:
            try:
                await interaction.followup.send("❌ Format invalide (ex : `5 24`), aucune modification.", ephemeral=True)
            except discord.HTTPException:
                pass
        try:
            view = BLLimitView(author_id, guild)
            await panel.edit(embed=build_bl_limit_embed(guild), view=view)
            view.message = panel
        except discord.HTTPException:
            pass


class BLLimitView(OwnedView):
    def __init__(self, author_id, guild=None):
        super().__init__(author_id, guild)
        self.add_item(BLLimitSelect())

    @discord.ui.button(label="Réinitialiser", style=discord.ButtonStyle.danger, emoji="♻️", row=1)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        reset_bl_limits()
        view = BLLimitView(self.author_id, self.guild)
        await interaction.response.edit_message(embed=build_bl_limit_embed(self.guild), view=view)
        view.message = interaction.message


@bot.command(name="limite", aliases=["limites"])
async def _limite(ctx):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=denied(4))
    view = BLLimitView(ctx.author.id, ctx.guild)
    view.message = await ctx.send(embed=build_bl_limit_embed(ctx.guild), view=view)


@bot.command(name="quota")
async def _quota(ctx):
    rank = get_rank_db(ctx.author.id)
    if rank < 1:
        return
    m, h = get_bl_limit_for_rank(rank)
    used = count_bl_actions(ctx.author.id, h)
    oldest = oldest_action_in_window(ctx.author.id, h)
    desc = f"Rang **{rank_name(rank)}** : **{used}/{m}** blacklist(s) sur les dernières **{fmt_hours(h)}**.\n" \
           f"Il t'en reste **{max(0, m - used)}**."
    if used >= m and oldest:
        desc += f"\nProchaine possible <t:{oldest + h * 3600}:R>."
    await ctx.send(embed=info_embed("📊 Ton quota de blacklist", desc))


# ========================= RANGS =========================

async def _rank_list(ctx, level, min_rank, title):
    if not has_min_rank(ctx.author.id, min_rank):
        return await ctx.send(embed=denied(min_rank))
    ids = get_ranks_by_level(level)
    await send_pager(ctx, f"📋 {title} ({len(ids)})", [f"<@{u}> (`{u}`)" for u in ids] or ["Aucun utilisateur."], 15)


async def _rank_add(ctx, user_input, level, min_rank, label):
    if not has_min_rank(ctx.author.id, min_rank):
        return await ctx.send(embed=denied(min_rank))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))
    cur = get_rank_db(uid)
    if cur == level:
        return await ctx.send(embed=error_embed(f"Déjà {label}", f"{format_user_display(display, uid)} est déjà **{label}**."))
    if cur > level:
        return await ctx.send(embed=error_embed("❌ Erreur", f"{format_user_display(display, uid)} a un rang supérieur."))
    if get_blacklist_entry(uid):
        return await ctx.send(embed=error_embed("❌ Blacklisté", f"{format_user_display(display, uid)} est blacklisté : retire d'abord la BL."))
    set_rank_db(uid, level)
    await ctx.send(embed=success_embed(f"✅ {label} ajouté", f"{format_user_display(display, uid)} a été ajouté en **{label}**."))
    await send_log(ctx.guild, f"{label} ajouté", ctx.author, display, uid, color=0x43b581)


async def _rank_remove(ctx, user_input, level, min_rank, label):
    if not has_min_rank(ctx.author.id, min_rank):
        return await ctx.send(embed=denied(min_rank))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention, ID ou nom requis."))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))
    if get_rank_db(uid) != level:
        return await ctx.send(embed=error_embed(f"Pas {label}", f"{format_user_display(display, uid)} n'est pas **{label}**."))
    set_rank_db(uid, 0)
    await ctx.send(embed=success_embed(f"✅ {label} retiré", f"{format_user_display(display, uid)} a été retiré des **{label}**."))
    await send_log(ctx.guild, f"{label} retiré", ctx.author, display, uid, color=0xfaa61a)


@bot.command(name="sys")
async def _sys(ctx, *, user_input: str = None):
    if user_input is None:
        return await _rank_list(ctx, 3, 4, "Liste Sys")
    await _rank_add(ctx, user_input, 3, 4, "Sys")


@bot.command(name="unsys")
async def _unsys(ctx, *, user_input: str = None):
    await _rank_remove(ctx, user_input, 3, 4, "Sys")


@bot.command(name="owner")
async def _owner(ctx, *, user_input: str = None):
    if user_input is None:
        return await _rank_list(ctx, 2, 3, "Liste Owner")
    await _rank_add(ctx, user_input, 2, 3, "Owner")


@bot.command(name="unowner")
async def _unowner(ctx, *, user_input: str = None):
    await _rank_remove(ctx, user_input, 2, 3, "Owner")


@bot.command(name="wl")
async def _wl(ctx, *, user_input: str = None):
    if user_input is None:
        return await _rank_list(ctx, 1, 2, "Whitelist")
    await _rank_add(ctx, user_input, 1, 2, "Whitelist")


@bot.command(name="unwl")
async def _unwl(ctx, *, user_input: str = None):
    await _rank_remove(ctx, user_input, 1, 2, "Whitelist")


# ========================= BLACKLIST =========================

def split_duration_reason(rest):
    """'7j raison...' -> (604800, 'raison...') ; 'raison...' -> (None, 'raison...')."""
    if not rest:
        return None, ""
    parts = rest.strip().split(maxsplit=1)
    secs = parse_duration(parts[0])
    if secs is not None:
        return secs, (parts[1] if len(parts) > 1 else "")
    return None, rest.strip()


def reason_error(cmd):
    return error_embed("❌ Raison requise",
                       f"Tu dois fournir une raison d'au moins **{REASON_MIN} caractères**.\n"
                       f"Usage : `{get_prefix_cached()}{cmd} @user [durée] <raison>`\n"
                       f"Durée optionnelle : `30m`, `12h`, `7j`, `2s` (semaines).")


async def _do_blacklist(ctx, user_input, rest, is_super):
    cmd = "superbl" if is_super else "bl"
    secs, reason = split_duration_reason(rest)
    if is_super and secs:
        return await ctx.send(embed=error_embed("❌ Super BL définitive", "La super blacklist ne peut pas être temporaire."))
    if len(reason) < REASON_MIN:
        return await ctx.send(embed=reason_error(cmd))
    reason = reason[:REASON_MAX]
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))
    if uid == ctx.author.id or (bot.user and uid == bot.user.id):
        return await ctx.send(embed=error_embed("❌ Impossible", "Cible invalide."))
    author_rank, target_rank = get_rank_db(ctx.author.id), get_rank_db(uid)
    if target_rank >= author_rank:
        return await ctx.send(embed=error_embed("❌ Permission refusée", f"Tu ne peux pas blacklist quelqu'un avec le rang **{rank_name(target_rank)}**."))
    entry = get_blacklist_entry(uid)
    if entry and (entry["is_super"] or not is_super):
        what = "super blacklisté" if entry["is_super"] else "blacklisté"
        return await ctx.send(embed=error_embed("Déjà BL", f"{format_user_display(display, uid)} est déjà {what} ({entry_expiry(entry)})."))
    # Limite vérifiée AVANT toute modification (l'ancien superbl retirait la BL avant ce test)
    err = check_limit(ctx.author.id)
    if err:
        return await ctx.send(embed=err)
    expires = int(time.time()) + secs if secs else None
    add_blacklist(uid, ctx.author.id, reason, 1 if is_super else 0, expires, ctx.guild.id)
    record_bl_action(ctx.author.id)
    add_history(uid, ctx.author.id, ("superbl" if is_super else "bl") + (f" {fmt_duration(secs)}" if secs else ""), reason)
    label = "Super blacklist" if is_super else ("Blacklist temporaire" if secs else "Blacklist")
    ok, failed = await ban_everywhere(uid, f"{label} par {ctx.author} | {reason}")
    lines = [f"{format_user_display(display, uid)} a été **{'super ' if is_super else ''}blacklisté**"
             + (f" pour **{fmt_duration(secs)}**" if secs else "") + ".",
             f"**Raison :** {reason}",
             f"**Ban :** {ok} serveur(s)" + (f" · échec sur {len(failed)}" if failed else ""),
             f"**Fin :** <t:{expires}:F> (<t:{expires}:R>)" if secs else "**Fin :** définitive"]
    if is_super:
        lines.append(f"Seul `{get_prefix_cached()}unsuperbl` (Sys+) peut la retirer.")
    await ctx.send(embed=success_embed("⛔ Super Blacklisté" if is_super else "✅ Blacklisté", "\n".join(lines)))
    await send_log(ctx.guild, label, ctx.author, display, uid, reason,
                   extra=f"Ban sur {ok} serveur(s)" + (f" · expire <t:{expires}:R>" if secs else ""))


async def _do_unblacklist(ctx, user_input, is_super):
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))
    entry = get_blacklist_entry(uid)
    if not entry or bool(entry["is_super"]) != is_super:
        if entry and entry["is_super"] and not is_super:
            return await ctx.send(embed=error_embed("❌ Super Blacklisté",
                                                    f"Cet utilisateur est **super blacklisté**. Utilise `{get_prefix_cached()}unsuperbl` (Sys+ requis)."))
        return await ctx.send(embed=error_embed("Pas BL" if not is_super else "Pas Super BL",
                                                f"{format_user_display(display, uid)} n'est pas {'super ' if is_super else ''}blacklisté."))
    adder_id, adder_rank = entry["added_by"], get_rank_db(entry["added_by"])
    my_rank = get_rank_db(ctx.author.id)
    if str(ctx.author.id) != str(adder_id) and my_rank <= adder_rank:
        return await ctx.send(embed=error_embed(
            "🔒 Blacklist protégée",
            f"{format_user_display(display, uid)} a été blacklisté par <@{adder_id}> (**{rank_name(adder_rank)}**).\n\n"
            f"Tu es **{rank_name(my_rank)}** : seul son auteur, ou un rang **strictement supérieur** à **{rank_name(adder_rank)}**, peut la retirer."))
    remove_blacklist(uid)
    add_history(uid, ctx.author.id, "unsuperbl" if is_super else "unbl")
    ok, kept = await unban_everywhere(uid, f"Unblacklist par {ctx.author}")
    desc = f"{format_user_display(display, uid)} retiré de la **{'super ' if is_super else ''}blacklist**.\n**Débanni de :** {ok} serveur(s)"
    if kept:
        desc += f"\n*Toujours banni sur {kept} serveur(s) pour une autre raison (ban manuel conservé).*"
    await ctx.send(embed=success_embed("✅ Super BL retiré" if is_super else "✅ Unblacklisté", desc))
    await send_log(ctx.guild, "Un-super-blacklist" if is_super else "Unblacklist", ctx.author, display, uid, color=0x43b581,
                   extra=f"Débanni de {ok} serveur(s)" + (f" · {kept} ban(s) manuel(s) conservé(s)" if kept else ""))


@bot.command(name="bl")
async def _bl(ctx, user_input: str = None, *, rest: str = None):
    if not has_min_rank(ctx.author.id, 1):
        return await ctx.send(embed=denied(1))
    if user_input is None:
        entries = get_all_blacklist(0)
        lines = [f"<@{e['user_id']}> (`{e['user_id']}`) — {entry_date(e)} · " + ("⏳ " + entry_expiry(e) if e.get("expires_at") else "définitive")
                 for e in entries]
        return await send_pager(ctx, f"📋 Blacklist ({len(entries)})", lines or ["Aucun utilisateur blacklisté."],
                                footer=f"{get_prefix_cached()}blinfo @user pour le détail")
    await _do_blacklist(ctx, user_input, rest, False)


@bot.command(name="unbl")
async def _unbl(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 1):
        return await ctx.send(embed=denied(1))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention, ID ou nom requis."))
    await _do_unblacklist(ctx, user_input, False)


@bot.command(name="superbl")
async def _superbl(ctx, user_input: str = None, *, rest: str = None):
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=denied(3))
    if user_input is None:
        entries = get_all_blacklist(1)
        lines = [f"<@{e['user_id']}> (`{e['user_id']}`) — {entry_date(e)} · par <@{e['added_by']}>" for e in entries]
        return await send_pager(ctx, f"⛔ Super Blacklist ({len(entries)})", lines or ["Aucun utilisateur super blacklisté."])
    # Une BL normale existante est "upgradée" : l'écriture remplace la ligne, rien n'est retiré avant le test de limite.
    await _do_blacklist(ctx, user_input, rest, True)


@bot.command(name="unsuperbl")
async def _unsuperbl(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=denied(3))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention, ID ou nom requis."))
    await _do_unblacklist(ctx, user_input, True)


@bot.command(name="blinfo")
async def _blinfo(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 1):
        return await ctx.send(embed=denied(1))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention, ID ou nom requis."))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))
    entry = get_blacklist_entry(uid)
    name = display.name if display else f"ID {uid} (hors serveur)"
    em = discord.Embed(title=f"📋 Infos — {name}", color=0xf04747 if entry else 0x43b581)
    if display:
        em.set_thumbnail(url=display.display_avatar.url)
    em.add_field(name="ID", value=f"`{uid}`", inline=True)
    em.add_field(name="Rang", value=rank_name(get_rank_db(uid)), inline=True)
    if display is not None:
        em.add_field(name="Compte créé", value=f"<t:{int(display.created_at.timestamp())}:R>", inline=True)
    if entry:
        em.add_field(name="Statut BL", value="⛔ Super Blacklisté" if entry["is_super"] else "🔨 Blacklisté", inline=True)
        em.add_field(name="Par", value=f"<@{entry['added_by']}>", inline=True)
        em.add_field(name="Date", value=entry_date(entry), inline=True)
        em.add_field(name="Fin", value=entry_expiry(entry), inline=True)
        em.add_field(name="Raison", value=(entry["reason"] or "Aucune raison fournie")[:1024], inline=False)
    else:
        em.add_field(name="Statut BL", value="✅ Clean", inline=False)
    n = db_one("SELECT COUNT(*) AS n FROM bl_history WHERE target_id = ?", (str(uid),))["n"]
    if n:
        em.set_footer(text=f"{n} entrée(s) d'historique · {get_prefix_cached()}bllog {uid}")
    await ctx.send(embed=em)


ACTION_LABELS = {"bl": "🔨 Blacklist", "superbl": "⛔ Super BL", "unbl": "✅ Unblacklist", "unsuperbl": "✅ Super BL retiré",
                 "expiration": "⏳ Expiration"}


@bot.command(name="bllog", aliases=["blhistory"])
async def _bllog(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 1):
        return await ctx.send(embed=denied(1))
    if not user_input:
        rows = db_all("SELECT * FROM bl_history ORDER BY id DESC LIMIT 100")
        title = "📜 Dernières actions de blacklist"
    else:
        display, uid = await resolve_user_or_id(ctx, user_input)
        if uid is None:
            return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))
        rows = db_all("SELECT * FROM bl_history WHERE target_id = ? ORDER BY id DESC LIMIT 100", (str(uid),))
        title = f"📜 Historique — {display.name if display else uid}"
    lines = []
    for r in rows:
        base = r["action"].split()[0]
        lab = ACTION_LABELS.get(base, r["action"]) + (f" ({r['action'].split(' ', 1)[1]})" if " " in r["action"] else "")
        line = f"<t:{r['ts']}:d> {lab} · <@{r['target_id']}> par <@{r['author_id']}>"
        if r["reason"]:
            line += f"\n> {r['reason'][:120]}"
        lines.append(line)
    await send_pager(ctx, title, lines or ["Aucun historique."], per_page=8)


@bot.command(name="blsync")
async def _blsync(ctx):
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=denied(3))
    msg = await ctx.send(embed=info_embed("⏳ Synchronisation…", f"Vérification de {len(bot.guilds)} serveur(s)."))
    total = 0
    for g in bot.guilds:
        total += await sync_guild(g)
    await msg.edit(embed=success_embed("🔄 Synchronisation terminée", f"**{total}** blacklisté(s) présent(s) ont été bannis."))
    await send_log(ctx.guild, "Synchronisation", ctx.author, ctx.author, ctx.author.id, extra=f"{total} ban(s)", color=0x3498db)


# ========================= MASSBAN =========================

class MassBanConfirmView(OwnedView):
    def __init__(self, author_id, ids, reason):
        super().__init__(author_id, timeout=60)
        self.ids, self.reason, self.done = ids, reason, False

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return await interaction.response.defer()
        self.done = True
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=info_embed("⏳ Ban en cours…", f"Traitement de **{len(self.ids)}** ID(s)."), view=self)
        success, failed = [], []
        guild = interaction.guild
        for uid in self.ids:
            if get_rank_db(uid) >= 1:
                failed.append(uid)  # on ne massban jamais un rang
                continue
            try:
                await guild.ban(discord.Object(id=uid), reason=f"{BAN_TAG} Massban | {self.reason}"[:512], delete_message_seconds=0)
                success.append(uid)
            except discord.HTTPException:
                failed.append(uid)
        desc = f"✅ Bannis : **{len(success)}**\n❌ Échecs : **{len(failed)}**"
        if failed:
            desc += "\n\n**IDs non bannis :**\n" + ", ".join(f"`{i}`" for i in failed[:20]) + (f" *(+{len(failed) - 20} autres)*" if len(failed) > 20 else "")
        await interaction.followup.send(embed=success_embed("✅ Massban terminé", desc))
        em = discord.Embed(title="📋 Massban", color=0xf04747, timestamp=datetime.now(timezone.utc))
        em.add_field(name="Modérateur", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
        em.add_field(name="Bannis / Total", value=f"{len(success)} / {len(self.ids)}", inline=True)
        em.add_field(name="Raison", value=self.reason[:1024], inline=False)
        await send_log_embed(guild, em)

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=error_embed("✖️ Annulé", "Massban annulé."), view=self)


@bot.command(name="massban")
async def _massban(ctx, *, args: str = None):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=denied(4))
    if not args:
        return await ctx.send(embed=error_embed("Argument manquant", f"Usage : `{get_prefix_cached()}massban id1 id2 id3 … | raison optionnelle`"))
    ids_part, reason = (args.split("|", 1) + [""])[:2] if "|" in args else (args, "")
    ids, invalid = [], []
    for tok in ids_part.replace(",", " ").split():
        cleaned = tok.strip("<@!>")
        (ids if cleaned.isdigit() else invalid).append(int(cleaned) if cleaned.isdigit() else tok)
    ids = list(dict.fromkeys(ids))
    if not ids:
        return await ctx.send(embed=error_embed("❌ Aucun ID valide", "Fournis au moins un ID numérique valide."))
    desc = f"Tu vas bannir **{len(ids)}** utilisateur(s) de ce serveur.\n"
    if reason.strip():
        desc += f"**Raison :** {reason.strip()[:300]}\n"
    if invalid:
        desc += f"\n⚠️ **{len(invalid)}** élément(s) ignoré(s) : {', '.join(map(str, invalid[:10]))}"
    desc += "\n\n*Les membres ayant un rang ne sont jamais bannis.*"
    view = MassBanConfirmView(ctx.author.id, ids, reason.strip() or "Massban")
    view.message = await ctx.send(embed=error_embed("⚠️ Confirmation requise", desc), view=view)


# ========================= ID (commande cachée) =========================

@bot.command(name="id")
async def _id(ctx):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=denied(4))
    guild = ctx.guild
    if not guild.chunked:
        try:
            await guild.chunk()
        except discord.HTTPException:
            pass
    ids = sorted(m.id for m in guild.members)
    await send_pager(ctx, f"🆔 IDs du serveur ({len(ids)})", [f"`{k + 1}.` <@{u}> — `{u}`" for k, u in enumerate(ids)] or ["Aucun membre trouvé."])


# ========================= ERREURS =========================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandInvokeError):
        error = error.original
    if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
        return
    if isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
        await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Impossible de trouver cet utilisateur."))
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send(embed=error_embed("❌ Salon introuvable", "Impossible de trouver ce salon."))
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=error_embed("❌ Argument manquant", f"Il te manque l'argument : `{error.param.name}`."))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=error_embed("❌ Argument invalide", str(error)[:300]))
    else:
        log.error(f"Erreur non gérée '{ctx.command}' par {ctx.author} : {error}\n"
                  + "".join(traceback.format_exception(type(error), error, error.__traceback__)))
        try:
            await ctx.send(embed=error_embed("❌ Erreur interne", "Une erreur inattendue est survenue. Les logs ont été générés."))
        except discord.HTTPException:
            pass


# ========================= RUN =========================
if __name__ == "__main__":
    try:
        log.info("Démarrage du bot BL...")
        bot.run(BOT_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        log.info("Arrêt demandé.")
    except Exception as e:
        log.error(f"Erreur fatale au démarrage : {e}", exc_info=True)
        sys.exit(1)
