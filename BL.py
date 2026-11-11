"""
================================================================================
  MOH — Bot de gestion
================================================================================
Hiérarchie : Buyer (12) > Owner (11) > Whitelist (10) > perm1..perm9 > Public (0)

  - Modération : ban (perm/temp), kick, mute, warn, clear, lock, slowmode...
  - Permissions par commande, par rôle ou par membre (niveaux configurables)
  - Logs par catégorie, snipe, historique des sanctions
  - Clone d'emojis depuis d'autres serveurs (+create)

Variables d'environnement : TOKEN_MOH (ou TOKEN), DATA_DIR
================================================================================
"""
import discord
from discord.ext import commands, tasks
import os
import sys
import re
import math
import time
import asyncio
import sqlite3
import logging
import traceback
import aiohttp
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ========================= CONFIG =========================
BOT_TOKEN = os.environ.get("TOKEN_MOH") or os.environ.get("TOKEN")
if not BOT_TOKEN:
    print("[ERREUR CRITIQUE] Aucune variable TOKEN_MOH ni TOKEN trouvée.")
    sys.exit(1)

# Buyer : hardcodé, niveau max, non modifiable en commande.
BUYER_IDS = [
    142365250803466240,
    1546586066407133308,
]

PARIS_TZ = ZoneInfo("Europe/Paris")
DEFAULT_PREFIX = "+"

DATA_DIR = os.environ.get("DATA_DIR")
if not DATA_DIR:
    print("[ERREUR CRITIQUE] DATA_DIR non défini. Configure DATA_DIR=/data dans Railway.")
    sys.exit(1)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "moh.db")

MAX_CLEAR = 100
YOUNG_ACCOUNT_DAYS = 7
MAX_EMOJI_BYTES = 256 * 1024
MAX_CREATE = 30               # emojis clonés max par commande

# Couleurs — reprises de la DA du bot BL
COLOR_DEFAULT = 0x2b2d31
COLOR_SUCCESS = 0x43b581
COLOR_ERROR = 0xf04747
COLOR_WARNING = 0xfaa61a
COLOR_INFO = 0x3498db

# ========================= NIVEAUX =========================
#   0 = Public · 1..9 = perm1..perm9 · 10 = WL · 11 = Owner · 12 = Buyer
LEVEL_PUBLIC = 0
MAX_TIER = 9
LEVEL_WL = 10
LEVEL_OWNER = 11
LEVEL_BUYER = 12

BUYER_RETIER_CMDS = {"owner", "unowner"}
DANGEROUS_PERMS = ("administrator", "manage_guild", "manage_roles", "ban_members",
                   "kick_members", "manage_channels", "manage_webhooks", "mention_everyone",
                   "moderate_members")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%d/%m/%Y %H:%M:%S")
log = logging.getLogger("moh")

_prefix_cache = {"value": None}


# ========================= CATALOGUE =========================
COMMANDS = {
    # Aide / permissions
    "help":      {"args": "",                     "desc": "Le menu d'aide",                   "cat": "perms", "level": 0,  "locked": True},
    "perms":     {"args": "",                     "desc": "Qui a quel niveau",                "cat": "perms", "level": 0,  "locked": True},
    "helpall":   {"args": "",                     "desc": "Les commandes par niveau",         "cat": "perms", "level": 0,  "locked": True},

    # Rangs
    "owner":     {"args": "[@u]",                 "desc": "Ajouter ou lister les owners",     "cat": "perms", "level": 12, "locked": False},
    "unowner":   {"args": "@u",                   "desc": "Retirer un owner",                 "cat": "perms", "level": 12, "locked": False},
    "wl":        {"args": "[@u]",                 "desc": "Ajouter ou lister la whitelist",   "cat": "perms", "level": 11, "locked": True},
    "unwl":      {"args": "@u",                   "desc": "Retirer de la whitelist",          "cat": "perms", "level": 11, "locked": True},
    "setperm":   {"args": "<cmd|@rôle|@u> <niv>", "desc": "Définir un niveau d'accès",        "cat": "perms", "level": 11, "locked": True},
    "resetperm": {"args": "<cmd|@rôle|@u>",       "desc": "Remettre un niveau par défaut",    "cat": "perms", "level": 11, "locked": True},
    "botban":    {"args": "@u",                   "desc": "Bloquer l'accès au bot",           "cat": "perms", "level": 11, "locked": True},
    "unbotban":  {"args": "@u",                   "desc": "Débloquer l'accès au bot",         "cat": "perms", "level": 11, "locked": True},

    # Configuration
    "prefix":    {"args": "[nouveau]",            "desc": "Changer le prefix",                "cat": "config", "level": 12, "locked": True},
    "allow":     {"args": "[#salon]",             "desc": "Réautoriser les commandes publiques", "cat": "config", "level": 11, "locked": True},
    "unallow":   {"args": "#salon",               "desc": "Bloquer les commandes publiques",  "cat": "config", "level": 11, "locked": True},
    "setlog":    {"args": "<type> #salon",        "desc": "Définir un salon de logs",         "cat": "config", "level": 11, "locked": True},
    "dellog":    {"args": "<type>",               "desc": "Retirer un salon de logs",         "cat": "config", "level": 11, "locked": True},
    "logs":      {"args": "",                     "desc": "Voir la config des logs",          "cat": "config", "level": 11, "locked": True},
    "renew":     {"args": "",                     "desc": "Recréer le salon",                 "cat": "config", "level": 12, "locked": True},

    # Infos
    "userinfo":  {"args": "[@u]",                 "desc": "Les infos d'un membre",            "cat": "infos", "level": 0,  "locked": False},
    "serverinfo":{"args": "",                     "desc": "Les infos du serveur",             "cat": "infos", "level": 0,  "locked": False},
    "avatar":    {"args": "[@u]",                 "desc": "L'avatar d'un membre",             "cat": "infos", "level": 0,  "locked": False},
    "roleinfo":  {"args": "@rôle",                "desc": "Les infos d'un rôle",              "cat": "infos", "level": 0,  "locked": False},
    "ping":      {"args": "",                     "desc": "La latence du bot",                "cat": "infos", "level": 0,  "locked": False},

    # Modération
    "ban":       {"args": "@u [durée] [raison]",  "desc": "Bannir (perm ou temporaire)",      "cat": "moderation", "level": 11, "locked": False},
    "unban":     {"args": "<id> [raison]",        "desc": "Débannir",                         "cat": "moderation", "level": 11, "locked": False},
    "kick":      {"args": "@u [raison]",          "desc": "Expulser",                         "cat": "moderation", "level": 11, "locked": False},
    "mute":      {"args": "@u <durée> [raison]",  "desc": "Mettre en timeout",                "cat": "moderation", "level": 11, "locked": False},
    "unmute":    {"args": "@u",                   "desc": "Retirer le timeout",               "cat": "moderation", "level": 11, "locked": False},
    "warn":      {"args": "@u <raison>",          "desc": "Avertir",                          "cat": "moderation", "level": 11, "locked": False},
    "warns":     {"args": "[@u]",                 "desc": "Voir les avertissements",          "cat": "moderation", "level": 11, "locked": False},
    "delwarn":   {"args": "<id>",                 "desc": "Supprimer un avertissement",       "cat": "moderation", "level": 11, "locked": False},
    "clearwarns":{"args": "@u",                   "desc": "Effacer tous les avertissements",  "cat": "moderation", "level": 11, "locked": False},
    "softban":   {"args": "@u [raison]",          "desc": "Bannir puis débannir (nettoie)",   "cat": "moderation", "level": 11, "locked": False},
    "clear":     {"args": "[@u] <n>",             "desc": "Supprimer des messages",           "cat": "moderation", "level": 11, "locked": False},
    "lock":      {"args": "[#salon]",             "desc": "Verrouiller un salon",             "cat": "moderation", "level": 11, "locked": False},
    "unlock":    {"args": "[#salon]",             "desc": "Déverrouiller un salon",           "cat": "moderation", "level": 11, "locked": False},
    "slowmode":  {"args": "<durée>",              "desc": "Régler le slowmode",               "cat": "moderation", "level": 11, "locked": False},
    "banlist":   {"args": "",                     "desc": "Lister les bannis",                "cat": "moderation", "level": 11, "locked": False},
    "snipe":     {"args": "",                     "desc": "Le dernier message supprimé",      "cat": "moderation", "level": 11, "locked": False},
    "history":   {"args": "[@u]",                 "desc": "L'historique des sanctions",       "cat": "moderation", "level": 11, "locked": False},
    "case":      {"args": "<id>",                 "desc": "Le détail d'une sanction",         "cat": "moderation", "level": 11, "locked": False},
    "reason":    {"args": "<id> <raison>",        "desc": "Modifier la raison d'une sanction","cat": "moderation", "level": 11, "locked": False},

    # Outils
    "role":      {"args": "@u @rôle",             "desc": "Ajouter ou retirer un rôle",       "cat": "outils", "level": 11, "locked": False},
    "addrole":   {"args": "@rôle @u...",          "desc": "Donner un rôle à plusieurs membres","cat": "outils", "level": 11, "locked": False},
    "delrole":   {"args": "@rôle @u...",          "desc": "Retirer un rôle à plusieurs membres","cat": "outils", "level": 11, "locked": False},
    "roleall":   {"args": "@rôle",                "desc": "Donner un rôle à tout le monde",   "cat": "outils", "level": 11, "locked": False},
    "nick":      {"args": "@u [pseudo]",          "desc": "Changer le pseudo",                "cat": "outils", "level": 11, "locked": False},
    "derank":    {"args": "@u",                   "desc": "Retirer tous les rôles",           "cat": "outils", "level": 11, "locked": False},
    "say":       {"args": "<message>",            "desc": "Faire parler le bot",              "cat": "outils", "level": 11, "locked": False},
    "dm":        {"args": "@u <message>",         "desc": "Envoyer un message privé",         "cat": "outils", "level": 11, "locked": False},
    "embed":     {"args": "<titre | message>",    "desc": "Envoyer un embed",                 "cat": "outils", "level": 11, "locked": False},
    "create":    {"args": "<emojis...>",          "desc": "Cloner des emojis sur le serveur", "cat": "outils", "level": 11, "locked": False},
    "delemoji":  {"args": "<emoji>",              "desc": "Supprimer un emoji du serveur",    "cat": "outils", "level": 11, "locked": False},
}

CATEGORY_META = {
    "moderation": {"emoji": "⚖️", "label": "Modération",    "sub": "Sanctionner et garder le serveur propre."},
    "infos":      {"emoji": "📁", "label": "Infos",         "sub": "Les infos des membres, rôles et du serveur."},
    "outils":     {"emoji": "🧰", "label": "Outils",        "sub": "Gérer les rôles, les emojis et le staff."},
    "config":     {"emoji": "⚙️", "label": "Configuration", "sub": "Régler le bot pour ton serveur."},
    "perms":      {"emoji": "👥", "label": "Permissions",   "sub": "Gérer qui a accès à quoi."},
}

CATEGORY_GROUPS = (
    ("Gestion", ["moderation", "outils"]),
    ("Infos & admin", ["infos", "config", "perms"]),
)

LOG_TYPES = ["moderation", "messages", "members", "voice", "roles", "commands"]


# ========================= DATABASE =========================
# Connexion partagée unique, en autocommit (le bot tourne sur un seul thread).

_DB = None


def get_db():
    global _DB
    if _DB is None:
        _DB = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False, isolation_level=None)
        _DB.row_factory = sqlite3.Row
        _DB.execute("PRAGMA journal_mode=WAL")
        _DB.execute("PRAGMA foreign_keys=ON")
    return _DB


def db_exec(sql, params=()):
    return get_db().execute(sql, params)


def db_one(sql, params=()):
    return db_exec(sql, params).fetchone()


def db_all(sql, params=()):
    return db_exec(sql, params).fetchall()


def init_db():
    get_db().executescript("""
        CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS ranks (user_id TEXT PRIMARY KEY, rank INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS role_perms (
            guild_id TEXT NOT NULL, role_id TEXT NOT NULL, level INTEGER NOT NULL,
            PRIMARY KEY (guild_id, role_id));
        CREATE TABLE IF NOT EXISTS member_perms (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, level INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id));
        CREATE TABLE IF NOT EXISTS cmd_perms (
            guild_id TEXT NOT NULL, command TEXT NOT NULL, level INTEGER NOT NULL,
            PRIMARY KEY (guild_id, command));
        CREATE TABLE IF NOT EXISTS blocked_channels (
            guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            PRIMARY KEY (guild_id, channel_id));
        CREATE TABLE IF NOT EXISTS bot_bans (user_id TEXT PRIMARY KEY, banned_by TEXT, banned_at TEXT);
        CREATE TABLE IF NOT EXISTS log_channels (
            guild_id TEXT NOT NULL, log_type TEXT NOT NULL, channel_id TEXT NOT NULL,
            PRIMARY KEY (guild_id, log_type));
        CREATE TABLE IF NOT EXISTS sanctions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, moderator_id TEXT NOT NULL,
            type TEXT NOT NULL, reason TEXT, duration INTEGER,
            timestamp TEXT NOT NULL, active INTEGER DEFAULT 1);
        CREATE INDEX IF NOT EXISTS idx_sanctions_user ON sanctions(guild_id, user_id);
        CREATE INDEX IF NOT EXISTS idx_sanctions_active ON sanctions(active, type);
    """)
    db_exec("INSERT OR IGNORE INTO config VALUES ('prefix', ?)", (DEFAULT_PREFIX,))


# ---- Config ----

def get_config(key):
    row = db_one("SELECT value FROM config WHERE key = ?", (key,))
    return row["value"] if row else None


def set_config(key, value):
    db_exec("INSERT OR REPLACE INTO config VALUES (?, ?)", (key, str(value)))
    if key == "prefix":
        _prefix_cache["value"] = str(value)


def get_prefix_cached():
    if _prefix_cache["value"] is None:
        _prefix_cache["value"] = get_config("prefix") or DEFAULT_PREFIX
    return _prefix_cache["value"]


# ---- Rangs de base (WL / Owner) ----

def get_base_rank(user_id):
    if int(user_id) in BUYER_IDS:
        return LEVEL_BUYER
    row = db_one("SELECT rank FROM ranks WHERE user_id = ?", (str(user_id),))
    return row["rank"] if row else 0


def set_base_rank(user_id, rank):
    if rank == 0:
        db_exec("DELETE FROM ranks WHERE user_id = ?", (str(user_id),))
    else:
        db_exec("INSERT OR REPLACE INTO ranks VALUES (?, ?)", (str(user_id), rank))


def get_users_by_rank(rank):
    return [r["user_id"] for r in db_all("SELECT user_id FROM ranks WHERE rank = ?", (rank,))]


# ---- Perms rôle (cache mémoire par serveur) ----

_role_perm_cache = {}


def _guild_role_perms(guild_id):
    gid = int(guild_id)
    if gid not in _role_perm_cache:
        rows = db_all("SELECT role_id, level FROM role_perms WHERE guild_id = ?", (str(gid),))
        _role_perm_cache[gid] = {int(r["role_id"]): r["level"] for r in rows}
    return _role_perm_cache[gid]


def set_role_perm(guild_id, role_id, level):
    if level == 0:
        db_exec("DELETE FROM role_perms WHERE guild_id = ? AND role_id = ?", (str(guild_id), str(role_id)))
    else:
        db_exec("INSERT OR REPLACE INTO role_perms VALUES (?, ?, ?)", (str(guild_id), str(role_id), level))
    _role_perm_cache.pop(int(guild_id), None)


def get_role_perm(guild_id, role_id):
    return _guild_role_perms(guild_id).get(int(role_id))


def del_role_perm(guild_id, role_id):
    n = db_exec("DELETE FROM role_perms WHERE guild_id = ? AND role_id = ?",
                (str(guild_id), str(role_id))).rowcount
    _role_perm_cache.pop(int(guild_id), None)
    return n > 0


# ---- Perms membre ----

def set_member_perm(guild_id, user_id, level):
    if level == 0:
        db_exec("DELETE FROM member_perms WHERE guild_id = ? AND user_id = ?", (str(guild_id), str(user_id)))
    else:
        db_exec("INSERT OR REPLACE INTO member_perms VALUES (?, ?, ?)", (str(guild_id), str(user_id), level))


def get_member_perm(guild_id, user_id):
    row = db_one("SELECT level FROM member_perms WHERE guild_id = ? AND user_id = ?",
                 (str(guild_id), str(user_id)))
    return row["level"] if row else None


def del_member_perm(guild_id, user_id):
    return db_exec("DELETE FROM member_perms WHERE guild_id = ? AND user_id = ?",
                   (str(guild_id), str(user_id))).rowcount > 0


def get_perms_at_level(guild_id, level):
    roles = [str(rid) for rid, lvl in _guild_role_perms(guild_id).items() if lvl == level]
    users = [r["user_id"] for r in db_all(
        "SELECT user_id FROM member_perms WHERE guild_id = ? AND level = ?", (str(guild_id), level))]
    return roles, users


# ---- Override niveau commande ----

def set_cmd_level(guild_id, command, level):
    db_exec("INSERT OR REPLACE INTO cmd_perms VALUES (?, ?, ?)", (str(guild_id), command, level))


def get_cmd_override(guild_id, command):
    row = db_one("SELECT level FROM cmd_perms WHERE guild_id = ? AND command = ?", (str(guild_id), command))
    return row["level"] if row else None


def del_cmd_override(guild_id, command):
    return db_exec("DELETE FROM cmd_perms WHERE guild_id = ? AND command = ?",
                   (str(guild_id), command)).rowcount > 0


# ---- Bot bans ----

def is_bot_banned(user_id):
    if int(user_id) in BUYER_IDS:
        return False
    return db_one("SELECT 1 FROM bot_bans WHERE user_id = ?", (str(user_id),)) is not None


def add_bot_ban(user_id, banned_by):
    now = datetime.now(PARIS_TZ).strftime("%d/%m/%Y %Hh%M")
    db_exec("INSERT OR REPLACE INTO bot_bans VALUES (?, ?, ?)", (str(user_id), str(banned_by), now))


def remove_bot_ban(user_id):
    db_exec("DELETE FROM bot_bans WHERE user_id = ?", (str(user_id),))


# ---- Blocked channels ----

def block_channel(guild_id, channel_id):
    db_exec("INSERT OR IGNORE INTO blocked_channels VALUES (?, ?)", (str(guild_id), str(channel_id)))


def unblock_channel(guild_id, channel_id):
    return db_exec("DELETE FROM blocked_channels WHERE guild_id = ? AND channel_id = ?",
                   (str(guild_id), str(channel_id))).rowcount > 0


def is_channel_blocked(guild_id, channel_id):
    return db_one("SELECT 1 FROM blocked_channels WHERE guild_id = ? AND channel_id = ? LIMIT 1",
                  (str(guild_id), str(channel_id))) is not None


def get_blocked_channels(guild_id):
    return [r["channel_id"] for r in db_all(
        "SELECT channel_id FROM blocked_channels WHERE guild_id = ?", (str(guild_id),))]


# ---- Log channels ----

def get_log_channel(guild_id, log_type):
    row = db_one("SELECT channel_id FROM log_channels WHERE guild_id = ? AND log_type = ?",
                 (str(guild_id), log_type))
    return row["channel_id"] if row else None


def set_log_channel(guild_id, log_type, channel_id):
    db_exec("INSERT OR REPLACE INTO log_channels VALUES (?, ?, ?)", (str(guild_id), log_type, str(channel_id)))


def delete_log_channel(guild_id, log_type):
    db_exec("DELETE FROM log_channels WHERE guild_id = ? AND log_type = ?", (str(guild_id), log_type))


def get_all_log_channels(guild_id):
    return {r["log_type"]: r["channel_id"] for r in db_all(
        "SELECT log_type, channel_id FROM log_channels WHERE guild_id = ?", (str(guild_id),))}


# ---- Sanctions ----

def add_sanction(guild_id, user_id, moderator_id, type_, reason=None, duration=None):
    now = datetime.now(PARIS_TZ).isoformat()
    cur = db_exec(
        "INSERT INTO sanctions (guild_id, user_id, moderator_id, type, reason, duration, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(guild_id), str(user_id), str(moderator_id), type_, reason, duration, now))
    return cur.lastrowid


def get_sanction(guild_id, sanction_id):
    row = db_one("SELECT * FROM sanctions WHERE id = ? AND guild_id = ?", (sanction_id, str(guild_id)))
    return dict(row) if row else None


def update_sanction_reason(guild_id, sanction_id, reason):
    return db_exec("UPDATE sanctions SET reason = ? WHERE id = ? AND guild_id = ?",
                   (reason, sanction_id, str(guild_id))).rowcount > 0


def deactivate_user_sanctions(guild_id, user_id, types):
    marks = ",".join("?" * len(types))
    return db_exec(
        f"UPDATE sanctions SET active = 0 WHERE guild_id = ? AND user_id = ? AND type IN ({marks}) AND active = 1",
        (str(guild_id), str(user_id), *types)).rowcount


def get_warns(guild_id, user_id):
    rows = db_all(
        "SELECT id, moderator_id, reason, timestamp FROM sanctions "
        "WHERE guild_id = ? AND user_id = ? AND type = 'warn' AND active = 1 "
        "ORDER BY id DESC LIMIT 25", (str(guild_id), str(user_id)))
    return [dict(r) for r in rows]


def delete_warn(guild_id, warn_id):
    return db_exec(
        "UPDATE sanctions SET active = 0 WHERE id = ? AND guild_id = ? AND type = 'warn' AND active = 1",
        (warn_id, str(guild_id))).rowcount > 0


def get_active_tempbans():
    return [dict(r) for r in db_all("SELECT * FROM sanctions WHERE type = 'tempban' AND active = 1")]


def deactivate_sanction(sanction_id):
    db_exec("UPDATE sanctions SET active = 0 WHERE id = ?", (sanction_id,))


def get_history(guild_id, user_id, limit=50):
    return [dict(r) for r in db_all(
        "SELECT * FROM sanctions WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
        (str(guild_id), str(user_id), limit))]


# ========================= NIVEAUX : HELPERS =========================

def level_name(lvl):
    if lvl >= LEVEL_BUYER:
        return "Buyer"
    if lvl == LEVEL_OWNER:
        return "Owner"
    if lvl == LEVEL_WL:
        return "Whitelist"
    if 1 <= lvl <= MAX_TIER:
        return f"perm{lvl}"
    return "Public"


def parse_level(s, max_level=MAX_TIER):
    if s is None:
        return None
    s = str(s).strip().lower()
    if s in ("public", "membre", "membres", "pub"):
        return 0
    if s == "wl":
        return LEVEL_WL if max_level >= LEVEL_WL else None
    if s == "owner":
        return LEVEL_OWNER if max_level >= LEVEL_OWNER else None
    if s == "buyer":
        return None
    m = re.match(r"^perm\s*(\d)$", s)
    if m:
        n = int(m.group(1))
        return n if 0 <= n <= min(max_level, MAX_TIER) else None
    if s.isdigit():
        n = int(s)
        return n if 0 <= n <= max_level else None
    return None


def get_user_level(member):
    if member.id in BUYER_IDS:
        return LEVEL_BUYER
    lvl = get_base_rank(member.id)
    guild = getattr(member, "guild", None)
    if guild is None:
        return lvl
    mp = get_member_perm(guild.id, member.id)
    if mp is not None:
        lvl = max(lvl, mp)
    rp = _guild_role_perms(guild.id)
    for r in getattr(member, "roles", []):
        lvl = max(lvl, rp.get(r.id, 0))
    return lvl


def get_command_level(guild_id, command_name):
    meta = COMMANDS.get(command_name)
    if not meta:
        return LEVEL_OWNER
    if meta["locked"]:
        return meta["level"]
    ov = get_cmd_override(guild_id, command_name)
    return ov if ov is not None else meta["level"]


def is_owner_plus(member):
    return get_user_level(member) >= LEVEL_OWNER


def check_target(ctx, target, uid=None):
    """Renvoie un message d'erreur, ou None si l'action est autorisée."""
    uid = target.id if target is not None else uid
    if uid == ctx.author.id:
        return "Tu ne peux pas faire ça sur toi-même."
    if bot.user and uid == bot.user.id:
        return "Je ne vais pas faire ça sur moi-même."
    if uid in BUYER_IDS:
        return "Impossible sur un Buyer."
    author_lvl = get_user_level(ctx.author)
    target_lvl = get_user_level(target) if isinstance(target, discord.Member) else get_base_rank(uid)
    if target_lvl >= author_lvl:
        return f"La cible a un niveau égal ou supérieur au tien ({level_name(target_lvl)})."
    if isinstance(target, discord.Member):
        if target.id == ctx.guild.owner_id:
            return "Impossible sur le propriétaire du serveur."
        if target.top_role >= ctx.guild.me.top_role:
            return "Ce membre a un rôle au-dessus ou égal au mien."
        if (target.top_role >= ctx.author.top_role and author_lvl < LEVEL_OWNER
                and ctx.author.id != ctx.guild.owner_id):
            return "Ce membre a un rôle égal ou supérieur au tien."
    return None


# ========================= EMBEDS (DA du bot BL) =========================

def trunc(text, limit):
    text = str(text) if text is not None else ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def apply_thumbnail(em, guild):
    if guild and getattr(guild, "icon", None):
        try:
            em.set_thumbnail(url=guild.icon.url)
        except (AttributeError, TypeError):
            pass


def info_embed(title, desc=""):
    return discord.Embed(title=title, description=trunc(desc, 4000), color=COLOR_DEFAULT)


def success_embed(title, desc=""):
    return discord.Embed(title=title, description=trunc(desc, 4000), color=COLOR_SUCCESS)


def error_embed(title, desc=""):
    return discord.Embed(title=title, description=trunc(desc, 4000), color=COLOR_ERROR)


def warning_embed(title, desc=""):
    return discord.Embed(title=title, description=trunc(desc, 4000), color=COLOR_WARNING)


def denied():
    return error_embed("Permission refusée", "Tu n'as pas le niveau requis pour cette commande.")


def mod_embed(action, target_obj, target_id, moderator, reason=None, duration=None, case_id=None, color=COLOR_ERROR):
    em = discord.Embed(title=action, color=color, timestamp=datetime.now(timezone.utc))
    if target_obj is not None:
        em.add_field(name="Membre", value=f"{target_obj.mention} (`{target_obj.id}`)", inline=True)
        try:
            em.set_thumbnail(url=target_obj.display_avatar.url)
        except Exception:
            pass
    else:
        em.add_field(name="Membre", value=f"<@{target_id}> (`{target_id}`)", inline=True)
    em.add_field(name="Modérateur", value=moderator.mention, inline=True)
    if duration:
        em.add_field(name="Durée", value=duration, inline=True)
    em.add_field(name="Raison", value=trunc(reason, 1024) if reason else "Aucune raison fournie", inline=False)
    if case_id is not None:
        em.set_footer(text=f"Sanction #{case_id}")
    return em


# ========================= DURÉES =========================

def get_french_time():
    now = datetime.now(PARIS_TZ)
    JOURS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    MOIS = ["janvier", "février", "mars", "avril", "mai", "juin",
            "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
    return f"{JOURS[now.weekday()]} {now.day} {MOIS[now.month-1]} {now.year} — {now.strftime('%Hh%M')}"


def format_duration_fr(seconds):
    if not seconds or seconds <= 0:
        return "0 sec"
    if seconds < 60:
        return f"{int(seconds)} sec"
    units = [(86400*365, "an", "ans"), (86400, "jour", "jours"), (3600, "heure", "heures"),
             (60, "minute", "minutes"), (1, "seconde", "secondes")]
    parts, rem = [], int(seconds)
    for sec, sing, plur in units:
        n, rem = divmod(rem, sec)
        if n > 0:
            parts.append(f"{n} {sing if n == 1 else plur}")
        if len(parts) >= 2:
            break
    return ", ".join(parts) if parts else "0 sec"


DURATION_RE = re.compile(r"(\d+)\s*([smhdjw])", re.IGNORECASE)
DURATION_FULL_RE = re.compile(r"^(\d+[smhdjw])+$", re.IGNORECASE)


def parse_duration(s):
    if not s:
        return None
    s = s.strip().lower().replace(" ", "")
    if s.isdigit():
        return int(s) * 60
    if not DURATION_FULL_RE.match(s):
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "j": 86400, "w": 604800}
    return sum(int(m.group(1)) * units[m.group(2)] for m in DURATION_RE.finditer(s))


def parse_id(raw):
    try:
        return int(str(raw).strip().strip("<@!&#>"))
    except (ValueError, TypeError):
        return None


async def resolve_member(ctx, user_input):
    if not user_input:
        return None
    mid = parse_id(user_input)
    if mid is not None:
        m = ctx.guild.get_member(mid)
        if m:
            return m
    try:
        return await commands.MemberConverter().convert(ctx, str(user_input))
    except commands.CommandError:
        return None


async def resolve_user_or_id(ctx, user_input):
    if not user_input:
        return None, None
    raw = str(user_input).strip()
    uid = parse_id(raw)
    if uid is None:
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
        member = ctx.guild.get_member(uid)
        if member:
            return member, uid
    try:
        return await bot.fetch_user(uid), uid
    except discord.NotFound:
        return None, None
    except discord.HTTPException:
        return None, uid


async def resolve_channel(ctx, channel_input):
    cid = parse_id(channel_input)
    if cid is not None:
        ch = ctx.guild.get_channel(cid)
        if ch:
            return ch, cid
    try:
        ch = await commands.TextChannelConverter().convert(ctx, str(channel_input))
        return ch, ch.id
    except commands.CommandError:
        return None, cid


def format_user_display(display_obj, user_id):
    if display_obj is not None:
        return f"{display_obj.mention} (`{display_obj.id}`)"
    return f"<@{user_id}> (`{user_id}`) *(hors serveur)*"


async def safe_dm(user, embed):
    try:
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return False


# ========================= BOT SETUP =========================

init_db()
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True


def get_prefix(bot_, message):
    return commands.when_mentioned_or(get_prefix_cached())(bot_, message)


bot = commands.Bot(
    command_prefix=get_prefix,
    intents=intents,
    help_command=None,
    case_insensitive=True,
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
)


# ========================= VUES =========================

class OwnedView(discord.ui.View):
    def __init__(self, author_id, guild=None, timeout=120):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.guild = guild
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                f"Ce menu n'est pas à toi. Fais `{get_prefix_cached()}help` pour voir le tien.", ephemeral=True)
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
    def __init__(self, author_id, guild, title, lines, per_page=10, footer=None, color=COLOR_DEFAULT):
        super().__init__(author_id, guild, timeout=180)
        self.title, self.lines, self.per_page, self.footer, self.color = title, lines, per_page, footer, color
        self.page = 0
        self.pages = max(1, math.ceil(len(lines) / per_page))
        self._sync()

    def _sync(self):
        self.prev.disabled = self.page <= 0
        self.next.disabled = self.page >= self.pages - 1

    def embed(self):
        chunk = self.lines[self.page * self.per_page:(self.page + 1) * self.per_page]
        em = discord.Embed(title=self.title, description=trunc("\n".join(chunk), 4000) or "Rien à afficher.", color=self.color)
        foot = f"Page {self.page + 1}/{self.pages}" + (f" · {self.footer}" if self.footer else "")
        em.set_footer(text=foot)
        apply_thumbnail(em, self.guild)
        return em

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction, button):
        self.page = max(0, self.page - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page = min(self.pages - 1, self.page + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)


async def send_pager(ctx, title, lines, per_page=10, footer=None, color=COLOR_DEFAULT):
    view = PagerView(ctx.author.id, ctx.guild, title, lines, per_page, footer, color)
    view.message = await ctx.send(embed=view.embed(), view=view if view.pages > 1 else None)
    return view.message


class ConfirmView(OwnedView):
    def __init__(self, author_id, timeout=30):
        super().__init__(author_id, timeout=timeout)
        self.value = None

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.danger)
    async def confirm_b(self, interaction, button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel_b(self, interaction, button):
        self.value = False
        await interaction.response.defer()
        self.stop()


# ========================= GLOBAL CHECK =========================

class BotBannedError(commands.CheckFailure):
    pass


class ChannelBlockedError(commands.CheckFailure):
    pass


class PermLevelError(commands.CheckFailure):
    def __init__(self, required):
        super().__init__()
        self.required = required


@bot.check
async def global_check(ctx):
    if ctx.command is None:
        return True
    if is_bot_banned(ctx.author.id):
        raise BotBannedError()
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    level = get_user_level(ctx.author)
    required = get_command_level(ctx.guild.id, ctx.command.name)
    if level < LEVEL_WL and is_channel_blocked(ctx.guild.id, ctx.channel.id):
        raise ChannelBlockedError()
    if level < required:
        raise PermLevelError(required)
    return True


# ========================= EVENTS =========================

@bot.event
async def on_ready():
    log.info(f"Moh connecté : {bot.user} ({bot.user.id}) sur {len(bot.guilds)} serveur(s)")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.playing, name="au stake fils de hechek"))
    if not tempban_loop.is_running():
        tempban_loop.start()


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandInvokeError):
        error = error.original
    if isinstance(error, BotBannedError):
        try:
            await ctx.send(embed=error_embed("Accès refusé", "Tu es banni du bot."))
        except discord.HTTPException:
            pass
        return
    if isinstance(error, (ChannelBlockedError, PermLevelError)):
        try:
            await ctx.message.add_reaction("🔇" if isinstance(error, ChannelBlockedError) else "⛔")
        except discord.HTTPException:
            pass
        return
    if isinstance(error, (commands.CommandNotFound, commands.NoPrivateMessage)):
        return
    try:
        if isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
            await ctx.send(embed=error_embed("Utilisateur introuvable", "Impossible de trouver cet utilisateur."))
        elif isinstance(error, commands.ChannelNotFound):
            await ctx.send(embed=error_embed("Salon introuvable", "Ce salon n'existe pas."))
        elif isinstance(error, commands.RoleNotFound):
            await ctx.send(embed=error_embed("Rôle introuvable", "Ce rôle n'existe pas."))
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(embed=error_embed("Argument manquant", f"Il te manque l'argument `{error.param.name}`."))
        elif isinstance(error, commands.BadArgument):
            await ctx.send(embed=error_embed("Argument invalide", str(error)[:300]))
        elif isinstance(error, discord.Forbidden):
            await ctx.send(embed=error_embed("Permissions Discord insuffisantes",
                                             "Il me manque une permission Discord pour faire ça."))
        else:
            log.error(f"Erreur '{ctx.command}' par {ctx.author} : {error}\n"
                      + "".join(traceback.format_exception(type(error), error, error.__traceback__)))
            await ctx.send(embed=error_embed("Erreur interne", "Une erreur inattendue est survenue."))
    except discord.HTTPException:
        pass


@bot.event
async def on_command_completion(ctx):
    if ctx.guild is None or ctx.command is None:
        return
    await send_log(ctx.guild, "commands", f"Commande · {get_prefix_cached()}{ctx.command.qualified_name}",
                   author=ctx.author,
                   desc=f"**Salon :** {ctx.channel.mention}\n**Message :** {trunc(ctx.message.content, 1000)}",
                   color=COLOR_DEFAULT)


# ========================= LOGS =========================

async def send_log(guild, log_type, title, *, author=None, target=None, desc=None, color=None, fields=None):
    channel_id = get_log_channel(guild.id, log_type)
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if not channel:
        return
    em = discord.Embed(title=trunc(title, 256), color=color if color is not None else COLOR_DEFAULT,
                       timestamp=datetime.now(timezone.utc))
    if author:
        em.add_field(name="Auteur", value=f"{author.mention} (`{author.id}`)", inline=True)
    if target:
        em.add_field(name="Membre", value=f"{target.mention} (`{target.id}`)", inline=True)
    if desc:
        em.description = trunc(desc, 4000)
    if fields:
        for name, value, inline in fields:
            em.add_field(name=name, value=trunc(value, 1024), inline=inline)
    try:
        await channel.send(embed=em, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        log.warning(f"send_log: {e}")


# ========================= TEMPBAN LOOP =========================

@tasks.loop(minutes=1)
async def tempban_loop():
    try:
        tempbans = get_active_tempbans()
        if not tempbans:
            return
        now = datetime.now(PARIS_TZ)
        for s in tempbans:
            if not s.get("duration"):
                continue
            try:
                start = datetime.fromisoformat(s["timestamp"])
            except (ValueError, TypeError):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=PARIS_TZ)
            if start + timedelta(seconds=int(s["duration"])) > now:
                continue
            guild = bot.get_guild(int(s["guild_id"]))
            if not guild:
                continue
            try:
                await guild.unban(discord.Object(id=int(s["user_id"])), reason="Tempban expiré")
                deactivate_sanction(s["id"])
                await send_log(guild, "moderation", "Tempban expiré",
                               desc=f"**Membre :** <@{s['user_id']}>\n**Sanction :** `#{s['id']}`",
                               color=COLOR_INFO)
            except discord.NotFound:
                deactivate_sanction(s["id"])
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"tempban unban {s['id']}: {e}")
    except Exception as e:
        log.error(f"tempban_loop: {e}", exc_info=True)


@tempban_loop.before_loop
async def _bef_tempban():
    await bot.wait_until_ready()


# ========================= MODÉRATION =========================

@bot.command(name="ban")
async def _ban(ctx, user_input: str = None, *, args: str = None):
    if not user_input:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}ban @user [durée] [raison]`"))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("Utilisateur introuvable"))
    duration_seconds = None
    reason = args or "Aucune raison fournie"
    if args:
        first, _, rest = args.partition(" ")
        d = parse_duration(first)
        if d is not None and d > 0:
            duration_seconds = d
            reason = rest.strip() or "Aucune raison fournie"
    member = ctx.guild.get_member(uid)
    err = check_target(ctx, member, uid)
    if err:
        return await ctx.send(embed=error_embed("Action impossible", err))
    if member:
        await safe_dm(member, error_embed(
            f"Tu as été banni de {ctx.guild.name}",
            f"**Modérateur :** {ctx.author}\n**Raison :** {reason}\n**Durée :** "
            + (format_duration_fr(duration_seconds) if duration_seconds else "Permanent")))
    try:
        await ctx.guild.ban(discord.Object(id=uid), reason=trunc(f"[{ctx.author}] {reason}", 512),
                            delete_message_seconds=0)
    except discord.NotFound:
        return await ctx.send(embed=error_embed("Introuvable", "Cet utilisateur n'existe pas sur Discord."))
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Je n'ai pas la permission de bannir."))
    deactivate_user_sanctions(ctx.guild.id, uid, ("tempban",))
    sid = add_sanction(ctx.guild.id, uid, ctx.author.id, "tempban" if duration_seconds else "ban",
                       reason, duration_seconds)
    duration_str = format_duration_fr(duration_seconds) if duration_seconds else "Permanent"
    await ctx.send(embed=mod_embed("Membre banni", display, uid, ctx.author, reason, duration_str, sid))
    await send_log(ctx.guild, "moderation", "Ban", author=ctx.author,
                   desc=f"**Membre :** <@{uid}> (`{uid}`)\n**Durée :** {duration_str}\n**Raison :** {reason}\n**Sanction :** `#{sid}`",
                   color=COLOR_ERROR)


@bot.command(name="unban")
async def _unban(ctx, user_input: str = None, *, reason: str = None):
    user_id = parse_id(user_input)
    if not user_id:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}unban <id> [raison]`"))
    try:
        await ctx.guild.unban(discord.Object(id=user_id), reason=trunc(f"[{ctx.author}] {reason or ''}", 512))
    except discord.NotFound:
        return await ctx.send(embed=error_embed("Aucun ban", "Aucun ban trouvé pour cet ID."))
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Je n'ai pas la permission."))
    deactivate_user_sanctions(ctx.guild.id, user_id, ("ban", "tempban"))
    await ctx.send(embed=success_embed("Membre débanni", f"<@{user_id}> a été débanni."))
    await send_log(ctx.guild, "moderation", "Unban", author=ctx.author,
                   desc=f"**Membre :** <@{user_id}> (`{user_id}`)\n**Raison :** {reason or 'Aucune'}", color=COLOR_SUCCESS)


@bot.command(name="kick")
async def _kick(ctx, member: discord.Member = None, *, reason: str = "Aucune raison fournie"):
    if not member:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}kick @user [raison]`"))
    err = check_target(ctx, member)
    if err:
        return await ctx.send(embed=error_embed("Action impossible", err))
    await safe_dm(member, error_embed(f"Tu as été expulsé de {ctx.guild.name}",
                                      f"**Modérateur :** {ctx.author}\n**Raison :** {reason}"))
    try:
        await member.kick(reason=trunc(f"[{ctx.author}] {reason}", 512))
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Je n'ai pas la permission."))
    sid = add_sanction(ctx.guild.id, member.id, ctx.author.id, "kick", reason)
    await ctx.send(embed=mod_embed("Membre expulsé", member, member.id, ctx.author, reason, case_id=sid, color=COLOR_WARNING))
    await send_log(ctx.guild, "moderation", "Kick", author=ctx.author, target=member,
                   desc=f"**Raison :** {reason}\n**Sanction :** `#{sid}`", color=COLOR_WARNING)


@bot.command(name="mute", aliases=["timeout"])
async def _mute(ctx, member: discord.Member = None, duration: str = None, *, reason: str = "Aucune raison fournie"):
    if not member or not duration:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}mute @user <durée> [raison]`\nExemples : `30m`, `2h`, `1d`"))
    err = check_target(ctx, member)
    if err:
        return await ctx.send(embed=error_embed("Action impossible", err))
    if member.guild_permissions.administrator:
        return await ctx.send(embed=error_embed("Action impossible", "On ne peut pas timeout un administrateur (limite Discord)."))
    seconds = parse_duration(duration)
    if seconds is None or seconds <= 0:
        return await ctx.send(embed=error_embed("Durée invalide", "Exemples : `30m`, `2h`, `1d`"))
    if seconds > 60*60*24*28:
        return await ctx.send(embed=error_embed("Trop long", "28 jours maximum (limite Discord)."))
    try:
        await member.timeout(timedelta(seconds=seconds), reason=trunc(f"[{ctx.author}] {reason}", 512))
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Je n'ai pas la permission."))
    except discord.HTTPException as e:
        return await ctx.send(embed=error_embed("Erreur Discord", str(e)))
    sid = add_sanction(ctx.guild.id, member.id, ctx.author.id, "mute", reason, seconds)
    duration_str = format_duration_fr(seconds)
    await safe_dm(member, warning_embed(f"Tu as été mute sur {ctx.guild.name}",
                                        f"**Modérateur :** {ctx.author}\n**Durée :** {duration_str}\n**Raison :** {reason}"))
    await ctx.send(embed=mod_embed("Membre mute", member, member.id, ctx.author, reason, duration_str, sid, color=COLOR_WARNING))
    await send_log(ctx.guild, "moderation", "Mute", author=ctx.author, target=member,
                   desc=f"**Durée :** {duration_str}\n**Raison :** {reason}\n**Sanction :** `#{sid}`", color=COLOR_WARNING)


@bot.command(name="unmute", aliases=["untimeout"])
async def _unmute(ctx, member: discord.Member = None, *, reason: str = None):
    if not member:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}unmute @user [raison]`"))
    if not member.is_timed_out():
        return await ctx.send(embed=info_embed("Pas de mute", f"{member.mention} n'est pas mute."))
    try:
        await member.timeout(None, reason=trunc(f"[{ctx.author}] {reason or ''}", 512))
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Je n'ai pas la permission."))
    deactivate_user_sanctions(ctx.guild.id, member.id, ("mute",))
    await ctx.send(embed=success_embed("Mute retiré", f"{member.mention} a été démuté."))
    await send_log(ctx.guild, "moderation", "Unmute", author=ctx.author, target=member,
                   desc=f"**Raison :** {reason or 'Aucune'}", color=COLOR_SUCCESS)


@bot.command(name="warn")
async def _warn(ctx, member: discord.Member = None, *, reason: str = None):
    if not member or not reason:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}warn @user <raison>`"))
    err = check_target(ctx, member)
    if err:
        return await ctx.send(embed=error_embed("Action impossible", err))
    sid = add_sanction(ctx.guild.id, member.id, ctx.author.id, "warn", reason)
    warn_count = len(get_warns(ctx.guild.id, member.id))
    await safe_dm(member, warning_embed(f"Avertissement sur {ctx.guild.name}",
                                        f"**Modérateur :** {ctx.author}\n**Raison :** {reason}\n**Total :** {warn_count} avertissement(s)"))
    em = mod_embed("Membre averti", member, member.id, ctx.author, reason, case_id=sid, color=COLOR_WARNING)
    em.add_field(name="Total", value=f"{warn_count} avertissement(s)", inline=True)
    await ctx.send(embed=em)
    await send_log(ctx.guild, "moderation", "Warn", author=ctx.author, target=member,
                   desc=f"**Raison :** {reason}\n**Total :** {warn_count}\n**Sanction :** `#{sid}`", color=COLOR_WARNING)


@bot.command(name="warns", aliases=["warnings"])
async def _warns(ctx, *, user_input: str = None):
    if user_input:
        display, uid = await resolve_user_or_id(ctx, user_input)
        if uid is None:
            return await ctx.send(embed=error_embed("Utilisateur introuvable"))
    else:
        display, uid = ctx.author, ctx.author.id
    warns = get_warns(ctx.guild.id, uid)
    if not warns:
        return await ctx.send(embed=info_embed("Avertissements", f"{format_user_display(display, uid)} n'a aucun avertissement actif."))
    lines = []
    for w in warns[:15]:
        mod = ctx.guild.get_member(int(w["moderator_id"]))
        mod_name = mod.display_name if mod else f"<@{w['moderator_id']}>"
        try:
            ts = datetime.fromisoformat(w["timestamp"]).strftime("%d/%m/%Y")
        except (ValueError, TypeError):
            ts = "?"
        lines.append(f"`#{w['id']}` · {ts} · par **{mod_name}**\n{trunc(w['reason'], 200)}")
    name = getattr(display, "display_name", str(uid))
    em = info_embed(f"Avertissements de {name} ({len(warns)})", "\n\n".join(lines))
    if display is not None:
        em.set_thumbnail(url=display.display_avatar.url)
    await ctx.send(embed=em)


@bot.command(name="delwarn", aliases=["unwarn"])
async def _delwarn(ctx, warn_id: int = None):
    if not warn_id:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}delwarn <id>`"))
    if delete_warn(ctx.guild.id, warn_id):
        await ctx.send(embed=success_embed("Avertissement supprimé", f"L'avertissement `#{warn_id}` a été retiré."))
        await send_log(ctx.guild, "moderation", "Avertissement supprimé", author=ctx.author,
                       desc=f"Avertissement `#{warn_id}` supprimé.", color=COLOR_SUCCESS)
    else:
        await ctx.send(embed=error_embed("Introuvable", f"L'avertissement `#{warn_id}` n'existe pas ou est déjà supprimé."))


@bot.command(name="clearwarns")
async def _clearwarns(ctx, *, user_input: str = None):
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}clearwarns @user`"))
    count = deactivate_user_sanctions(ctx.guild.id, uid, ("warn",))
    if count:
        await ctx.send(embed=success_embed("Avertissements effacés", f"**{count}** avertissement(s) retiré(s) à {format_user_display(display, uid)}."))
        await send_log(ctx.guild, "moderation", "Avertissements effacés", author=ctx.author,
                       desc=f"**Membre :** <@{uid}>\n**{count}** avertissements retirés", color=COLOR_SUCCESS)
    else:
        await ctx.send(embed=error_embed("Rien à faire", f"{format_user_display(display, uid)} n'a aucun avertissement actif."))


@bot.command(name="softban")
async def _softban(ctx, member: discord.Member = None, *, reason: str = "Aucune raison fournie"):
    if not member:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}softban @user [raison]`"))
    err = check_target(ctx, member)
    if err:
        return await ctx.send(embed=error_embed("Action impossible", err))
    await safe_dm(member, warning_embed(f"Softban sur {ctx.guild.name}",
                                        f"**Modérateur :** {ctx.author}\n**Raison :** {reason}\nTu peux revenir, tes messages récents ont été supprimés."))
    try:
        await ctx.guild.ban(member, reason=trunc(f"[{ctx.author}] softban: {reason}", 512), delete_message_seconds=86400)
        await ctx.guild.unban(discord.Object(id=member.id), reason="softban (auto-unban)")
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Permission insuffisante."))
    sid = add_sanction(ctx.guild.id, member.id, ctx.author.id, "softban", reason)
    await ctx.send(embed=mod_embed("Membre softban", member, member.id, ctx.author, reason, case_id=sid, color=COLOR_WARNING))
    await send_log(ctx.guild, "moderation", "Softban", author=ctx.author, target=member,
                   desc=f"**Raison :** {reason}\n**Sanction :** `#{sid}`", color=COLOR_WARNING)


@bot.command(name="clear", aliases=["purge"])
async def _clear(ctx, *args):
    member, amount = None, None
    for a in args:
        if a.isdigit() and len(a) <= 4:
            amount = int(a)
        else:
            member = await resolve_member(ctx, a)
            if member is None:
                return await ctx.send(embed=error_embed("Utilisateur introuvable"))
    if not amount:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}clear [@user] <nombre>` (max {MAX_CLEAR})"))
    if amount < 1 or amount > MAX_CLEAR:
        return await ctx.send(embed=error_embed("Nombre invalide", f"Entre 1 et {MAX_CLEAR}."))
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass
    try:
        if member:
            remaining = [amount]

            def check(m):
                if m.author.id != member.id or remaining[0] <= 0:
                    return False
                remaining[0] -= 1
                return True
            deleted = await ctx.channel.purge(limit=min(amount * 10, 1000), check=check)
        else:
            deleted = await ctx.channel.purge(limit=amount)
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Je n'ai pas la permission de supprimer."))
    who = f" de {member.mention}" if member else ""
    await ctx.send(f"**{len(deleted)}** message(s){who} supprimé(s).", delete_after=5)
    await send_log(ctx.guild, "moderation", "Clear", author=ctx.author,
                   desc=f"**Salon :** {ctx.channel.mention}\n**Messages :** {len(deleted)}{who}", color=COLOR_INFO)


@bot.command(name="lock")
async def _lock(ctx, channel: discord.TextChannel = None, *, reason: str = "Pas de raison"):
    channel = channel or ctx.channel
    try:
        ow = channel.overwrites_for(ctx.guild.default_role)
        ow.send_messages = False
        ow.add_reactions = False
        ow.create_public_threads = False
        await channel.set_permissions(ctx.guild.default_role, overwrite=ow, reason=trunc(f"[{ctx.author}] {reason}", 512))
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Permission insuffisante."))
    await ctx.send(embed=warning_embed("Salon verrouillé", f"{channel.mention} est passé en lecture seule."))
    await send_log(ctx.guild, "moderation", "Lock", author=ctx.author,
                   desc=f"**Salon :** {channel.mention}\n**Raison :** {reason}", color=COLOR_WARNING)


@bot.command(name="unlock")
async def _unlock(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    try:
        ow = channel.overwrites_for(ctx.guild.default_role)
        ow.send_messages = None
        ow.add_reactions = None
        ow.create_public_threads = None
        await channel.set_permissions(ctx.guild.default_role, overwrite=None if ow.is_empty() else ow,
                                      reason=f"[{ctx.author}] unlock")
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Permission insuffisante."))
    await ctx.send(embed=success_embed("Salon déverrouillé", f"{channel.mention} est de nouveau ouvert."))
    await send_log(ctx.guild, "moderation", "Unlock", author=ctx.author,
                   desc=f"**Salon :** {channel.mention}", color=COLOR_SUCCESS)


@bot.command(name="slowmode", aliases=["slow"])
async def _slowmode(ctx, duration: str = None):
    if duration is None:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}slowmode <durée>` (0 pour désactiver, max 6h)"))
    seconds = int(duration) if duration.isdigit() else parse_duration(duration)
    if seconds is None or seconds < 0 or seconds > 21600:
        return await ctx.send(embed=error_embed("Durée invalide", "Entre 0 et 6h (21600 secondes)."))
    try:
        await ctx.channel.edit(slowmode_delay=seconds, reason=f"[{ctx.author}] slowmode")
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Permission insuffisante."))
    if seconds == 0:
        await ctx.send(embed=success_embed("Slowmode désactivé", f"{ctx.channel.mention}"))
    else:
        await ctx.send(embed=success_embed("Slowmode activé", f"{ctx.channel.mention} → {format_duration_fr(seconds)}"))


@bot.command(name="banlist", aliases=["bans"])
async def _banlist(ctx):
    try:
        entries = [e async for e in ctx.guild.bans(limit=1000)]
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Il me faut la permission « Bannir des membres »."))
    if not entries:
        return await ctx.send(embed=info_embed("Bannis", "Aucun membre banni."))
    lines = [f"**{e.user}** (`{e.user.id}`) · {trunc(e.reason or 'sans raison', 60)}" for e in entries]
    await send_pager(ctx, f"Bannis ({len(entries)})", lines, per_page=15)


@bot.command(name="snipe")
async def _snipe(ctx):
    data = _snipe_cache.get(ctx.channel.id)
    if not data:
        return await ctx.send(embed=info_embed("Snipe", "Aucun message supprimé récemment ici."))
    ts = int(data["time"].timestamp())
    em = discord.Embed(description=trunc(data["content"], 4000) or "*pas de texte*", color=COLOR_DEFAULT)
    em.set_author(name=data["author"], icon_url=data["avatar"])
    em.add_field(name="Supprimé", value=f"<t:{ts}:R>", inline=True)
    if data.get("image"):
        em.set_image(url=data["image"])
    await ctx.send(embed=em)


SANCTION_LABEL = {"ban": "Ban", "tempban": "Ban temporaire", "kick": "Kick", "mute": "Mute",
                  "warn": "Avertissement", "softban": "Softban"}


@bot.command(name="history", aliases=["sanctions"])
async def _history(ctx, *, user_input: str = None):
    if user_input:
        display, uid = await resolve_user_or_id(ctx, user_input)
        if uid is None:
            return await ctx.send(embed=error_embed("Utilisateur introuvable"))
    else:
        display, uid = ctx.author, ctx.author.id
    rows = get_history(ctx.guild.id, uid, limit=50)
    if not rows:
        return await ctx.send(embed=info_embed("Historique", f"{format_user_display(display, uid)} n'a aucune sanction."))
    lines = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["timestamp"]).strftime("%d/%m/%Y")
        except (ValueError, TypeError):
            ts = "?"
        mod = ctx.guild.get_member(int(r["moderator_id"]))
        mod_name = mod.display_name if mod else f"<@{r['moderator_id']}>"
        marker = "" if r["active"] else " *(retirée)*"
        dur = f" · {format_duration_fr(r['duration'])}" if r["duration"] else ""
        lines.append(f"`#{r['id']}` · {ts} · **{SANCTION_LABEL.get(r['type'], r['type'])}**{dur}{marker}\n"
                     f"par {mod_name} : {trunc(r['reason'] or 'sans raison', 150)}")
    name = getattr(display, "display_name", str(uid))
    thumb = display.display_avatar.url if display is not None else None
    await send_pager(ctx, f"Historique de {name} ({len(rows)})", lines, per_page=8,
                     footer=f"{get_prefix_cached()}case <id> pour le détail")


@bot.command(name="case")
async def _case(ctx, case_id: int = None):
    if not case_id:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}case <id>`"))
    s = get_sanction(ctx.guild.id, case_id)
    if not s:
        return await ctx.send(embed=error_embed("Introuvable", f"La sanction `#{case_id}` n'existe pas."))
    try:
        ts = int(datetime.fromisoformat(s["timestamp"]).timestamp())
        when = f"<t:{ts}:F> (<t:{ts}:R>)"
    except (ValueError, TypeError):
        when = "?"
    em = discord.Embed(title=f"Sanction #{s['id']} · {SANCTION_LABEL.get(s['type'], s['type'])}", color=COLOR_DEFAULT)
    em.add_field(name="Membre", value=f"<@{s['user_id']}> (`{s['user_id']}`)", inline=True)
    em.add_field(name="Modérateur", value=f"<@{s['moderator_id']}>", inline=True)
    em.add_field(name="Statut", value="Active" if s["active"] else "Retirée", inline=True)
    if s["duration"]:
        em.add_field(name="Durée", value=format_duration_fr(s["duration"]), inline=True)
    em.add_field(name="Date", value=when, inline=False)
    em.add_field(name="Raison", value=trunc(s["reason"] or "Aucune raison fournie", 1024), inline=False)
    await ctx.send(embed=em)


@bot.command(name="reason")
async def _reason(ctx, case_id: int = None, *, new_reason: str = None):
    if not case_id or not new_reason:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}reason <id> <nouvelle raison>`"))
    s = get_sanction(ctx.guild.id, case_id)
    if not s:
        return await ctx.send(embed=error_embed("Introuvable", f"La sanction `#{case_id}` n'existe pas."))
    if int(s["moderator_id"]) != ctx.author.id and not is_owner_plus(ctx.author):
        return await ctx.send(embed=error_embed("Action impossible", "Seul l'auteur de la sanction ou un Owner peut la modifier."))
    update_sanction_reason(ctx.guild.id, case_id, new_reason)
    await ctx.send(embed=success_embed("Raison modifiée", f"Sanction `#{case_id}` → {trunc(new_reason, 500)}"))
    await send_log(ctx.guild, "moderation", "Raison modifiée", author=ctx.author,
                   desc=f"**Sanction :** `#{case_id}`\n**Avant :** {s['reason'] or 'aucune'}\n**Après :** {new_reason}",
                   color=COLOR_INFO)


# ========================= INFOS =========================

@bot.command(name="userinfo", aliases=["ui", "whois"])
async def _userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    created = int(member.created_at.timestamp())
    joined = int(member.joined_at.timestamp()) if member.joined_at else None
    roles = [r.mention for r in reversed(member.roles) if r != ctx.guild.default_role]
    lvl = get_user_level(member)
    em = discord.Embed(title=str(member), color=member.color if member.color.value else COLOR_DEFAULT)
    em.set_thumbnail(url=member.display_avatar.url)
    em.add_field(name="ID", value=f"`{member.id}`", inline=True)
    em.add_field(name="Surnom", value=member.nick or "aucun", inline=True)
    em.add_field(name="Bot", value="oui" if member.bot else "non", inline=True)
    em.add_field(name="Compte créé", value=f"<t:{created}:R>", inline=True)
    if joined:
        em.add_field(name="A rejoint", value=f"<t:{joined}:R>", inline=True)
    em.add_field(name="Niveau", value=level_name(lvl), inline=True)
    if member.is_timed_out() and member.timed_out_until:
        em.add_field(name="Mute jusqu'à", value=f"<t:{int(member.timed_out_until.timestamp())}:R>", inline=True)
    warns = len(get_warns(ctx.guild.id, member.id))
    if warns:
        em.add_field(name="Avertissements", value=str(warns), inline=True)
    em.add_field(name=f"Rôles ({len(roles)})", value=trunc(", ".join(roles[:30]), 1024) if roles else "aucun", inline=False)
    await ctx.send(embed=em)


@bot.command(name="serverinfo", aliases=["si"])
async def _serverinfo(ctx):
    g = ctx.guild
    created = int(g.created_at.timestamp())
    bots = sum(1 for m in g.members if m.bot)
    humans = (g.member_count or 0) - bots
    em = discord.Embed(title=g.name, color=COLOR_DEFAULT)
    apply_thumbnail(em, g)
    em.add_field(name="ID", value=f"`{g.id}`", inline=True)
    em.add_field(name="Propriétaire", value=f"<@{g.owner_id}>", inline=True)
    em.add_field(name="Créé", value=f"<t:{created}:R>", inline=True)
    em.add_field(name="Membres", value=f"{g.member_count} ({humans} membres / {bots} bots)", inline=True)
    em.add_field(name="Salons", value=f"{len(g.text_channels)} écrits / {len(g.voice_channels)} vocaux", inline=True)
    em.add_field(name="Rôles", value=str(len(g.roles)), inline=True)
    em.add_field(name="Boosts", value=f"{g.premium_subscription_count} (niveau {g.premium_tier})", inline=True)
    em.add_field(name="Emojis", value=str(len(g.emojis)), inline=True)
    await ctx.send(embed=em)


@bot.command(name="avatar", aliases=["av", "pdp"])
async def _avatar(ctx, member: discord.Member = None):
    member = member or ctx.author
    em = discord.Embed(title=f"Avatar de {member.display_name}", color=COLOR_DEFAULT)
    em.set_image(url=member.display_avatar.url)
    em.description = f"[Lien direct]({member.display_avatar.url})"
    await ctx.send(embed=em)


@bot.command(name="ping")
async def _ping(ctx):
    await ctx.send(embed=success_embed("Pong", f"Latence : **{round(bot.latency*1000)} ms**"))


@bot.command(name="roleinfo", aliases=["ri"])
async def _roleinfo(ctx, *, role_input: str = None):
    if not role_input:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}roleinfo @rôle`"))
    try:
        role = await commands.RoleConverter().convert(ctx, role_input)
    except commands.CommandError:
        return await ctx.send(embed=error_embed("Rôle introuvable"))
    created = int(role.created_at.timestamp())
    em = discord.Embed(title=role.name, color=role.color if role.color.value else COLOR_DEFAULT)
    em.add_field(name="ID", value=f"`{role.id}`", inline=True)
    em.add_field(name="Membres", value=str(len(role.members)), inline=True)
    em.add_field(name="Couleur", value=str(role.color), inline=True)
    em.add_field(name="Position", value=str(role.position), inline=True)
    em.add_field(name="Mentionnable", value="oui" if role.mentionable else "non", inline=True)
    em.add_field(name="Affiché à part", value="oui" if role.hoist else "non", inline=True)
    rp = get_role_perm(ctx.guild.id, role.id)
    if rp:
        em.add_field(name="Niveau", value=f"perm{rp}", inline=True)
    em.add_field(name="Créé", value=f"<t:{created}:R>", inline=False)
    await ctx.send(embed=em)


# ========================= OUTILS =========================

@bot.command(name="role")
async def _role(ctx, member: discord.Member = None, *, role_input: str = None):
    if not member or not role_input:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}role @user @rôle`"))
    try:
        role = await commands.RoleConverter().convert(ctx, role_input)
    except commands.CommandError:
        return await ctx.send(embed=error_embed("Rôle introuvable"))
    err = _role_guard(ctx, role)
    if err:
        return await ctx.send(embed=error_embed("Action impossible", err))
    try:
        if role in member.roles:
            if member.id != ctx.author.id:
                terr = check_target(ctx, member)
                if terr:
                    return await ctx.send(embed=error_embed("Action impossible", terr))
            await member.remove_roles(role, reason=f"[{ctx.author}] role")
            await ctx.send(embed=success_embed("Rôle retiré", f"{role.mention} retiré à {member.mention}."))
        else:
            await member.add_roles(role, reason=f"[{ctx.author}] role")
            await ctx.send(embed=success_embed("Rôle ajouté", f"{role.mention} ajouté à {member.mention}."))
    except discord.Forbidden:
        await ctx.send(embed=error_embed("Permission refusée", "Permission insuffisante."))


def _role_guard(ctx, role):
    """Renvoie un message d'erreur si le rôle ne peut pas être manipulé, sinon None."""
    if role >= ctx.guild.me.top_role:
        return "Ce rôle est au-dessus du mien."
    if role >= ctx.author.top_role and not is_owner_plus(ctx.author):
        return "Ce rôle est au-dessus ou égal au tien."
    if role.is_default() or role.is_premium_subscriber() or role.managed:
        return "Ce rôle ne peut pas être attribué manuellement."
    if not is_owner_plus(ctx.author) and any(getattr(role.permissions, p) for p in DANGEROUS_PERMS):
        return "Ce rôle a des permissions sensibles : réservé aux Owners."
    return None


async def _mass_role(ctx, role_input, members, add):
    try:
        role = await commands.RoleConverter().convert(ctx, role_input)
    except commands.CommandError:
        return await ctx.send(embed=error_embed("Rôle introuvable"))
    err = _role_guard(ctx, role)
    if err:
        return await ctx.send(embed=error_embed("Action impossible", err))
    if not members:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}{'addrole' if add else 'delrole'} @rôle @u1 @u2 ...`"))
    done = 0
    for m in members:
        try:
            if add and role not in m.roles:
                await m.add_roles(role, reason=f"[{ctx.author}] addrole")
                done += 1
            elif not add and role in m.roles:
                await m.remove_roles(role, reason=f"[{ctx.author}] delrole")
                done += 1
        except discord.HTTPException:
            pass
    verb = "ajouté à" if add else "retiré à"
    await ctx.send(embed=success_embed("Rôle mis à jour", f"{role.mention} {verb} **{done}** membre(s)."))
    await send_log(ctx.guild, "roles", "Rôle (masse)", author=ctx.author,
                   desc=f"{role.mention} {verb} {done} membre(s)", color=COLOR_INFO)


@bot.command(name="addrole")
async def _addrole(ctx, role_input: str = None, members: commands.Greedy[discord.Member] = None):
    if not role_input:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}addrole @rôle @u1 @u2 ...`"))
    await _mass_role(ctx, role_input, members or [], True)


@bot.command(name="delrole")
async def _delrole(ctx, role_input: str = None, members: commands.Greedy[discord.Member] = None):
    if not role_input:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}delrole @rôle @u1 @u2 ...`"))
    await _mass_role(ctx, role_input, members or [], False)


@bot.command(name="roleall")
async def _roleall(ctx, *, role_input: str = None):
    if not role_input:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}roleall @rôle`"))
    try:
        role = await commands.RoleConverter().convert(ctx, role_input)
    except commands.CommandError:
        return await ctx.send(embed=error_embed("Rôle introuvable"))
    err = _role_guard(ctx, role)
    if err:
        return await ctx.send(embed=error_embed("Action impossible", err))
    targets = [m for m in ctx.guild.members if not m.bot and role not in m.roles]
    if not targets:
        return await ctx.send(embed=info_embed("Rien à faire", "Tout le monde a déjà ce rôle."))
    view = ConfirmView(ctx.author.id)
    view.message = await ctx.send(embed=warning_embed(
        "Donner le rôle à tout le monde ?",
        f"{role.mention} va être donné à **{len(targets)}** membre(s). Ça peut prendre un moment."), view=view)
    await view.wait()
    if not view.value:
        try:
            await view.message.edit(embed=info_embed("Annulé", "Aucun rôle donné."), view=None)
        except discord.HTTPException:
            pass
        return
    msg = await ctx.send(embed=info_embed("En cours…", f"Attribution à {len(targets)} membre(s)."))
    done = 0
    for m in targets:
        try:
            await m.add_roles(role, reason=f"[{ctx.author}] roleall")
            done += 1
        except discord.HTTPException:
            pass
        if done % 20 == 0:
            await asyncio.sleep(1)
    await msg.edit(embed=success_embed("Terminé", f"{role.mention} donné à **{done}** membre(s)."))
    await send_log(ctx.guild, "roles", "Roleall", author=ctx.author,
                   desc=f"{role.mention} donné à {done} membre(s)", color=COLOR_INFO)


@bot.command(name="nick", aliases=["setnick"])
async def _nick(ctx, member: discord.Member = None, *, nickname: str = None):
    if not member:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}nick @user [pseudo]` (vide pour réinitialiser)"))
    if member.id != ctx.author.id:
        err = check_target(ctx, member)
        if err:
            return await ctx.send(embed=error_embed("Action impossible", err))
    elif member.top_role >= ctx.guild.me.top_role or member.id == ctx.guild.owner_id:
        return await ctx.send(embed=error_embed("Action impossible", "Je ne peux pas modifier ton pseudo (rôle trop haut)."))
    if nickname and len(nickname) > 32:
        return await ctx.send(embed=error_embed("Trop long", "32 caractères maximum."))
    try:
        await member.edit(nick=nickname, reason=f"[{ctx.author}] nick")
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Permission insuffisante."))
    if nickname:
        await ctx.send(embed=success_embed("Pseudo changé", f"{member.mention} → **{nickname}**"))
    else:
        await ctx.send(embed=success_embed("Pseudo réinitialisé", f"{member.mention}"))


@bot.command(name="derank")
async def _derank(ctx, member: discord.Member = None):
    if not member:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}derank @user`"))
    err = check_target(ctx, member)
    if err:
        return await ctx.send(embed=error_embed("Action impossible", err))
    to_remove = [r for r in member.roles
                 if r != ctx.guild.default_role and r < ctx.guild.me.top_role and not r.managed]
    if not to_remove:
        return await ctx.send(embed=error_embed("Rien à faire", "Aucun rôle gérable à retirer."))
    try:
        await member.remove_roles(*to_remove, reason=f"[{ctx.author}] derank")
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Permission insuffisante."))
    await ctx.send(embed=success_embed("Derank", f"**{len(to_remove)}** rôle(s) retiré(s) à {member.mention}."))
    await send_log(ctx.guild, "roles", "Derank", author=ctx.author, target=member,
                   desc=f"**{len(to_remove)}** rôles retirés : {trunc(', '.join(r.mention for r in to_remove), 3500)}",
                   color=COLOR_WARNING)


@bot.command(name="say")
async def _say(ctx, *, message: str = None):
    if not message:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}say <message>`"))
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass
    await ctx.send(trunc(message, 2000))


@bot.command(name="dm")
async def _dm(ctx, member: discord.Member = None, *, message: str = None):
    if not member or not message:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}dm @user <message>`"))
    em = discord.Embed(title=f"Message de {ctx.guild.name}", description=trunc(message, 4000), color=COLOR_INFO)
    if await safe_dm(member, em):
        await ctx.send(embed=success_embed("Message envoyé", f"À {member.mention}."))
    else:
        await ctx.send(embed=error_embed("Échec", "Impossible d'envoyer le message (MP fermés ?)."))


@bot.command(name="embed")
async def _embed_cmd(ctx, *, message: str = None):
    if not message:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}embed <titre | message>`"))
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass
    title, sep, body = message.partition("|")
    if sep:
        em = discord.Embed(title=trunc(title.strip(), 256), description=trunc(body.strip(), 4000), color=COLOR_DEFAULT)
    else:
        em = discord.Embed(description=trunc(message, 4000), color=COLOR_DEFAULT)
    await ctx.send(embed=em)


EMOJI_RE = re.compile(r"<(a?):(\w{2,32}):(\d+)>")


@bot.command(name="create", aliases=["addemoji", "addemote", "steal", "clone"])
async def _create(ctx, *, source: str = None):
    """Clone un ou plusieurs emojis sur ce serveur.
    Marche avec des emojis d'autres serveurs collés directement, des URLs, ou une image jointe."""
    if not ctx.guild.me.guild_permissions.manage_expressions and not ctx.guild.me.guild_permissions.manage_emojis:
        return await ctx.send(embed=error_embed("Permission refusée", "Il me faut la permission « Gérer les emojis »."))

    jobs = []   # (nom, url)
    if source:
        for anim, name, eid in EMOJI_RE.findall(source):
            url = f"https://cdn.discordapp.com/emojis/{eid}.{'gif' if anim else 'png'}"
            jobs.append((name, url))

    # Pièce jointe image → besoin d'un nom fourni en argument
    if not jobs and ctx.message.attachments:
        att = ctx.message.attachments[0]
        name = (source or "").strip().strip(":") or os.path.splitext(att.filename)[0]
        name = re.sub(r"[^A-Za-z0-9_]", "", name)[:32] or "emoji"
        if len(name) < 2:
            name = f"emoji_{name}"
        jobs.append((name, att.url))

    # Une seule URL fournie en argument (format : +create nom url)
    if not jobs and source:
        parts = source.split()
        if len(parts) == 2 and parts[1].startswith("https://"):
            nm = re.sub(r"[^A-Za-z0-9_]", "", parts[0].strip(":"))[:32] or "emoji"
            jobs.append((nm if len(nm) >= 2 else f"emoji_{nm}", parts[1]))

    if not jobs:
        return await ctx.send(embed=error_embed(
            "Usage", f"`{get_prefix_cached()}create <emoji1> <emoji2> ...`\n"
                     "Colle un ou plusieurs emojis d'autres serveurs, ou joins une image avec un nom."))

    jobs = jobs[:MAX_CREATE]
    created, failed = [], []
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for name, url in jobs:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        failed.append(name)
                        continue
                    data = await resp.content.read(MAX_EMOJI_BYTES + 1)
                if len(data) > MAX_EMOJI_BYTES:
                    failed.append(f"{name} (trop lourd)")
                    continue
                emoji = await ctx.guild.create_custom_emoji(name=name, image=data, reason=f"[{ctx.author}] create")
                created.append(str(emoji))
            except discord.Forbidden:
                return await ctx.send(embed=error_embed("Permission refusée", "Il me faut la permission « Gérer les emojis »."))
            except discord.HTTPException:
                # Serveur plein, nom en double, format refusé...
                failed.append(name)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                failed.append(name)

    if created and not failed:
        em = success_embed(f"{len(created)} emoji(s) ajouté(s)", " ".join(created))
    elif created:
        em = success_embed(f"{len(created)} emoji(s) ajouté(s)", " ".join(created))
        em.add_field(name="Échecs", value=trunc(", ".join(failed), 1024), inline=False)
    else:
        em = error_embed("Aucun emoji ajouté", "Vérifie que le serveur n'est pas plein et que les noms sont libres.\n"
                                               + trunc(", ".join(failed), 1000))
    await ctx.send(embed=em)
    if created:
        await send_log(ctx.guild, "roles", "Emojis clonés", author=ctx.author,
                       desc=f"**{len(created)}** emoji(s) ajouté(s)", color=COLOR_SUCCESS)


@bot.command(name="delemoji", aliases=["delemote", "removeemoji"])
async def _delemoji(ctx, *, emoji_input: str = None):
    if not emoji_input:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}delemoji <emoji>`"))
    emoji = None
    m = EMOJI_RE.search(emoji_input)
    if m:
        emoji = ctx.guild.get_emoji(int(m.group(3)))
    if emoji is None:
        name = emoji_input.strip().strip(":")
        emoji = discord.utils.get(ctx.guild.emojis, name=name)
    if emoji is None:
        return await ctx.send(embed=error_embed("Introuvable", "Cet emoji n'est pas sur ce serveur."))
    name = emoji.name
    try:
        await emoji.delete(reason=f"[{ctx.author}] delemoji")
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Il me faut la permission « Gérer les emojis »."))
    await ctx.send(embed=success_embed("Emoji supprimé", f"`:{name}:` a été retiré du serveur."))


# ========================= LOG LISTENERS + SNIPE =========================

_snipe_cache = {}


@bot.event
async def on_message_delete(message):
    if not message.guild or message.author.bot:
        return
    image = next((a.url for a in message.attachments
                  if a.content_type and a.content_type.startswith("image/")), None)
    if message.content or image:
        _snipe_cache[message.channel.id] = {
            "content": message.content, "author": str(message.author),
            "author_id": message.author.id, "avatar": message.author.display_avatar.url,
            "time": datetime.now(timezone.utc), "image": image,
        }
    desc = f"**Auteur :** {message.author.mention} (`{message.author.id}`)\n**Salon :** {message.channel.mention}\n"
    if message.content:
        desc += f"**Contenu :**\n{trunc(message.content, 1500)}"
    if message.attachments:
        desc += f"\n**Pièces jointes :** {len(message.attachments)}"
    await send_log(message.guild, "messages", "Message supprimé", desc=desc, color=COLOR_ERROR)


@bot.event
async def on_bulk_message_delete(messages):
    if not messages or not messages[0].guild:
        return
    ch = messages[0].channel
    await send_log(messages[0].guild, "messages", "Suppression en masse",
                   desc=f"**Salon :** {ch.mention}\n**Messages :** {len(messages)}", color=COLOR_ERROR)


@bot.event
async def on_message_edit(before, after):
    if not before.guild or before.author.bot or before.content == after.content:
        return
    desc = (f"**Auteur :** {before.author.mention}\n**Salon :** {before.channel.mention}\n"
            f"**Avant :** {trunc(before.content, 1500) or 'vide'}\n**Après :** {trunc(after.content, 1500) or 'vide'}\n"
            f"[Aller au message]({after.jump_url})")
    await send_log(before.guild, "messages", "Message édité", desc=desc, color=COLOR_INFO)


@bot.event
async def on_member_join(member):
    age = (datetime.now(timezone.utc) - member.created_at).days
    young = age < YOUNG_ACCOUNT_DAYS
    await send_log(member.guild, "members", "Nouveau membre (compte récent)" if young else "Nouveau membre",
                   target=member,
                   desc=f"**Compte créé :** <t:{int(member.created_at.timestamp())}:R> ({age} jours)\n"
                        f"**Membres :** {member.guild.member_count}",
                   color=COLOR_WARNING if young else COLOR_SUCCESS)


@bot.event
async def on_member_remove(member):
    roles = ", ".join(r.mention for r in member.roles if r != member.guild.default_role) or "aucun"
    await send_log(member.guild, "members", "Membre parti", target=member,
                   desc=f"**Rôles :** {trunc(roles, 3500)}", color=COLOR_WARNING)


@bot.event
async def on_member_update(before, after):
    if before.nick != after.nick:
        await send_log(after.guild, "members", "Pseudo changé", target=after,
                       desc=f"**Avant :** {before.nick or before.name}\n**Après :** {after.nick or after.name}", color=COLOR_INFO)
    if set(before.roles) != set(after.roles):
        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        parts = []
        if added:
            parts.append("**Ajoutés :** " + ", ".join(r.mention for r in added))
        if removed:
            parts.append("**Retirés :** " + ", ".join(r.mention for r in removed))
        if parts:
            await send_log(after.guild, "roles", "Rôles modifiés", target=after, desc="\n".join(parts), color=COLOR_INFO)


@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel == after.channel:
        return
    if before.channel is None and after.channel:
        action, desc, color = "Rejoint un vocal", f"**Salon :** {after.channel.mention}", COLOR_SUCCESS
    elif after.channel is None and before.channel:
        action, desc, color = "Quitte un vocal", f"**Salon :** {before.channel.mention}", COLOR_WARNING
    else:
        action, desc, color = "Change de vocal", f"**De :** {before.channel.mention}\n**Vers :** {after.channel.mention}", COLOR_INFO
    await send_log(member.guild, "voice", action, target=member, desc=desc, color=color)


@bot.event
async def on_guild_role_create(role):
    await send_log(role.guild, "roles", "Rôle créé", desc=f"**Rôle :** {role.mention} (`{role.id}`)", color=COLOR_SUCCESS)


@bot.event
async def on_guild_role_delete(role):
    if get_role_perm(role.guild.id, role.id) is not None:
        del_role_perm(role.guild.id, role.id)
    await send_log(role.guild, "roles", "Rôle supprimé", desc=f"**Rôle :** `{role.name}` (`{role.id}`)", color=COLOR_ERROR)


@bot.event
async def on_member_ban(guild, user):
    await send_log(guild, "moderation", "Membre banni (audit)", desc=f"**Membre :** {user.mention} (`{user.id}`)", color=COLOR_ERROR)


@bot.event
async def on_member_unban(guild, user):
    await send_log(guild, "moderation", "Membre débanni (audit)", desc=f"**Membre :** {user.mention} (`{user.id}`)", color=COLOR_SUCCESS)


# ========================= RANGS : OWNER / WL =========================

@bot.command(name="owner")
async def _owner(ctx, *, user_input: str = None):
    if user_input is None:
        ids = get_users_by_rank(LEVEL_OWNER)
        buyers = "\n".join(f"<@{i}>" for i in BUYER_IDS) or "aucun"
        owners = "\n".join(f"<@{i}>" for i in ids) or "aucun"
        return await ctx.send(embed=info_embed("Owners & Buyers", f"**Buyers :**\n{buyers}\n\n**Owners :**\n{owners}"))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("Utilisateur introuvable"))
    if uid in BUYER_IDS:
        return await ctx.send(embed=error_embed("Déjà Buyer", "C'est déjà un Buyer."))
    if getattr(display, "bot", False):
        return await ctx.send(embed=error_embed("Action impossible", "On ne donne pas de rang à un bot."))
    if get_base_rank(uid) == LEVEL_OWNER:
        return await ctx.send(embed=error_embed("Déjà Owner", f"{format_user_display(display, uid)} est déjà Owner."))
    set_base_rank(uid, LEVEL_OWNER)
    await ctx.send(embed=success_embed("Owner ajouté", f"{format_user_display(display, uid)} est maintenant Owner."))


@bot.command(name="unowner")
async def _unowner(ctx, *, user_input: str = None):
    if not user_input:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}unowner @user`"))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("Utilisateur introuvable"))
    if uid == ctx.author.id:
        return await ctx.send(embed=error_embed("Action impossible", "Tu ne peux pas te retirer toi-même le rang Owner."))
    if get_base_rank(uid) != LEVEL_OWNER:
        return await ctx.send(embed=error_embed("Pas Owner", f"{format_user_display(display, uid)} n'est pas Owner."))
    set_base_rank(uid, 0)
    await ctx.send(embed=success_embed("Owner retiré", f"{format_user_display(display, uid)} n'est plus Owner."))


@bot.command(name="wl")
async def _wl(ctx, *, user_input: str = None):
    if user_input is None:
        ids = get_users_by_rank(LEVEL_WL)
        if not ids:
            return await ctx.send(embed=info_embed("Whitelist", "Personne dans la whitelist."))
        return await send_pager(ctx, f"Whitelist ({len(ids)})", [f"<@{i}> (`{i}`)" for i in ids], per_page=15)
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("Utilisateur introuvable"))
    if getattr(display, "bot", False):
        return await ctx.send(embed=error_embed("Action impossible", "On ne donne pas de rang à un bot."))
    if get_base_rank(uid) >= LEVEL_WL:
        return await ctx.send(embed=error_embed("Déjà whitelist", f"{format_user_display(display, uid)} a déjà un rang égal ou supérieur."))
    set_base_rank(uid, LEVEL_WL)
    await ctx.send(embed=success_embed("Whitelist ajouté", f"{format_user_display(display, uid)} est maintenant whitelist."))


@bot.command(name="unwl")
async def _unwl(ctx, *, user_input: str = None):
    if not user_input:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}unwl @user`"))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("Utilisateur introuvable"))
    if uid == ctx.author.id:
        return await ctx.send(embed=error_embed("Action impossible", "Tu ne peux pas te retirer toi-même la whitelist."))
    if get_base_rank(uid) != LEVEL_WL:
        return await ctx.send(embed=error_embed("Pas whitelist", f"{format_user_display(display, uid)} n'est pas whitelist."))
    set_base_rank(uid, 0)
    await ctx.send(embed=success_embed("Whitelist retiré", f"{format_user_display(display, uid)} n'est plus whitelist."))


# ========================= BOTBAN =========================

@bot.command(name="botban")
async def _botban(ctx, *, user_input: str = None):
    if not user_input:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}botban @user`"))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("Utilisateur introuvable"))
    member = ctx.guild.get_member(uid)
    target_lvl = get_user_level(member) if member else get_base_rank(uid)
    if uid in BUYER_IDS or uid == ctx.author.id or target_lvl >= get_user_level(ctx.author):
        return await ctx.send(embed=error_embed("Action impossible", "Tu ne peux pas bot-ban un rang égal ou supérieur."))
    if is_bot_banned(uid):
        return await ctx.send(embed=error_embed("Déjà banni", f"{format_user_display(display, uid)} est déjà bot-banni."))
    add_bot_ban(uid, ctx.author.id)
    await ctx.send(embed=success_embed("Bot-banni", f"{format_user_display(display, uid)} ne peut plus utiliser le bot."))


@bot.command(name="unbotban")
async def _unbotban(ctx, *, user_input: str = None):
    if not user_input:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}unbotban @user`"))
    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("Utilisateur introuvable"))
    if not is_bot_banned(uid):
        return await ctx.send(embed=error_embed("Pas banni", f"{format_user_display(display, uid)} n'est pas bot-banni."))
    remove_bot_ban(uid)
    await ctx.send(embed=success_embed("Bot-débanni", f"{format_user_display(display, uid)} peut de nouveau utiliser le bot."))


# ========================= PERMS =========================

@bot.command(name="setperm")
async def _setperm(ctx, target: str = None, level_str: str = None):
    p = get_prefix_cached()
    if not target or level_str is None:
        return await ctx.send(embed=error_embed(
            "Usage",
            f"`{p}setperm <commande|@rôle|@membre> <niveau>`\n"
            "Niveau : `public`, `0` à `9` (ou `perm3`).\n\n"
            f"`{p}setperm ban 4` — la commande ban demande perm4\n"
            f"`{p}setperm @Modo 4` — le rôle Modo passe perm4\n"
            f"`{p}setperm @membre 2` — ce membre passe perm2"))

    cmd = bot.get_command(target.lower())
    if cmd is not None and cmd.name in COMMANDS:
        meta = COMMANDS[cmd.name]
        if meta["locked"]:
            return await ctx.send(embed=error_embed("Commande verrouillée", f"`{cmd.name}` ne peut pas être reconfigurée."))
        if cmd.name in BUYER_RETIER_CMDS and ctx.author.id not in BUYER_IDS:
            return await ctx.send(embed=error_embed("Réservé au Buyer", f"Seul un Buyer peut changer le niveau de `{cmd.name}`."))
        cmd_lvl = parse_level(level_str, max_level=LEVEL_OWNER)
        if cmd_lvl is None:
            return await ctx.send(embed=error_embed("Niveau invalide", "Niveau : `public`, `0`-`9`, `wl` ou `owner`."))
        set_cmd_level(ctx.guild.id, cmd.name, cmd_lvl)
        return await ctx.send(embed=success_embed("Commande configurée",
                                                  f"`{p}{cmd.name}` demande maintenant **{level_name(cmd_lvl)}**."))

    lvl = parse_level(level_str)
    if lvl is None:
        return await ctx.send(embed=error_embed("Niveau invalide", "Niveau : `public`, `0` à `9` (ex : `perm4`)."))

    try:
        role = await commands.RoleConverter().convert(ctx, target)
    except commands.CommandError:
        role = None
    if role is not None:
        if role.is_default():
            return await ctx.send(embed=error_embed("Action impossible", "Utilise plutôt `setperm <commande> public`."))
        set_role_perm(ctx.guild.id, role.id, lvl)
        if lvl == 0:
            return await ctx.send(embed=success_embed("Perm retirée", f"{role.mention} repasse public."))
        return await ctx.send(embed=success_embed("Rôle configuré", f"{role.mention} a maintenant **{level_name(lvl)}**."))

    display, uid = await resolve_user_or_id(ctx, target)
    if uid is not None:
        set_member_perm(ctx.guild.id, uid, lvl)
        if lvl == 0:
            return await ctx.send(embed=success_embed("Perm retirée", f"{format_user_display(display, uid)} repasse public."))
        return await ctx.send(embed=success_embed("Membre configuré", f"{format_user_display(display, uid)} a maintenant **{level_name(lvl)}**."))

    await ctx.send(embed=error_embed("Cible introuvable", "Donne une commande, un rôle ou un membre."))


@bot.command(name="resetperm", aliases=["delperm"])
async def _resetperm(ctx, *, target: str = None):
    if not target:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}resetperm <commande|@rôle|@membre>`"))
    cmd = bot.get_command(target.lower())
    if cmd is not None and cmd.name in COMMANDS:
        if COMMANDS[cmd.name]["locked"]:
            return await ctx.send(embed=error_embed("Commande verrouillée", "Cette commande ne peut pas être reconfigurée."))
        if cmd.name in BUYER_RETIER_CMDS and ctx.author.id not in BUYER_IDS:
            return await ctx.send(embed=error_embed("Réservé au Buyer", f"Seul un Buyer peut réinitialiser `{cmd.name}`."))
        del_cmd_override(ctx.guild.id, cmd.name)
        return await ctx.send(embed=success_embed("Commande réinitialisée",
                                                  f"`{cmd.name}` revient à **{level_name(COMMANDS[cmd.name]['level'])}**."))
    try:
        role = await commands.RoleConverter().convert(ctx, target)
    except commands.CommandError:
        role = None
    if role is not None:
        if del_role_perm(ctx.guild.id, role.id):
            return await ctx.send(embed=success_embed("Perm retirée", f"{role.mention} repasse public."))
        return await ctx.send(embed=error_embed("Rien à faire", "Ce rôle n'avait pas de perm."))
    display, uid = await resolve_user_or_id(ctx, target)
    if uid is not None:
        if del_member_perm(ctx.guild.id, uid):
            return await ctx.send(embed=success_embed("Perm retirée", f"{format_user_display(display, uid)} repasse public."))
        return await ctx.send(embed=error_embed("Rien à faire", "Ce membre n'avait pas de perm."))
    await ctx.send(embed=error_embed("Cible introuvable"))


@bot.command(name="perms")
async def _perms(ctx):
    viewer = get_user_level(ctx.author)
    em = discord.Embed(title="Qui a quel niveau", color=COLOR_DEFAULT,
                       description=f"Tu vois jusqu'à ton niveau (**{level_name(viewer)}**).")
    apply_thumbnail(em, ctx.guild)
    if viewer >= LEVEL_BUYER:
        em.add_field(name="Buyer", value="\n".join(f"<@{i}>" for i in BUYER_IDS) or "aucun", inline=False)
    if viewer >= LEVEL_OWNER:
        em.add_field(name="Owner", value="\n".join(f"<@{i}>" for i in get_users_by_rank(LEVEL_OWNER)) or "aucun", inline=False)
    if viewer >= LEVEL_WL:
        em.add_field(name="Whitelist", value="\n".join(f"<@{i}>" for i in get_users_by_rank(LEVEL_WL)) or "aucun", inline=False)
    for lvl in range(MAX_TIER, 0, -1):
        if lvl > viewer:
            continue
        roles, users = get_perms_at_level(ctx.guild.id, lvl)
        if not roles and not users:
            continue
        val = ""
        if roles:
            val += "Rôles : " + ", ".join(f"<@&{r}>" for r in roles) + "\n"
        if users:
            val += "Membres : " + ", ".join(f"<@{u}>" for u in users)
        em.add_field(name=f"perm{lvl}", value=trunc(val, 1024) or "vide", inline=False)
    em.set_footer(text=f"{get_prefix_cached()}helpall pour les commandes par niveau")
    await ctx.send(embed=em)


@bot.command(name="helpall")
async def _helpall(ctx):
    viewer = get_user_level(ctx.author)
    by_level = {}
    for name in COMMANDS:
        by_level.setdefault(get_command_level(ctx.guild.id, name), []).append(name)
    order = [LEVEL_BUYER, LEVEL_OWNER, LEVEL_WL] + list(range(MAX_TIER, -1, -1))
    levels = [lvl for lvl in order if lvl <= viewer and by_level.get(lvl)]
    if not levels:
        return await ctx.send(embed=info_embed("Commandes", "Aucune commande accessible."))
    p = get_prefix_cached()
    em = discord.Embed(title="Commandes par niveau", color=COLOR_DEFAULT)
    apply_thumbnail(em, ctx.guild)
    for lvl in levels:
        cmds = sorted(by_level[lvl])
        em.add_field(name=level_name(lvl), value=" ".join(f"`{p}{c}`" for c in cmds), inline=False)
    await ctx.send(embed=em)


# ========================= ALLOW / SETLOG / PREFIX / RENEW =========================

@bot.command(name="allow")
async def _allow(ctx, *, channel_input: str = None):
    if channel_input is None:
        blocked = get_blocked_channels(ctx.guild.id)
        if not blocked:
            return await ctx.send(embed=info_embed(
                "Salons bloqués",
                "Aucun. Les membres peuvent utiliser les commandes publiques partout.\n"
                f"`{get_prefix_cached()}unallow #salon` pour en bloquer un."))
        lines = []
        for cid in blocked:
            ch = ctx.guild.get_channel(int(cid))
            lines.append(f"{ch.mention} (`{cid}`)" if ch else f"`{cid}`")
        return await ctx.send(embed=info_embed(f"Salons bloqués ({len(blocked)})", "\n".join(lines)))
    channel, raw_id = await resolve_channel(ctx, channel_input)
    if not channel:
        if raw_id is not None and unblock_channel(ctx.guild.id, raw_id):
            return await ctx.send(embed=success_embed("Salon réautorisé", f"`{raw_id}`"))
        return await ctx.send(embed=error_embed("Salon introuvable"))
    if unblock_channel(ctx.guild.id, channel.id):
        await ctx.send(embed=success_embed("Salon réautorisé", f"Les commandes publiques remarchent dans {channel.mention}."))
    else:
        await ctx.send(embed=info_embed("Déjà autorisé", f"{channel.mention} n'était pas bloqué."))


@bot.command(name="unallow")
async def _unallow(ctx, *, channel_input: str = None):
    if not channel_input:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}unallow #salon`"))
    channel, raw_id = await resolve_channel(ctx, channel_input)
    if not channel:
        return await ctx.send(embed=error_embed("Salon introuvable"))
    block_channel(ctx.guild.id, channel.id)
    await ctx.send(embed=success_embed("Salon bloqué",
                                       f"Les membres ne peuvent plus utiliser les commandes publiques dans {channel.mention}.\nLe staff passe toujours."))


@bot.command(name="setlog")
async def _setlog(ctx, log_type: str = None, channel: discord.TextChannel = None):
    types = "`, `".join(LOG_TYPES)
    if not log_type or not channel:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}setlog <type|all> #salon`\nTypes : `{types}`"))
    log_type = log_type.lower()
    if log_type == "all":
        for t in LOG_TYPES:
            set_log_channel(ctx.guild.id, t, channel.id)
        return await ctx.send(embed=success_embed("Logs configurés", f"Tous les logs → {channel.mention}"))
    if log_type not in LOG_TYPES:
        return await ctx.send(embed=error_embed("Type inconnu", f"Types : `{types}` (ou `all`)"))
    perms = channel.permissions_for(ctx.guild.me)
    if not (perms.send_messages and perms.embed_links):
        return await ctx.send(embed=error_embed("Action impossible", f"Je ne peux pas envoyer d'embeds dans {channel.mention}."))
    set_log_channel(ctx.guild.id, log_type, channel.id)
    await ctx.send(embed=success_embed("Log configuré", f"**{log_type}** → {channel.mention}"))


@bot.command(name="dellog")
async def _dellog(ctx, log_type: str = None):
    types = "`, `".join(LOG_TYPES)
    if not log_type:
        return await ctx.send(embed=error_embed("Usage", f"`{get_prefix_cached()}dellog <type|all>`\nTypes : `{types}`"))
    log_type = log_type.lower()
    if log_type == "all":
        for t in LOG_TYPES:
            delete_log_channel(ctx.guild.id, t)
        return await ctx.send(embed=success_embed("Logs retirés", "Tous les logs sont désactivés."))
    if log_type not in LOG_TYPES:
        return await ctx.send(embed=error_embed("Type inconnu", f"Types : `{types}` (ou `all`)"))
    delete_log_channel(ctx.guild.id, log_type)
    await ctx.send(embed=success_embed("Log retiré", f"**{log_type}** désactivé"))


@bot.command(name="logs")
async def _logs(ctx):
    all_logs = get_all_log_channels(ctx.guild.id)
    lines = []
    for t in LOG_TYPES:
        cid = all_logs.get(t)
        if cid:
            ch = ctx.guild.get_channel(int(cid))
            lines.append(f"**{t}** → {ch.mention if ch else f'`{cid}` (salon supprimé)'}")
        else:
            lines.append(f"**{t}** → non défini")
    await ctx.send(embed=info_embed("Configuration des logs",
                                    "\n".join(lines) + f"\n\n`{get_prefix_cached()}setlog <type|all> #salon`"))


@bot.command(name="prefix")
async def _prefix(ctx, new_prefix: str = None):
    if not new_prefix:
        return await ctx.send(embed=info_embed("Prefix", f"Prefix actuel : `{get_prefix_cached()}`\nTu peux aussi me mentionner."))
    if len(new_prefix) > 5:
        return await ctx.send(embed=error_embed("Trop long", "5 caractères maximum."))
    set_config("prefix", new_prefix)
    await ctx.send(embed=success_embed("Prefix modifié", f"Nouveau prefix : `{new_prefix}`"))


@bot.command(name="renew", aliases=["nuke"])
async def _renew(ctx):
    channel = ctx.channel
    view = ConfirmView(ctx.author.id)
    view.message = await ctx.send(embed=warning_embed(
        "Recréer ce salon ?",
        f"{channel.mention} sera supprimé et recréé à l'identique. Tous les messages seront perdus."), view=view)
    await view.wait()
    if not view.value:
        try:
            await view.message.edit(embed=info_embed("Annulé", "Le salon n'a pas été touché."), view=None)
        except discord.HTTPException:
            pass
        return
    try:
        new_ch = await channel.clone(reason=f"[{ctx.author}] renew")
        try:
            await new_ch.edit(position=channel.position)
        except discord.HTTPException:
            pass
        await channel.delete(reason=f"[{ctx.author}] renew")
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Permission refusée", "Il me faut la permission « Gérer les salons »."))
    except discord.HTTPException as e:
        return await ctx.send(embed=error_embed("Erreur", str(e)))
    if is_channel_blocked(ctx.guild.id, channel.id):
        unblock_channel(ctx.guild.id, channel.id)
        block_channel(ctx.guild.id, new_ch.id)
    for t, cid in get_all_log_channels(ctx.guild.id).items():
        if int(cid) == channel.id:
            set_log_channel(ctx.guild.id, t, new_ch.id)
    try:
        await new_ch.send(f"Salon recréé par {ctx.author.mention}")
    except discord.HTTPException:
        pass


# ========================= HELP (dropdown, filtré par niveau) =========================

def cat_visible_commands(guild_id, cat, viewer_level):
    out = []
    for name, meta in COMMANDS.items():
        if meta["cat"] != cat:
            continue
        lvl = get_command_level(guild_id, name)
        if viewer_level >= lvl:
            out.append((name, meta, lvl))
    out.sort(key=lambda x: (x[2], x[0]))
    return out


def category_visible(guild_id, cat, viewer_level):
    return len(cat_visible_commands(guild_id, cat, viewer_level)) > 0


def build_help_home_embed(guild, viewer_level):
    p = get_prefix_cached()
    em = discord.Embed(
        title="Moh",
        description=f"**Prefix :** `{p}` · **Ton niveau :** {level_name(viewer_level)}\n\n"
                    "Choisis une catégorie dans le menu pour voir ses commandes.",
        color=COLOR_DEFAULT)
    try:
        if bot.user:
            em.set_thumbnail(url=bot.user.display_avatar.url)
    except Exception:
        pass
    for gname, keys in CATEGORY_GROUPS:
        lines = []
        for k in keys:
            if category_visible(guild.id, k, viewer_level):
                meta = CATEGORY_META[k]
                lines.append(f"{meta['emoji']} **{meta['label']}** — {meta['sub']}")
        if lines:
            em.add_field(name=gname, value="\n".join(lines), inline=False)
    total = sum(len(cat_visible_commands(guild.id, c, viewer_level)) for c in CATEGORY_META)
    em.set_footer(text=f"{total} commande(s) accessible(s) · {len(bot.guilds)} serveur(s)")
    return em


def build_category_embed(guild, cat, viewer_level):
    p = get_prefix_cached()
    meta = CATEGORY_META[cat]
    items = cat_visible_commands(guild.id, cat, viewer_level)
    em = discord.Embed(title=f"{meta['emoji']}  {meta['label']}", description=meta["sub"], color=COLOR_DEFAULT)
    apply_thumbnail(em, guild)
    if not items:
        em.add_field(name="Aucune commande", value="Tu n'as pas le niveau pour cette catégorie.", inline=False)
        return em
    lines = []
    for name, cmeta, lvl in items:
        line = f"`{p}{name}`" + (f" `{cmeta['args']}`" if cmeta["args"] else "")
        line += f" — {cmeta['desc']}"
        if lvl > 0:
            line += f"  ·  *{level_name(lvl)}*"
        lines.append(line)
    em.description = meta["sub"] + "\n\n" + "\n".join(lines)
    return em


class HelpSelect(discord.ui.Select):
    def __init__(self, parent):
        self.parent_view = parent
        options = [discord.SelectOption(label="Accueil", emoji="🏠", value="home")]
        for cat, meta in CATEGORY_META.items():
            if category_visible(parent.guild.id, cat, parent.viewer_level):
                options.append(discord.SelectOption(label=meta["label"], emoji=meta["emoji"],
                                                    value=cat, description=meta["sub"][:100]))
        super().__init__(placeholder="Choisis une catégorie...", min_values=1, max_values=1, options=options[:25])

    async def callback(self, interaction):
        v = self.parent_view
        val = self.values[0]
        em = (build_help_home_embed(v.guild, v.viewer_level) if val == "home"
              else build_category_embed(v.guild, val, v.viewer_level))
        await interaction.response.edit_message(embed=em, view=v)


class HelpView(OwnedView):
    def __init__(self, author_id, guild, viewer_level):
        super().__init__(author_id, guild, timeout=180)
        self.viewer_level = viewer_level
        self.add_item(HelpSelect(self))


@bot.command(name="help", aliases=["aide", "h"])
async def _help(ctx):
    level = get_user_level(ctx.author)
    view = HelpView(ctx.author.id, ctx.guild, level)
    view.message = await ctx.send(embed=build_help_home_embed(ctx.guild, level), view=view)


# ========================= RUN =========================

if __name__ == "__main__":
    try:
        log.info("Démarrage de Moh...")
        bot.run(BOT_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        log.info("Arrêt demandé.")
    except Exception as e:
        log.error(f"Erreur fatale : {e}", exc_info=True)
        sys.exit(1)
    finally:
        if _DB is not None:
            _DB.close()
