import discord
from discord.ext import commands
import os
import asyncio
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

# ========================= CONFIG =========================
BOT_TOKEN = os.environ["TOKEN"]
PARIS_TZ = ZoneInfo("Europe/Paris")

DEFAULT_BUYER_IDS = [1312375517927706630]  # Ajoute d'autres IDs ici si besoin
DEFAULT_PREFIX = "&"

# ========================= DATABASE =========================

def get_db():
    conn = sqlite3.connect("bl_bot.db")
    conn.row_factory = sqlite3.Row
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

    # Default config
    import json
    c.execute("INSERT OR IGNORE INTO config VALUES ('prefix', ?)", (DEFAULT_PREFIX,))
    c.execute("INSERT OR IGNORE INTO config VALUES ('buyer_ids', ?)", (json.dumps([str(i) for i in DEFAULT_BUYER_IDS]),))

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


def get_rank_db(user_id):
    import json
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


# ========================= BOT SETUP =========================

init_db()

intents = discord.Intents.all()


def get_prefix(bot, message):
    return get_config("prefix") or DEFAULT_PREFIX


bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)


# ========================= LOG HELPER =========================

async def send_log(guild, action, author, target, reason=None, color=0xf04747):
    channel_id = get_log_channel(guild.id)
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if not channel:
        return

    em = discord.Embed(title=f"📋 {action}", color=color)
    em.add_field(name="Modérateur", value=f"{author.mention} (`{author.id}`)", inline=True)
    em.add_field(name="Utilisateur", value=f"{target.mention if hasattr(target, 'mention') else f'<@{target.id}>'} (`{target.id}`)", inline=True)
    if reason:
        em.add_field(name="Raison", value=reason, inline=False)
    em.set_footer(text=get_french_time())
    try:
        await channel.send(embed=em)
    except discord.Forbidden:
        pass


# ========================= EVENTS =========================

@bot.event
async def on_ready():
    print(f"[OK] Bot connecté en tant que {bot.user} ({bot.user.id})")
    print(f"[OK] Prefix: {get_config('prefix')}")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="blacklist"))


@bot.event
async def on_member_join(member):
    entry = get_blacklist_entry(member.id)
    if entry:
        try:
            await member.ban(reason="Utilisateur blacklisté a tenté de rejoindre.")
        except discord.Forbidden:
            pass


# ========================= HELP =========================

# ========================= HELP SYSTEM (filtré par rang) =========================

# Rangs BL : 0 = Aucun, 1 = Whitelist, 2 = Owner, 3 = Sys, 4 = Buyer

HELP_CATEGORIES = {
    "bl": {
        "emoji": "🔨",
        "label": "Blacklist",
        "title": "Blacklist",
        "subtitle": "Blacklister des utilisateurs sur le serveur.",
        "sections": [
            ("⚔️", "Sanctionner", [
                ("bl @user [raison]",    "Blacklist un utilisateur",     1),
                ("unbl @user",           "Retirer la blacklist",         1),
            ]),
            ("👁️", "Consulter", [
                ("bl",                   "Afficher la blacklist",        0),
                ("blinfo @user",         "Infos sur un utilisateur",     0),
            ]),
        ],
    },
    "superbl": {
        "emoji": "⛔",
        "label": "Super BL",
        "title": "Super Blacklist",
        "subtitle": "Blacklist renforcée — ne peut être retirée que par Sys+.",
        "sections": [
            ("⛔", "Super blacklist (Owner+)", [
                ("superbl @user [raison]",  "Super blacklist un user",    2),
                ("superbl",                 "Afficher la super bl",       0),
            ]),
            ("🔓", "Retirer (Sys+ uniquement)", [
                ("unsuperbl @user",         "Retirer une super bl",       3),
            ]),
        ],
    },
    "perms": {
        "emoji": "👥",
        "label": "Permissions",
        "title": "Permissions",
        "subtitle": "Gérer les rangs du bot (wl, owner, sys).",
        "sections": [
            ("✨", "Whitelist (Owner+)", [
                ("wl @user / unwl @user",       "Gérer la whitelist",  2),
                ("wl",                          "Lister les WL",       2),
            ]),
            ("⭐", "Owner (Sys+)", [
                ("owner @user / unowner @user", "Gérer les owners",    3),
                ("owner",                       "Lister les owners",   3),
            ]),
            ("🔧", "Sys (Buyer)", [
                ("sys @user / unsys @user",     "Gérer les sys",       4),
                ("sys",                         "Lister les sys",      4),
            ]),
        ],
    },
    "system": {
        "emoji": "🛠️",
        "label": "Système",
        "title": "Système",
        "subtitle": "Configuration du bot (prefix, logs).",
        "sections": [
            ("⚙️", "Buyer only", [
                ("prefix [nouveau]",  "Changer le prefix",   4),
                ("setlog #salon",     "Salon de logs",       4),
            ]),
        ],
    },
    "hierarchy": {
        "emoji": "📋",
        "label": "Hiérarchie",
        "title": "Hiérarchie",
        "subtitle": "Les différents rangs du bot et leurs pouvoirs.",
        "min_rank": 1,
        "items": [],
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


def _bl_category_visible(category_key, rank):
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
    p = get_config("prefix") or DEFAULT_PREFIX
    cat = HELP_CATEGORIES[category_key]
    emoji = cat.get("emoji", "📋")
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

    em.set_footer(text="Made by gp ・ Bot BL")
    return em


def build_bl_hierarchy_embed(rank, guild=None):
    em = discord.Embed(
        title="📋  Hiérarchie",
        description="Les différents rangs du bot et leurs pouvoirs.",
        color=embed_color(),
    )
    _bl_apply_thumbnail(em, guild)

    levels = [
        (4, "👑", "Buyer",      "Accès total, gère les Sys"),
        (3, "🔧", "Sys",        "Gère Owner/WL, bl tout le monde, seul à pouvoir unsuperbl"),
        (2, "⭐", "Owner",       "Gère les WL, bl & superbl ceux en dessous"),
        (1, "✨", "Whitelist",   "Bl ceux sans rang uniquement"),
        (0, "👤", "Aucun",       "Peut seulement consulter la BL"),
    ]
    for lvl, icon, name, desc in levels:
        marker = "  ← **toi**" if lvl == rank else ""
        em.add_field(
            name=f"{icon} {name}{marker}",
            value=desc,
            inline=False,
        )

    em.add_field(
        name="ℹ️ Règle importante",
        value="Un rang ne peut **jamais** bl quelqu'un de rang égal ou supérieur.",
        inline=False,
    )
    em.set_footer(text="Made by gp ・ Bot BL")
    return em


def build_bl_home_embed(rank, guild=None):
    p = get_config("prefix") or DEFAULT_PREFIX
    rank_labels = {0: "Aucun", 1: "Whitelist", 2: "Owner", 3: "Sys", 4: "Buyer"}
    rank_label = rank_labels.get(rank, "Aucun")

    em = discord.Embed(
        title="🔨  Panel d'aide — Bot BL",
        description=(
            f"Bot de **blacklist cross-serveur** pour Meira.\n"
            f"**Prefix :** `{p}` ・ **Ton rang :** {rank_label}\n\n"
            f"*Choisis une catégorie ci-dessous pour voir ses commandes.*"
        ),
        color=embed_color(),
    )
    _bl_apply_thumbnail(em, guild)

    category_descs = {
        "bl":         "Blacklister et voir la liste",
        "superbl":    "Blacklist renforcée (irréversible sauf Sys+)",
        "perms":      "Gérer les rangs (wl, owner, sys)",
        "system":     "Config du bot",
        "hierarchy":  "Qui peut faire quoi",
    }

    user_keys  = ["bl", "superbl"]
    admin_keys = ["perms", "system", "hierarchy"]

    user_lines = []
    for key in user_keys:
        if _bl_category_visible(key, rank):
            cat = HELP_CATEGORIES[key]
            user_lines.append(f"{cat['emoji']} **{cat['label']}** — {category_descs[key]}")
    if user_lines:
        em.add_field(name="🔨 Blacklist", value="\n".join(user_lines), inline=False)

    admin_lines = []
    for key in admin_keys:
        if _bl_category_visible(key, rank):
            cat = HELP_CATEGORIES[key]
            admin_lines.append(f"{cat['emoji']} **{cat['label']}** — {category_descs[key]}")
    if admin_lines:
        em.add_field(name="🛠️ Staff & Admin", value="\n".join(admin_lines), inline=False)

    em.set_footer(text=f"Made by gp ・ Bot BL ・ {get_french_time()}")
    return em


def build_bl_embed_for(key, rank, guild=None):
    if key == "home":
        return build_bl_home_embed(rank, guild=guild)
    if key == "hierarchy":
        return build_bl_hierarchy_embed(rank, guild=guild)
    return build_bl_category_embed(key, rank, guild=guild)


class HelpDropdown(discord.ui.Select):
    def __init__(self, rank, guild=None):
        self.rank = rank
        self.guild = guild
        options = [discord.SelectOption(label="Accueil", emoji="🏠", value="home")]
        for key, cat in HELP_CATEGORIES.items():
            if _bl_category_visible(key, rank):
                options.append(discord.SelectOption(
                    label=cat["label"], emoji=cat["emoji"], value=key
                ))
        super().__init__(
            placeholder="📂 Choisis une catégorie...",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        if key != "home" and not _bl_category_visible(key, self.rank):
            return await interaction.response.send_message(
                "Tu n'as pas accès à cette catégorie.", ephemeral=True
            )
        await interaction.response.edit_message(
            embed=build_bl_embed_for(key, self.rank, guild=self.guild),
            view=self.view,
        )


class HelpView(discord.ui.View):
    def __init__(self, author_id, rank, guild=None):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.rank = rank
        self.guild = guild
        self.add_item(HelpDropdown(rank, guild=guild))

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
    view = HelpView(ctx.author.id, rank, guild=ctx.guild)
    await ctx.send(embed=build_bl_home_embed(rank, guild=ctx.guild), view=view)


# ========================= PREFIX & LOGS =========================

@bot.command(name="prefix")
async def _prefix(ctx, new_prefix: str = None):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul le **Buyer** peut changer le prefix."))
    if not new_prefix:
        return await ctx.send(embed=info_embed("Prefix actuel", f"`{get_config('prefix')}`"))
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
async def _sys(ctx, member: discord.Member = None):
    if member is None:
        if not has_min_rank(ctx.author.id, 4):
            return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul le **Buyer** peut voir la liste sys."))
        ids = get_ranks_by_level(3)
        if not ids:
            return await ctx.send(embed=info_embed("📋 Liste Sys", "Aucun utilisateur sys."))
        return await ctx.send(embed=info_embed(f"📋 Liste Sys ({len(ids)})", "\n".join([f"<@{uid}>" for uid in ids])))

    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul le **Buyer** peut ajouter des sys."))
    if get_rank_db(member.id) == 3:
        return await ctx.send(embed=error_embed("Déjà Sys", f"{member.mention} est déjà sys."))
    set_rank_db(member.id, 3)
    await ctx.send(embed=success_embed("✅ Sys ajouté", f"{member.mention} a été ajouté en **sys**."))
    await send_log(ctx.guild, "Sys ajouté", ctx.author, member, color=0x43b581)


@bot.command(name="unsys")
async def _unsys(ctx, member: discord.Member = None):
    if not has_min_rank(ctx.author.id, 4):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "Seul le **Buyer** peut retirer des sys."))
    if member is None:
        return await ctx.send(embed=error_embed("Argument manquant", "Mentionne un utilisateur."))
    if get_rank_db(member.id) != 3:
        return await ctx.send(embed=error_embed("Pas Sys", f"{member.mention} n'est pas sys."))
    set_rank_db(member.id, 0)
    await ctx.send(embed=success_embed("✅ Sys retiré", f"{member.mention} a été retiré des **sys**."))
    await send_log(ctx.guild, "Sys retiré", ctx.author, member, color=0xfaa61a)


# ========================= OWNER =========================

@bot.command(name="owner")
async def _owner(ctx, member: discord.Member = None):
    if member is None:
        if not has_min_rank(ctx.author.id, 3):
            return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
        ids = get_ranks_by_level(2)
        if not ids:
            return await ctx.send(embed=info_embed("📋 Liste Owner", "Aucun owner."))
        return await ctx.send(embed=info_embed(f"📋 Liste Owner ({len(ids)})", "\n".join([f"<@{uid}>" for uid in ids])))

    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis pour ajouter des owners."))
    if get_rank_db(member.id) >= 3:
        return await ctx.send(embed=error_embed("❌ Erreur", f"{member.mention} a un rang supérieur ou égal."))
    set_rank_db(member.id, 2)
    await ctx.send(embed=success_embed("✅ Owner ajouté", f"{member.mention} a été ajouté en **owner**."))
    await send_log(ctx.guild, "Owner ajouté", ctx.author, member, color=0x43b581)


@bot.command(name="unowner")
async def _unowner(ctx, member: discord.Member = None):
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
    if member is None:
        return await ctx.send(embed=error_embed("Argument manquant", "Mentionne un utilisateur."))
    if get_rank_db(member.id) != 2:
        return await ctx.send(embed=error_embed("Pas Owner", f"{member.mention} n'est pas owner."))
    set_rank_db(member.id, 0)
    await ctx.send(embed=success_embed("✅ Owner retiré", f"{member.mention} a été retiré des **owners**."))
    await send_log(ctx.guild, "Owner retiré", ctx.author, member, color=0xfaa61a)


# ========================= WHITELIST =========================

@bot.command(name="wl")
async def _wl(ctx, member: discord.Member = None):
    if member is None:
        if not has_min_rank(ctx.author.id, 2):
            return await ctx.send(embed=error_embed("❌ Permission refusée", "**Owner+** requis."))
        ids = get_ranks_by_level(1)
        if not ids:
            return await ctx.send(embed=info_embed("📋 Whitelist", "Aucun utilisateur whitelisté."))
        return await ctx.send(embed=info_embed(f"📋 Whitelist ({len(ids)})", "\n".join([f"<@{uid}>" for uid in ids])))

    if not has_min_rank(ctx.author.id, 2):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Owner+** requis pour ajouter des wl."))
    if get_rank_db(member.id) >= 2:
        return await ctx.send(embed=error_embed("❌ Erreur", f"{member.mention} a un rang supérieur ou égal."))
    set_rank_db(member.id, 1)
    await ctx.send(embed=success_embed("✅ Whitelist ajouté", f"{member.mention} a été ajouté à la **whitelist**."))
    await send_log(ctx.guild, "Whitelist ajouté", ctx.author, member, color=0x43b581)


@bot.command(name="unwl")
async def _unwl(ctx, member: discord.Member = None):
    if not has_min_rank(ctx.author.id, 2):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Owner+** requis."))
    if member is None:
        return await ctx.send(embed=error_embed("Argument manquant", "Mentionne un utilisateur."))
    if get_rank_db(member.id) != 1:
        return await ctx.send(embed=error_embed("Pas WL", f"{member.mention} n'est pas whitelisté."))
    set_rank_db(member.id, 0)
    await ctx.send(embed=success_embed("✅ Whitelist retiré", f"{member.mention} a été retiré de la **whitelist**."))
    await send_log(ctx.guild, "Whitelist retiré", ctx.author, member, color=0xfaa61a)


# ========================= BLACKLIST =========================

@bot.command(name="bl")
async def _bl(ctx, member: discord.Member = None, *, reason: str = "Aucune raison fournie"):
    if member is None:
        if not has_min_rank(ctx.author.id, 1):
            return await ctx.send(embed=error_embed("❌ Permission refusée", "**Whitelist+** requis."))
        entries = get_all_blacklist(is_super=0)
        if not entries:
            return await ctx.send(embed=info_embed("📋 Blacklist", "Aucun utilisateur blacklisté."))
        desc = "\n".join([f"<@{e['user_id']}> — {e['added_at']}" for e in entries])
        return await ctx.send(embed=info_embed(f"📋 Blacklist ({len(entries)})", desc))

    if not has_min_rank(ctx.author.id, 1):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Whitelist+** requis."))

    author_rank = get_rank_db(ctx.author.id)
    target_rank = get_rank_db(member.id)

    if target_rank >= author_rank:
        return await ctx.send(embed=error_embed("❌ Permission refusée", f"Tu ne peux pas blacklist quelqu'un avec le rang **{rank_name(target_rank)}**."))

    if is_blacklisted(member.id):
        return await ctx.send(embed=error_embed("Déjà BL", f"{member.mention} est déjà blacklisté."))

    add_blacklist(member.id, ctx.author.id, reason, is_super=0)

    try:
        await member.ban(reason=f"Blacklist par {ctx.author} | {reason}")
        await ctx.send(embed=success_embed("✅ Blacklisté", f"{member.mention} a été **blacklisté** et ban.\n**Raison :** {reason}"))
    except discord.Forbidden:
        await ctx.send(embed=error_embed("⚠️ Blacklisté (ban échoué)", f"{member.mention} ajouté à la **blacklist** mais ban impossible. Vérifie mes permissions."))

    await send_log(ctx.guild, "Blacklist", ctx.author, member, reason=reason)


@bot.command(name="unbl")
async def _unbl(ctx, member: discord.User = None):
    if not has_min_rank(ctx.author.id, 1):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Whitelist+** requis."))
    if member is None:
        return await ctx.send(embed=error_embed("Argument manquant", "Mentionne un utilisateur."))

    entry = get_blacklist_entry(member.id)
    if not entry:
        return await ctx.send(embed=error_embed("Pas BL", f"{member.mention} n'est pas blacklisté."))
    if entry["is_super"]:
        return await ctx.send(embed=error_embed("❌ Super Blacklisté", "Cet utilisateur est **super blacklisté**. Utilise `unsuperbl` (Sys+ requis)."))

    remove_blacklist(member.id)

    unban_success = False
    for guild in bot.guilds:
        try:
            await guild.unban(member, reason=f"Unblacklist par {ctx.author}")
            unban_success = True
        except:
            pass

    if unban_success:
        await ctx.send(embed=success_embed("✅ Unblacklisté", f"{member.mention} retiré de la **blacklist** et unban."))
    else:
        await ctx.send(embed=error_embed("⚠️ Unblacklisté (unban échoué)", f"{member.mention} retiré de la **blacklist** mais unban impossible."))

    await send_log(ctx.guild, "Unblacklist", ctx.author, member, color=0x43b581)


# ========================= BLINFO =========================

@bot.command(name="blinfo")
async def _blinfo(ctx, member: discord.User = None):
    if not has_min_rank(ctx.author.id, 1):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Whitelist+** requis."))
    if member is None:
        return await ctx.send(embed=error_embed("Argument manquant", "Mentionne un utilisateur."))

    entry = get_blacklist_entry(member.id)
    rank = get_rank_db(member.id)

    em = discord.Embed(title=f"📋 Infos — {member}", color=embed_color())
    em.set_thumbnail(url=member.display_avatar.url)
    em.add_field(name="ID", value=f"`{member.id}`", inline=True)
    em.add_field(name="Rang", value=rank_name(rank), inline=True)

    if entry:
        status = "⛔ Super Blacklisté" if entry["is_super"] else "🔨 Blacklisté"
        em.add_field(name="Statut", value=status, inline=False)
        em.add_field(name="Blacklisté par", value=f"<@{entry['added_by']}>", inline=True)
        em.add_field(name="Date", value=entry["added_at"], inline=True)
        em.add_field(name="Raison", value=entry["reason"] or "Aucune raison fournie", inline=False)
    else:
        em.add_field(name="Statut", value="✅ Clean", inline=False)

    em.set_footer(text="Blacklist Bot")
    await ctx.send(embed=em)


# ========================= SUPER BLACKLIST =========================

@bot.command(name="superbl")
async def _superbl(ctx, member: discord.Member = None, *, reason: str = "Aucune raison fournie"):
    if member is None:
        if not has_min_rank(ctx.author.id, 3):
            return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis."))
        entries = get_all_blacklist(is_super=1)
        if not entries:
            return await ctx.send(embed=info_embed("📋 Super Blacklist", "Aucun utilisateur super blacklisté."))
        desc = "\n".join([f"<@{e['user_id']}> — {e['added_at']}" for e in entries])
        return await ctx.send(embed=info_embed(f"📋 Super Blacklist ({len(entries)})", desc))

    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis pour super blacklist."))

    author_rank = get_rank_db(ctx.author.id)
    target_rank = get_rank_db(member.id)

    if target_rank >= author_rank:
        return await ctx.send(embed=error_embed("❌ Permission refusée", f"Tu ne peux pas super blacklist quelqu'un avec le rang **{rank_name(target_rank)}**."))

    # Retire de la bl normale si présent
    entry = get_blacklist_entry(member.id)
    if entry and not entry["is_super"]:
        remove_blacklist(member.id)

    if entry and entry["is_super"]:
        return await ctx.send(embed=error_embed("Déjà Super BL", f"{member.mention} est déjà super blacklisté."))

    add_blacklist(member.id, ctx.author.id, reason, is_super=1)

    try:
        await member.ban(reason=f"Super blacklist par {ctx.author} | {reason}")
        await ctx.send(embed=success_embed("⛔ Super Blacklisté", f"{member.mention} a été **super blacklisté**.\n**Raison :** {reason}\nSeul `unsuperbl` (Sys+) peut retirer ça."))
    except discord.Forbidden:
        await ctx.send(embed=error_embed("⚠️ Super BL (ban échoué)", f"{member.mention} ajouté à la **super blacklist** mais ban impossible."))

    await send_log(ctx.guild, "Super Blacklist", ctx.author, member, reason=reason)


@bot.command(name="unsuperbl")
async def _unsuperbl(ctx, member: discord.User = None):
    if not has_min_rank(ctx.author.id, 3):
        return await ctx.send(embed=error_embed("❌ Permission refusée", "**Sys+** requis pour retirer la super blacklist."))
    if member is None:
        return await ctx.send(embed=error_embed("Argument manquant", "Mentionne un utilisateur."))

    entry = get_blacklist_entry(member.id)
    if not entry or not entry["is_super"]:
        return await ctx.send(embed=error_embed("Pas Super BL", f"{member.mention} n'est pas super blacklisté."))

    remove_blacklist(member.id)

    unban_success = False
    for guild in bot.guilds:
        try:
            await guild.unban(member, reason=f"Un-super-blacklist par {ctx.author}")
            unban_success = True
        except:
            pass

    if unban_success:
        await ctx.send(embed=success_embed("✅ Super BL retiré", f"{member.mention} retiré de la **super blacklist** et unban."))
    else:
        await ctx.send(embed=error_embed("⚠️ Super BL retiré (unban échoué)", f"{member.mention} retiré de la **super blacklist** mais unban impossible."))

    await send_log(ctx.guild, "Un-super-blacklist", ctx.author, member, color=0x43b581)


# ========================= ERROR HANDLING =========================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MemberNotFound) or isinstance(error, commands.UserNotFound):
        await ctx.send(embed=error_embed("❌ Utilisateur introuvable", "Impossible de trouver cet utilisateur."))
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=error_embed("❌ Argument manquant", "Tu as oublié un argument."))
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send(embed=error_embed("❌ Salon introuvable", "Impossible de trouver ce salon."))
    else:
        print(f"Erreur: {error}")


# ========================= RUN =========================
try:
    print("[...] Démarrage du bot...")
    bot.run(BOT_TOKEN)
except Exception as e:
    print(f"\n[ERREUR] {e}")
    input("\nAppuie sur Entrée pour fermer...")
