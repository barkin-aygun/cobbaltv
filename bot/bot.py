import asyncio
import json
import logging
import os

import aiohttp
from dotenv import load_dotenv
import discord
from discord import app_commands

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bloodcam")
logging.getLogger("discord").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Config — set these in .env on PebbleHost
#   DISCORD_TOKEN       : bot token from Discord Developer Portal
#   DISCORD_CHANNEL_ID  : ID of the channel to post galleries in
#   R2_PUBLIC_URL       : public bucket URL (e.g. https://pub-xxxx.r2.dev)
#   POLL_INTERVAL       : seconds between manifest checks (default 3)
# ---------------------------------------------------------------------------
TOKEN         = os.environ["DISCORD_TOKEN"]
CHANNEL_ID    = int(os.environ["DISCORD_CHANNEL_ID"])
R2_PUBLIC_URL = os.environ["R2_PUBLIC_URL"].rstrip("/")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 3))

MANIFEST_URL = f"{R2_PUBLIC_URL}/manifest.json"


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
class Session:
    def __init__(self):
        self.active = False
        self.match_info = ""
        self.photos: list[str] = []
        self.message: discord.Message | None = None
        self.current_index = 0

    def reset(self, match_info: str):
        self.active = True
        self.match_info = match_info
        self.photos = []
        self.message = None
        self.current_index = 0


session = Session()


# ---------------------------------------------------------------------------
# Gallery helpers
# ---------------------------------------------------------------------------
def build_embed() -> discord.Embed:
    idx = session.current_index
    photo_url = f"{R2_PUBLIC_URL}/{session.photos[idx]}"
    color = discord.Color.red() if session.active else discord.Color.dark_gray()
    embed = discord.Embed(title=session.match_info, color=color)
    embed.set_image(url=photo_url)
    status = "🔴 LIVE  •  " if session.active else ""
    embed.set_footer(text=f"{status}Photo {idx + 1} of {len(session.photos)}")
    return embed


async def refresh_gallery() -> None:
    if not session.message or not session.photos:
        return
    await session.message.edit(embed=build_embed(), view=GalleryView())


class GalleryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id == "gallery_prev":
                    item.disabled = session.current_index <= 0
                elif item.custom_id == "gallery_next":
                    item.disabled = session.current_index >= len(session.photos) - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, custom_id="gallery_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not session.photos:
            await interaction.response.send_message(
                "Session data unavailable — the bot may have restarted.", ephemeral=True
            )
            return
        log.info("Gallery nav: prev (user=%s, %d → %d)", interaction.user, session.current_index, max(0, session.current_index - 1))
        if session.current_index > 0:
            session.current_index -= 1
        await interaction.response.defer()
        await refresh_gallery()

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="gallery_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not session.photos:
            await interaction.response.send_message(
                "Session data unavailable — the bot may have restarted.", ephemeral=True
            )
            return
        last = len(session.photos) - 1
        log.info("Gallery nav: next (user=%s, %d → %d)", interaction.user, session.current_index, min(last, session.current_index + 1))
        if session.current_index < last:
            session.current_index += 1
        await interaction.response.defer()
        await refresh_gallery()


# ---------------------------------------------------------------------------
# Manifest poller — no R2 credentials needed, just a public HTTP fetch
# ---------------------------------------------------------------------------
async def process_new_photo(key: str) -> None:
    log.info("New photo: %s (session total: %d)", key, len(session.photos) + 1)

    was_at_latest = not session.photos or session.current_index == len(session.photos) - 1
    session.photos.append(key)
    if was_at_latest:
        session.current_index = len(session.photos) - 1

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        log.error("Channel %d not found — check DISCORD_CHANNEL_ID", CHANNEL_ID)
        return

    if session.message is None:
        log.info("First photo — posting gallery message to channel %d", CHANNEL_ID)
        session.message = await channel.send(embed=build_embed(), view=GalleryView())
    elif was_at_latest:
        await refresh_gallery()


async def poll_manifest() -> None:
    headers = {"Cache-Control": "no-cache"}
    seen: set[str] = set()

    async with aiohttp.ClientSession() as http:
        # Seed seen with whatever is already in the manifest on startup
        try:
            async with http.get(MANIFEST_URL, headers=headers) as resp:
                if resp.status == 200:
                    manifest: list[str] = await resp.json(content_type=None)
                    seen = set(manifest)
                    log.info("Polling %s every %ds (%d existing entries ignored)", MANIFEST_URL, POLL_INTERVAL, len(seen))
        except Exception as e:
            log.warning("Could not read initial manifest: %s", e)

        while True:
            await asyncio.sleep(POLL_INTERVAL)

            if not session.active:
                continue

            try:
                async with http.get(MANIFEST_URL, headers=headers) as resp:
                    if resp.status == 404:
                        log.debug("manifest.json not found yet — waiting for first upload")
                        continue
                    if resp.status != 200:
                        log.warning("Manifest fetch returned HTTP %d", resp.status)
                        continue

                    manifest = await resp.json(content_type=None)
                    new_keys = sorted(set(manifest) - seen, key=lambda k: int(k.split(".")[0]))
                    for key in new_keys:
                        seen.add(key)
                        await process_new_photo(key)
            except Exception as e:
                log.error("Manifest poll error: %s", e)


# ---------------------------------------------------------------------------
# Discord bot + commands
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


@bot.event
async def on_ready():
    bot.add_view(GalleryView())
    await tree.sync()
    log.info("Bot ready as %s (guild commands synced)", bot.user)
    asyncio.get_event_loop().create_task(poll_manifest())


@tree.command(name="start", description="Start a new bloodcam session")
@app_commands.describe(match_info="Match details shown in the gallery header")
async def cmd_start(interaction: discord.Interaction, match_info: str):
    if session.active:
        log.warning("/start called by %s but session already active", interaction.user)
        await interaction.response.send_message(
            "A session is already active. Use /stop first.", ephemeral=True
        )
        return
    session.reset(match_info)
    log.info("Session started by %s: %r", interaction.user, match_info)
    await interaction.response.send_message(
        f"Session started: **{match_info}**\nWaiting for photos from the Pi...",
        ephemeral=True,
    )


@tree.command(name="stop", description="Stop the current bloodcam session")
async def cmd_stop(interaction: discord.Interaction):
    if not session.active:
        log.warning("/stop called by %s but no session active", interaction.user)
        await interaction.response.send_message("No active session.", ephemeral=True)
        return
    session.active = False
    log.info("Session stopped by %s (%d photos)", interaction.user, len(session.photos))
    await refresh_gallery()
    await interaction.response.send_message("Session stopped.", ephemeral=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    log.info(
        "Starting bloodcam bot (channel=%d, manifest=%s, poll=%ds)",
        CHANNEL_ID, MANIFEST_URL, POLL_INTERVAL,
    )
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
