import discord
from discord.ext import commands
import os

# Volume persistant : DATA_DIR doit pointer vers un dossier persistant (volume Railway)
DATA_DIR = os.environ.get("DATA_DIR")
if not DATA_DIR:
    print("[ERREUR CRITIQUE] DATA_DIR non défini. Configure DATA_DIR=/data dans Railway.")
    import sys as _sys_exit; _sys_exit.exit(1)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bl_bot.db")
import asyncio
import sqlite3
import json
import sys
import logging
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

# ========================= CONFIG =========================
BOT_TOKEN = os.environ.get("TOKEN")
if not BOT_TOKEN:
    print("[ERREUR CRITIQUE] La variable d'environnement TOKEN n'est pas définie.")
    sys.exit(1)

PARIS_TZ = ZoneInfo("Europe/Paris")

DEFAULT_BUYER_IDS = [1312375517927706630, 1312375955737542676, 1173948561881317389]
DEFAULT_PREFIX = "&"

# Logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%d/%m/%Y %H:%M:%S",
)
log = logging.getLogger("bl")

# Cache du prefix
_prefix_cache = {"value": None}


# ========================= DATABASE =========================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS ranks (
            user_id TEXT PRIMARY KEY,
            rank INTEGER NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            user_id TEXT PRIMARY KEY,
            added_by TEXT NOT NULL,
            reason TEXT,
            added_at TEXT NOT NULL,
            is_super INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS log_channels (
            guild_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL
        )
    """)

    c.execute("INSERT OR IGNORE INTO config VALUES ('prefix', ?)", (DEFAULT_PREFIX,))
    c.execute("INSERT OR IGNORE INTO config VALUES ('buyer_ids', ?)",
              (json.dumps([str(i) for i in DEFAULT_BUYER_IDS]),))

    conn.commit()
    conn.close()


def get_config(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_config(key, value):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO config VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()
    if key == "prefix":
        _prefix_cache["value"] = str(value)


def get_prefix_cached():
    if _prefix_cache["value"] is None:
        _prefix_cache["value"] = get_config("prefix") or DEFAULT_PREFIX
    return _prefix_cache["value"]


def get_rank_db(user_id):
    buyer_ids_raw = get_config("buyer_ids")
    if buyer_ids_raw:
        buyer_ids = json.loads(buyer_ids_raw)
        if str(user_id) in buyer_ids:
            return 4
    conn = get_db()
    row = conn.execute("SELECT rank FROM ranks WHERE user_id = ?", (str(user_id),)).fetchone()
    conn.close()
    return row["rank"] if row else 0


def set_rank_db(user_id, rank):
    conn = get_db()
    if rank == 0:
        conn.execute("DELETE FROM ranks WHERE user_id = ?", (str(user_id),))
    else:
        conn.execute("INSERT OR REPLACE INTO ranks VALUES (?, ?)", (str(user_id), rank))
    conn.commit()
    conn.close()


def get_ranks_by_level(level):
    conn = get_db()
    rows = conn.execute("SELECT user_id FROM ranks WHERE rank = ?", (level,)).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def add_blacklist(user_id, added_by, reason, is_super=0):
    conn = get_db()
    now = datetime.now(PARIS_TZ).strftime("%d/%m/%Y %Hh%M")
    conn.execute(
        "INSERT OR REPLACE INTO blacklist VALUES (?, ?, ?, ?, ?)",
        (str(user_id), str(added_by), reason, now, is_super)
    )
    conn.commit()
    conn.close()


def remove_blacklist(user_id):
    conn = get_db()
    conn.execute("DELETE FROM blacklist WHERE user_id = ?", (str(user_id),))
    conn.commit()
    conn.close()


def get_blacklist_entry(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM blacklist WHERE user_id = ?", (str(user_id),)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_blacklist(is_super=0):
    conn = get_db()
    rows = conn.execute("SELECT * FROM blacklist WHERE is_super = ?", (is_super,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def is_blacklisted(user_id):
    return get_blacklist_entry(user_id) is not None


def get_log_channel(guild_id):
    conn = get_db()
    row = conn.execute("SELECT channel_id FROM log_channels WHERE guild_id = ?", (str(guild_id),)).fetchone()
    conn.close()
    return row["channel_id"] if row else None


def set_log_channel(guild_id, channel_id):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO log_channels VALUES (?, ?)", (str(guild_id), str(channel_id)))
    conn.commit()
    conn.close()


# ========================= HELPERS =========================

def rank_name(level):
    return {4: "Buyer", 3: "Sys", 2: "Owner", 1: "Whitelist", 0: "Aucun"}[level]


def has_min_rank(user_id, minimum):
    return get_rank_db(user_id) >= minimum


def embed_color():
    return 0x2b2d31


def success_embed(title, desc=""):
    em = discord.Embed(title=title, description=desc, color=0x43b581)
    em.set_footer(text="Blacklist Bot")
    return em


def error_embed(title, desc=""):
    em = discord.Embed(title=title, description=desc, color=0xf04747)
    em.set_footer(text="Blacklist Bot")
    return em


def info_embed(title, desc=""):
    em = discord.Embed(title=title, description=desc, color=embed_color())
    em.set_footer(text="Blacklist Bot")
    return em


def get_french_time():
    now = datetime.now(PARIS_TZ)
    JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin",
               "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
    jour = JOURS_FR[now.weekday()]
    mois = MOIS_FR[now.month - 1]
    return f"{jour} {now.day} {mois} {now.year} — {now.strftime('%Hh%M')}"


# ========================= RESOLVE USER =========================

async def resolve_user_or_id(ctx, user_input):
    """
    Retourne (display_obj, user_id) — marche même si la personne n'est plus sur le serveur.
    - (Member, id) si membre du serveur
    - (User, id) si existe sur Discord mais pas sur le serveur
    - (None, id) si ID valide mais compte supprimé/introuvable
    - (None, None) si input vide ou pas parsable
    """
    if not user_input:
        return None, None

    raw = user_input.strip()
    cleaned = raw.strip("<@!>")

    # Tentative : ID numérique ?
    user_id = None
    try:
        user_id = int(cleaned)
    except ValueError:
        # Pas un ID : tente les converters par nom/tag
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

    # On a un ID : d'abord dans la guild
    if ctx.guild:
        member = ctx.guild.get_member(user_id)
        if member:
            return member, user_id

    # Pas membre : fetch global
    try:
        user = await bot.fetch_user(user_id)
        return user, user_id
    except discord.NotFound:
        return None, user_id
    except discord.HTTPException as e:
        log.warning(f"resolve_user_or_id: fetch_user({user_id}) a échoué : {e}")
        return None, user_id


def format_user_display(display_obj, user_id):
    """Affichage mention + ID, avec marqueur hors-serveur si applicable."""
    if display_obj is not None:
        return f"{display_obj.mention} (`{display_obj.id}`)"
    return f"<@{user_id}> (`{user_id}`) *(hors serveur)*"


# ========================= BOT SETUP =========================

init_db()

intents = discord.Intents.all()


def get_prefix(bot, message):
    return get_prefix_cached()


bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)


# ========================= LOG HELPER =========================

async def send_log(guild, action, author, target_display, target_id, reason=None, color=0xf04747):
    channel_id = get_log_channel(guild.id)
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if not channel:
        return

    em = discord.Embed(title=f"📋 {action}", color=color)
    em.add_field(name="Modérateur", value=f"{author.mention} (`{author.id}`)", inline=True)
    em.add_field(name="Utilisateur", value=format_user_display(target_display, target_id), inline=True)
    if reason:
        em.add_field(name="Raison", value=reason, inline=False)
    em.set_footer(text=get_french_time())
    try:
        await channel.send(embed=em)
    except discord.HTTPException as e:
        log.warning(f"send_log: échec d'envoi : {e}")


# ========================= EVENTS =========================

@bot.event
async def on_ready():
    log.info(f"Bot connecté : {bot.user} ({bot.user.id})")
    log.info(f"Prefix : {get_prefix_cached()}")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="blacklist"))


@bot.event
async def on_member_join(member):
    entry = get_blacklist_entry(member.id)
    if entry:
        try:
            await member.ban(reason="Utilisateur blacklisté a tenté de rejoindre.")
        except discord.Forbidden:
            pass
        except discord.HTTPException as e:
            log.warning(f"on_member_join: ban échoué pour {member.id} : {e}")


# ========================= HELP DYNAMIQUE =========================

# Chaque commande porte son rang minimum.
# Rangs : 0 = Aucun, 1 = WL, 2 = Owner, 3 = Sys, 4 = Buyer

HELP_CATEGORIES = {
    "bl": {
        "emoji": "🔨",
        "label": "Blacklist",
        "title": "🔨  Blacklist",
        "items": [
            ("bl @user <raison>", "Blacklist (raison min. 5 caractères)", 1),
            ("unbl @user",        "Retirer la blacklist",                 1),
            ("bl",                "Afficher la blacklist",                1),
            ("blinfo @user",      "Infos sur un utilisateur",             1),
        ],
    },
    "superbl": {
        "emoji": "⛔",
        "label": "Super Blacklist",
        "title": "⛔  Super Blacklist",
        "items": [
            ("superbl @user <raison>", "Super blacklist (raison min. 5 caractères)", 3),
            ("unsuperbl @user",        "Retirer la super blacklist",                  3),
            ("superbl",                "Afficher la super blacklist",                 3),
        ],
    },
    "perms": {
        "emoji": "👥",
        "label": "Permissions",
        "title": "👥  Permissions",
        "items": [
            ("wl @user / unwl @user",        "Gérer la whitelist", 2),
            ("owner @user / unowner @user",  "Gérer les owners",   3),
            ("sys @user / unsys @user",      "Gérer les sys",      4),
        ],
    },
    "system": {
        "emoji": "🛠️",
        "label": "Système",
        "title": "🛠️  Système",
        "items": [
            ("setlog #salon",   "Salon de logs",     4),
            ("prefix [nouveau]", "Changer le prefix", 4),
        ],
    },
    "hierarchy": {
        "emoji": "📋",
        "label": "Hiérarchie",
        "title": "📋  Hiérarchie",
        "min_rank": 1,  # Visible à partir de WL
        "items": [],
    },
}


def help_accessible_items(key, rank):
    cat = HELP_CATEGORIES.get(key, {})
    return [(s, d) for (s, d, mr) in cat.get("items", []) if rank >= mr]


def help_category_visible(key, rank):
    cat = HELP_CATEGORIES.get(key, {})
    if "min_rank" in cat:
        return rank >= cat["min_rank"]
    return len(help_accessible_items(key, rank)) > 0


def build_help_category_embed(key, rank):
    p = get_prefix_cached()
    cat = HELP_CATEGORIES[key]
    em = discord.Embed(title=cat["title"], color=embed_color())
    items = help_accessible_items(key, rank)
    if not items:
        em.description = "*Aucune commande accessible à ton rang.*"
    else:
        max_syntax = max(len(f"{p}{syntax}") for syntax, _ in items)
        lines = [
            f"{p}{syntax}".ljust(max_syntax + 2) + f"→ {desc}"
            for syntax, desc in items
        ]
        em.description = "```\n" + "\n".join(lines) + "\n```"
    em.set_footer(text="Made by gp ・ Bot BL")
    return em


def build_help_hierarchy_embed(rank):
    em = discord.Embed(title="📋  Hiérarchie", color=embed_color())
    lines = ["```\nBuyer > Sys > Owner > Whitelist > Aucun\n```\n"]
    levels = [
        (4, "👑 **Buyer**",     "Accès total, `&prefix`, `&setlog`, `&sys`/`&unsys`"),
        (3, "🔧 **Sys**",       "`&superbl`/`&unsuperbl`, `&owner`/`&unowner`, bl tout le monde"),
        (2, "⭐ **Owner**",     "`&wl`/`&unwl`, bl ceux en dessous"),
        (1, "✨ **Whitelist**", "Bl ceux sans rang uniquement"),
        (0, "👤 **Aucun**",     "Ne peut rien faire sur le bot"),
    ]
    for lvl, name, desc in levels:
        marker = " ← **toi**" if lvl == rank else ""
        lines.append(f"> {name} — {desc}{marker}")
    lines.append("")
    lines.append("ℹ️ Un rang ne peut **jamais** bl quelqu'un de rang égal ou supérieur.")
    em.description = "\n".join(lines)
    em.set_footer(text="Made by gp ・ Bot BL")
    return em


def build_help_home_embed(rank):
    p = get_prefix_cached()
    em = discord.Embed(color=embed_color())
    em.set_author(name="Bot BL ─ Blacklist")

    rank_label = rank_name(rank)
    intro = (
        f"```\n🕐  {get_french_time()}\n```\n"
        f"Bienvenue sur le **Bot BL**.\n\n"
        f"**Prefix :** `{p}` ・ **Ton rang :** {rank_label}\n\n"
    )

    category_descriptions = {
        "bl":        "Blacklist classique",
        "superbl":   "Super blacklist (irréversible sauf Sys+)",
        "perms":     "Attribuer les rangs",
        "system":    "Configuration du bot",
        "hierarchy": "Qui peut faire quoi",
    }
    visible = []
    for key, lbl in category_descriptions.items():
        if help_category_visible(key, rank):
            cat = HELP_CATEGORIES[key]
            visible.append(f"> {cat['emoji']} **{cat['label']}** — {lbl}")

    if visible:
        em.description = intro + "\n".join(visible)
    else:
        em.description = (
            intro
            + "*Tu n'as accès à aucune catégorie de commandes.*\n"
            + "*Contacte un membre du staff si tu penses que c'est une erreur.*"
        )
    em.set_footer(text="Made by gp ・ Bot BL")
    return em


def build_help_embed_for(key, rank):
    if key == "home":
        return build_help_home_embed(rank)
    if key == "hierarchy":
        return build_help_hierarchy_embed(rank)
    return build_help_category_embed(key, rank)


class HelpDropdown(discord.ui.Select):
    def __init__(self, user_rank):
        self.user_rank = user_rank
        options = [discord.SelectOption(label="Accueil", emoji="🏠", value="home")]
        for key, cat in HELP_CATEGORIES.items():
            if help_category_visible(key, user_rank):
                options.append(discord.SelectOption(
                    label=cat["label"], emoji=cat["emoji"], value=key
                ))
        super().__init__(
            placeholder="📂 Choisis une catégorie...",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        if key != "home" and not help_category_visible(key, self.user_rank):
            return await interaction.response.send_message(
                "Tu n'as pas accès à cette catégorie.", ephemeral=True
            )
        await interaction.response.edit_message(
            embed=build_help_embed_for(key, self.user_rank), view=self.view
        )


class HelpView(discord.ui.View):
    def __init__(self, author_id, user_rank, has_any_access=True):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.user_rank = user_rank
        # On n'ajoute le dropdown que si l'user a accès à au moins 1 catégorie
        if has_any_access:
            self.add_item(HelpDropdown(user_rank))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Ce menu n'est pas à toi. Fais `&help` pour voir le tien.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


@bot.command(name="help")
async def _help(ctx):
    rank = get_rank_db(ctx.author.id)
    # Y a-t-il au moins une catégorie visible ?
    has_access = any(help_category_visible(key, rank) for key in HELP_CATEGORIES)
    view = HelpView(ctx.author.id, rank, has_any_access=has_access)
    await ctx.send(embed=build_help_home_embed(rank), view=view)


# ========================= PREFIX & LOGS =========================

@bot.command(name="prefix")
async def _prefix(ctx, new_prefix: str = None):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul le **Buyer** peut changer le prefix."))
    if not new_prefix:
        return await ctx.send(embed=info_embed("Prefix actuel", f"`{get_prefix_cached()}`"))
    set_config("prefix", new_prefix)
    await ctx.send(embed=success_embed("✅ Prefix modifié", f"Nouveau prefix : `{new_prefix}`"))


@bot.command(name="setlog")
async def _setlog(ctx, channel: discord.TextChannel = None):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul le **Buyer** peut définir le salon de logs."))
    if not channel:
        return await ctx.send(embed=error_embed("Argument manquant", "Mentionne un salon."))
    set_log_channel(ctx.guild.id, channel.id)
    await ctx.send(embed=success_embed("✅ Salon de logs défini", f"Les logs seront envoyés dans {channel.mention}."))


# ========================= SYS =========================

@bot.command(name="sys")
async def _sys(ctx, *, user_input: str = None):
    if user_input is None:
        if not has_min_rank(ctx.author.id, 4):
            return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul le **Buyer** peut voir la liste sys."))
        ids = get_ranks_by_level(3)
        if not ids:
            return await ctx.send(embed=info_embed("📋 Liste Sys", "Aucun utilisateur sys."))
        return await ctx.send(embed=info_embed(f"📋 Liste Sys ({len(ids)})", "\n".join([f"<@{uid}>" for uid in ids])))

    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul le **Buyer** peut ajouter des sys."))

    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))

    if get_rank_db(uid) == 3:
        return await ctx.send(embed=error_embed("Déjà Sys", f"{format_user_display(display, uid)} est déjà sys."))
    set_rank_db(uid, 3)
    await ctx.send(embed=success_embed("✅ Sys ajouté", f"{format_user_display(display, uid)} a été ajouté en **sys**."))
    await send_log(ctx.guild, "Sys ajouté", ctx.author, display, uid, color=0x43b581)


@bot.command(name="unsys")
async def _unsys(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul le **Buyer** peut retirer des sys."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention, ID ou nom requis."))

    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))

    if get_rank_db(uid) != 3:
        return await ctx.send(embed=error_embed("Pas Sys", f"{format_user_display(display, uid)} n'est pas sys."))
    set_rank_db(uid, 0)
    await ctx.send(embed=success_embed("✅ Sys retiré", f"{format_user_display(display, uid)} a été retiré des **sys**."))
    await send_log(ctx.guild, "Sys retiré", ctx.author, display, uid, color=0xfaa61a)


# ========================= OWNER =========================

@bot.command(name="owner")
async def _owner(ctx, *, user_input: str = None):
    if user_input is None:
        if not has_min_rank(ctx.author.id, 3):
            return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
        ids = get_ranks_by_level(2)
        if not ids:
            return await ctx.send(embed=info_embed("📋 Liste Owner", "Aucun owner."))
        return await ctx.send(embed=info_embed(f"📋 Liste Owner ({len(ids)})", "\n".join([f"<@{uid}>" for uid in ids])))

    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis pour ajouter des owners."))

    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))

    if get_rank_db(uid) >= 3:
        return await ctx.send(embed=error_embed("❌ Erreur", f"{format_user_display(display, uid)} a un rang supérieur ou égal."))
    set_rank_db(uid, 2)
    await ctx.send(embed=success_embed("✅ Owner ajouté", f"{format_user_display(display, uid)} a été ajouté en **owner**."))
    await send_log(ctx.guild, "Owner ajouté", ctx.author, display, uid, color=0x43b581)


@bot.command(name="unowner")
async def _unowner(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention, ID ou nom requis."))

    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))

    if get_rank_db(uid) != 2:
        return await ctx.send(embed=error_embed("Pas Owner", f"{format_user_display(display, uid)} n'est pas owner."))
    set_rank_db(uid, 0)
    await ctx.send(embed=success_embed("✅ Owner retiré", f"{format_user_display(display, uid)} a été retiré des **owners**."))
    await send_log(ctx.guild, "Owner retiré", ctx.author, display, uid, color=0xfaa61a)


# ========================= WHITELIST =========================

@bot.command(name="wl")
async def _wl(ctx, *, user_input: str = None):
    if user_input is None:
        if not has_min_rank(ctx.author.id, 2):
            return await ctx.send(embed=error_embed("❌ Permission refusée", "**Owner+** requis."))
        ids = get_ranks_by_level(1)
        if not ids:
            return await ctx.send(embed=info_embed("📋 Whitelist", "Aucun utilisateur whitelisté."))
        return await ctx.send(embed=info_embed(f"📋 Whitelist ({len(ids)})", "\n".join([f"<@{uid}>" for uid in ids])))

    if not has_min_rank(ctx.author.id, 2):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Owner+** requis pour ajouter des wl."))

    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))

    if get_rank_db(uid) >= 2:
        return await ctx.send(embed=error_embed("❌ Erreur", f"{format_user_display(display, uid)} a un rang supérieur ou égal."))
    set_rank_db(uid, 1)
    await ctx.send(embed=success_embed("✅ Whitelist ajouté", f"{format_user_display(display, uid)} a été ajouté à la **whitelist**."))
    await send_log(ctx.guild, "Whitelist ajouté", ctx.author, display, uid, color=0x43b581)


@bot.command(name="unwl")
async def _unwl(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 2):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Owner+** requis."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention, ID ou nom requis."))

    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))

    if get_rank_db(uid) != 1:
        return await ctx.send(embed=error_embed("Pas WL", f"{format_user_display(display, uid)} n'est pas whitelisté."))
    set_rank_db(uid, 0)
    await ctx.send(embed=success_embed("✅ Whitelist retiré", f"{format_user_display(display, uid)} a été retiré de la **whitelist**."))
    await send_log(ctx.guild, "Whitelist retiré", ctx.author, display, uid, color=0xfaa61a)


# ========================= BLACKLIST =========================

async def _try_ban_everywhere(user_id, reason):
    """Tente de bannir un user_id sur toutes les guilds du bot. Retourne le nombre de succès."""
    count = 0
    # On a besoin d'un objet User/Object pour passer à guild.ban
    user_obj = discord.Object(id=user_id)
    for guild in bot.guilds:
        try:
            await guild.ban(user_obj, reason=reason)
            count += 1
        except (discord.Forbidden, discord.HTTPException):
            pass
    return count


async def _try_unban_everywhere(user_id, reason):
    count = 0
    user_obj = discord.Object(id=user_id)
    for guild in bot.guilds:
        try:
            await guild.unban(user_obj, reason=reason)
            count += 1
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass
    return count


@bot.command(name="bl")
async def _bl(ctx, user_input: str = None, *, reason: str = None):
    if user_input is None:
        if not has_min_rank(ctx.author.id, 1):
            return await ctx.send(embed=error_embed("❌ Permission refusée", "**Whitelist+** requis."))
        entries = get_all_blacklist(is_super=0)
        if not entries:
            return await ctx.send(embed=info_embed("📋 Blacklist", "Aucun utilisateur blacklisté."))
        desc = "\n".join([f"<@{e['user_id']}> — {e['added_at']}" for e in entries])
        return await ctx.send(embed=info_embed(f"📋 Blacklist ({len(entries)})", desc))

    if not has_min_rank(ctx.author.id, 1):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Whitelist+** requis."))

    # FIX: raison obligatoire, minimum 5 caractères (après strip)
    if not reason or len(reason.strip()) < 5:
        return await ctx.send(embed=error_embed(
            "❌ Raison requise",
            f"Tu dois fournir une raison d'au moins **5 caractères** pour un blacklist.\n"
            f"Usage : `{get_prefix_cached()}bl @user <raison détaillée>`\n\n"
            f"*Une raison claire aide tout le monde à comprendre pourquoi la personne est BL.*"
        ))
    reason = reason.strip()

    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))

    author_rank = get_rank_db(ctx.author.id)
    target_rank = get_rank_db(uid)

    if target_rank >= author_rank:
        return await ctx.send(embed=error_embed(
            "❌ Permission refusée",
            f"Tu ne peux pas blacklist quelqu'un avec le rang **{rank_name(target_rank)}**."
        ))

    if is_blacklisted(uid):
        return await ctx.send(embed=error_embed("Déjà BL", f"{format_user_display(display, uid)} est déjà blacklisté."))

    add_blacklist(uid, ctx.author.id, reason, is_super=0)

    # Tente de ban partout où le bot est (comme ça même un non-membre sera banni s'il revient)
    banned_count = await _try_ban_everywhere(uid, f"Blacklist par {ctx.author} | {reason}")

    if banned_count > 0:
        await ctx.send(embed=success_embed(
            "✅ Blacklisté",
            f"{format_user_display(display, uid)} a été **blacklisté** et banni de **{banned_count}** serveur(s).\n"
            f"**Raison :** {reason}"
        ))
    else:
        await ctx.send(embed=success_embed(
            "✅ Blacklisté",
            f"{format_user_display(display, uid)} a été ajouté à la **blacklist**.\n"
            f"*(ban pas effectué — pas membre ou perms manquantes)*\n"
            f"Il sera ban automatiquement s'il rejoint.\n"
            f"**Raison :** {reason}"
        ))

    await send_log(ctx.guild, "Blacklist", ctx.author, display, uid, reason=reason)


@bot.command(name="unbl")
async def _unbl(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 1):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Whitelist+** requis."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention, ID ou nom requis."))

    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))

    entry = get_blacklist_entry(uid)
    if not entry:
        return await ctx.send(embed=error_embed("Pas BL", f"{format_user_display(display, uid)} n'est pas blacklisté."))
    if entry["is_super"]:
        return await ctx.send(embed=error_embed(
            "❌ Super Blacklisté",
            "Cet utilisateur est **super blacklisté**. Utilise `unsuperbl` (Sys+ requis)."
        ))

    remove_blacklist(uid)
    unban_count = await _try_unban_everywhere(uid, f"Unblacklist par {ctx.author}")

    if unban_count > 0:
        await ctx.send(embed=success_embed(
            "✅ Unblacklisté",
            f"{format_user_display(display, uid)} retiré de la **blacklist** et débanni de **{unban_count}** serveur(s)."
        ))
    else:
        await ctx.send(embed=success_embed(
            "✅ Unblacklisté",
            f"{format_user_display(display, uid)} retiré de la **blacklist**.\n"
            f"*(aucun unban effectué — pas banni ou perms manquantes)*"
        ))

    await send_log(ctx.guild, "Unblacklist", ctx.author, display, uid, color=0x43b581)


# ========================= BLINFO =========================

@bot.command(name="blinfo")
async def _blinfo(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 1):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Whitelist+** requis."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention, ID ou nom requis."))

    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))

    entry = get_blacklist_entry(uid)
    rank = get_rank_db(uid)

    display_name = display.name if display else f"ID {uid} (hors serveur)"
    em = discord.Embed(title=f"📋 Infos — {display_name}", color=embed_color())
    if display:
        em.set_thumbnail(url=display.display_avatar.url)
    em.add_field(name="ID", value=f"`{uid}`", inline=True)
    em.add_field(name="Rang", value=rank_name(rank), inline=True)
    if not display:
        em.add_field(name="Statut serveur", value="*Hors serveur ou compte supprimé*", inline=True)

    if entry:
        status = "⛔ Super Blacklisté" if entry["is_super"] else "🔨 Blacklisté"
        em.add_field(name="Statut BL", value=status, inline=False)
        em.add_field(name="Blacklisté par", value=f"<@{entry['added_by']}>", inline=True)
        em.add_field(name="Date", value=entry["added_at"], inline=True)
        em.add_field(name="Raison", value=entry["reason"] or "Aucune raison fournie", inline=False)
    else:
        em.add_field(name="Statut BL", value="✅ Clean", inline=False)

    em.set_footer(text="Blacklist Bot")
    await ctx.send(embed=em)


# ========================= SUPER BLACKLIST =========================

@bot.command(name="superbl")
async def _superbl(ctx, user_input: str = None, *, reason: str = None):
    if user_input is None:
        if not has_min_rank(ctx.author.id, 3):
            return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
        entries = get_all_blacklist(is_super=1)
        if not entries:
            return await ctx.send(embed=info_embed("📋 Super Blacklist", "Aucun utilisateur super blacklisté."))
        desc = "\n".join([f"<@{e['user_id']}> — {e['added_at']}" for e in entries])
        return await ctx.send(embed=info_embed(f"📋 Super Blacklist ({len(entries)})", desc))

    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis pour super blacklist."))

    # FIX: raison obligatoire, minimum 5 caractères (après strip)
    if not reason or len(reason.strip()) < 5:
        return await ctx.send(embed=error_embed(
            "❌ Raison requise",
            f"Tu dois fournir une raison d'au moins **5 caractères** pour un super blacklist.\n"
            f"Usage : `{get_prefix_cached()}superbl @user <raison détaillée>`\n\n"
            f"*Le super BL est irréversible sans Sys+, une raison claire est obligatoire.*"
        ))
    reason = reason.strip()

    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))

    author_rank = get_rank_db(ctx.author.id)
    target_rank = get_rank_db(uid)

    if target_rank >= author_rank:
        return await ctx.send(embed=error_embed(
            "❌ Permission refusée",
            f"Tu ne peux pas super blacklist quelqu'un avec le rang **{rank_name(target_rank)}**."
        ))

    entry = get_blacklist_entry(uid)
    if entry and entry["is_super"]:
        return await ctx.send(embed=error_embed("Déjà Super BL", f"{format_user_display(display, uid)} est déjà super blacklisté."))

    # Retire une bl normale avant d'upgrade
    if entry and not entry["is_super"]:
        remove_blacklist(uid)

    add_blacklist(uid, ctx.author.id, reason, is_super=1)
    banned_count = await _try_ban_everywhere(uid, f"Super blacklist par {ctx.author} | {reason}")

    if banned_count > 0:
        await ctx.send(embed=success_embed(
            "⛔ Super Blacklisté",
            f"{format_user_display(display, uid)} a été **super blacklisté** et banni de **{banned_count}** serveur(s).\n"
            f"**Raison :** {reason}\n"
            f"Seul `unsuperbl` (Sys+) peut retirer ça."
        ))
    else:
        await ctx.send(embed=success_embed(
            "⛔ Super Blacklisté",
            f"{format_user_display(display, uid)} ajouté à la **super blacklist**.\n"
            f"*(ban pas effectué — pas membre ou perms manquantes)*\n"
            f"Il sera ban automatiquement s'il rejoint.\n"
            f"**Raison :** {reason}"
        ))

    await send_log(ctx.guild, "Super Blacklist", ctx.author, display, uid, reason=reason)


@bot.command(name="unsuperbl")
async def _unsuperbl(ctx, *, user_input: str = None):
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis pour retirer la super blacklist."))
    if not user_input:
        return await ctx.send(embed=error_embed("Argument manquant", "Mention, ID ou nom requis."))

    display, uid = await resolve_user_or_id(ctx, user_input)
    if uid is None:
        return await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Mention, ID ou nom requis."))

    entry = get_blacklist_entry(uid)
    if not entry or not entry["is_super"]:
        return await ctx.send(embed=error_embed("Pas Super BL", f"{format_user_display(display, uid)} n'est pas super blacklisté."))

    remove_blacklist(uid)
    unban_count = await _try_unban_everywhere(uid, f"Un-super-blacklist par {ctx.author}")

    if unban_count > 0:
        await ctx.send(embed=success_embed(
            "✅ Super BL retiré",
            f"{format_user_display(display, uid)} retiré de la **super blacklist** et débanni de **{unban_count}** serveur(s)."
        ))
    else:
        await ctx.send(embed=success_embed(
            "✅ Super BL retiré",
            f"{format_user_display(display, uid)} retiré de la **super blacklist**.\n"
            f"*(aucun unban effectué — pas banni ou perms manquantes)*"
        ))

    await send_log(ctx.guild, "Un-super-blacklist", ctx.author, display, uid, color=0x43b581)


# ========================= ERROR HANDLING =========================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandInvokeError):
        error = error.original

    if isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
        await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Impossible de trouver cet utilisateur."))
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=error_embed(
            "❌ Argument manquant",
            f"Il te manque l'argument : `{error.param.name}`."
        ))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=error_embed("❌ Argument invalide", str(error)))
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send(embed=error_embed("❌ Salon introuvable", "Impossible de trouver ce salon."))
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        log.error(
            f"Erreur non gérée '{ctx.command}' par {ctx.author} : {error}\n"
            + "".join(traceback.format_exception(type(error), error, error.__traceback__))
        )
        try:
            await ctx.send(embed=error_embed(
                "❌ Erreur interne",
                "Une erreur inattendue est survenue. Les logs ont été générés."
            ))
        except discord.HTTPException:
            pass


# ========================= RUN =========================
if __name__ == "__main__":
    try:
        log.info("Démarrage du bot BL...")
        bot.run(BOT_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        log.info("Arrêt demandé par l'utilisateur.")
    except Exception as e:
        log.error(f"Erreur fatale au démarrage : {e}", exc_info=True)
        sys.exit(1)
