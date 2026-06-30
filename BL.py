import discord
from discord.ext import commands
import os
import asyncio
import time
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

DEFAULT_BUYER_IDS = [923200874669563914, 142365250803466240]
DEFAULT_PREFIX = "&"

# Volume persistant Railway : DATA_DIR doit pointer vers un dossier persistant
DATA_DIR = os.environ.get("DATA_DIR")
if not DATA_DIR:
    print("[ERREUR CRITIQUE] DATA_DIR non défini. Configure DATA_DIR=/data dans Railway.")
    sys.exit(1)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bl_bot.db")

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

    c.execute("""
        CREATE TABLE IF NOT EXISTS bl_actions (
            user_id TEXT NOT NULL,
            ts INTEGER NOT NULL
        )
    """)

    c.execute("INSERT OR IGNORE INTO config VALUES ('prefix', ?)", (DEFAULT_PREFIX,))
    c.execute("INSERT OR REPLACE INTO config VALUES ('buyer_ids', ?)",
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


# ---- Emojis personnalisables ----
# Chaque emoji des embeds (ici : le help) est modifiable via &setemoji.
DEFAULT_EMOJIS = {
    "help_home_title":  "🔨",
    "help_cat_bl":      "🔨",
    "help_cat_superbl": "⛔",
    "help_cat_system":  "🛠️",
}

EMOJI_LABELS = {
    "help_home_title":  "Help · Titre accueil",
    "help_cat_bl":      "Help · Catégorie Blacklist",
    "help_cat_superbl": "Help · Catégorie Super BL",
    "help_cat_system":  "Help · Catégorie Système",
}


def get_emojis():
    raw = get_config("emojis")
    data = {}
    if raw:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data = {}
    merged = dict(DEFAULT_EMOJIS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_EMOJIS})
    return merged


def get_emoji(key):
    return get_emojis().get(key, DEFAULT_EMOJIS.get(key, ""))


def set_emoji(key, value):
    emojis = get_emojis()
    emojis[key] = value
    set_config("emojis", json.dumps(emojis))


def reset_emojis():
    set_config("emojis", json.dumps(dict(DEFAULT_EMOJIS)))


# ---- Limites de blacklist (anti-raid / anti-selfbot) ----
# Nombre maximum de blacklists qu'un rang peut faire par fenêtre glissante.
BL_LIMIT_WINDOW = 86400  # 24h en secondes

DEFAULT_BL_LIMITS = {"1": 5, "2": 15, "3": 50, "4": 9999}


def get_bl_limits():
    raw = get_config("bl_limits")
    data = {}
    if raw:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data = {}
    merged = dict(DEFAULT_BL_LIMITS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_BL_LIMITS})
    return merged


def get_bl_limit_for_rank(rank):
    return int(get_bl_limits().get(str(rank), 0))


def set_bl_limit_for_rank(rank, limit):
    limits = get_bl_limits()
    limits[str(rank)] = int(limit)
    set_config("bl_limits", json.dumps(limits))


def reset_bl_limits():
    set_config("bl_limits", json.dumps(dict(DEFAULT_BL_LIMITS)))


def record_bl_action(user_id):
    conn = get_db()
    conn.execute("INSERT INTO bl_actions VALUES (?, ?)", (str(user_id), int(time.time())))
    conn.commit()
    conn.close()


def count_bl_actions(user_id, window=BL_LIMIT_WINDOW):
    cutoff = int(time.time()) - window
    conn = get_db()
    conn.execute("DELETE FROM bl_actions WHERE ts < ?", (cutoff,))  # nettoyage des vieilles entrées
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM bl_actions WHERE user_id = ? AND ts >= ?",
        (str(user_id), cutoff)
    ).fetchone()
    conn.commit()
    conn.close()
    return row["c"]


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
    return discord.Embed(title=title, description=desc, color=0x43b581)


def error_embed(title, desc=""):
    return discord.Embed(title=title, description=desc, color=0xf04747)


def info_embed(title, desc=""):
    return discord.Embed(title=title, description=desc, color=embed_color())


def _emoji_for_select(raw):
    """Convertit une chaîne emoji (unicode ou <:nom:id>) en PartialEmoji, ou None si invalide."""
    if not raw:
        return None
    try:
        return discord.PartialEmoji.from_str(raw)
    except Exception:
        return None


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
    try:
        await channel.send(embed=em)
    except discord.HTTPException as e:
        log.warning(f"send_log: échec d'envoi : {e}")


# ========================= EVENTS =========================

@bot.event
async def on_ready():
    log.info(f"Bot connecté : {bot.user} ({bot.user.id})")
    log.info(f"Prefix : {get_prefix_cached()}")


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


# ========================= HELP SYSTEM (filtré par rang) =========================

# Rangs : 0 = Aucun, 1 = WL, 2 = Owner, 3 = Sys, 4 = Buyer

HELP_CATEGORIES = {
    "bl": {
        "emoji": "🔨",
        "label": "Blacklist",
        "title": "Blacklist",
        "subtitle": "Blacklister des utilisateurs (ban automatique cross-serveur).",
        "sections": [
            ("⚔️", "Sanctionner", [
                ("bl @user <raison>",    "Blacklist (raison min. 5 caractères)", 1),
                ("unbl @user",           "Retirer la blacklist",                 1),
            ]),
            ("👁️", "Consulter", [
                ("bl",                   "Afficher la blacklist",                1),
                ("blinfo @user",         "Infos sur un utilisateur",             1),
            ]),
        ],
    },
    "superbl": {
        "emoji": "⛔",
        "label": "Super BL",
        "title": "Super Blacklist",
        "subtitle": "Blacklist renforcée — irréversible sauf Sys+.",
        "sections": [
            ("⛔", "Super blacklist (Sys+)", [
                ("superbl @user <raison>",  "Super blacklist (raison min. 5 caractères)", 3),
                ("superbl",                 "Afficher la super blacklist",                3),
            ]),
            ("🔓", "Retirer (Sys+)", [
                ("unsuperbl @user",         "Retirer la super blacklist",                 3),
            ]),
        ],
    },
    "system": {
        "emoji": "🛠️",
        "label": "Système",
        "title": "Système",
        "subtitle": "Configuration du bot (prefix, logs, emojis).",
        "sections": [
            ("⚙️", "Buyer only", [
                ("prefix [nouveau]",   "Changer le prefix",            4),
                ("setlog #salon",      "Salon de logs",                4),
                ("setemoji",           "Personnaliser les emojis",     4),
                ("limite",             "Limites de blacklist (anti-raid)", 4),
            ]),
        ],
    },
}


def _bl_accessible_sections(category_key, rank):
    cat = HELP_CATEGORIES.get(category_key, {})
    result = []
    for section in cat.get("sections", []):
        emoji, title, items = section
        visible = [(syn, desc) for (syn, desc, mr) in items if rank >= mr]
        if visible:
            result.append((emoji, title, visible))
    return result


def _bl_accessible_items(category_key, rank):
    cat = HELP_CATEGORIES.get(category_key, {})
    result = []
    for section in cat.get("sections", []):
        _e, _t, items = section
        for (syn, desc, mr) in items:
            if rank >= mr:
                result.append((syn, desc))
    return result


def help_category_visible(category_key, rank):
    cat = HELP_CATEGORIES.get(category_key, {})
    if "min_rank" in cat:
        return rank >= cat["min_rank"]
    return len(_bl_accessible_items(category_key, rank)) > 0


def _bl_apply_thumbnail(em, guild):
    if guild and getattr(guild, "icon", None):
        try:
            em.set_thumbnail(url=guild.icon.url)
        except (AttributeError, TypeError):
            pass


def build_bl_category_embed(category_key, rank, guild=None):
    p = get_prefix_cached()
    cat = HELP_CATEGORIES[category_key]
    emoji = get_emoji(f"help_cat_{category_key}") or cat.get("emoji", "📋")
    title = cat.get("title", "Commandes")
    subtitle = cat.get("subtitle", "")

    em = discord.Embed(
        title=f"{emoji}  {title}",
        description=subtitle if subtitle else None,
        color=embed_color(),
    )
    _bl_apply_thumbnail(em, guild)

    sections = _bl_accessible_sections(category_key, rank)
    if not sections:
        em.add_field(
            name="⛔ Aucune commande accessible",
            value="Tu n'as pas les permissions pour cette catégorie.",
            inline=False,
        )
    else:
        for s_emoji, s_title, s_items in sections:
            cmd_lines = [f"`{p}{syntax}` — {desc}" for syntax, desc in s_items]
            em.add_field(
                name=f"{s_emoji} {s_title}",
                value="\n".join(cmd_lines),
                inline=False,
            )

    if category_key == "bl":
        em.add_field(
            name="💡 Règle importante",
            value="Tu peux uniquement blacklister les personnes d'un rang **inférieur** au tien.",
            inline=False,
        )

    return em


def build_bl_home_embed(rank, guild=None):
    p = get_prefix_cached()
    rank_label = rank_name(rank)

    em = discord.Embed(
        title=f"{get_emoji('help_home_title')}  Bot BL",
        description=(
            f"**Prefix :** `{p}` ・ **Ton rang :** {rank_label}\n\n"
            f"*Choisis une catégorie ci-dessous pour voir ses commandes.*"
        ),
        color=embed_color(),
    )
    _bl_apply_thumbnail(em, guild)

    category_descs = {
        "bl":         "Blacklister et voir la liste",
        "superbl":    "Blacklist renforcée (irréversible sauf Sys+)",
        "system":     "Config du bot",
    }

    user_keys  = ["bl", "superbl"]
    admin_keys = ["system"]

    user_lines = []
    for key in user_keys:
        if help_category_visible(key, rank):
            cat = HELP_CATEGORIES[key]
            user_lines.append(f"{get_emoji(f'help_cat_{key}')} **{cat['label']}** — {category_descs[key]}")
    if user_lines:
        em.add_field(name="🔨 Blacklist", value="\n".join(user_lines), inline=False)

    admin_lines = []
    for key in admin_keys:
        if help_category_visible(key, rank):
            cat = HELP_CATEGORIES[key]
            admin_lines.append(f"{get_emoji(f'help_cat_{key}')} **{cat['label']}** — {category_descs[key]}")
    if admin_lines:
        em.add_field(name="🛠️ Staff & Admin", value="\n".join(admin_lines), inline=False)

    return em


def build_help_embed_for(key, rank, guild=None):
    if key == "home":
        return build_bl_home_embed(rank, guild=guild)
    return build_bl_category_embed(key, rank, guild=guild)


class HelpDropdown(discord.ui.Select):
    def __init__(self, user_rank, guild=None):
        self.user_rank = user_rank
        self.guild = guild
        options = [discord.SelectOption(label="Accueil", emoji="🏠", value="home")]
        for key, cat in HELP_CATEGORIES.items():
            if help_category_visible(key, user_rank):
                options.append(discord.SelectOption(
                    label=cat["label"], emoji=_emoji_for_select(get_emoji(f"help_cat_{key}")), value=key
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
            embed=build_help_embed_for(key, self.user_rank, guild=self.guild),
            view=self.view,
        )


class HelpView(discord.ui.View):
    def __init__(self, author_id, user_rank, guild=None, has_any_access=True):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.user_rank = user_rank
        self.guild = guild
        if has_any_access:
            self.add_item(HelpDropdown(user_rank, guild=guild))

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
    if rank < 1:
        return  # Aucun rang -> la commande ne fait rien
    has_access = any(help_category_visible(key, rank) for key in HELP_CATEGORIES)
    view = HelpView(ctx.author.id, rank, guild=ctx.guild, has_any_access=has_access)
    await ctx.send(embed=build_bl_home_embed(rank, guild=ctx.guild), view=view)


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


# ---- &setemoji (éditeur interactif des emojis) ----

def build_emoji_embed(guild=None):
    emojis = get_emojis()
    em = discord.Embed(
        title="🎨 Emojis personnalisables",
        description="Choisis un emoji à modifier dans le menu déroulant.\n"
                    "Tu peux mettre un emoji classique (🔥) ou un emoji du serveur (`<:nom:id>`).",
        color=embed_color(),
    )
    em.add_field(
        name="🎙️ Help",
        value="\n".join(f"{emojis[k]} **{EMOJI_LABELS[k]}**" for k in DEFAULT_EMOJIS),
        inline=False,
    )
    _bl_apply_thumbnail(em, guild)
    return em


class EmojiSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for key in DEFAULT_EMOJIS:
            options.append(discord.SelectOption(
                label=EMOJI_LABELS.get(key, key)[:100],
                value=key,
                emoji=_emoji_for_select(get_emoji(key)),
                description=key[:100],
            ))
        super().__init__(placeholder="Choisis un emoji à modifier...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        label = EMOJI_LABELS.get(key, key)
        panel_msg = interaction.message
        author_id = self.view.author_id
        guild = self.view.guild

        # Le bot demande l'emoji dans le salon
        await interaction.response.send_message(
            f"📨 Envoie l'emoji pour **{label}** dans ce salon. *(60 secondes)*"
        )

        def check(m):
            return m.author.id == author_id and m.channel.id == interaction.channel.id

        try:
            reply = await bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            try:
                await interaction.edit_original_response(content="⏱️ Temps écoulé, aucun emoji reçu.")
            except discord.HTTPException:
                pass
            return

        val = reply.content.strip()
        if val:
            set_emoji(key, val)

        # Supprime la demande du bot puis l'emoji envoyé par l'utilisateur
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        try:
            await reply.delete()
        except discord.HTTPException:
            pass

        # Met à jour le panneau
        try:
            await panel_msg.edit(embed=build_emoji_embed(guild), view=EmojiView(author_id, guild))
        except discord.HTTPException:
            pass


class EmojiResetButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Tout réinitialiser", style=discord.ButtonStyle.danger, emoji="♻️")

    async def callback(self, interaction: discord.Interaction):
        reset_emojis()
        await interaction.response.edit_message(
            embed=build_emoji_embed(self.view.guild),
            view=EmojiView(self.view.author_id, self.view.guild),
        )


class EmojiView(discord.ui.View):
    def __init__(self, author_id, guild=None):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.guild = guild
        self.add_item(EmojiSelect())
        self.add_item(EmojiResetButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Ce menu n'est pas à toi.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


@bot.command(name="setemoji")
async def _setemoji(ctx, key: str = None, *, value: str = None):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul le **Buyer** peut modifier les emojis."))

    # Mode direct : &setemoji <clé> <emoji>
    if key is not None and value is not None:
        if key not in DEFAULT_EMOJIS:
            valid = "\n".join(f"`{k}`" for k in DEFAULT_EMOJIS)
            return await ctx.send(embed=error_embed("❌ Clé inconnue", f"Clés valides :\n{valid}"))
        set_emoji(key, value.strip())
        return await ctx.send(embed=success_embed("✅ Emoji modifié", f"`{key}` → {value.strip()}"))

    # Mode panneau interactif
    await ctx.send(embed=build_emoji_embed(ctx.guild), view=EmojiView(ctx.author.id, ctx.guild))


# ---- &limite (limites de blacklist par rang, anti-selfbot) ----

RANK_LEVELS_EDIT = [(4, "Buyer"), (3, "Sys"), (2, "Owner"), (1, "Whitelist")]


def build_bl_limit_embed(guild=None):
    limits = get_bl_limits()
    em = discord.Embed(
        title="🛡️ Limites de blacklist",
        description="Nombre maximum de blacklists par **24h** et par rang.\n"
                    "Choisis un rang dans le menu pour modifier sa limite.\n"
                    "*Protection contre un ban-all via compte compromis / selfbot.*",
        color=embed_color(),
    )
    for lvl, name in RANK_LEVELS_EDIT:
        em.add_field(name=name, value=f"**{limits.get(str(lvl), 0)}** / 24h", inline=True)
    _bl_apply_thumbnail(em, guild)
    return em


class BLLimitSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=name, value=str(lvl), description=f"Modifier la limite {name}")
            for lvl, name in RANK_LEVELS_EDIT
        ]
        super().__init__(placeholder="Choisis un rang à modifier...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        lvl = int(self.values[0])
        name = rank_name(lvl)
        panel_msg = interaction.message
        author_id = self.view.author_id
        guild = self.view.guild

        await interaction.response.send_message(
            f"📨 Envoie le nombre de blacklists max / 24h pour **{name}**. *(60 secondes)*"
        )

        def check(m):
            return m.author.id == author_id and m.channel.id == interaction.channel.id

        try:
            reply = await bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            try:
                await interaction.edit_original_response(content="⏱️ Temps écoulé, aucune valeur reçue.")
            except discord.HTTPException:
                pass
            return

        raw = reply.content.strip()
        if raw.isdigit():
            set_bl_limit_for_rank(lvl, int(raw))
        else:
            try:
                await interaction.followup.send("❌ Nombre invalide, aucune modification.", ephemeral=True)
            except discord.HTTPException:
                pass

        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        try:
            await reply.delete()
        except discord.HTTPException:
            pass
        try:
            await panel_msg.edit(embed=build_bl_limit_embed(guild), view=BLLimitView(author_id, guild))
        except discord.HTTPException:
            pass


class BLLimitResetButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Réinitialiser", style=discord.ButtonStyle.danger, emoji="♻️")

    async def callback(self, interaction: discord.Interaction):
        reset_bl_limits()
        await interaction.response.edit_message(
            embed=build_bl_limit_embed(self.view.guild),
            view=BLLimitView(self.view.author_id, self.view.guild),
        )


class BLLimitView(discord.ui.View):
    def __init__(self, author_id, guild=None):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.guild = guild
        self.add_item(BLLimitSelect())
        self.add_item(BLLimitResetButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Ce menu n'est pas à toi.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


@bot.command(name="limite")
async def _limite(ctx):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul le **Buyer** peut gérer les limites de blacklist."))
    await ctx.send(embed=build_bl_limit_embed(ctx.guild), view=BLLimitView(ctx.author.id, ctx.guild))


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

    # Raison obligatoire, minimum 5 caractères (après strip)
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

    # Anti-raid : limite de blacklists par 24h selon le rang
    bl_limit = get_bl_limit_for_rank(author_rank)
    bl_used = count_bl_actions(ctx.author.id)
    if bl_used >= bl_limit:
        return await ctx.send(embed=error_embed(
            "⛔ Limite de blacklist atteinte",
            f"Ton rang (**{rank_name(author_rank)}**) est limité à **{bl_limit}** blacklist(s) par 24h.\n"
            f"Tu en as déjà effectué **{bl_used}**. Réessaie plus tard."
        ))

    add_blacklist(uid, ctx.author.id, reason, is_super=0)
    record_bl_action(ctx.author.id)

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

    # Hiérarchie : on ne peut retirer qu'une bl posée par soi-même ou par un rang inférieur au sien
    adder_id = entry["added_by"]
    adder_rank = get_rank_db(adder_id)
    if str(ctx.author.id) != str(adder_id) and get_rank_db(ctx.author.id) <= adder_rank:
        return await ctx.send(embed=error_embed(
            "🔒 Blacklist protégée",
            f"{format_user_display(display, uid)} a été blacklisté par <@{adder_id}> "
            f"(**{rank_name(adder_rank)}**).\n\n"
            f"Tu es **{rank_name(get_rank_db(ctx.author.id))}** : tu ne peux pas retirer une blacklist "
            f"posée par un rang **égal ou supérieur** au tien.\n"
            f"👉 Seul son auteur, ou quelqu'un d'un rang **strictement supérieur** à **{rank_name(adder_rank)}**, peut la retirer."
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

    # Raison obligatoire, minimum 5 caractères (après strip)
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

    # Anti-raid : limite de blacklists par 24h selon le rang
    bl_limit = get_bl_limit_for_rank(author_rank)
    bl_used = count_bl_actions(ctx.author.id)
    if bl_used >= bl_limit:
        return await ctx.send(embed=error_embed(
            "⛔ Limite de blacklist atteinte",
            f"Ton rang (**{rank_name(author_rank)}**) est limité à **{bl_limit}** blacklist(s) par 24h.\n"
            f"Tu en as déjà effectué **{bl_used}**. Réessaie plus tard."
        ))

    add_blacklist(uid, ctx.author.id, reason, is_super=1)
    record_bl_action(ctx.author.id)
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

    # Hiérarchie : on ne peut retirer qu'une super bl posée par soi-même ou par un rang inférieur au sien
    adder_id = entry["added_by"]
    adder_rank = get_rank_db(adder_id)
    if str(ctx.author.id) != str(adder_id) and get_rank_db(ctx.author.id) <= adder_rank:
        return await ctx.send(embed=error_embed(
            "🔒 Super blacklist protégée",
            f"{format_user_display(display, uid)} a été super blacklisté par <@{adder_id}> "
            f"(**{rank_name(adder_rank)}**).\n\n"
            f"Tu es **{rank_name(get_rank_db(ctx.author.id))}** : tu ne peux pas retirer une super blacklist "
            f"posée par un rang **égal ou supérieur** au tien.\n"
            f"👉 Seul son auteur, ou quelqu'un d'un rang **strictement supérieur** à **{rank_name(adder_rank)}**, peut la retirer."
        ))

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
