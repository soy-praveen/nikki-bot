import os
import json
import re
import time
import threading
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands
import google.generativeai as genai
from flask import Flask
import requests

# ==== ENVIRONMENT VARIABLES ====
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PORT = int(os.environ.get("PORT", 10000))
PING_URL = os.environ.get("PING_URL")  # e.g., https://your-render-url.onrender.com/

# ==== FLASK SELF-PING SERVER ====
app = Flask(__name__)

@app.route("/")
def home():
    return "Nikki bot is alive!"

def ping_self():
    while True:
        try:
            if PING_URL:
                requests.get(PING_URL)
        except Exception as e:
            print(f"Self-ping failed: {e}")
        time.sleep(600)  # every 10 minutes

# ==== DISCORD BOT SETUP ====
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

# ==== FILES & STORAGE ====
MEMORY_FILE = "conversation_memory.json"
REMINDERS_FILE = "reminders.json"
AIRDROPS_FILE = "airdrops.json"
MAIN_CHANNEL_ID = 1376073097068675183

conversation_memory = {}
active_reminders = {}
reminder_id_counter = 0
airdrops_data = {}
airdrop_id_counter = 0

# ==== LOAD/SAVE HELPERS ====
def load_json(file, default):
    try:
        with open(file, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save_json(file, data):
    with open(file, 'w') as f:
        json.dump(data, f, indent=2)

def load_memory():
    global conversation_memory
    conversation_memory = load_json(MEMORY_FILE, {})

def save_memory():
    save_json(MEMORY_FILE, conversation_memory)

def load_reminders():
    global active_reminders, reminder_id_counter
    data = load_json(REMINDERS_FILE, {'reminders': {}, 'counter': 0})
    active_reminders = data.get('reminders', {})
    reminder_id_counter = data.get('counter', 0)

def save_reminders():
    save_json(REMINDERS_FILE, {'reminders': active_reminders, 'counter': reminder_id_counter})

def load_airdrops():
    global airdrops_data, airdrop_id_counter
    data = load_json(AIRDROPS_FILE, {'airdrops': {}, 'counter': 0})
    airdrops_data = data.get('airdrops', {})
    airdrop_id_counter = data.get('counter', 0)

def save_airdrops():
    save_json(AIRDROPS_FILE, {'airdrops': airdrops_data, 'counter': airdrop_id_counter})

# ==== TIME HELPERS ====
def parse_time_to_seconds(time_str):
    time_str = time_str.lower().strip()
    matches = re.findall(r'(\d+)([smhd])', time_str)
    if not matches: return None
    total = 0
    for val, unit in matches:
        val = int(val)
        if unit == 's': total += val
        elif unit == 'm': total += val * 60
        elif unit == 'h': total += val * 3600
        elif unit == 'd': total += val * 86400
    return total if total > 0 else None

def format_time(seconds):
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if secs: parts.append(f"{secs}s")
    return " ".join(parts) if parts else "0s"

def parse_date(date_str):
    try:
        if '/' in date_str:
            return datetime.strptime(date_str, "%d/%m/%Y")
        elif '-' in date_str:
            return datetime.strptime(date_str, "%d-%m-%Y")
    except ValueError:
        return None

# ==== GEMINI SETUP ====
genai.configure(api_key=GEMINI_API_KEY)
def get_model():
    return genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        generation_config={"temperature": 0.9, "top_p": 0.95, "top_k": 40, "max_output_tokens": 1000},
        safety_settings=[
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
    )

PERSONALITY_PROMPT = (
    "You are Nikhita alias Nikki, a friendly and casual 21-year-old girl who loves chatting with people on Discord. "
    "Key traits: You're bubbly, enthusiastic, use casual language, remember conversations, answer directly, never mention you're an AI, no emojis, "
    "interests in music, movies, gaming, everyday life, helpful but real, sometimes slang/sarcastic, playful. "
    "IMPORTANT: Never mention that you're an AI, chatbot, or language model. Always respond as if you're a real person named Nikki."
)

def clean_response(response):
    ai_phrases = [
        "As an AI", "I'm an AI", "I'm a language model", "I'm ChatGPT",
        "I don't have personal experiences", "I can't actually",
        "I should mention that", "It's important to note",
        "I'm programmed to", "As a chatbot"
    ]
    for phrase in ai_phrases:
        response = response.replace(phrase, "")
    return response.strip()

def build_context(user_id, current_message):
    user_data = conversation_memory[user_id]
    username = user_data["username"]
    context = f"{PERSONALITY_PROMPT}\n\nYou're chatting with {username}. "
    if user_data["conversations"]:
        context += "Here's your recent conversation history:\n\n"
        for conv in user_data["conversations"][-10:]:
            if conv["response"]:
                context += f"{username}: {conv['user']}\nNikki: {conv['response']}\n\n"
    context += f"\nCurrent message from {username}: {current_message}\n\nRespond as Nikki naturally, remembering your previous conversations:"
    return context

# ==== REMINDER VIEW ====
class ReminderView(discord.ui.View):
    def __init__(self, reminder_id, user_id):
        super().__init__(timeout=None)
        self.reminder_id = reminder_id
        self.user_id = user_id

    @discord.ui.button(label="✅ Completed", style=discord.ButtonStyle.success, custom_id="reminder_completed")
    async def completed_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("Only the person who set this reminder can mark it as completed!", ephemeral=True)
            return
        if self.reminder_id in active_reminders:
            reminder = active_reminders[self.reminder_id]
            reminder['next_reminder'] = (datetime.now() + timedelta(seconds=reminder['interval'])).isoformat()
            save_reminders()
            await interaction.response.edit_message(
                content=f"✅ **Reminder completed!** Next reminder in {format_time(reminder['interval'])}.\n\n📝 **Message:** {reminder['message']}",
                view=self
            )
        else:
            await interaction.response.edit_message(content="❌ This reminder no longer exists.", view=None)

    @discord.ui.button(label="🗑️ Revoke", style=discord.ButtonStyle.danger, custom_id="reminder_revoke")
    async def revoke_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("Only the person who set this reminder can revoke it!", ephemeral=True)
            return
        if self.reminder_id in active_reminders:
            reminder_message = active_reminders[self.reminder_id]['message']
            del active_reminders[self.reminder_id]
            save_reminders()
            await interaction.response.edit_message(
                content=f"🗑️ **Reminder revoked successfully!**\n\n~~📝 **Message:** {reminder_message}~~",
                view=None
            )
        else:
            await interaction.response.edit_message(content="❌ This reminder was already revoked.", view=None)

# ==== EVENTS ====
@bot.event
async def on_ready():
    print(f'{bot.user} has logged in!')
    load_airdrops()
    load_memory()
    load_reminders()
    for reminder_id, reminder in active_reminders.items():
        bot.add_view(ReminderView(reminder_id, str(reminder['user_id'])))
    if not check_reminders.is_running():
        check_reminders.start()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    should_respond = False
    if message.channel.id == MAIN_CHANNEL_ID:
        should_respond = True
    elif (bot.user.mentioned_in(message) or 
          isinstance(message.channel, discord.DMChannel) or
          any(name in message.content.lower() for name in ['nikki', 'nikhita'])):
        should_respond = True
    if should_respond:
        await handle_conversation(message)
    await bot.process_commands(message)

async def handle_conversation(message):
    user_id = str(message.author.id)
    user_message = message.content.replace(f'<@{bot.user.id}>', '').strip()
    if user_id not in conversation_memory:
        conversation_memory[user_id] = {
            "username": message.author.display_name,
            "conversations": [],
            "user_info": {}
        }
    conversation_memory[user_id]["conversations"].append({
        "timestamp": datetime.now().isoformat(),
        "user": user_message,
        "response": None
    })
    if len(conversation_memory[user_id]["conversations"]) > 20:
        conversation_memory[user_id]["conversations"] = conversation_memory[user_id]["conversations"][-20:]
    context = build_context(user_id, user_message)
    try:
        async with message.channel.typing():
            model = get_model()
            response = model.generate_content(context)
            bot_response = clean_response(response.text)
            if len(bot_response) > 2000:
                chunks = [bot_response[i:i+2000] for i in range(0, len(bot_response), 2000)]
                for chunk in chunks:
                    await message.reply(chunk)
            else:
                await message.reply(bot_response)
            conversation_memory[user_id]["conversations"][-1]["response"] = bot_response
            save_memory()
    except Exception as e:
        await message.reply("Ugh, something went wrong on my end 😅 Can you try again?")
        print(f"Error: {e}")

# ==== REMINDER TASK ====
@tasks.loop(seconds=30)
async def check_reminders():
    current_time = datetime.now()
    for reminder_id, reminder in list(active_reminders.items()):
        next_reminder_time = datetime.fromisoformat(reminder['next_reminder'])
        if current_time >= next_reminder_time:
            try:
                channel = bot.get_channel(reminder['channel_id'])
                user = bot.get_user(reminder['user_id'])
                if channel and user:
                    view = ReminderView(reminder_id, str(reminder['user_id']))
                    embed = discord.Embed(
                        title="⏰ Reminder!",
                        description=f"📝 **Message:** {reminder['message']}\n\n⏱️ **Recurring every:** {format_time(reminder['interval'])}",
                        color=0xffaa00,
                        timestamp=current_time
                    )
                    embed.set_footer(text=f"Reminder ID: {reminder_id}")
                    await channel.send(f"{user.mention}", embed=embed, view=view)
                    reminder['next_reminder'] = (current_time + timedelta(seconds=reminder['interval'])).isoformat()
                    save_reminders()
            except Exception as e:
                print(f"Error sending reminder {reminder_id}: {e}")
                del active_reminders[reminder_id]
                save_reminders()

# ==== SLASH COMMANDS ====
@bot.tree.command(name="remind", description="Set a recurring reminder")
@app_commands.describe(
    time_period="Time period to recur (e.g., '1h30m', '45s', '2d', '30m')",
    message="The message to remind you with"
)
async def remind_slash(interaction: discord.Interaction, time_period: str, message: str):
    global reminder_id_counter
    interval_seconds = parse_time_to_seconds(time_period)
    if interval_seconds is None or interval_seconds < 30:
        await interaction.response.send_message(
            "❌ **Invalid time format or too short!** Use formats like `30s`, `5m`, `1h30m`, `2d` (min 30s).",
            ephemeral=True
        )
        return
    if len(message) > 500:
        await interaction.response.send_message("❌ **Reminder message is too long!** Maximum 500 characters.", ephemeral=True)
        return
    reminder_id_counter += 1
    reminder_id = f"reminder_{reminder_id_counter}"
    next_reminder_time = datetime.now() + timedelta(seconds=interval_seconds)
    active_reminders[reminder_id] = {
        'user_id': interaction.user.id,
        'channel_id': interaction.channel.id,
        'message': message,
        'interval': interval_seconds,
        'next_reminder': next_reminder_time.isoformat(),
        'created_at': datetime.now().isoformat()
    }
    save_reminders()
    embed = discord.Embed(
        title="✅ Reminder Set Successfully!",
        description=f"📝 **Message:** {message}\n\n⏱️ **Recurring every:** {format_time(interval_seconds)}\n\n🕐 **First reminder:** <t:{int(next_reminder_time.timestamp())}:R>",
        color=0x00ff00,
        timestamp=datetime.now()
    )
    embed.set_footer(text=f"Reminder ID: {reminder_id}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="reminders", description="List all your active reminders")
async def reminders_slash(interaction: discord.Interaction):
    user_reminders = {k: v for k, v in active_reminders.items() if v['user_id'] == interaction.user.id}
    if not user_reminders:
        await interaction.response.send_message("📭 **You have no active reminders!**", ephemeral=True)
        return
    embed = discord.Embed(
        title="📋 Your Active Reminders",
        color=0x0099ff,
        timestamp=datetime.now()
    )
    for reminder_id, reminder in user_reminders.items():
        next_time = datetime.fromisoformat(reminder['next_reminder'])
        embed.add_field(
            name=f"🔔 {reminder_id}",
            value=f"**Message:** {reminder['message'][:100]}{'...' if len(reminder['message']) > 100 else ''}\n"
                  f"**Interval:** {format_time(reminder['interval'])}\n"
                  f"**Next:** <t:{int(next_time.timestamp())}:R>",
            inline=False
        )
    embed.set_footer(text=f"Total: {len(user_reminders)} reminder(s)")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="forget", description="Clear your conversation history with Nikki")
async def forget_slash(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id in conversation_memory:
        conversation_memory[user_id]["conversations"] = []
        save_memory()
        await interaction.response.send_message("Okay, I've cleared our chat history! Starting fresh 😊", ephemeral=True)
    else:
        await interaction.response.send_message("We haven't chatted before, so there's nothing to forget!", ephemeral=True)

@bot.tree.command(name="memory", description="Check how many messages you've exchanged with Nikki")
async def memory_slash(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id in conversation_memory:
        count = len(conversation_memory[user_id]["conversations"])
        await interaction.response.send_message(f"We've had {count} messages in our conversation! 💭", ephemeral=True)
    else:
        await interaction.response.send_message("We haven't started chatting yet!", ephemeral=True)

@bot.tree.command(name="info", description="Get information about Nikki")
async def info_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Hey! I'm Nikki! 👋",
        description="I'm a 21-year-old girl who loves chatting with people here on Discord!",
        color=0xff69b4
    )
    embed.add_field(
        name="What I do 💬", 
        value="I chat naturally and remember our conversations! I respond to all messages in my main channel and when you mention my name elsewhere.",
        inline=False
    )
    embed.add_field(
        name="My interests 🎮", 
        value="Music, movies, gaming, and just everyday life stuff! I love having casual conversations.",
        inline=False
    )
    embed.add_field(
        name="Commands 🔧", 
        value="`/forget` - Clear our chat history\n`/memory` - See how many messages we've exchanged\n`/info` - This message!\n`/stats` - Bot statistics\n`/remind` - Set recurring reminders\n`/reminders` - List your active reminders",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="stats", description="Get bot statistics")
async def stats_slash(interaction: discord.Interaction):
    total_users = len(conversation_memory)
    total_conversations = sum(len(user_data["conversations"]) for user_data in conversation_memory.values())
    total_reminders = len(active_reminders)
    embed = discord.Embed(
        title="📊 Nikki's Stats",
        color=0x00ff00
    )
    embed.add_field(name="Total Users", value=f"{total_users}", inline=True)
    embed.add_field(name="Total Messages", value=f"{total_conversations}", inline=True)
    embed.add_field(name="Active Reminders", value=f"{total_reminders}", inline=True)
    embed.add_field(name="Servers", value=f"{len(bot.guilds)}", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ==== AIRDROP TRACKER (add your airdrop commands here as in your original code) ====
# ... (for brevity, you can copy your airdrop management commands from your original code here)

# ==== MAIN ====
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT), daemon=True).start()
    threading.Thread(target=ping_self, daemon=True).start()
    bot.run(DISCORD_BOT_TOKEN)
