# multi_server_leveling_bot_sqlite.py
# Requirements: py-cord
# pip install -U py-cord

import os
import discord
from discord.ext import commands, tasks
from discord import Option
import random
import asyncio
import sqlite3
import math
import functools
from typing import Optional, Tuple, List

# ====== CONFIG ======
BOT_TOKEN = "token"
OWNER_ID = 1237644967422459996  # for DM forwarding
ADMINS = [1237644967422459996]   # global admin IDs
TOP_CHECK_INTERVAL = 60         # seconds
XP_PER_MESSAGE = (5, 15)
XP_PER_REACTION = (5, 15)
LEVEL_MULTIPLIER = 100
XP_COOLDOWN = 60
REACTION_COOLDOWN = 90
STATUS_INTERVAL = 20
DB_PATH = "xp_data.db"
# ====================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
cooldowns = {}  # Stores cooldown timestamps for XP gain

# ====== SQLite (sync) setup but run blocking ops in executor ======
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()
db_lock = asyncio.Lock()  # protect against concurrent writes at Python level

def init_db_sync():
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS xp_data (
        user_id   INTEGER,
        guild_id  INTEGER,
        xp        INTEGER DEFAULT 0,
        level     INTEGER DEFAULT 1,
        PRIMARY KEY (user_id, guild_id)
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reward_roles (
        guild_id INTEGER PRIMARY KEY,
        role_id  INTEGER
    )""")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id INTEGER PRIMARY KEY,
        levelup_channel INTEGER
    )""")
    conn.commit()

# run the sync init at import/start
init_db_sync()

# Helper to run blocking DB calls in executor
async def run_db(func, /, *args, commit: bool = False):
    loop = asyncio.get_running_loop()
    # wrap to execute under db_lock to help avoid sqlite db locked issues
    async with db_lock:
        return await loop.run_in_executor(None, functools.partial(_db_call_sync, func, args, commit))

def _db_call_sync(func, args, commit):
    try:
        result = func(*args)
        if commit:
            conn.commit()
        return result
    except Exception as e:
        # propagate to caller via exception
        raise

# Very small sync helper wrappers for common queries
def _fetchone_sync(query, params=()):
    cur = conn.cursor()
    cur.execute(query, params)
    return cur.fetchone()

def _fetchall_sync(query, params=()):
    cur = conn.cursor()
    cur.execute(query, params)
    return cur.fetchall()

def _execute_sync(query, params=()):
    cur = conn.cursor()
    cur.execute(query, params)
    return cur

# ====== XP helpers (async wrappers around sync DB) ======
def xp_for_next_level(level: int) -> int:
    # Linear formula (keeps your original style). Change if desired.
    return level * LEVEL_MULTIPLIER

async def get_user_data(user_id: int, guild_id: int) -> dict:
    row = await run_db(_fetchone_sync, f"SELECT xp, level FROM xp_data WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
    if row:
        return {"xp": row["xp"], "level": row["level"]}
    # create default
    await run_db(_execute_sync, "INSERT OR REPLACE INTO xp_data (user_id, guild_id, xp, level) VALUES (?, ?, 0, 1)", (user_id, guild_id), commit=True)
    return {"xp": 0, "level": 1}

async def update_user_data(user_id: int, guild_id: int, xp: int, level: int):
    await run_db(_execute_sync, "UPDATE xp_data SET xp = ?, level = ? WHERE user_id = ? AND guild_id = ?", (xp, level, user_id, guild_id), commit=True)

async def add_xp(user: discord.User, guild: discord.Guild, amount: int) -> Tuple[bool, int]:
    data = await get_user_data(user.id, guild.id)
    data["xp"] += amount
    leveled_up = False
    next_level = xp_for_next_level(data["level"])
    if data["xp"] >= next_level:
        data["level"] += 1
        data["xp"] -= next_level
        leveled_up = True
    await update_user_data(user.id, guild.id, data["xp"], data["level"])

    # schedule top-user check (fire-and-forget)
    try:
        bot.loop.create_task(check_top_user_change(guild))
    except Exception:
        pass
    return leveled_up, data["level"]

async def get_top_user_id(guild_id: int) -> Optional[int]:
    row = await run_db(_fetchone_sync, "SELECT user_id FROM xp_data WHERE guild_id = ? ORDER BY level DESC, xp DESC LIMIT 1", (guild_id,))
    return int(row["user_id"]) if row else None

async def get_reward_role_id(guild_id: int) -> Optional[int]:
    row = await run_db(_fetchone_sync, "SELECT role_id FROM reward_roles WHERE guild_id = ?", (guild_id,))
    return int(row["role_id"]) if row else None

async def set_reward_role_id(guild_id: int, role_id: int):
    await run_db(_execute_sync, "INSERT OR REPLACE INTO reward_roles (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id), commit=True)

async def get_level_channel_id(guild_id: int) -> Optional[int]:
    row = await run_db(_fetchone_sync, "SELECT levelup_channel FROM guild_settings WHERE guild_id = ?", (guild_id,))
    return int(row["levelup_channel"]) if row and row["levelup_channel"] is not None else None

async def set_level_channel_id(guild_id: int, channel_id: int):
    await run_db(_execute_sync, "INSERT OR REPLACE INTO guild_settings (guild_id, levelup_channel) VALUES (?, ?)", (guild_id, channel_id), commit=True)

async def add_xp_manual(user_id: int, guild_id: int, amount: int) -> Tuple[bool, int]:
    # convenience for admin xp commands
    dummy_user = discord.Object(id=user_id)
    guild_dummy = discord.Object(id=guild_id)
    # Use add_xp but we need a discord.User and discord.Guild for the function signature;
    # we'll call DB helpers directly to avoid object issues:
    data = await get_user_data(user_id, guild_id)
    data["xp"] += amount
    leveled_up = False
    next_level = xp_for_next_level(data["level"])
    while data["xp"] >= next_level:
        data["level"] += 1
        data["xp"] -= next_level
        leveled_up = True
        next_level = xp_for_next_level(data["level"])
    await update_user_data(user_id, guild_id, data["xp"], data["level"])
    # return new level
    return leveled_up, data["level"]

async def remove_user_xp(user_id: int, guild_id: int, amount: int) -> int:
    data = await get_user_data(user_id, guild_id)
    data["xp"] = max(0, data["xp"] - amount)
    # do not change level automatically here; admin intent is to reduce xp only
    await update_user_data(user_id, guild_id, data["xp"], data["level"])
    return data["xp"]

async def reset_user_xp(user_id: int, guild_id: int):
    await run_db(_execute_sync, "DELETE FROM xp_data WHERE user_id = ? AND guild_id = ?", (user_id, guild_id), commit=True)

# ====== Admin check helper ======
def is_admin_member(member: discord.Member) -> bool:
    if member is None:
        return False
    if member.id in ADMINS:
        return True
    return member.guild_permissions.administrator

# ====== Top1 role assignment & periodic refresh ======
last_top_user = {}  # guild_id -> user_id

async def check_top_user_change(guild: discord.Guild):
    try:
        guild_id = guild.id
        top_id = await get_top_user_id(guild_id)
        if not top_id:
            return
        prev = last_top_user.get(guild_id)
        if prev == top_id:
            return  # no change

        last_top_user[guild_id] = top_id

        role_id = await get_reward_role_id(guild_id)
        if not role_id:
            return

        role = guild.get_role(role_id)
        if not role:
            print(f"[WARN] reward role {role_id} not present in guild {guild.name}")
            return

        # remove role from previous top user if present
        if prev:
            try:
                prev_member = guild.get_member(int(prev))
                if prev_member and role in prev_member.roles:
                    await prev_member.remove_roles(role, reason="New top user replaced previous top")
            except Exception as e:
                print("Failed removing role from prev:", e)

        # assign to new top
        new_member = guild.get_member(int(top_id))
        if new_member:
            try:
                await new_member.add_roles(role, reason="Assigned reward role to top XP user")
                ch = guild.system_channel
                if ch:
                    await ch.send(f"🏅 Congrats {new_member.mention} — you're now #1 in {guild.name}! You got {role.mention}.")
                print(f"Assigned reward role to {new_member} in {guild.name}")
            except discord.Forbidden:
                print(f"Missing permission to add role {role_id} in guild {guild.name}")
            except Exception as e:
                print("Error assigning role:", e)
    except Exception as exc:
        print("check_top_user_change error:", exc)

@tasks.loop(seconds=TOP_CHECK_INTERVAL)
async def periodic_top_check():
    await bot.wait_until_ready()
    for guild in bot.guilds:
        try:
            await check_top_user_change(guild)
        except Exception as e:
            print("periodic_top_check inner error:", e)

# ====== DM forwarding (unaltered) ======
@bot.event
async def on_message(message: discord.Message):
    # 1) DM forwarding
    if isinstance(message.channel, discord.DMChannel) and not message.author.bot:
        try:
            owner = await bot.fetch_user(OWNER_ID)
            embed = discord.Embed(title="✉️ New DM to Bot",
                                  description=message.content or "[No text]",
                                  color=discord.Color.blurple())
            embed.set_author(name=f"{message.author} ({message.author.id})", icon_url=getattr(message.author.display_avatar, "url", None))
            embed.set_footer(text="Forwarded automatically")
            await owner.send(embed=embed)
            # forward attachments
            for att in message.attachments:
                try:
                    await owner.send(file=await att.to_file())
                except Exception:
                    pass
        except Exception as e:
            print("Failed forwarding DM:", e)
        return  # do not process DMs further

    # 2) XP awarding (only in guilds)
    # allow commands, so process_commands happens at end
    if not message.guild or message.author.bot:
        await bot.process_commands(message)
        return

    guild_id = message.guild.id
    user_id = message.author.id
    now = asyncio.get_event_loop().time()
    last_time = cooldowns.get((guild_id, user_id), 0)

    if now - last_time >= XP_COOLDOWN:
        exp_gain = random.randint(*XP_PER_MESSAGE)
        leveled_up, level = await add_xp(message.author, message.guild, exp_gain)
        cooldowns[(guild_id, user_id)] = now
        if leveled_up:
            # pick the configured level-up channel if set, else use message.channel
            channel_id = await get_level_channel_id(guild_id)
            ch = message.guild.get_channel(channel_id) if channel_id else message.channel
            try:
                embed = discord.Embed(title="Level Up!", description=f"{message.author.mention} just reached **Level {level}** 🎉", color=discord.Color.green())
                await ch.send(embed=embed)
            except Exception:
                pass

    await bot.process_commands(message)

# Reaction XP
@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    if user.bot or not reaction.message.guild:
        return

    guild_id = reaction.message.guild.id
    now = asyncio.get_event_loop().time()
    last_time = cooldowns.get((guild_id, f"react_{user.id}"), 0)

    if now - last_time >= REACTION_COOLDOWN:
        exp_gain = random.randint(*XP_PER_REACTION)
        leveled_up, level = await add_xp(user, reaction.message.guild, exp_gain)
        cooldowns[(guild_id, f"react_{user.id}")] = now
        if leveled_up:
            try:
                await reaction.message.channel.send(f"⭐ {user.mention} leveled up to **Level {level}** via reactions!")
            except Exception:
                pass

    # give xp to message author as well (like earlier)
    author = reaction.message.author
    if author and not author.bot:
        last_author = cooldowns.get((guild_id, f"react_{author.id}"), 0)
        if now - last_author >= REACTION_COOLDOWN:
            exp_gain = random.randint(*XP_PER_REACTION)
            await add_xp(author, reaction.message.guild, exp_gain)
            cooldowns[(guild_id, f"react_{author.id}")] = now

# ====== Commands: prefix and slash versions (embeds) ======
def build_rank_embed(member: discord.Member, data: dict) -> discord.Embed:
    next_xp = xp_for_next_level(data["level"])
    em = discord.Embed(title=f"🏅 {member.display_name}'s Rank", color=discord.Color.blue())
    em.add_field(name="Level", value=str(data["level"]), inline=True)
    em.add_field(name="XP", value=f"{data['xp']}/{next_xp}", inline=True)
    return em

# !rank
@bot.command(name="rank")
async def rank_prefix(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    data = await get_user_data(member.id, ctx.guild.id)
    await ctx.send(embed=build_rank_embed(member, data))

# /rank
@bot.slash_command(description="Show a member's rank")
async def rank(ctx, member: Option(discord.Member, "member to check", required=False) = None):
    member = member or ctx.author
    data = await get_user_data(member.id, ctx.guild.id)
    await ctx.respond(embed=build_rank_embed(member, data))

# Leaderboard helper
def format_leaderboard_embed(guild: discord.Guild, rows: List[sqlite3.Row]) -> discord.Embed:
    embed = discord.Embed(title=f"🏆 {guild.name} Leaderboard", color=discord.Color.gold())
    desc = ""
    for i, row in enumerate(rows, start=1):
        uid = int(row["user_id"])
        xp = row["xp"]
        lvl = row["level"]
        member = guild.get_member(uid)
        name = member.display_name if member else f"<@{uid}>"
        desc += f"**{i}.** {name} — Level {lvl} ({xp} XP)\n"
    embed.description = desc or "No XP data found."
    return embed

# !leaderboard
@bot.command(name="leaderboard")
async def leaderboard_prefix(ctx: commands.Context):
    rows = await run_db(_fetchall_sync, "SELECT user_id, xp, level FROM xp_data WHERE guild_id = ? ORDER BY level DESC, xp DESC LIMIT 10", (ctx.guild.id,))
    await ctx.send(embed=format_leaderboard_embed(ctx.guild, rows))

# /leaderboard
@bot.slash_command(description="Show server leaderboard")
async def leaderboard(ctx):
    rows = await run_db(_fetchall_sync, "SELECT user_id, xp, level FROM xp_data WHERE guild_id = ? ORDER BY level DESC, xp DESC LIMIT 10", (ctx.guild.id,))
    await ctx.respond(embed=format_leaderboard_embed(ctx.guild, rows))

 
# ==== HELP COMMAND ====
bot.remove_command("help") 
@bot.command(name="help")
async def help_prefix(ctx):
    embed = discord.Embed(
        title="🧭 Kairo Leveling Bot — Help Menu",
        description="Here's a list of all available commands and how to use them.",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="🆙 Leveling",
        value="`!rank` — Show your current level and XP.\n"
              "`!leaderboard` — Show top 10 users in this server.\n"
              "`!setlevelchannel #channel` — Set where level-up messages appear (Admin only).",
        inline=False
    )

    embed.add_field(
        name="🏅 XP Management (Admins)",
        value="`!xpadd @user <amount>` — Add XP to a user manually.\n"
              "`!setrewardrole @role` — Set the role for the top XP user.",
        inline=False
    )

    embed.add_field(
        name="💬 Other",
        value="`!dm @user <message>` — Send a DM through the bot (Admins only).\n"
              "`!help` — Show this help menu.",
        inline=False
    )

    


@bot.slash_command(description="Show all available commands and how to use them")
async def help(ctx):
    embed = discord.Embed(
        title="🧭 Kairo Leveling Bot — Help Menu",
        description="Here's a list of all available commands and how to use them.",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="🆙 Leveling",
        value="`/rank` — Show your current level and XP.\n"
              "`/leaderboard` — Show top 10 users in this server.\n"
              "`/setlevelchannel` — Set where level-up messages appear (Admin only).",
        inline=False
    )

    embed.add_field(
        name="🏅 XP Management (Admins)",
        value="`/xpadd` — Add XP to a user manually.\n"
              "`/setrewardrole` — Set the role for the top XP user.",
        inline=False
    )

    embed.add_field(
        name="💬 Other",
        value="`/dm` — Send a DM through the bot (Admins only).\n"
              "`/help` — Show this help menu.",
        inline=False
    )

    embed.set_footer(text="You can also use text commands like !rank or !leaderboard and if you have any problem with the bot, contract @dj_kaif_bd ")
    await ctx.respond(embed=embed)

    
# ---- setlevelchannel ----------------
# !setlevelchannel
@bot.command(name="setlevelchannel")
async def setlevelchannel_prefix(ctx: commands.Context, channel: discord.TextChannel):
    if not is_admin_member(ctx.author):
        return await ctx.send("❌ You need Administrator permission to use this.")
    await set_level_channel_id(ctx.guild.id, channel.id)
    await ctx.send(f"✅ Level-up messages will now appear in {channel.mention}")

# /setlevelchannel
@bot.slash_command(description="Set the level-up announcement channel (Admin only)")
async def setlevelchannel(ctx, channel: Option(discord.TextChannel, "channel to send level-up messages")):
    if not is_admin_member(ctx.author):
        return await ctx.respond("❌ You need Administrator permission to use this.", ephemeral=True)
    await set_level_channel_id(ctx.guild.id, channel.id)
    await ctx.respond(f"✅ Level-up messages will now appear in {channel.mention}")

# ---- setrewardrole ----------------
@bot.command(name="setrewardrole")
async def setrewardrole_prefix(ctx: commands.Context, role: discord.Role):
    if not is_admin_member(ctx.author):
        return await ctx.send("❌ You need Administrator permission to use this.")
    await set_reward_role_id(ctx.guild.id, role.id)
    await ctx.send(f"✅ Reward role set to {role.mention}")

@bot.slash_command(description="Set reward role for this server (Admin only)")
async def setrewardrole(ctx, role: Option(discord.Role, "role to set")):
    if not is_admin_member(ctx.author):
        return await ctx.respond("❌ You need Administrator permission to use this.", ephemeral=True)
    await set_reward_role_id(ctx.guild.id, role.id)
    await ctx.respond(f"✅ Reward role set to {role.mention}")

# ---- xpadd (admin) ----------------
@bot.command(name="addxp")
async def addxp_prefix(ctx: commands.Context, member: discord.Member, amount: int):
    if not is_admin_member(ctx.author):
        return await ctx.send("❌ You need Administrator permission to use this.")
    leveled_up, level = await add_xp_manual(member.id, ctx.guild.id, amount)
    await ctx.send(f"✅ Added {amount} XP to {member.mention}. New level: {level}")

@bot.slash_command(description="Add XP to a member (Admin only)")
async def addxp(ctx, member: Option(discord.Member, "member"), amount: Option(int, "amount")):
    if not is_admin_member(ctx.author):
        return await ctx.respond("❌ You need Administrator permission to use this.", ephemeral=True)
    leveled_up, level = await add_xp_manual(member.id, ctx.guild.id, int(amount))
    await ctx.respond(f"✅ Added {amount} XP to {member.mention}. New level: {level}")

# ---- removexp (admin) ----------------
@bot.command(name="removexp")
async def removexp_prefix(ctx: commands.Context, member: discord.Member, amount: int):
    if not is_admin_member(ctx.author):
        return await ctx.send("❌ You need Administrator permission to use this.")
    await reset_user_xp(member.id, ctx.guild.id)
    await ctx.respond(f"✅ Reset XP for {member.mention}")

# ---- dm command for ADMINS (like before) ----------------
@bot.command(name="dm")
async def dm_prefix(ctx: commands.Context, user: discord.User, *, message_content: str):
    if ctx.author.id not in ADMINS and not is_admin_member(ctx.author):
        await ctx.send("❌ You don't have permission to use this command.")
        return
    try:
        await user.send(message_content)
        await ctx.send(f"✅ DM sent to {user}")
    except Exception as e:
        await ctx.send(f"❌ Failed to DM: {e}")

# ====== Status rotation (same idea) ======
STATUS_ITEMS = [
    {"type": "playing",  "name": "and Leveling up across servers"},
    {"type": "watching", "name": "the leaderboards 👀"},
    {"type": "listening", "name": "to /rank commands"}
]

async def status_loop():
    await bot.wait_until_ready()
    i = 0
    while not bot.is_closed():
        item = STATUS_ITEMS[i % len(STATUS_ITEMS)]
        try:
            if item["type"] == "playing":
                await bot.change_presence(activity=discord.Game(name=item["name"]))
            elif item["type"] == "watching":
                await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=item["name"]))
            elif item["type"] == "listening":
                await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=item["name"]))
        except Exception:
            pass
        i += 1
        await asyncio.sleep(STATUS_INTERVAL)

# ====== Startup handlers ======
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    # start periodic check/task if not already started
    if not periodic_top_check.is_running():
        periodic_top_check.start()
    # start status loop task
    if not hasattr(bot, "_status_task_started"):
        bot.loop.create_task(status_loop())
        bot._status_task_started = True
    # try to sync commands
    try:
        if hasattr(bot, "sync_commands"):
            await bot.sync_commands()
            print("Synced slash commands (sync_commands).")
        elif hasattr(bot, "tree"):
            try:
                await bot.tree.sync()
                print("Synced application commands (tree.sync).")
            except Exception:
                pass
    except Exception as e:
        print("Command sync error:", e)

@bot.event
async def on_guild_join(guild):
    # Try to find a general or system channel to send welcome
    channel = guild.system_channel
    if not channel:
        # fallback: find first text channel bot can send messages in
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                channel = ch
                break
    if not channel:
        return

    embed = discord.Embed(
        title=f"👋 Hello, {guild.name}!",
        description=(
            "Thanks for inviting **Kairo Leveling Bot**!\n\n"
            "📈 This bot tracks user activity, gives XP for messages & reactions, "
            "and rewards the most active member with a special role!\n\n"
            "💡 Try these commands to get started:\n"
            "• `/help` — Show all commands\n"
            "• `/setrewardrole` — Set the role for top XP user (Admin only)\n"
            "• `/setlevelchannel` — Choose where to post level-up messages\n\n"
            "Happy leveling! 🚀"
        ),
        color=discord.Color.blurple()
    )

    embed.set_footer(text="Developed by DJ KAIF ( @dj_kaif_bd ) | Powered by Kairo Bot ⚡")
    await channel.send(embed=embed)

        
        
# ====== Run bot ======
if __name__ == "__main__":
    # start bot
    bot.run(BOT_TOKEN)
      
