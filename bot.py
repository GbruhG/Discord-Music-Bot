import random
import spotipy
import re
import os
import yt_dlp
import asyncio
import discord
from discord.ext import commands
from discord.ui import Button, View
from spotipy.oauth2 import SpotifyClientCredentials
from typing import Optional, Dict, List


from dotenv import load_dotenv

load_dotenv()

load_dotenv()  # Load .env file

bot_token = os.getenv('BOT_TOKEN')

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Spotify API credentials
SPOTIFY_CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')

# Initialize Spotify client
spotify = spotipy.Spotify(client_credentials_manager=SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
))

ytdl_format_options = {
    'cookies': 'cookies.txt',
    'format': 'bestaudio/best',
    'restrictfilenames': True,
    'noplaylist': False,
    'nocheckcertificate': True,
    'ignoreerrors': True,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'extract_flat': 'in_playlist',  # Changed from True to 'in_playlist'
    'skip_download': True,
    'playlistend': None,  # Remove any playlist limit
    'ignoreerrors': True,
    'no_warnings': True,

}

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

class GuildState:
    def __init__(self):
        self.song_queue = []  # Will store URLs and basic info
        self.current_song = None
        self.is_playing = False
        self.is_processing = False

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)
guild_states: Dict[int, GuildState] = {}

class SpotifyTrackInfo:
    def __init__(self, track_data):
        self.title = track_data['name']
        self.artist = track_data['artists'][0]['name']
        self.duration = int(track_data['duration_ms'] / 1000)
        self.thumbnail = track_data['album']['images'][0]['url'] if track_data['album']['images'] else ''
        self.search_query = f"{self.title} {self.artist}"

async def get_spotify_tracks(url: str) -> List[SpotifyTrackInfo]:
    tracks = []
    
    try:
        if 'track' in url:
            # Single track
            track = spotify.track(url)
            tracks.append(SpotifyTrackInfo(track))
        
        elif 'album' in url:
            # Album
            album = spotify.album(url)
            for track in album['tracks']['items']:
                track['album'] = album
                tracks.append(SpotifyTrackInfo(track))
        
        elif 'playlist' in url:
            # Playlist
            playlist = spotify.playlist(url)
            for item in playlist['tracks']['items']:
                if item['track']:  # Check if track exists (not None)
                    tracks.append(SpotifyTrackInfo(item['track']))
        
        return tracks
    except Exception as e:
        print(f"Error processing Spotify URL: {e}")
        return []

class Song:
    def __init__(self, url: str, title: str, thumbnail: str, duration: int, requester: discord.Member, source_type: str = 'youtube'):
        self.url = url
        self.title = title
        self.thumbnail = thumbnail
        self.duration = duration
        self.requester = requester
        self.source = None
        self.source_type = source_type

    @staticmethod
    def parse_duration(duration: int) -> str:
        if not duration:
            return "LIVE"
        
        hours = duration // 3600
        minutes = (duration % 3600) // 60
        seconds = duration % 60
        
        if hours > 0:
            return f"{hours} hr {minutes} min {seconds} sec"
        elif minutes > 0:
            return f"{minutes} min {seconds} sec"
        else:
            return f"{seconds} sec"

async def process_spotify_track(track: SpotifyTrackInfo, requester: discord.Member) -> Optional[Song]:
    try:
        # Search for the track on YouTube
        search_query = f"ytsearch1:{track.search_query}"
        info = await asyncio.get_event_loop().run_in_executor(None, lambda: ytdl.extract_info(search_query, download=False))
        
        if info and 'entries' in info and info['entries']:
            entry = info['entries'][0]
            return Song(
                url=entry.get('url', entry.get('webpage_url', '')),
                title=f"{track.title} - {track.artist}",
                thumbnail=track.thumbnail,
                duration=track.duration,
                requester=requester,
                source_type='spotify'
            )
    except Exception as e:
        print(f"Error processing Spotify track: {e}")
    return None

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('webpage_url', '')
        self.thumbnail = data.get('thumbnail', '')
        self.duration = Song.parse_duration(int(data.get('duration', 0)))

    @classmethod
    async def create_source(cls, song: Song, *, loop: Optional[asyncio.BaseEventLoop] = None):
        loop = loop or asyncio.get_event_loop()
        
        ytdl_single_options = ytdl_format_options.copy()
        ytdl_single_options.update({
            'extract_flat': False,
            'ignoreerrors': True,
            'no_warnings': True,
            'quiet': True,
            'age_limit': None  # Ignore age restrictions
        })
        
        ytdl_single = yt_dlp.YoutubeDL(ytdl_single_options)
        
        try:
            data = await loop.run_in_executor(None, lambda: ytdl_single.extract_info(song.url, download=False))
            
            if data is None:
                raise Exception("Video is unavailable")
                
            if 'url' not in data:
                raise Exception("Could not extract video URL")
                
            filename = data['url']
            return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)
            
        except Exception as e:
            print(f"Error creating source for {song.title}: {str(e)}")
            raise

async def extract_playlist_info(query: str, requester: discord.Member, ctx) -> List[Song]:
    try:
        is_url = query.startswith(('http://', 'https://', 'www.'))
        
        # If it's not a URL, modify the query to use YouTube search
        if not is_url:
            query = f"ytsearch1:{query}"
        
        # Extract basic info from playlist or search result
        info = await asyncio.get_event_loop().run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
        
        songs = []
        if 'entries' in info:
            # For each entry in playlist or search results
            entries = info['entries']
            
            # If it's a playlist with partial information, get full details
            if is_url and any(entry.get('_type') == 'url' for entry in entries if entry):
                # Create new ytdl instance with full extraction
                full_info_options = ytdl_format_options.copy()
                full_info_options['extract_flat'] = False
                
                ytdl_full = yt_dlp.YoutubeDL(full_info_options)
                info = await asyncio.get_event_loop().run_in_executor(None, lambda: ytdl_full.extract_info(query, download=False))
                entries = info['entries'] if 'entries' in info else []
            
            # Process all entries
            for entry in entries:
                if entry and (entry.get('url') or entry.get('webpage_url')):
                    try:
                        # Create Song object with all available information
                        song = Song(
                            url=entry.get('url', entry.get('webpage_url', '')),
                            title=entry.get('title', 'Unknown'),
                            thumbnail=entry.get('thumbnail', ''),
                            duration=entry.get('duration', 0),
                            requester=requester
                        )
                        songs.append(song)
                    except Exception as e:
                        print(f"Error processing playlist entry: {str(e)}")
                        continue
                        
            if len(songs) > 0:
                if len(songs) > 500:  # Add a reasonable upper limit
                    songs = songs[:500]
                    await ctx.send(embed=discord.Embed(
                        description="⚠️ Playlist too large! Only the first 500 songs will be queued.",
                        color=discord.Color.yellow()
                    ))
                
                # Add progress message for large playlists
                if len(songs) > 50:
                    await ctx.send(embed=discord.Embed(
                        description=f"Processing large playlist... Added {len(songs)} songs to queue.",
                        color=discord.Color.blue()
                    ))
                    
        else:
            # Single video
            if info and (info.get('url') or info.get('webpage_url')):
                song = Song(
                    url=info.get('webpage_url', info.get('url', query)),
                    title=info.get('title', 'Unknown'),
                    thumbnail=info.get('thumbnail', ''),
                    duration=info.get('duration', 0),
                    requester=requester
                )
                songs.append(song)
            
        return songs
    except Exception as e:
        print(f"Error extracting info: {str(e)}")
        raise


def get_guild_state(guild_id: int) -> GuildState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState()
    return guild_states[guild_id]

def create_music_control_view():
    view = discord.ui.View()
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.primary, label="⏭️ Skip", custom_id="skip"))
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.secondary, label="⏯️ Pause/Resume", custom_id="pause_resume"))
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.danger, label="⏹️ Stop", custom_id="stop"))
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.success, label="🔀 Shuffle Queue", custom_id="shuffle"))
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.blurple, label="❌ Clear Queue", custom_id="clear_queue"))
    return view

async def update_player_message(ctx):
    guild_state = get_guild_state(ctx.guild.id)
    embed = discord.Embed(title="🎵 Music Player", color=discord.Color.blue())
    
    if guild_state.current_song:
        source_emoji = "🎵" if guild_state.current_song.source_type == 'youtube' else "💚"
        embed.add_field(
            name=f"Now Playing {source_emoji}",
            value=f"[{guild_state.current_song.title}]({guild_state.current_song.url})",
            inline=False
        )
        
        duration = Song.parse_duration(guild_state.current_song.duration)
        embed.add_field(name="Duration", value=duration, inline=True)
        embed.add_field(name="Requested By", value=guild_state.current_song.requester.mention, inline=True)
        embed.set_thumbnail(url=guild_state.current_song.thumbnail)
    else:
        embed.add_field(name="Now Playing", value="Nothing is playing right now.", inline=False)
    
    if guild_state.song_queue:
        queue_str = "\n".join([
            f"{i+1}. {'💚' if song.source_type == 'spotify' else '🎵'} {song.title} ({Song.parse_duration(song.duration)}) | {song.requester.display_name}"
            for i, song in enumerate(guild_state.song_queue[:5])
        ])
        remaining = len(guild_state.song_queue) - 5 if len(guild_state.song_queue) > 5 else 0
        queue_str += f"\n\n*and {remaining} more*" if remaining > 0 else ""
        embed.add_field(name="Queue", value=queue_str, inline=False)
    else:
        embed.add_field(name="Queue", value="The queue is empty.", inline=False)

    await ctx.send(embed=embed, view=create_music_control_view())


async def handle_playback_error(ctx, voice_client, error):
    """Handle errors that occur during playback"""
    if error:
        print(f"Playback error: {str(error)}")
        await ctx.send(embed=discord.Embed(
            description="An error occurred during playback. Skipping to next song...",
            color=discord.Color.yellow()
        ))
    
    await play_next(ctx, voice_client)


async def play_next(ctx, voice_client):
    guild_state = get_guild_state(ctx.guild.id)
    
    if not guild_state.song_queue:
        guild_state.current_song = None
        guild_state.is_playing = False
        await update_player_message(ctx)
        return
    
    if guild_state.is_processing:
        return
        
    guild_state.is_processing = True
    
    max_retries = 3
    current_retry = 0
    
    while current_retry < max_retries and guild_state.song_queue:
        try:
            # Get the next song from queue
            next_song = guild_state.song_queue[0]
            guild_state.song_queue.pop(0)
            
            # Create the source for the next song
            source = await YTDLSource.create_source(next_song, loop=bot.loop)
            next_song.source = source
            
            # Set as current song and play
            guild_state.current_song = next_song
            voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
                handle_playback_error(ctx, voice_client, e), bot.loop))
            
            await update_player_message(ctx)
            break
            
        except Exception as e:
            print(f"Error playing {next_song.title if next_song else 'unknown song'}: {str(e)}")
            current_retry += 1
            
            if current_retry >= max_retries and guild_state.song_queue:
                # If we've exhausted retries, try the next song in queue
                await ctx.send(embed=discord.Embed(
                    description=f"Skipping unavailable song: {next_song.title}",
                    color=discord.Color.yellow()
                ))
                continue
    
    guild_state.is_processing = False
    
    if current_retry >= max_retries and not guild_state.song_queue:
        await ctx.send(embed=discord.Embed(
            description="Could not play any more songs due to errors",
            color=discord.Color.red()
        ))
        guild_state.current_song = None
        guild_state.is_playing = False
        await update_player_message(ctx)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")

@bot.command(name='shuffle')
async def shuffle(ctx):
    guild_state = get_guild_state(ctx.guild.id)
    if guild_state.song_queue:
        random.shuffle(guild_state.song_queue)
        await ctx.send(embed=discord.Embed(
            description="Queue has been shuffled!",
            color=discord.Color.green()
        ))
        await update_player_message(ctx)
    else:
        await ctx.send(embed=discord.Embed(
            description="The queue is empty!",
            color=discord.Color.red()
        ))

@bot.command(name='play', help='To play song or playlist (URL or search query)')
async def play(ctx, *, query):
    guild_state = get_guild_state(ctx.guild.id)
    voice_client = ctx.guild.voice_client

    if not ctx.author.voice:
        await ctx.send(embed=discord.Embed(
            description="You need to be in a voice channel to play music!",
            color=discord.Color.red()
        ))
        return

    if voice_client is None:
        await ctx.author.voice.channel.connect()
        voice_client = ctx.guild.voice_client

    async with ctx.typing():
        try:
            # Handle Spotify or YouTube content
            songs = []
            spotify_pattern = r'open.spotify.com\/(track|album|playlist)\/[a-zA-Z0-9]+'
            is_spotify = bool(re.search(spotify_pattern, query))
            
            if is_spotify:
                spotify_tracks = await get_spotify_tracks(query)
                for track in spotify_tracks:
                    song = await process_spotify_track(track, ctx.author)
                    if song:
                        songs.append(song)
            else:
                songs = await extract_playlist_info(query, ctx.author, ctx)
            
            if not songs:
                await ctx.send(embed=discord.Embed(
                    description="Could not find any valid songs to play",
                    color=discord.Color.red()
                ))
                return
            
            # Add valid songs to queue
            guild_state.song_queue.extend(songs)
            
            # Notify user
            if len(songs) > 1:
                await ctx.send(embed=discord.Embed(
                    description=f"Added {len(songs)} songs to the queue",
                    color=discord.Color.green()
                ))
            else:
                await ctx.send(embed=discord.Embed(
                    description=f"{'Added to queue' if voice_client.is_playing() else 'Playing'}: {songs[0].title}",
                    color=discord.Color.green()
                ))
            
            # Start playing if nothing is playing
            if not voice_client.is_playing():
                await play_next(ctx, voice_client)
                
        except Exception as e:
            await ctx.send(embed=discord.Embed(
                description=f"An error occurred: {str(e)}",
                color=discord.Color.red()
            ))

@bot.command(name='skip')
async def skip(ctx):
    voice_client = ctx.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await ctx.send(embed=discord.Embed(description="Skipped the current song", color=discord.Color.blue()))
    else:
        await ctx.send(embed=discord.Embed(
            description="Nothing is playing right now.",
            color=discord.Color.red()
        ))

@bot.command(name='pause')
async def pause(ctx):
    voice_client = ctx.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await ctx.send(embed=discord.Embed(description="Paused the current song", color=discord.Color.blue()))
    else:
        await ctx.send(embed=discord.Embed(
            description="Nothing is playing right now.",
            color=discord.Color.red()
        ))

@bot.command(name='resume')
async def resume(ctx):
    voice_client = ctx.guild.voice_client
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        await ctx.send(embed=discord.Embed(description="Resumed the current song", color=discord.Color.green()))
    else:
        await ctx.send(embed=discord.Embed(
            description="The music is not paused.",
            color=discord.Color.red()
        ))

@bot.command(name='stop')
async def stop(ctx):
    voice_client = ctx.guild.voice_client
    guild_state = get_guild_state(ctx.guild.id)
    
    if voice_client and voice_client.is_playing():
        guild_state.song_queue.clear()
        voice_client.stop()
        await ctx.send(embed=discord.Embed(description="Stopped the music and cleared the queue", color=discord.Color.blue()))
    else:
        await ctx.send(embed=discord.Embed(
            description="Nothing is playing right now.",
            color=discord.Color.red()
        ))

@bot.command(name='join', help='Tells the bot to join the voice channel')
async def join(ctx):
    if not ctx.message.author.voice:
        await ctx.send(embed=discord.Embed(description="You are not connected to a voice channel.", color=discord.Color.red()))
        return
    channel = ctx.message.author.voice.channel
    await channel.connect()
    await ctx.send(embed=discord.Embed(description=f"Joined {channel.name}", color=discord.Color.green()))

@bot.command(name='leave', help='To make the bot leave the voice channel')
async def leave(ctx):
    voice_client = ctx.message.guild.voice_client
    if voice_client.is_connected():
        await voice_client.disconnect()
        await ctx.send(embed=discord.Embed(description="Disconnected from voice channel", color=discord.Color.blue()))
    else:
        await ctx.send(embed=discord.Embed(description="The bot is not connected to a voice channel.", color=discord.Color.red()))

@bot.command(name='queue')
async def queue(ctx):
    await update_player_message(ctx)

@bot.command(name='clear_queue')
async def clear_queue(ctx):
    guild_state = get_guild_state(ctx.guild.id)
    if guild_state.song_queue:
        guild_state.song_queue.clear()
        await ctx.send(embed=discord.Embed(
            description="Queue has been cleared!",
            color=discord.Color.green()
        ))
        await update_player_message(ctx)
    else:
        await ctx.send(embed=discord.Embed(
            description="The queue is empty!",
            color=discord.Color.red()
        ))
@bot.event
async def on_interaction(interaction):
    if interaction.type == discord.InteractionType.component:
        if isinstance(interaction.data, dict) and 'custom_id' in interaction.data:
            custom_id = interaction.data['custom_id']
            ctx = await bot.get_context(interaction.message)
            
            if custom_id == "skip":
                await skip(ctx)
            elif custom_id == "pause_resume":
                voice_client = interaction.guild.voice_client
                if voice_client and voice_client.is_playing():
                    await pause(ctx)
                elif voice_client and voice_client.is_paused():
                    await resume(ctx)
            elif custom_id == "stop":
                await stop(ctx)
            elif custom_id == "shuffle":
                await shuffle(ctx)
            elif custom_id == "clear_queue":
                await clear_queue(ctx)
            
            await interaction.response.defer()
        else:
            await interaction.response.send_message(
                "An error occurred processing this button.",
                ephemeral=True
            )

bot_token = os.getenv('BOT_TOKEN')
bot.run(bot_token)