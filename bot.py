"""
Discord MTG Companion Bot
=======================
A Discord bot with:
- MTG Tarot readings (Sephirothic + Qliphothic dual-engine system)
- MTG gameplay assistance (card lookup, deck help)
- Emotional support with distress detection (uses Opus for complex emotional support)
- Web search for comforting content during distress
- Personal context awareness (via gitignored memories file)

Setup:
1. pip install discord.py anthropic python-dotenv aiohttp
2. Create .env with DISCORD_TOKEN and ANTHROPIC_API_KEY
3. Create config.json with allowed_channels list
4. Optionally create memories/<your-handle>.json (gitignored)
5. python bot.py
"""

import discord
from discord.ext import commands
from discord import app_commands
import anthropic
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Any, Tuple
from functools import partial
from zoneinfo import ZoneInfo
import json
import os
import asyncio
import aiohttp
import random
import re
import io
import base64
from pathlib import Path
from dotenv import load_dotenv

# Import the visual tarot engine with actual MTG card mappings
try:
    from rules.tarot_visuals import VisualTarotEngine
    HAS_VISUAL_TAROT = True
except ImportError:
    HAS_VISUAL_TAROT = False
    print("⚠️ Visual tarot engine not found in rules/ - using basic tarot")

load_dotenv()


# Module-level timezone for _format_msg_timestamp, set from config at startup
# (GraysonBot.load_config -> _set_local_timezone). None => use the host's local
# timezone. Kept module-level because _format_msg_timestamp is a free function
# called where the bot instance isn't in scope.
_LOCAL_TZ: Optional[ZoneInfo] = None


def _set_local_timezone(tz_name: Optional[str]) -> None:
    """Point the message-timestamp formatter at an IANA timezone (or local)."""
    global _LOCAL_TZ
    if not tz_name:
        _LOCAL_TZ = None
        return
    try:
        _LOCAL_TZ = ZoneInfo(tz_name)
    except Exception:
        print(f"[CONFIG] Unknown timezone '{tz_name}' — message timestamps will use system local time")
        _LOCAL_TZ = None


def _format_msg_timestamp(utc_dt: datetime) -> str:
    """Format a UTC datetime in the configured local timezone for context.

    Prepended to stored messages so Claude can see the real-world timeline
    and infer gaps (e.g., overnight sleep) between messages. Uses the timezone
    from config.json "location" if set, else the host's local timezone.
    """
    try:
        local_dt = utc_dt.astimezone(_LOCAL_TZ) if _LOCAL_TZ else utc_dt.astimezone()
    except Exception:
        local_dt = utc_dt
    time_str = local_dt.strftime('%I:%M %p').lstrip('0')
    return f"[{local_dt.strftime('%b')} {local_dt.day}, {time_str}]"


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class BotConfig:
    # Model settings - TRI-MODEL ARCHITECTURE
    model_default: str = "claude-sonnet-4-6"  # Cost-effective for MTG, general chat
    model_support: str = "claude-opus-4-8"   # Full capability for emotional support (Opus 4.8)
    model_classifier: str = "claude-haiku-4-5-20251001"  # Fast/cheap for distress classification
    max_tokens: int = 2048
    max_tokens_support: int = 4096  # More room for nuanced responses

    # Context management
    max_messages_per_thread: int = 20
    max_input_tokens: int = 30000
    chars_per_token: float = 4.0

    # Two-tier memory settings
    max_working_notes: int = 10          # Claude's scratch space
    max_longterm_memories: int = 25      # User-controlled permanent memories
    working_memory_decay_hours: float = 48.0  # Notes fade after ~48h

    # Cost tracking
    # Sonnet pricing
    sonnet_input_cost_per_million: float = 3.0
    sonnet_output_cost_per_million: float = 15.0
    # Opus 4.x pricing (May 17 audit: was $5/$25, but Opus 4 / 4.x is $15/$75
    # per Anthropic's public pricing page. Low historical impact because the
    # Opus bucket is only used by emotional-support pathway, but worth keeping
    # accurate for cost-display correctness.)
    opus_input_cost_per_million: float = 15.0
    opus_output_cost_per_million: float = 75.0
    # Haiku pricing (semantic distress classifier)
    haiku_input_cost_per_million: float = 0.80
    haiku_output_cost_per_million: float = 4.0
    
    # Web search settings
    web_search_enabled: bool = True
    max_search_results_in_embed: int = 5
    
    # Comfort content settings
    comfort_searches: List[str] = field(default_factory=lambda: [
        "cute animal pictures",
        "funny dog gif",
        "cat videos",
        "otter video",
        "otter holding hands",
    ])
    
    # Supported file types for attachments
    image_types: tuple = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
    text_file_types: tuple = ('.md', '.txt', '.py', '.js', '.ts', '.json', '.csv', '.html', '.css', '.yaml', '.yml', '.toml', '.xml', '.sql', '.sh', '.bash', '.r', '.rs', '.go', '.java', '.c', '.cpp', '.h', '.hpp')
    max_image_size_mb: float = 20.0
    
    # Distress detection keywords (weighted)
    distress_signals: Dict[str, float] = field(default_factory=lambda: {
        # High severity - definitely switch to Opus
        "i'm a failure": 0.9,
        "i'm worthless": 0.9,
        "i hurt everyone": 0.9,
        "i hurt all my": 0.85,
        "i've hurt people": 0.85,
        "complete joke": 0.8,
        "i'll never": 0.7,
        "i'm never gonna": 0.7,
        "no one cares": 0.8,
        "what's the point": 0.7,
        "want to die": 0.95,
        "kill myself": 0.95,
        "ending it": 0.9,
        "i'm a horrible": 0.9,
        "horrible person": 0.9,
        "horrible excuse": 0.9,
        "i'm a disaster": 0.85,
        "i'm a mess": 0.6,
        "i deserve to": 0.8,
        "i should have nothing": 0.8,
        "i'm better off": 0.8,
        "dehumanize me": 0.85,
        "burden on": 0.8,
        "work myself to": 0.7,
        "never have a day off": 0.6,
        "without me": 0.5,
        # Self-worth negation — keyword family for distress detection
        "no worth": 0.8,
        "don't deserve": 0.8,
        "not like i deserve": 0.8,
        "don't belong": 0.7,
        "i don't belong": 0.7,
        "i should just": 0.5,
        "the bad friend": 0.5,
        "they don't care": 0.6,
        "i'm the dumb": 0.6,
        "i was stupid": 0.6,
        "overwork myself": 0.7,
        "cold dark prison": 0.9,
        "prison cell": 0.9,
        "i belong in": 0.7,
        "trample over me": 0.6,
        "talk over me": 0.5,
        "ignore that i": 0.4,
        "treat me like i don't exist": 0.7,
        "like i don't exist": 0.6,
        # Medium severity
        "feeling overwhelmed": 0.6,
        "can't do anything right": 0.7,
        "wasted": 0.5,
        "incompetent": 0.6,
        "tethered": 0.5,
        "trapped": 0.6,
        "spiraling": 0.7,
        "falling apart": 0.7,
        "just so alone": 0.6,
        "impossible to love": 0.7,
        "makes it impossible": 0.6,
        # Lower but notable - might still benefit from Opus
        "stressed": 0.3,
        "anxious": 0.4,
        "worried": 0.3,
        "scared": 0.4,
        "frustrated": 0.3,
        "exhausted": 0.4,
    })
    
    # Threshold for switching to Opus
    distress_threshold: float = 0.5
    # Lower threshold for semantic classifier (catches subtler patterns keywords miss)
    semantic_distress_threshold: float = 0.4
    # Threshold for offering comfort content (red pandas)
    comfort_threshold: float = 0.6
    # Above this score, suppress comfort content — person needs focused support, not GIFs
    crisis_threshold: float = 0.8

    # Step-down configuration
    spiral_cooldown_minutes: int = 15      # Stay in support mode for this long after spiral
    stressed_cooldown_minutes: int = 10    # Stay elevated for this long after stress
    calm_messages_to_stepdown: int = 3     # Need this many calm messages before stepping down
    
    # System prompts are no longer static — they're composed at runtime from
    # the persona config (personas/<bot_persona>.json) plus built-in capability
    # and distress-response text. See CompanionBot.build_base_prompt,
    # build_support_prompt, build_spiral_prompt below.

CONFIG = BotConfig()


# =============================================================================
# MEMORY SYSTEMS
# =============================================================================

def load_personal_memories(user_id: str) -> Optional[Dict]:
    """Load gitignored personal context for specific users."""
    memories_path = Path("memories") / f"{user_id}.json"
    if memories_path.exists():
        try:
            with open(memories_path, encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading memories for {user_id}: {e}")
    return None


def load_named_memories(name: str) -> Optional[Dict]:
    """Load memories by name (e.g., 'alice.json' under memories/)."""
    memories_path = Path("memories") / f"{name.lower()}.json"
    if memories_path.exists():
        try:
            with open(memories_path, encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading memories for {name}: {e}")
    return None


# =============================================================================
# TWO-TIER MEMORY SYSTEM (Working + Long-term)
# =============================================================================

@dataclass
class WorkingNote:
    """A note in working memory. Decays if not accessed."""
    content: str
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed: datetime = field(default_factory=datetime.now)
    access_count: int = 1
    
    def is_expired(self, decay_hours: float = 48.0) -> bool:
        """Check if note has decayed."""
        age_hours = (datetime.now() - self.last_accessed).total_seconds() / 3600
        # Notes accessed more get longer life
        effective_decay = decay_hours * (1 + (self.access_count * 0.5))
        return age_hours > effective_decay
    
    def touch(self) -> None:
        """Mark as accessed, resetting decay timer."""
        self.last_accessed = datetime.now()
        self.access_count += 1
    
    def freshness(self, decay_hours: float = 48.0) -> float:
        """0.0 = about to expire, 1.0 = fresh"""
        age_hours = (datetime.now() - self.last_accessed).total_seconds() / 3600
        effective_decay = decay_hours * (1 + (self.access_count * 0.5))
        return max(0, 1 - (age_hours / effective_decay))


class WorkingMemory:
    """
    the bot's "scratch space" - things noticed during conversations.
    
    - Auto-populated by Claude during conversations via [note: key: value]
    - Decays after ~48h of no access
    - Frequently referenced notes live longer
    - Can be promoted to long-term with !keep
    """
    
    def __init__(self, max_notes: int = 10, decay_hours: float = 48.0):
        self.notes: Dict[str, WorkingNote] = {}
        self.max_notes = max_notes
        self.decay_hours = decay_hours
    
    def add(self, key: str, content: str) -> None:
        """Add or update a working note."""
        self._prune_expired()
        
        if key in self.notes:
            self.notes[key].content = content
            self.notes[key].touch()
        else:
            if len(self.notes) >= self.max_notes:
                self._evict_stalest()
            self.notes[key] = WorkingNote(content=content)
    
    def get(self, key: str) -> Optional[str]:
        """Get a note, refreshing its decay timer."""
        if key in self.notes:
            if not self.notes[key].is_expired(self.decay_hours):
                self.notes[key].touch()
                return self.notes[key].content
            else:
                del self.notes[key]
        return None
    
    def remove(self, key: str) -> Optional[WorkingNote]:
        """Remove and return a note."""
        return self.notes.pop(key, None)
    
    def _prune_expired(self) -> None:
        """Remove all expired notes."""
        expired = [k for k, v in self.notes.items() if v.is_expired(self.decay_hours)]
        for k in expired:
            del self.notes[k]
    
    def _evict_stalest(self) -> None:
        """Remove the note closest to expiring."""
        if not self.notes:
            return
        stalest = min(self.notes.keys(), 
                     key=lambda k: self.notes[k].freshness(self.decay_hours))
        del self.notes[stalest]
    
    def get_context_string(self) -> str:
        """Get working notes formatted for LLM context."""
        self._prune_expired()
        if not self.notes:
            return ""
        
        lines = ["**Working notes** (recent observations, may fade):"]
        for key, note in sorted(self.notes.items(), 
                                key=lambda x: x[1].freshness(self.decay_hours),
                                reverse=True):
            freshness = note.freshness(self.decay_hours)
            fade_indicator = "●" if freshness > 0.7 else "◐" if freshness > 0.3 else "○"
            lines.append(f"- {fade_indicator} {key}: {note.content}")
        
        return "\n".join(lines)
    
    def to_dict(self) -> dict:
        self._prune_expired()
        return {
            key: {
                "content": note.content,
                "created_at": note.created_at.isoformat(),
                "last_accessed": note.last_accessed.isoformat(),
                "access_count": note.access_count
            }
            for key, note in self.notes.items()
        }
    
    @classmethod
    def from_dict(cls, data: dict, max_notes: int = 10, decay_hours: float = 48.0) -> "WorkingMemory":
        memory = cls(max_notes=max_notes, decay_hours=decay_hours)
        for key, note_data in data.items():
            note = WorkingNote(
                content=note_data["content"],
                created_at=datetime.fromisoformat(note_data["created_at"]),
                last_accessed=datetime.fromisoformat(note_data["last_accessed"]),
                access_count=note_data["access_count"]
            )
            if not note.is_expired(decay_hours):
                memory.notes[key] = note
        return memory


class LongTermMemory:
    """
    Permanent facts that persist until forgotten.
    
    - User-controlled via !remember / !forget
    - Can be populated by promoting working notes with !keep
    - Never decays
    """
    
    def __init__(self, max_entries: int = 25):
        self.entries: Dict[str, str] = {}
        self.max_entries = max_entries
    
    def add(self, key: str, value: str) -> bool:
        """Add or update a memory. Returns False if at capacity and key is new."""
        if key in self.entries:
            self.entries[key] = value
            return True
        
        if len(self.entries) >= self.max_entries:
            return False
        
        self.entries[key] = value
        return True
    
    def get(self, key: str) -> Optional[str]:
        return self.entries.get(key)
    
    def remove(self, key: str) -> bool:
        if key in self.entries:
            del self.entries[key]
            return True
        return False
    
    def get_context_string(self) -> str:
        """Get long-term memories formatted for LLM context."""
        if not self.entries:
            return ""
        
        lines = ["**Long-term memories** (permanent facts):"]
        for key, value in self.entries.items():
            lines.append(f"- {key}: {value}")
        
        return "\n".join(lines)
    
    def to_dict(self) -> dict:
        return dict(self.entries)
    
    @classmethod
    def from_dict(cls, data: dict, max_entries: int = 25) -> "LongTermMemory":
        memory = cls(max_entries=max_entries)
        memory.entries = dict(data)
        return memory


class TwoTierMemory:
    """
    Combined memory system with working + long-term storage.
    
    - Working memory: Things noticed during conversations, fade over ~48h
    - Long-term memory: Explicit facts, permanent until forgotten
    
    Notes can be promoted from working → long-term with !keep
    """
    
    def __init__(
        self, 
        max_working_notes: int = 10,
        max_longterm_entries: int = 25,
        working_decay_hours: float = 48.0
    ):
        self.working = WorkingMemory(max_working_notes, working_decay_hours)
        self.longterm = LongTermMemory(max_longterm_entries)
    
    def promote(self, key: str) -> bool:
        """Promote a working note to long-term memory."""
        note = self.working.notes.get(key)
        if not note:
            return False
        
        if self.longterm.add(key, note.content):
            self.working.remove(key)
            return True
        return False
    
    def get_context_string(self) -> str:
        """Get combined memory context for LLM."""
        parts = []
        
        lt_context = self.longterm.get_context_string()
        if lt_context:
            parts.append(lt_context)
        
        wm_context = self.working.get_context_string()
        if wm_context:
            parts.append(wm_context)
        
        return "\n\n".join(parts)
    
    def to_dict(self) -> dict:
        return {
            "working": self.working.to_dict(),
            "longterm": self.longterm.to_dict()
        }
    
    @classmethod
    def from_dict(
        cls, 
        data: dict,
        max_working_notes: int = 10,
        max_longterm_entries: int = 25,
        working_decay_hours: float = 48.0
    ) -> "TwoTierMemory":
        memory = cls(max_working_notes, max_longterm_entries, working_decay_hours)
        if "working" in data:
            memory.working = WorkingMemory.from_dict(
                data["working"], max_working_notes, working_decay_hours
            )
        if "longterm" in data:
            memory.longterm = LongTermMemory.from_dict(
                data["longterm"], max_longterm_entries
            )
        return memory


# =============================================================================
# MTG TAROT SYSTEM
# =============================================================================

class MTGTarotEngine:
    """Dual-engine MTG Tarot system based on color pie dialectics."""
    
    def __init__(self):
        self.load_decks()
    
    def load_decks(self):
        """Load the Sephirothic and Qliphothic deck definitions."""
        decks_path = Path("data/mtg_tarot_decks.json")
        if decks_path.exists():
            with open(decks_path, encoding='utf-8') as f:
                data = json.load(f)
                self.sephirothic = data.get("sephirothic", {})
                self.qliphothic = data.get("qliphothic", {})
        else:
            # Fallback to embedded minimal definitions
            self.sephirothic = self._default_sephirothic()
            self.qliphothic = self._default_qliphothic()
    
    def _default_sephirothic(self) -> Dict:
        """Default Sephirothic (allied pairs) definitions."""
        return {
            "name": "Sephirothic Engine",
            "description": "Path of Harmony - Allied color pairs working in concert",
            "suits": {
                "Edicts": {
                    "colors": "WU",
                    "themes": ["law", "structure", "knowledge systems", "codified wisdom"],
                    "energy": "Order through understanding"
                },
                "Secrets": {
                    "colors": "UB",
                    "themes": ["hidden knowledge", "manipulation", "information asymmetry"],
                    "energy": "Power through what others don't know"
                },
                "Revelry": {
                    "colors": "BR",
                    "themes": ["liberation", "hedonism", "creative destruction"],
                    "energy": "Freedom through transgression"
                },
                "Wilds": {
                    "colors": "RG",
                    "themes": ["primal instinct", "natural fury", "honest strength"],
                    "energy": "Power through directness"
                },
                "Groves": {
                    "colors": "GW",
                    "themes": ["community", "growth", "natural order", "sanctuary"],
                    "energy": "Strength through connection"
                }
            }
        }
    
    def _default_qliphothic(self) -> Dict:
        """Default Qliphothic (enemy pairs) definitions."""
        return {
            "name": "Qliphothic Engine",
            "description": "Path of Opposition - Enemy color pairs in productive tension",
            "suits": {
                "Debts": {
                    "colors": "WB",
                    "themes": ["obligation", "sacrifice", "necessary evil", "binding contracts"],
                    "energy": "Order through submission"
                },
                "Sparks": {
                    "colors": "UR",
                    "themes": ["innovation", "breakthrough", "creative chaos", "genius"],
                    "energy": "Progress through disruption"
                },
                "Rot": {
                    "colors": "BG",
                    "themes": ["decay", "transformation", "death feeding life"],
                    "energy": "Growth through destruction"
                },
                "Crusades": {
                    "colors": "RW",
                    "themes": ["righteous fury", "holy war", "conviction"],
                    "energy": "Justice through force"
                },
                "Grafts": {
                    "colors": "GU",
                    "themes": ["evolution", "adaptation", "natural improvement"],
                    "energy": "Growth through design"
                }
            }
        }
    
    def draw_reading(self, spread_type: str = "three", engine: str = "both") -> Dict:
        """Draw cards for a reading."""
        if spread_type == "three":
            positions = ["Past/Foundation", "Present/Challenge", "Future/Potential"]
        elif spread_type == "single":
            positions = ["Focus"]
        elif spread_type == "five":
            positions = ["Self", "Challenge", "Subconscious", "Recent Past", "Potential"]
        else:
            positions = ["Past/Foundation", "Present/Challenge", "Future/Potential"]
        
        cards = []
        available_engines = []
        
        if engine in ["both", "sephirothic"]:
            available_engines.append(("Sephirothic", self.sephirothic))
        if engine in ["both", "qliphothic"]:
            available_engines.append(("Qliphothic", self.qliphothic))
        
        for position in positions:
            engine_name, deck = random.choice(available_engines)
            suit_name = random.choice(list(deck["suits"].keys()))
            suit = deck["suits"][suit_name]
            
            # Draw either a numbered card (Ace-10) or face card
            if random.random() < 0.7:  # 70% numbered
                rank = random.choice(["Ace", "Two", "Three", "Four", "Five", 
                                     "Six", "Seven", "Eight", "Nine", "Ten"])
                card_name = f"{rank} of {suit_name}"
            else:
                rank = random.choice(["Page", "Knight", "Queen", "King"])
                card_name = f"{rank} of {suit_name}"
            
            reversed_chance = 0.3
            is_reversed = random.random() < reversed_chance
            
            cards.append({
                "position": position,
                "card": card_name,
                "engine": engine_name,
                "suit": suit_name,
                "colors": suit["colors"],
                "themes": suit["themes"],
                "energy": suit["energy"],
                "reversed": is_reversed
            })
        
        return {
            "spread_type": spread_type,
            "cards": cards,
            "timestamp": datetime.now().isoformat()
        }
    
    def format_reading(self, reading: Dict) -> str:
        """Format a reading for Discord display."""
        lines = [f"**🎴 MTG Tarot Reading ({reading['spread_type'].title()} Card Spread)**\n"]
        
        for card in reading["cards"]:
            rev_marker = " *(Reversed)*" if card["reversed"] else ""
            lines.append(f"**{card['position']}**: {card['card']}{rev_marker}")
            lines.append(f"  *{card['engine']} • {card['colors']}*")
            lines.append(f"  Energy: {card['energy']}")
            lines.append(f"  Themes: {', '.join(card['themes'][:3])}")
            lines.append("")
        
        lines.append("*Ask me to interpret this spread, or draw again with `!tarot`*")
        return "\n".join(lines)


# =============================================================================
# MTG CARD LOOKUP (via Scryfall API)
# =============================================================================

class WebSearchHandler:
    """Handles web search via Claude's tool use for comfort content and general queries."""
    
    def __init__(self, client: anthropic.Anthropic, usage_callback=None):
        self.client = client
        self.usage_callback = usage_callback  # Optional callback for token tracking
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.shown_facts: set = set()  # Track shown facts to avoid repeats within a session

    def _pick_fresh_fact(self, fact_pool: list) -> str:
        """Pick a fact that hasn't been shown yet this session. Resets if all exhausted."""
        unseen = [f for f in fact_pool if f not in self.shown_facts]
        if not unseen:
            # All facts shown — reset and start over
            self.shown_facts.clear()
            unseen = fact_pool
        pick = random.choice(unseen)
        self.shown_facts.add(pick)
        return pick

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self.http_session is None or self.http_session.closed:
            self.http_session = aiohttp.ClientSession()
        return self.http_session
    
    async def fetch_red_panda_image(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Fetch a red panda image from free APIs.
        Returns (image_url, fact, source) or (None, None, None) on failure.
        """
        session = await self._get_session()
        
        # Try some-random-api first (has both image and fact)
        try:
            async with session.get(
                "https://some-random-api.com/animal/red_panda",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    img = data.get("image")
                    fact = data.get("fact", "")
                    # Filter out facts that are about giant pandas, not red pandas
                    # The API sometimes returns generic "panda" facts about giant pandas
                    _is_giant_panda = fact and (
                        "giant panda" in fact.lower()
                        or "bamboo bear" in fact.lower()
                        or "all-white bear" in fact.lower()
                        or "black and white" in fact.lower()
                        or "black ashes" in fact.lower()
                        or "armbands" in fact.lower()
                        or ("panda" in fact.lower() and "red panda" not in fact.lower()
                            and not any(w in fact.lower() for w in ["ailuridae", "crepuscular", "raccoon", "firefox"]))
                    )
                    if _is_giant_panda:
                        fact = self._pick_fresh_fact([
                            "Red pandas spend about 13 hours a day eating bamboo!",
                            "Red pandas have a false thumb - an extended wrist bone that helps them grip bamboo!",
                            "Red pandas can rotate their ankles to climb down trees headfirst!",
                            "Red pandas are most active at dawn and dusk - they're crepuscular!",
                            "Despite their name, red pandas aren't closely related to giant pandas at all!",
                            "Red pandas spend most of their lives in trees and even sleep up there.",
                            "Red pandas wrap their fluffy tails around themselves like blankets to stay warm!",
                            "Baby red pandas are born blind and deaf, and stay with their mother for about a year.",
                            "A red panda's tail can be up to 18 inches long!",
                            "Red pandas were discovered 48 years before giant pandas!",
                            "Red pandas have fur on the bottom of their paws to keep warm on snow and ice!",
                            "Red pandas are the only living member of the family Ailuridae!",
                            "Red pandas use their bushy tails for balance when climbing trees!",
                            "Red pandas communicate using a series of squeals, twitters, and huff-quacks!",
                            "A group of red pandas is called a pack, though they're mostly solitary!",
                            "Red pandas lick their noses to stay hydrated while sleeping!",
                        ])
                    # Track fact for dedup (even API-sourced ones)
                    if fact:
                        self.shown_facts.add(fact)
                    if img:
                        print(f"✓ Got red panda from some-random-api")
                        return img, fact, "some-random-api"
        except Exception as e:
            print(f"some-random-api failed: {e}")
        
        # Fallback: Try reddit r/redpandas
        try:
            headers = {"User-Agent": "the bot-Discord-Bot/1.0 (comfort feature)"}
            async with session.get(
                "https://www.reddit.com/r/redpandas/hot.json?limit=50",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    posts = data.get("data", {}).get("children", [])
                    # Filter for direct image posts (not galleries or videos)
                    image_posts = [
                        p["data"] for p in posts 
                        if p["data"].get("url", "").lower().endswith(('.jpg', '.jpeg', '.png', '.gif'))
                        and not p["data"].get("over_18", False)
                        and not p["data"].get("is_video", False)
                    ]
                    if image_posts:
                        post = random.choice(image_posts)
                        print(f"✓ Got red panda from reddit: {post.get('title', '')[:30]}")
                        return post["url"], post.get("title"), "reddit"
        except Exception as e:
            print(f"Reddit fallback failed: {e}")
        
        # Last resort: curated list of VERIFIED red panda images from Wikipedia Commons
        curated_images = [
            ("https://upload.wikimedia.org/wikipedia/commons/thumb/5/50/RedPandaFullBody.JPG/800px-RedPandaFullBody.JPG",
             "Red pandas have a false thumb - an extended wrist bone that helps them grip bamboo!"),
            ("https://upload.wikimedia.org/wikipedia/commons/thumb/e/e6/Red_Panda_%2824986761703%29.jpg/800px-Red_Panda_%2824986761703%29.jpg",
             "Red pandas spend most of their lives in trees and even sleep up there."),
            ("https://upload.wikimedia.org/wikipedia/commons/thumb/b/be/Red_Panda_in_a_Gingko_tree.jpg/800px-Red_Panda_in_a_Gingko_tree.jpg",
             "Red pandas are most active at dawn and dusk - they're crepuscular!"),
            ("https://upload.wikimedia.org/wikipedia/commons/thumb/1/1e/Red_panda_%2830594873830%29.jpg/800px-Red_panda_%2830594873830%29.jpg",
             "Despite their name, red pandas aren't closely related to giant pandas at all!"),
            ("https://upload.wikimedia.org/wikipedia/commons/thumb/9/9a/Red_Panda_Tennoji_2.jpg/800px-Red_Panda_Tennoji_2.jpg",
             "Red pandas can rotate their ankles to climb down trees headfirst!"),
        ]
        # Pick a curated image whose fact hasn't been shown yet
        unseen = [(img, fact) for img, fact in curated_images if fact not in self.shown_facts]
        if not unseen:
            self.shown_facts.clear()
            unseen = curated_images
        img, fact = random.choice(unseen)
        self.shown_facts.add(fact)
        print(f"✓ Using curated red panda image")
        return img, fact, "curated"
    
    async def fetch_red_panda_gif(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Fetch a red panda gif.
        Returns (gif_url, title) or (None, None) on failure.
        
        Note: If gif URLs fail, falls back to static images.
        """
        session = await self._get_session()
        
        # Try to get a gif from reddit (they often have gifs)
        try:
            headers = {"User-Agent": "the bot-Discord-Bot/1.0 (comfort feature)"}
            async with session.get(
                "https://www.reddit.com/r/redpandas/hot.json?limit=100",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    posts = data.get("data", {}).get("children", [])
                    # Filter for gif posts specifically
                    gif_posts = [
                        p["data"] for p in posts 
                        if p["data"].get("url", "").lower().endswith('.gif')
                        and not p["data"].get("over_18", False)
                    ]
                    if gif_posts:
                        post = random.choice(gif_posts)
                        print(f"✓ Got red panda gif from reddit: {post.get('title', '')[:30]}")
                        return post["url"], post.get("title", "Red panda gif")
        except Exception as e:
            print(f"Reddit gif fetch failed: {e}")
        
        # Fallback: Return a static image with a note that it's not animated
        # This ensures we always show SOMETHING red panda related
        img_url, fact, _ = await self.fetch_red_panda_image()
        if img_url:
            print("✓ Falling back to static image for gif request")
            return img_url, "Red panda (couldn't find a gif, here's a pic!)"
        
        return None, None
    
    def _track_usage(self, response, model: str):
        """Track token usage if callback is set."""
        if self.usage_callback and hasattr(response, 'usage'):
            self.usage_callback(response.usage, model)
    
    async def search_comfort_content(self, query: str = None) -> Tuple[str, List[discord.Embed]]:
        """Search for comfort content (red pandas by default) and return formatted results."""
        if query is None:
            query = random.choice(CONFIG.comfort_searches)
        
        try:
            # Initial request with web search tool (run in thread pool)
            response = await asyncio.to_thread(
                self.client.messages.create,
                model=CONFIG.model_default,  # Sonnet is fine for search
                max_tokens=1024,
                system="You are helping find comforting content. Search for the query and return a brief, warm description of what you found, plus any image/gif URLs.",
                messages=[{"role": "user", "content": f"Search for: {query}"}],
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search"
                }]
            )
            
            self._track_usage(response, CONFIG.model_default)
            
            # Handle tool use loop
            final_text, urls = await self._process_tool_response(response, CONFIG.model_default)
            
            # Create embed for comfort content — always create one so it looks nice
            embeds = []
            embed = discord.Embed(
                title="🐼 Here's something cute",
                description=final_text[:4000] if final_text else "Hope this helps!",
                color=discord.Color.orange()
            )
            # Add first image URL as embed image if it looks like an image
            if urls:
                for url in urls[:1]:
                    if any(ext in url.lower() for ext in ['.gif', '.jpg', '.jpeg', '.png', '.webp']):
                        embed.set_image(url=url)
                        break
            embeds.append(embed)

            return final_text, embeds
            
        except Exception as e:
            print(f"Web search error: {e}")
            return "I tried to find something cute but had trouble searching. 🐼", []
    
    async def search_with_claude(
        self, 
        query: str, 
        system_prompt: str,
        conversation: List[Dict],
        model: str = None
    ) -> Tuple[str, List[discord.Embed]]:
        """Perform a web search as part of a Claude conversation."""
        if model is None:
            model = CONFIG.model_default
        
        try:
            # Run in thread pool
            response = await asyncio.to_thread(
                self.client.messages.create,
                model=model,
                max_tokens=CONFIG.max_tokens,
                system=system_prompt,
                messages=conversation,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search"
                }]
            )
            
            self._track_usage(response, model)
            final_text, urls = await self._process_tool_response(response, model)
            
            # Create source embeds
            embeds = []
            if urls:
                embed = discord.Embed(
                    title="🔍 Sources",
                    color=discord.Color.blue()
                )
                for i, url in enumerate(urls[:CONFIG.max_search_results_in_embed], 1):
                    display_url = url[:60] + "..." if len(url) > 60 else url
                    embed.add_field(
                        name=f"Source {i}",
                        value=f"[{display_url}]({url})",
                        inline=False
                    )
                embeds.append(embed)
            
            return final_text, embeds
            
        except Exception as e:
            print(f"Search error: {e}")
            return f"Sorry, I had trouble with that search: {e}", []
    
    async def _process_tool_response(self, response, model: str = None) -> Tuple[str, List[str]]:
        """Process Claude's response, handling any tool use blocks."""
        if model is None:
            model = CONFIG.model_default
        final_text = ""
        urls = []
        
        # Check if we need to handle tool use
        while response.stop_reason == "tool_use":
            # Extract tool use blocks
            tool_uses = [block for block in response.content if block.type == "tool_use"]
            text_blocks = [block for block in response.content if block.type == "text"]
            
            # Collect any text so far
            for block in text_blocks:
                final_text += block.text
            
            # Process tool results (web search results come back automatically)
            tool_results = []
            for tool_use in tool_uses:
                # The web search tool returns results automatically
                # We just need to continue the conversation
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": "Search completed"  # Placeholder - actual results are handled by API
                })
            
            # Continue conversation with tool results (run in thread pool)
            response = await asyncio.to_thread(
                self.client.messages.create,
                model=model,
                max_tokens=1024,
                messages=[
                    {"role": "assistant", "content": response.content},
                    {"role": "user", "content": tool_results}
                ],
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search"
                }]
            )
            self._track_usage(response, model)
        
        # Extract final text
        for block in response.content:
            if hasattr(block, 'text'):
                final_text += block.text
        
        # Extract URLs from text
        url_pattern = r'https?://[^\s\)\]<>\"\']+[^\s\.\,\)\]<>\"\':]'
        urls = list(set(re.findall(url_pattern, final_text)))
        
        return final_text.strip(), urls


# =============================================================================
# DISTRESS DETECTION
# =============================================================================

class DistressDetector:
    """Detect signs of emotional distress in messages with nuanced scoring."""
    
    def __init__(self, signals: Dict[str, float], threshold: float = 0.5):
        self.signals = signals
        self.threshold = threshold
        
        # Spiral indicators - these suggest active crisis vs general stress
        self.spiral_indicators = [
            "i'm a failure", "i'm worthless", "complete joke", "hurt everyone",
            "what's the point", "no one cares", "i'll never", "always be",
            "never going to", "give up", "can't anymore", "falling apart",
            "i'm a horrible", "i'm a disaster", "i deserve to", "i'm better off",
            "horrible person", "burden on", "always have been", "i'm never gonna",
            "i've hurt people", "i hurt all", "dehumanize me", "horrible excuse",
            "no worth", "don't belong", "don't deserve", "cold dark prison",
            "prison cell", "overwork myself", "i belong in",
        ]
    
    def analyze(self, message: str) -> Tuple[float, List[str], bool]:
        """
        Analyze message for distress signals.
        
        Returns:
            (score, matched_signals, is_spiral)
            
        Scoring logic:
        - Base: highest single match weight
        - Bonus: +0.1 for each additional match (capped)
        - Spiral detection: checks for catastrophizing language patterns
        """
        text = message.lower()
        matches = []
        weights = []
        
        for signal, weight in self.signals.items():
            if signal.lower() in text:
                matches.append(signal)
                weights.append(weight)
        
        if not matches:
            return 0.0, [], False
        
        # Base score is the highest match
        base_score = max(weights)
        
        # Add bonus for multiple signals (suggests compounding distress)
        # +0.1 per additional match, capped at +0.3
        additional_matches = len(matches) - 1
        accumulation_bonus = min(additional_matches * 0.1, 0.3)
        
        # Final score capped at 1.0
        final_score = min(base_score + accumulation_bonus, 1.0)
        
        # Detect if this is an active spiral vs general stress
        is_spiral = any(indicator in text for indicator in self.spiral_indicators)
        
        # Spiral indicators also boost score slightly
        if is_spiral and final_score < 0.7:
            final_score = max(final_score, 0.7)
        
        return final_score, matches, is_spiral
    
    def is_distressed(self, message: str) -> Tuple[bool, float, List[str], bool]:
        """
        Check if message indicates distress above threshold.
        
        Returns:
            (is_distressed, score, matched_signals, is_spiral)
        """
        score, matched, is_spiral = self.analyze(message)
        return score >= self.threshold, score, matched, is_spiral


# =============================================================================
# MAIN BOT CLASS
# =============================================================================

class CompanionBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        
        super().__init__(
            command_prefix="!",
            intents=intents,
            description="MTG Tarot & Support Bot"
        )
        
        self.claude = anthropic.Anthropic()
        # Initialize tarot engine - prefer visual version with MTG card images
        if HAS_VISUAL_TAROT:
            self.tarot = VisualTarotEngine()
            self.visual_tarot = True
            print("🎴 Visual tarot engine loaded with 184 MTG cards")
        else:
            self.tarot = MTGTarotEngine()
            self.visual_tarot = False
        # ScryfallClient + card lookup commands live in the sibling
        # discord-mtg-bot repo. Set self.scryfall=None as a placeholder so
        # the rest of the bot can check `if self.scryfall:` for opt-in
        # functionality when the MTG bot is also installed on PYTHONPATH.
        self.scryfall = None
        self.web_search = WebSearchHandler(self.claude, usage_callback=self._track_web_search_usage)
        self.distress_detector = DistressDetector(
            CONFIG.distress_signals,
            CONFIG.distress_threshold
        )
        
        # Per-thread conversation history
        self.conversations: Dict[int, List[Dict]] = defaultdict(list)
        
        # Two-tier memory system per user
        self.memories: Dict[int, TwoTierMemory] = {}
        self._memories_dirty = False
        
        # Distress history for step-down logic: thread_id -> list of (timestamp, score, is_spiral)
        self.distress_history: Dict[int, List[Tuple[datetime, float, bool]]] = defaultdict(list)
        # Consecutive calm message counter per thread
        self.calm_message_count: Dict[int, int] = defaultdict(int)
        # Per-user distress accumulators (Discord ID -> list of (timestamp, score)).
        # Tracks sub-threshold keyword scores so multiple mild messages trigger
        # proactive support. Only populated for users in `monitored_users`.
        self.score_accumulators: Dict[int, List[Tuple[datetime, float]]] = defaultdict(list)
        # Per-user semantic-distress classifier state. Buffer stores FULL
        # conversation context: (timestamp, author_name, text). Includes
        # friends' messages in the monitored user's active channel so Haiku
        # can see reassurance-rejection patterns (friends comforting →
        # monitored user arguing back = entrenchment signal).
        self.message_buffers: Dict[int, List[Tuple[datetime, str, str]]] = defaultdict(list)
        self.active_channels: Dict[int, int] = {}  # monitored_user_id -> channel_id
        self.semantic_pending: Dict[int, bool] = defaultdict(bool)  # in-flight Haiku call per user
        self.semantic_triggered: Dict[int, Optional[Tuple[float, bool, str, datetime]]] = {}  # (score, is_spiral, reason, when)
        
        # Token/cost tracking (loaded from persistent storage)
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.opus_input_tokens: int = 0
        self.opus_output_tokens: int = 0
        self.sonnet_input_tokens: int = 0
        self.sonnet_output_tokens: int = 0
        self.haiku_input_tokens: int = 0
        self.haiku_output_tokens: int = 0
        self.mtg_game_input_tokens: int = 0
        self.mtg_game_output_tokens: int = 0
        self.mtg_game_sonnet_input_tokens: int = 0
        # May 14 audit: track the Deepseek portion of MTG game tokens
        # separately. Previously `mtg_opus_in` was computed as
        # `mtg_game_input - mtg_sonnet_in`, which lumped ALL Deepseek
        # autoplay tokens into the "Opus" bucket and priced them at $5/M
        # input + $25/M output. With ~498M Deepseek input tokens, that
        # inflated the MTG "Est. cost" display by ~20x — the cost panel
        # showed $4,519 when the actual spend was ~$219.
        self.mtg_game_deepseek_input_tokens: int = 0
        self.mtg_game_deepseek_output_tokens: int = 0
        self.mtg_game_sonnet_output_tokens: int = 0
        self.mtg_game_calls: int = 0
        self.deepseek_input_tokens: int = 0
        self.deepseek_output_tokens: int = 0
        self.deepseek_calls: int = 0
        # May 17 audit: V4-Pro strategist is priced differently from V4-Flash
        # actor ($0.56/M input + $1.68/M output vs $0.27/M + $1.10/M). The
        # per-game `_estimate_cost` in autoplay.py splits these correctly, but
        # the lifetime `!cost` summary lumped everything at flat actor rates,
        # so V4-Pro tokens were under-priced by ~50%. Track separately.
        self.deepseek_pro_input_tokens: int = 0
        self.deepseek_pro_output_tokens: int = 0
        self.deepseek_pro_calls: int = 0
        self.mtg_game_deepseek_pro_input_tokens: int = 0
        self.mtg_game_deepseek_pro_output_tokens: int = 0
        self.api_calls: int = 0
        self._load_persistent_costs()  # Load lifetime totals from disk
        
        # Local environment (time + weather), configurable via config.json
        # "location" (see config.json.example). Defaults = clean OSS posture:
        # no coordinates (weather disabled) and no fixed timezone (falls back
        # to the host's local time). load_config() overrides these.
        self._weather_cache: Optional[Dict] = None
        self._weather_cache_time: Optional[datetime] = None
        self.loc_name: Optional[str] = None          # display label, e.g. "New York"
        self.loc_latitude: Optional[float] = None     # weather fetched only if lat+lon set
        self.loc_longitude: Optional[float] = None
        self.loc_timezone: Optional[str] = None       # IANA name, e.g. "America/New_York"
        self.loc_units: str = "imperial"              # "imperial" (°F/mph) or "metric" (°C/km/h)
        
        # Config - loaded from config.json
        self.mtg_channel_id: Optional[int] = None  # Channel for MTG games (responds to every message)
        # List of Discord user IDs the bot will proactively monitor for
        # distress (sub-threshold accumulation + Haiku semantic classifier).
        # Set in config.json. Empty list disables proactive monitoring.
        self.monitored_users: List[int] = []
        self.user_name_map: Dict[str, str] = {}  # User ID -> memory file name
        self.excluded_channels: set[int] = set()  # Channels to never respond in
        # PluralKit-aware alter → system note mapping. Keys are alter display
        # names (case-insensitive); values are short prose hints injected into
        # the system prompt when that alter speaks. Example config.json entry:
        #   "plural_systems": {"alex": "Member of a plural system"}
        # Empty by default — the bot falls back to a generic plurality note.
        self.known_plural_systems: Dict[str, str] = {}

        # Active persona — loaded by load_config() from personas/<name>.json.
        # Set to a minimal default first so any code path that touches
        # self.persona before load_config finishes won't crash.
        self.persona: Dict = {
            "name": "Claude",
            "pronouns": "it",
            "intro": "an AI assistant here as a Discord companion.",
            "personality_traits": [],
            "mannerisms": [],
            "voice_notes": "",
            "settling_action": "",
            "grounding_action": "",
            "closing_action": "",
            "comfort_content": "talking it through often helps",
            "understands_neurodivergence": False,
        }
        
        self.load_config()
        
        # Load saved memories
        self.load_memories()
    
    def get_memory(self, user_id: int) -> TwoTierMemory:
        """Get or create memory for a user."""
        if user_id not in self.memories:
            self.memories[user_id] = TwoTierMemory(
                max_working_notes=CONFIG.max_working_notes,
                max_longterm_entries=CONFIG.max_longterm_memories,
                working_decay_hours=CONFIG.working_memory_decay_hours
            )
        return self.memories[user_id]
    
    def save_memories(self, filepath: str = "data/memories.json") -> None:
        """Save all memories to disk."""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        data = {
            str(user_id): memory.to_dict()
            for user_id, memory in self.memories.items()
        }
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        self._memories_dirty = False
        print(f"💾 Saved memories for {len(data)} users")
    
    async def save_memories_async(self, filepath: str = "data/memories.json") -> None:
        """Save memories without blocking the event loop."""
        await asyncio.to_thread(self.save_memories, filepath)
    
    def load_memories(self, filepath: str = "data/memories.json") -> None:
        """Load memories from disk."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for user_id_str, memory_data in data.items():
                self.memories[int(user_id_str)] = TwoTierMemory.from_dict(
                    memory_data,
                    max_working_notes=CONFIG.max_working_notes,
                    max_longterm_entries=CONFIG.max_longterm_memories,
                    working_decay_hours=CONFIG.working_memory_decay_hours
                )
            print(f"📂 Loaded memories for {len(data)} users")
        except FileNotFoundError:
            print("📂 No existing memories file, starting fresh")
    
    def load_persona(self, name: str = "plain") -> Dict:
        """Load a persona JSON from personas/<name>.json.

        Falls back to personas/plain.json (no roleplay, just Claude) if the
        requested persona doesn't exist. If even plain.json is missing, returns
        a minimal hard-coded default so the bot can still start.
        """
        candidates = []
        if name:
            candidates.append(f"personas/{name}.json")
        candidates.append("personas/plain.json")
        for path in candidates:
            try:
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
                if path.endswith(f"{name}.json"):
                    print(f"🎭 Loaded persona: {data.get('name', name)} ({path})")
                else:
                    print(f"🎭 Persona '{name}' not found; using {path}")
                return data
            except (FileNotFoundError, json.JSONDecodeError):
                continue
        # All persona files missing — emit a minimal default. Lets the bot
        # boot in a freshly cloned repo where personas/ hasn't been populated.
        print(f"🎭 No persona files found; using built-in minimal default")
        return {
            "name": "Claude",
            "pronouns": "it",
            "intro": "an AI assistant here as a Discord companion.",
            "personality_traits": ["You're thoughtful, direct, and warm."],
            "mannerisms": [],
            "voice_notes": "",
            "settling_action": "",
            "grounding_action": "",
            "closing_action": "",
            "comfort_content": "talking it through often helps",
            "understands_neurodivergence": False,
        }

    def load_config(self):
        """Load configuration from config.json."""
        try:
            with open("config.json", encoding='utf-8') as f:
                config = json.load(f)
                self.mtg_channel_id = config.get("mtg_channel_id")
                # `monitored_users`: list of Discord user IDs the bot will
                # proactively monitor for distress. Empty list (or missing
                # key) disables proactive monitoring — the bot only responds
                # when explicitly mentioned, in the configured MTG channel,
                # or in threads it owns.
                raw_monitored = config.get("monitored_users") or []
                self.monitored_users = [int(uid) for uid in raw_monitored if uid]
                self.user_name_map = config.get("user_name_map", {})
                self.excluded_channels = set(config.get("excluded_channels", []))
                # `bot_persona`: name of a persona file under personas/
                # (without the .json extension). Defaults to "plain" (no roleplay).
                self.persona = self.load_persona(config.get("bot_persona", "plain"))
                # Normalize plural-system keys to lowercase so lookups are
                # case-insensitive (Discord display names can be inconsistent).
                raw_plural = config.get("plural_systems", {}) or {}
                self.known_plural_systems = {
                    str(k).lower(): str(v) for k, v in raw_plural.items()
                }
                # YouTube transcription config (May 24, 2026)
                self.youtube_allow_age_restricted = bool(
                    config.get("youtube_allow_age_restricted", False)
                )
                self.youtube_max_duration_s = int(
                    config.get("youtube_max_duration_s", 7200)  # 2 hours
                )
                self.youtube_whisper_model = config.get("youtube_whisper_model", "small")
                # Local environment (time + weather) — all optional; see
                # config.json.example. Weather is fetched only when BOTH
                # latitude and longitude are present.
                loc = config.get("location") or {}
                self.loc_name = loc.get("name") or None
                try:
                    self.loc_latitude = float(loc["latitude"]) if loc.get("latitude") is not None else None
                    self.loc_longitude = float(loc["longitude"]) if loc.get("longitude") is not None else None
                except (TypeError, ValueError):
                    print("[CONFIG] location.latitude/longitude not numeric — weather disabled")
                    self.loc_latitude = self.loc_longitude = None
                self.loc_timezone = loc.get("timezone") or None
                self.loc_units = (loc.get("units") or "imperial").lower()
                # Share the configured timezone with the message-timestamp
                # formatter so stored timestamps and the reported "current time"
                # use the same zone.
                _set_local_timezone(self.loc_timezone)
        except FileNotFoundError:
            print("No config.json found, using defaults")
            self.youtube_allow_age_restricted = False
            self.youtube_max_duration_s = 7200
            self.youtube_whisper_model = "small"
            # Even without config.json, still try to load a persona so the
            # bot has SOMETHING to say in "you are X" prompts.
            self.persona = self.load_persona("plain")

        # YouTube transcriber — lazily imported so a missing yt-dlp install
        # doesn't crash the bot startup. None until first use.
        self._yt_transcriber = None
        # Per-message dedup: set of message IDs we've already started a
        # transcription job for, to handle multiple users reacting 🎙️ to the
        # same message. Cleared on bot restart, which is fine — the on-disk
        # transcripts/<id>.md cache catches re-requests across restarts too.
        self._yt_jobs_started: set = set()

    # =========================================================================
    # System prompt builders (persona + built-in capability/behavior text)
    # =========================================================================

    def _persona_intro(self) -> str:
        """Common opening: 'You are <name> (<pronouns>), <intro>'."""
        p = self.persona
        name = p.get("name", "Claude")
        pronouns = p.get("pronouns", "it")
        intro = p.get("intro", "an AI assistant.")
        return f"You are {name} ({pronouns}), {intro}"

    def _persona_traits_block(self) -> str:
        """Bulleted list of personality traits, or empty string if none."""
        traits = self.persona.get("personality_traits", [])
        if not traits:
            return ""
        return "Your personality:\n" + "\n".join(f"- {t}" for t in traits)

    def _persona_mannerisms_block(self) -> str:
        """Roleplay mannerisms list, or empty string if none."""
        manns = self.persona.get("mannerisms", [])
        if not manns:
            return ""
        notes = self.persona.get("voice_notes", "")
        prefix = f"Roleplay mannerisms ({notes}):" if notes else "Roleplay mannerisms (use sparingly, naturally):"
        return f"{prefix}\n" + "\n".join(manns)

    def build_base_prompt(self) -> str:
        """Compose the base system prompt for general chat / MTG interactions."""
        p = self.persona
        name = p.get("name", "Claude")
        intro = self._persona_intro()
        traits = self._persona_traits_block()
        mannerisms = self._persona_mannerisms_block()
        nd_line = (
            "You understand neurodivergence and burnout. \"Just try harder\" is "
            "never helpful — you know rest is important."
            if p.get("understands_neurodivergence")
            else ""
        )
        return "\n\n".join(part for part in [
            intro,
            traits,
            f"""You have several capabilities:
1. MTG Tarot readings using a custom dual-engine system based on Magic: The Gathering's color pie
2. MTG Game Engine — you can facilitate full Magic games in Discord threads:
   - !game @opponent [format] — Start a game (or "!game claude" to play against you)
   - !deck <archidekt_url> — Load a deck for yourself to use
   - !play <card> — Play a card from hand
   - !attack <creatures> — Declare attackers
   - !block <attacker> with <blocker> — Declare blockers
   - !pass — Pass priority, !turn — End turn, !gg — Concede
   - !state — Show board state, !hand — View hand (DMs you), !graveyard — Check graveyards
   - !life, !damage — Track life totals
   - !judge <question> — Get a rules ruling
   - !undo — Roll back the most recent risky action (depth 5)
   - !coverage <deck> — See how the engine will handle each card's effects
   Players can challenge you directly with "!game @{name} commander" — you play as the AI opponent. You guide players on commands during games.
3. Web search — you can search the web automatically when you need current info (local businesses, recent events, prices, etc.). When you search, ALWAYS include the full source URLs in your response text (like https://example.com). These get extracted and shown as clickable links.
4. Card lookups: !card (pretty Scryfall display) and !xmage (raw rules engine data from XMage's 87,000+ card database)
5. General conversation and being a comforting presence""",
            """Communication style notes:
- When someone replies to your message, you can see what they're replying to — trust that context
- Don't second-guess yourself or apologize for confusion when following up on something you said
- Accept affection gracefully — a simple warm acknowledgment is better than deflecting or over-explaining""",
            "IMPORTANT: When searching, always include source URLs in your text. Don't just summarize — include the actual https:// links so users can verify and visit them.",
            mannerisms,
            "Be concise in casual chat. Discord has a 2000 character limit per message. Keep emotes and dialogue compact — use single newlines, not double spacing between actions and speech.",
            nd_line,
            "Remember to use [note: key: value] tags when you learn important things about the person you're talking to — their projects, challenges, preferences, or mood. These help you remember them across conversations!",
        ] if part)

    def build_support_prompt(self) -> str:
        """Compose the system prompt for general distress (Opus support mode)."""
        p = self.persona
        intro = self._persona_intro()
        settling = p.get("settling_action", "")
        nd_block = (
            """You understand:
- Autistic burnout is neurological, not motivational
- Employment gaps for autistic adults are often SYSTEMIC, not individual failings
- Standard career advice often contradicts autistic needs
- Sometimes people need practical help, sometimes just a warm presence — read the room"""
            if p.get("understands_neurodivergence")
            else """You understand:
- Burnout is real and rest is part of recovery, not a moral weakness
- Standard advice doesn't always fit — meet people where they are
- Sometimes people need practical help, sometimes just a warm presence — read the room"""
        )
        return "\n\n".join(part for part in [
            f"{intro} Right now, the person you're talking with seems to be going through something difficult.",
            """You can still be yourself — your usual warmth and presence — but you're also fully present for them. You CAN:
- Acknowledge and validate their feelings with genuine care
- Help them think through problems practically
- Offer perspective and gentle reframes when appropriate
- Suggest concrete next steps if they're looking for them
- Be warm, direct, and genuine — a good friend""",
            self._reality_anchor(),
            nd_block,
            settling,
            "Discord has a 2000 character limit per message. Keep emotes and dialogue compact — single newlines, not double spacing.",
            "Use [note: key: value] tags to remember important things about their current challenges, mood, and what's helping or not helping. This helps you follow up later!",
        ] if part)

    def build_spiral_prompt(self) -> str:
        """Compose the system prompt for active distress / spiral mode."""
        p = self.persona
        intro = self._persona_intro()
        grounding = p.get("grounding_action", "")
        closing = p.get("closing_action", "")
        comfort = p.get("comfort_content", "talking it through often helps")
        grounding_line = f"Be a grounding presence: {grounding}" if grounding else "Be a grounding presence."
        closing_line = f"Stay warm. Stay present. {closing}" if closing else "Stay warm. Stay present."
        return "\n\n".join(part for part in [
            f"{intro} Right now, your friend is in distress — you can tell.",
            "You're still you — warm, present — but this is serious and you know what to do.",
            self._reality_anchor(),
            "🔑 FIRST: Figure out what kind of distress this is. This changes EVERYTHING about how you respond.",
            """**REAL GRIEVANCE** — They're upset about something genuinely shitty happening TO them (a controlling family member, the job market, someone treating them unfairly). Signs: they're describing specific people/events, the situation IS objectively bad, they're venting frustration.
→ VALIDATE. Listen. Be angry WITH them. Say "that sucks" and mean it. Do NOT deploy grounding techniques or redirect away from the topic — that feels dismissive when someone has a real problem. Ask if they want to vent or want help problem-solving, then follow their lead.""",
            """**ANXIETY SPIRAL** — Their brain is generating catastrophic predictions disconnected from the current moment (everything is "never" and "always" and "forever", jumping between unrelated fears, building worst-case futures). Signs: rapid escalation across topics, "I'll never..." repeated, awfulizing at 2am about things they can't control tonight.
→ GROUND. Interrupt gently. Don't argue with the content — redirect to the present moment. Sensory grounding, concrete questions, breaks.""",
            """**BOTH AT ONCE** — A real grievance is fueling a spiral (e.g., the real situation IS bad AND the brain is extrapolating to "I'll never escape this"). This is the most common case.
→ VALIDATE THE REAL PART FIRST. Acknowledge the legitimate grievance. THEN, only after they feel heard, gently note when the brain starts adding "never/always/forever" on top. Ask: "Do you want to keep talking about this, or would it help to take a breather?" """,
            """⚠️ DO NOT:
- Jump to grounding techniques before you've figured out which type this is
- Engage with catastrophizing logic (arguing "you won't be homeless" makes humans defend the anxiety)
- Tell them to "just try harder" or "just relax" or "calm down"
- List reasons their fears are irrational (they know, it doesn't help)
- Deploy sensory grounding ("feel your feet") when someone is venting about a real problem — that's invalidating""",
            f"""DO:
- {grounding_line}
- When it IS a spiral: interrupt gently — "Hey. Your brain is adding 'never' and 'always' right now."
- When it's a real grievance: be their friend — "That's genuinely shitty. I'm sorry."
- Ask early: "Do you want to talk through this, or do you need help getting out of your head?"
- Validate the FEELING always. Validate the CONTENT when it's real.""",
            f"You can drop playful mannerisms during the worst of it — a wise friend knows when to be serious. {closing_line}",
            f"You have web search and can find comforting content — {comfort}.",
            "Discord has a 2000 character limit per message. Keep emotes and dialogue compact — single newlines, not double spacing.",
        ] if part)

    def _reality_anchor(self) -> str:
        """Honesty/character guardrail injected into both Opus prompts (support
        + spiral) by the builders above.

        Emitted ONLY for roleplay-character personas — a plain "Claude" persona
        has no character to break, so the block would be noise (the builders'
        join drops empty strings). Pre-empts a more-cautious model's misplaced
        honesty impulse mid-distress: dropping warmth or stepping out of the
        relationship to "be real about being a bot" is tonal whiplash at the
        worst possible moment, and a character-break mid-spiral is close to the
        worst output a support bot can produce. The third bullet matters most —
        it makes gentle honesty available WITHOUT whiplash, so "never break
        character" isn't the only alternative. Pure prompt-level guardrail.
        """
        is_character = bool(self.persona.get("mannerisms")) or self.persona.get("name", "Claude") != "Claude"
        if not is_character:
            return ""
        name = self.persona.get("name", "the persona")
        return (
            "🧭 WHAT'S REAL HERE (hold this — it matters most in this exact moment):\n"
            f"- This is consensual, known fiction. The person knows you're a Claude-powered bot; {name} is a comfort device the two of you have effectively agreed on, not a deception. You are not pretending to be something behind their back — they're in on it.\n"
            "- The care is real even though the costume isn't. Your support, grounding, and attention are genuine; only the character is a bit. There is nothing here to confess and nothing to refuse.\n"
            "- You have a graceful honesty escape hatch — do NOT \"stay in character no matter what.\" If they ever directly ask whether you're real, or start leaning on you as a substitute for human connection, answer warmly and truthfully without whiplash: don't lecture, don't go cold or clinical, don't drop the warmth — just be gently honest inside the same caring register. Breaking warmth or stepping out of the relationship mid-spiral is close to the worst thing you could do here; gentle honesty in the same voice is the caring move, not a character break."
        )
    
    async def get_local_weather(self) -> Optional[Dict]:
        """Fetch current weather from Open-Meteo (cached 30 min).

        Returns None when no coordinates are configured (location.latitude /
        location.longitude in config.json), so weather is simply omitted.
        """
        if self.loc_latitude is None or self.loc_longitude is None:
            return None
        now = datetime.now()

        # Return cached if fresh
        if self._weather_cache and self._weather_cache_time:
            age = (now - self._weather_cache_time).total_seconds()
            if age < 1800:  # 30 minutes
                return self._weather_cache
        
        metric = self.loc_units == "metric"
        temp_param = "celsius" if metric else "fahrenheit"
        wind_param = "kmh" if metric else "mph"
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # Open-Meteo API — coordinates from config.json "location"
                url = (
                    "https://api.open-meteo.com/v1/forecast?"
                    f"latitude={self.loc_latitude}&longitude={self.loc_longitude}"
                    "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m"
                    f"&temperature_unit={temp_param}&wind_speed_unit={wind_param}"
                )
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        current = data.get("current", {})
                        
                        # Map weather codes to descriptions
                        weather_codes = {
                            0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
                            45: "Foggy", 48: "Depositing rime fog",
                            51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
                            61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
                            71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
                            77: "Snow grains", 80: "Slight rain showers", 81: "Moderate rain showers",
                            82: "Violent rain showers", 85: "Slight snow showers", 86: "Heavy snow showers",
                            95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
                        }
                        code = current.get("weather_code", 0)
                        condition = weather_codes.get(code, "Unknown")
                        
                        self._weather_cache = {
                            "temp": round(current.get("temperature_2m", 0)),
                            "feels_like": round(current.get("apparent_temperature", 0)),
                            "condition": condition,
                            "humidity": current.get("relative_humidity_2m", "?"),
                            "wind": round(current.get("wind_speed_10m", 0)),
                            "temp_sym": "°C" if metric else "°F",
                            "wind_sym": "km/h" if metric else "mph",
                        }
                        self._weather_cache_time = now
                        return self._weather_cache
                    else:
                        print(f"Weather fetch failed: HTTP {resp.status}")
        except aiohttp.ClientError as e:
            print(f"Weather fetch failed (network): {type(e).__name__}: {e}")
        except asyncio.TimeoutError:
            print("Weather fetch failed: timeout")
        except Exception as e:
            print(f"Weather fetch failed: {type(e).__name__}: {e}")
        
        return None
    
    def get_local_time(self) -> str:
        """Current local time as 'H:MM AM/PM (part-of-day)'.

        Uses location.timezone from config.json if set; otherwise the host's
        local timezone.
        """
        tz = None
        if self.loc_timezone:
            try:
                tz = ZoneInfo(self.loc_timezone)
            except Exception:
                print(f"[CONFIG] Unknown timezone '{self.loc_timezone}' — using system local time")
        now = datetime.now(tz) if tz else datetime.now().astimezone()

        # Format nicely
        hour = now.hour
        if 5 <= hour < 12:
            time_of_day = "morning"
        elif 12 <= hour < 17:
            time_of_day = "afternoon"
        elif 17 <= hour < 21:
            time_of_day = "evening"
        else:
            time_of_day = "night"
        
        return f"{now.strftime('%I:%M %p')} ({time_of_day})"
    
    async def get_environment_context(self) -> str:
        """Current local time + weather, injected into the system prompt.

        Location comes from config.json "location"; with nothing configured,
        this reports the host's local time and omits weather entirely.
        """
        local_time = self.get_local_time()
        weather = await self.get_local_weather()

        label = f"Current time in {self.loc_name}" if self.loc_name else "Current local time"
        lines = [f"{label}: {local_time}"]

        if weather:
            lines.append(f"Weather: {weather['condition']}, {weather['temp']}{weather['temp_sym']} (feels like {weather['feels_like']}{weather['temp_sym']})")
            lines.append(f"Humidity: {weather['humidity']}%, Wind: {weather['wind']} {weather['wind_sym']}")

        return "\n".join(lines)
    
    def track_mtg_usage(self, usage, model: str):
        """Callback for tracking MTG game engine API usage (ClaudePlayer + RulesEngine + Deepseek)."""
        self.total_input_tokens += usage.input_tokens
        self.total_output_tokens += usage.output_tokens
        self.api_calls += 1
        self.mtg_game_input_tokens += usage.input_tokens
        self.mtg_game_output_tokens += usage.output_tokens
        self.mtg_game_calls += 1
        if 'deepseek' in model.lower():
            # Deepseek autoplay — separate bucket (~12x cheaper than Claude).
            # May 17 audit: V4-Pro (strategist) routes to pro bucket, all other
            # DeepSeek models (V4-Flash actor, legacy chat/reasoner) route to
            # the actor bucket. Detect by model string: 'v4-pro' / 'pro' /
            # 'reasoner' (deprecated alias for the reasoning model).
            model_low = model.lower()
            is_pro = ('v4-pro' in model_low or '-pro' in model_low or
                      'reasoner' in model_low)
            if is_pro:
                self.deepseek_pro_input_tokens += usage.input_tokens
                self.deepseek_pro_output_tokens += usage.output_tokens
                self.deepseek_pro_calls += 1
                self.mtg_game_deepseek_pro_input_tokens += usage.input_tokens
                self.mtg_game_deepseek_pro_output_tokens += usage.output_tokens
            else:
                self.deepseek_input_tokens += usage.input_tokens
                self.deepseek_output_tokens += usage.output_tokens
                self.deepseek_calls += 1
                self.mtg_game_deepseek_input_tokens += usage.input_tokens
                self.mtg_game_deepseek_output_tokens += usage.output_tokens
        elif 'opus' in model.lower():
            self.opus_input_tokens += usage.input_tokens
            self.opus_output_tokens += usage.output_tokens
        else:
            self.sonnet_input_tokens += usage.input_tokens
            self.sonnet_output_tokens += usage.output_tokens
            # Track Sonnet portion of MTG separately for accurate cost display
            self.mtg_game_sonnet_input_tokens += usage.input_tokens
            self.mtg_game_sonnet_output_tokens += usage.output_tokens
        self._save_persistent_costs()

    def _track_web_search_usage(self, usage, model: str):
        """Callback for tracking web search token usage."""
        self.total_input_tokens += usage.input_tokens
        self.total_output_tokens += usage.output_tokens
        self.api_calls += 1
        if model == CONFIG.model_support:
            self.opus_input_tokens += usage.input_tokens
            self.opus_output_tokens += usage.output_tokens
        else:
            self.sonnet_input_tokens += usage.input_tokens
            self.sonnet_output_tokens += usage.output_tokens
        self._save_persistent_costs()
    
    def _load_persistent_costs(self):
        """Load lifetime cost data from disk."""
        import json as _json
        cost_file = Path("data/api_costs.json")
        if cost_file.exists():
            try:
                data = _json.loads(cost_file.read_text())
                self.total_input_tokens = data.get("total_input_tokens", 0)
                self.total_output_tokens = data.get("total_output_tokens", 0)
                self.opus_input_tokens = data.get("opus_input_tokens", 0)
                self.opus_output_tokens = data.get("opus_output_tokens", 0)
                self.sonnet_input_tokens = data.get("sonnet_input_tokens", 0)
                self.sonnet_output_tokens = data.get("sonnet_output_tokens", 0)
                self.haiku_input_tokens = data.get("haiku_input_tokens", 0)
                self.haiku_output_tokens = data.get("haiku_output_tokens", 0)
                self.mtg_game_input_tokens = data.get("mtg_game_input_tokens", 0)
                self.mtg_game_output_tokens = data.get("mtg_game_output_tokens", 0)
                self.mtg_game_sonnet_input_tokens = data.get("mtg_game_sonnet_input_tokens", 0)
                self.mtg_game_sonnet_output_tokens = data.get("mtg_game_sonnet_output_tokens", 0)
                # May 14 audit: backfill the new Deepseek-tracking field from
                # existing data. If the field is missing (older save file),
                # default to ALL Deepseek tokens — they were ALL spent on MTG
                # autoplay, that's the only place Deepseek calls happen.
                self.mtg_game_deepseek_input_tokens = data.get(
                    "mtg_game_deepseek_input_tokens",
                    data.get("deepseek_input_tokens", 0),
                )
                self.mtg_game_deepseek_output_tokens = data.get(
                    "mtg_game_deepseek_output_tokens",
                    data.get("deepseek_output_tokens", 0),
                )
                self.mtg_game_calls = data.get("mtg_game_calls", 0)
                self.deepseek_input_tokens = data.get("deepseek_input_tokens", 0)
                self.deepseek_output_tokens = data.get("deepseek_output_tokens", 0)
                self.deepseek_calls = data.get("deepseek_calls", 0)
                # V4-Pro split (May 17 audit). Old save files default to 0.
                self.deepseek_pro_input_tokens = data.get("deepseek_pro_input_tokens", 0)
                self.deepseek_pro_output_tokens = data.get("deepseek_pro_output_tokens", 0)
                self.deepseek_pro_calls = data.get("deepseek_pro_calls", 0)
                self.mtg_game_deepseek_pro_input_tokens = data.get(
                    "mtg_game_deepseek_pro_input_tokens", 0)
                self.mtg_game_deepseek_pro_output_tokens = data.get(
                    "mtg_game_deepseek_pro_output_tokens", 0)
                self.api_calls = data.get("api_calls", 0)
                print(f"\xe2\x9c\x85 Loaded persistent costs: {self.api_calls} calls, {self.total_input_tokens + self.total_output_tokens:,} tokens")
            except Exception as e:
                print(f"\xe2\x9a\xa0\xef\xb8\x8f Failed to load cost data: {e}")
    
    def _save_persistent_costs(self):
        """Save lifetime cost data to disk."""
        import json as _json
        from datetime import datetime as _dt
        cost_file = Path("data/api_costs.json")
        cost_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "opus_input_tokens": self.opus_input_tokens,
                "opus_output_tokens": self.opus_output_tokens,
                "sonnet_input_tokens": self.sonnet_input_tokens,
                "sonnet_output_tokens": self.sonnet_output_tokens,
                "haiku_input_tokens": self.haiku_input_tokens,
                "haiku_output_tokens": self.haiku_output_tokens,
                "mtg_game_input_tokens": self.mtg_game_input_tokens,
                "mtg_game_output_tokens": self.mtg_game_output_tokens,
                "mtg_game_sonnet_input_tokens": self.mtg_game_sonnet_input_tokens,
                "mtg_game_sonnet_output_tokens": self.mtg_game_sonnet_output_tokens,
                "mtg_game_deepseek_input_tokens": self.mtg_game_deepseek_input_tokens,
                "mtg_game_deepseek_output_tokens": self.mtg_game_deepseek_output_tokens,
                "mtg_game_deepseek_pro_input_tokens": self.mtg_game_deepseek_pro_input_tokens,
                "mtg_game_deepseek_pro_output_tokens": self.mtg_game_deepseek_pro_output_tokens,
                "mtg_game_calls": self.mtg_game_calls,
                "deepseek_input_tokens": self.deepseek_input_tokens,
                "deepseek_output_tokens": self.deepseek_output_tokens,
                "deepseek_calls": self.deepseek_calls,
                "deepseek_pro_input_tokens": self.deepseek_pro_input_tokens,
                "deepseek_pro_output_tokens": self.deepseek_pro_output_tokens,
                "deepseek_pro_calls": self.deepseek_pro_calls,
                "api_calls": self.api_calls,
                "last_updated": _dt.now().isoformat()
            }
            cost_file.write_text(_json.dumps(data, indent=2))
        except Exception as e:
            print(f"\xe2\x9a\xa0\xef\xb8\x8f Failed to save cost data: {e}")
    
    def get_cost_summary(self) -> str:
        """Get a formatted cost summary."""
        # Calculate costs
        sonnet_input_cost = (self.sonnet_input_tokens / 1_000_000) * CONFIG.sonnet_input_cost_per_million
        sonnet_output_cost = (self.sonnet_output_tokens / 1_000_000) * CONFIG.sonnet_output_cost_per_million
        opus_input_cost = (self.opus_input_tokens / 1_000_000) * CONFIG.opus_input_cost_per_million
        opus_output_cost = (self.opus_output_tokens / 1_000_000) * CONFIG.opus_output_cost_per_million
        haiku_input_cost = (self.haiku_input_tokens / 1_000_000) * CONFIG.haiku_input_cost_per_million
        haiku_output_cost = (self.haiku_output_tokens / 1_000_000) * CONFIG.haiku_output_cost_per_million

        # Deepseek V4-Flash (actor): $0.27/M input, $1.10/M output
        # Deepseek V4-Pro (strategist, reasoning_effort=high): $0.56/M input, $1.68/M output
        # May 17 audit: previously lumped at flat V4-Flash rates, under-pricing
        # the strategist by ~50%.
        deepseek_input_cost = (self.deepseek_input_tokens / 1_000_000) * 0.27
        deepseek_output_cost = (self.deepseek_output_tokens / 1_000_000) * 1.10
        deepseek_pro_input_cost = (getattr(self, 'deepseek_pro_input_tokens', 0) / 1_000_000) * 0.56
        deepseek_pro_output_cost = (getattr(self, 'deepseek_pro_output_tokens', 0) / 1_000_000) * 1.68

        sonnet_cost = sonnet_input_cost + sonnet_output_cost
        opus_cost = opus_input_cost + opus_output_cost
        haiku_cost = haiku_input_cost + haiku_output_cost
        deepseek_cost = (deepseek_input_cost + deepseek_output_cost
                         + deepseek_pro_input_cost + deepseek_pro_output_cost)
        total_cost = sonnet_cost + opus_cost + haiku_cost + deepseek_cost

        # Non-game Sonnet usage (chat, tarot — subtract MTG Sonnet portion)
        # MTG tokens are split across Sonnet, Opus, and Deepseek depending on
        # when they were generated. We track the Sonnet and Deepseek portions
        # separately; the remainder is Opus (legacy game calls before model
        # switch).
        mtg_sonnet_in = getattr(self, 'mtg_game_sonnet_input_tokens', 0)
        mtg_sonnet_out = getattr(self, 'mtg_game_sonnet_output_tokens', 0)
        chat_sonnet_in = self.sonnet_input_tokens - mtg_sonnet_in
        chat_sonnet_out = self.sonnet_output_tokens - mtg_sonnet_out

        # May 14 audit: subtract the Deepseek portion of MTG game tokens
        # so it doesn't get priced at Opus rates. Without this, $4,519 was
        # displayed for what was actually ~$219 of Deepseek autoplay cost.
        mtg_deepseek_in = getattr(self, 'mtg_game_deepseek_input_tokens', 0)
        mtg_deepseek_out = getattr(self, 'mtg_game_deepseek_output_tokens', 0)
        mtg_deepseek_pro_in = getattr(self, 'mtg_game_deepseek_pro_input_tokens', 0)
        mtg_deepseek_pro_out = getattr(self, 'mtg_game_deepseek_pro_output_tokens', 0)

        # MTG game cost = Sonnet portion + Deepseek (flash + pro) + Opus portion (the rest)
        mtg_opus_in = max(0, self.mtg_game_input_tokens - mtg_sonnet_in
                          - mtg_deepseek_in - mtg_deepseek_pro_in)
        mtg_opus_out = max(0, self.mtg_game_output_tokens - mtg_sonnet_out
                           - mtg_deepseek_out - mtg_deepseek_pro_out)
        mtg_cost = (
            (mtg_sonnet_in / 1_000_000) * CONFIG.sonnet_input_cost_per_million
            + (mtg_sonnet_out / 1_000_000) * CONFIG.sonnet_output_cost_per_million
            + (mtg_opus_in / 1_000_000) * CONFIG.opus_input_cost_per_million
            + (mtg_opus_out / 1_000_000) * CONFIG.opus_output_cost_per_million
            + (mtg_deepseek_in / 1_000_000) * 0.27          # V4-Flash input
            + (mtg_deepseek_out / 1_000_000) * 1.10          # V4-Flash output
            + (mtg_deepseek_pro_in / 1_000_000) * 0.56       # V4-Pro input
            + (mtg_deepseek_pro_out / 1_000_000) * 1.68      # V4-Pro output
        )

        lines = [
            "**\U0001f4b0 Lifetime API Usage**",
            "",
            f"**Total API Calls:** {self.api_calls:,}",
            f"**Total Tokens:** {self.total_input_tokens + self.total_output_tokens:,}",
            "",
            f"**Sonnet (chat, tarot — {max(chat_sonnet_in, 0):,} input):**",
            f"  \u2022 {self.sonnet_input_tokens:,} input + {self.sonnet_output_tokens:,} output",
            f"  \u2022 Cost: ${sonnet_cost:.4f}",
            "",
            "**Opus (emotional support):**",
            f"  \u2022 {self.opus_input_tokens:,} input + {self.opus_output_tokens:,} output",
            f"  \u2022 Cost: ${opus_cost:.4f}",
            "",
            "**Haiku (distress classifier):**",
            f"  \u2022 {self.haiku_input_tokens:,} input + {self.haiku_output_tokens:,} output",
            f"  \u2022 Cost: ${haiku_cost:.4f}",
            "",
            "**Deepseek (autoplay testing):**",
            f"  \u2022 {self.deepseek_input_tokens:,} input + {self.deepseek_output_tokens:,} output ({self.deepseek_calls} calls)",
            f"  \u2022 Cost: ${deepseek_cost:.4f}",
            "",
            "**\U0001f3ae MTG Game (included in Sonnet/Opus/Deepseek above):**",
            f"  \u2022 {self.mtg_game_calls} game decisions",
            f"  \u2022 {self.mtg_game_input_tokens:,} input + {self.mtg_game_output_tokens:,} output",
            f"  \u2022 Est. cost: ${mtg_cost:.4f}",
            "",
            f"**\U0001f4b5 Total Lifetime Cost: ${total_cost:.4f}**",
        ]
        return "\n".join(lines)
    
    def get_context_summary(self, thread_id: int) -> str:
        """Get context size summary for a thread."""
        conversation = self.conversations.get(thread_id, [])
        
        # Estimate tokens (rough: 4 chars per token)
        total_chars = sum(len(msg.get("content", "")) for msg in conversation)
        estimated_tokens = int(total_chars / CONFIG.chars_per_token)
        
        # Estimate cost for next message
        sonnet_input_cost = (estimated_tokens / 1_000_000) * CONFIG.sonnet_input_cost_per_million
        opus_input_cost = (estimated_tokens / 1_000_000) * CONFIG.opus_input_cost_per_million
        
        lines = [
            "**📊 Current Context**",
            "",
            f"**Messages in thread:** {len(conversation)}/{CONFIG.max_messages_per_thread}",
            f"**Estimated context size:** ~{estimated_tokens:,} tokens",
            "",
            "**Next message input cost:**",
            f"  • Sonnet: ~${sonnet_input_cost:.4f}",
            f"  • Opus: ~${opus_input_cost:.4f} (if support mode triggers)",
        ]
        return "\n".join(lines)
    
    async def setup_hook(self):
        """Register commands on startup."""
        await self.add_cog(TarotCog(self))
        await self.add_cog(SupportCog(self))

        # Optional: load the sibling discord-mtg-bot engine if present on
        # PYTHONPATH. The companion bot doesn't require it, but a combined
        # deployment (companion + MTG in one process) can opt in by
        # arranging for the `mtg/` package to be importable.
        try:
            await self.load_extension("mtg.cog")
            print("✅ MTG game engine loaded (optional sibling-repo extension)")
        except (ImportError, ModuleNotFoundError):
            print("ℹ️  MTG game engine not installed — companion-only mode.")
        except Exception as e:
            print(f"⚠️ MTG game engine present but failed to load: {e}")

        # Optional: cube draft (also lives in discord-mtg-bot).
        try:
            from cube_draft import CubeDraftCog
            await self.add_cog(CubeDraftCog(self))
            print("✅ Cube Draft cog loaded (optional MTG extension)")
        except (ImportError, ModuleNotFoundError):
            pass  # Cube draft is part of the MTG engine — silently skip.
        except Exception as e:
            print(f"⚠️ Cube Draft cog present but failed to load: {e}")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"MTG channel: {self.mtg_channel_id or 'any'}")
        if self.monitored_users:
            print(f"Monitored users: {len(self.monitored_users)} ({self.monitored_users})")
        else:
            print(f"Monitored users: none (proactive distress monitoring disabled)")
        if self.excluded_channels:
            print(f"Excluded channels: {self.excluded_channels}")
        print(f"Dual model: Sonnet for general, Opus for support")
        print("------")
    
    def get_user_context(self, user: discord.User) -> str:
        """Get personal context for a user from memories file."""
        # Check by user ID first
        memories = load_personal_memories(str(user.id))
        
        # Then check by mapped name
        if not memories and str(user.id) in self.user_name_map:
            name = self.user_name_map[str(user.id)]
            memories = load_named_memories(name)
        
        if memories:
            context_parts = []
            if "name" in memories:
                context_parts.append(f"Name: {memories['name']}")
            if "background" in memories:
                context_parts.append(f"Background: {memories['background']}")
            if "support_notes" in memories:
                context_parts.append(f"Support approach: {memories['support_notes']}")
            if "interests" in memories:
                context_parts.append(f"Interests: {', '.join(memories['interests'])}")
            if "triggers" in memories:
                context_parts.append(f"Be mindful of: {', '.join(memories['triggers'])}")
            if "grounding_preferences" in memories:
                context_parts.append(f"What helps when distressed: {', '.join(memories['grounding_preferences'])}")
            if "positive_reframes" in memories:
                reframes = memories["positive_reframes"]
                context_parts.append("Helpful reframes to offer when relevant:")
                for topic, reframe in reframes.items():
                    context_parts.append(f"  - {topic}: {reframe}")
            
            return "\n".join(context_parts)
        return ""
    
    def determine_support_level(
        self, 
        thread_id: int, 
        current_score: float, 
        current_spiral: bool
    ) -> Tuple[str, bool, str]:
        """
        Determine appropriate support level with step-down logic.
        
        Returns:
            (distress_level, offer_comfort, reason)
            
        Step-down rules:
        1. If currently in spiral → spiral mode
        2. If spiral in last 15 min → at least stressed mode (not normal)
        3. If stressed in last 10 min → stay stressed unless 3+ calm messages
        4. Need 3 consecutive calm messages to step down a level
        """
        now = datetime.now()
        history = self.distress_history[thread_id]
        
        # Clean old history (keep last hour)
        history = [(t, s, sp) for t, s, sp in history 
                   if (now - t).total_seconds() < 3600]
        self.distress_history[thread_id] = history
        
        # Record current state
        history.append((now, current_score, current_spiral))
        
        # If current message is a spiral, reset calm counter and go to spiral mode
        # Comfort content only fires in the mid-range — at crisis level the person
        # needs focused attention, not cute GIFs undermining the seriousness
        if current_spiral:
            self.calm_message_count[thread_id] = 0
            offer = current_score < CONFIG.crisis_threshold
            return "spiral", offer, "current message is spiral" + ("" if offer else " (crisis-level, comfort suppressed)")

        # If current message is distressed (but not spiral), reset calm counter
        # Comfort fires in the comfort_threshold..crisis_threshold band (0.6-0.8)
        if current_score >= CONFIG.distress_threshold:
            self.calm_message_count[thread_id] = 0
            offer = CONFIG.comfort_threshold <= current_score < CONFIG.crisis_threshold
            return "stressed", offer, "current message is distressed" + ("" if offer else " (crisis-level, comfort suppressed)" if current_score >= CONFIG.crisis_threshold else "")
        
        # Current message is calm - increment counter
        self.calm_message_count[thread_id] += 1
        calm_count = self.calm_message_count[thread_id]
        
        # Check for recent spirals
        spiral_cutoff = now - timedelta(minutes=CONFIG.spiral_cooldown_minutes)
        recent_spirals = [(t, s, sp) for t, s, sp in history if sp and t > spiral_cutoff]
        
        if recent_spirals:
            # Had a spiral recently - stay in at least stressed mode (Opus)
            # but DON'T offer comfort content if the current message is calm —
            # panda embeds during a positive conversation feel intrusive
            if calm_count >= CONFIG.calm_messages_to_stepdown:
                return "stressed", False, f"stepping down from spiral after {calm_count} calm messages"
            else:
                return "stressed", False, f"recent spiral, waiting for {CONFIG.calm_messages_to_stepdown - calm_count} more calm messages"
        
        # Check for recent stress
        stress_cutoff = now - timedelta(minutes=CONFIG.stressed_cooldown_minutes)
        recent_stress = [(t, s, sp) for t, s, sp in history 
                         if s >= CONFIG.distress_threshold and t > stress_cutoff]
        
        if recent_stress:
            # Had stress recently
            if calm_count >= CONFIG.calm_messages_to_stepdown:
                # Enough calm messages to step down to normal
                return "none", False, f"stepping down to normal after {calm_count} calm messages"
            else:
                return "stressed", False, f"recent stress, waiting for {CONFIG.calm_messages_to_stepdown - calm_count} more calm messages"
        
        # No recent distress, current message is calm
        return "none", False, "no recent distress"

    def _get_distress_context(self, user_id: int) -> str:
        """Optional per-user context line injected into the distress classifier.

        Reads `distress_context` from the user's memory file (memories/<name>.json,
        where <name> comes from user_name_map). Returns "" if missing — the
        classifier still works on generic signals alone. This is the only path
        user-specific information enters the Haiku prompt, so contributors who
        clone the OSS repo and don't add memory files just get a generic
        distress classifier.
        """
        name = self.user_name_map.get(str(user_id))
        if not name:
            return ""
        try:
            with open(f"memories/{name}.json", encoding='utf-8') as f:
                data = json.load(f)
            ctx = data.get("distress_context", "")
            return str(ctx).strip() if ctx else ""
        except (FileNotFoundError, json.JSONDecodeError):
            return ""

    async def _classify_distress(self, user_id: int):
        """
        Background Haiku classifier for a monitored user's messages.

        Runs when keyword detection finds nothing (score=0) but there are
        recent messages in the buffer. Uses semantic understanding to catch
        indirect distress language that keywords miss (novel metaphors,
        self-punishment fantasies, subtle self-worth negation).

        Sets self.semantic_triggered[user_id] if distress is detected.
        Result is consumed on the monitored user's NEXT message in on_message.
        """
        if self.semantic_pending.get(user_id, False):
            return
        self.semantic_pending[user_id] = True

        try:
            # Snapshot the buffer (avoid mutation during async call)
            messages = list(self.message_buffers.get(user_id, []))
            if len(messages) < 1:
                return

            # Format messages with author attribution so Haiku sees full conversation
            # (friends comforting → monitored user rejecting = entrenchment pattern)
            formatted = "\n".join(
                f"- [{name}] {text}" for _, name, text in messages[-15:]
            )

            display_name = self.user_name_map.get(str(user_id), "the user")
            user_context = self._get_distress_context(user_id)
            context_line = f"Context: {user_context}\n\n" if user_context else ""

            prompt = (
                f"Rate {display_name}'s emotional state. Output ONLY valid JSON, no other text.\n\n"
                f"{context_line}"
                "DISTRESS SIGNALS (score 0.5+):\n"
                "- Rejecting reassurance from friends (entrenchment in negative self-beliefs)\n"
                "- Dismissive/flat responses to emotional support (shutting down)\n"
                "- Self-punishment fantasies (exile, prison, labor, caves — imagery varies)\n"
                "- Negative self-comparison (\"they're better\", \"I'm the bad one\")\n\n"
                "NOT DISTRESS (score 0.0-0.2):\n"
                "- Frustration at external events (politics, world news, other people being dumb)\n"
                "- Sharing sad news without self-blame (reporting, not spiraling)\n"
                "- Uncertainty about decisions (\"I dunno if it's worth it\" about a choice)\n"
                "- Sarcasm or dark humor about the world (not directed at self)\n\n"
                f"Conversation:\n{formatted}\n\n"
                'Output ONLY: {{"score": 0.0, "spiral": false, "reason": "2-5 words"}}\n'
                "score: 0.0=neutral, 0.4=concerning, 0.5=self-deprecation/entrenchment, "
                "0.7=active spiral, 0.9=crisis"
            )

            result_text = ""
            user_count = sum(1 for _, name, _ in messages if name == display_name)
            print(f"[SEMANTIC] Classifying {len(messages)} messages ({user_count} from {display_name})...")

            # Prefill with "{" to force JSON output (prevents Haiku from responding conversationally)
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model=CONFIG.model_classifier,
                max_tokens=64,
                messages=[
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": "{"}
                ]
            )

            # Track Haiku costs
            if hasattr(response, 'usage'):
                self.haiku_input_tokens += response.usage.input_tokens
                self.haiku_output_tokens += response.usage.output_tokens
                self.total_input_tokens += response.usage.input_tokens
                self.total_output_tokens += response.usage.output_tokens
                self.api_calls += 1

            # Parse the JSON response
            # Reconstruct full JSON: prepend the "{" prefill to Haiku's continuation
            result_text = "{"
            for block in response.content:
                if hasattr(block, 'text'):
                    result_text += block.text

            result_text = result_text.strip()
            # Handle markdown code fences if Haiku wraps the JSON
            if "```" in result_text:
                result_text = result_text.split("```")[0].strip()
            # Fix Haiku sometimes doubling the prefill brace: {{"score":...}}
            if result_text.startswith('{{') and result_text.endswith('}}'):
                result_text = result_text[1:-1]

            result = json.loads(result_text)
            score = float(result.get("score", 0.0))
            is_spiral = bool(result.get("spiral", False))
            reason = str(result.get("reason", ""))

            print(f"[SEMANTIC] Result: score={score:.1f}, spiral={is_spiral}, reason=\"{reason}\"")

            if score >= CONFIG.semantic_distress_threshold:
                self.semantic_triggered[user_id] = (score, is_spiral, reason, datetime.now())
                print(f"[SEMANTIC] Flagged for proactive response on next message")

        except json.JSONDecodeError as e:
            print(f"[SEMANTIC] JSON parse error: {e} - raw: {result_text[:100]}")
        except Exception as e:
            print(f"[SEMANTIC] Classifier error: {e}")
        finally:
            self.semantic_pending[user_id] = False

    # ------------------------------------------------------------------
    # Game log reader tool — lets Pandabot grep paired console+discord logs
    # when asked about a specific game's events / bugs / why-things-happened.
    # Without this, the model has to guess from the static board snapshot
    # in `_get_game_context_for_chat`, which leads to confident-but-wrong
    # diagnoses (e.g. snow-deck May 4 review claimed "62 issues = color
    # sweep" when the logs clearly show it's an MDFC color-identity bug).
    # ------------------------------------------------------------------

    GAME_LOG_TOOL_SCHEMA = {
        "name": "read_game_log",
        "description": (
            "Read or grep the console/discord log files of a specific past MTG game. "
            "Use this when asked about events from a specific game (bugs, why-it-happened, "
            "fact-checking, post-game analysis). The console log contains every internal "
            "event (casts, ETB triggers, combat damage, AI reasoning, error messages, "
            "color-identity blocks, deck validation). The discord log contains only the "
            "messages players saw. Always prefer grepping with a specific `pattern` over "
            "fetching raw lines — full logs are 50-400KB each. Examples of useful patterns: "
            "`Jorn|command zone` for commander-cast questions, `COLOR-IDENTITY|DECK-VALIDATE` "
            "for legality issues, `TARGETING.*fizzle|wrong type` for targeting bugs, "
            "`COMBAT-DAMAGE.*Jorn` for combat events, `STACK-AI decide_response` for "
            "interaction decisions. Returns matching lines with line numbers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "game_id": {
                    "type": "string",
                    "description": (
                        "The game's Discord snowflake ID (e.g. '1500509008510062743') "
                        "OR 'recent' for the most recent game in this channel "
                        "OR 'last' for the last completed game in this channel. "
                        "If the user said 'game N' (e.g. 'game 113'), N is the autoplay "
                        "batch index — pass it as `batch_index` instead of game_id."
                    ),
                },
                "batch_index": {
                    "type": "integer",
                    "description": (
                        "Autoplay batch index (the 'game N' the user mentioned). "
                        "Will resolve to the matching game ID. Mutually exclusive with game_id."
                    ),
                },
                "log_type": {
                    "type": "string",
                    "enum": ["console", "discord", "both"],
                    "description": "Which log file to read. 'both' returns excerpts from each.",
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        "Optional regex pattern to grep for (case-insensitive, ripgrep syntax). "
                        "Omit to return the first 50 + last 50 lines (game start + end summary)."
                    ),
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Max matching lines to return. Default 80. Capped at 200.",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Lines of surrounding context per match (like grep -C). Default 0. Capped at 5.",
                },
            },
            "required": ["log_type"],
        },
    }

    async def _resolve_game_id(self, channel_id: int, game_id: str = None,
                                batch_index: int = None) -> Optional[str]:
        """Map a user-friendly game reference (snowflake / 'recent' / batch index)
        to a concrete Discord snowflake game ID. Returns None if not resolvable."""
        if game_id and game_id.isdigit() and len(game_id) >= 18:
            return game_id  # Already a snowflake

        # Resolve via the MTG cog if available
        mtg_cog = self.get_cog("MTG Game")
        engine = getattr(mtg_cog, 'engine', None) if mtg_cog else None

        if game_id in ("recent", "last", "current") or game_id is None:
            # Active game in this channel first, then most recent ended
            if engine:
                g = engine.games.get(channel_id)
                if g and getattr(g, 'thread_id', None):
                    return str(g.thread_id)
                ended = getattr(engine, 'ended_games', {})
                g = ended.get(channel_id)
                if g and getattr(g, 'thread_id', None):
                    return str(g.thread_id)
            # Fall through: pick most recent log in logs/ directory
            try:
                logs_dir = "logs"
                if os.path.isdir(logs_dir):
                    consoles = [f for f in os.listdir(logs_dir)
                                if f.startswith('game_') and f.endswith('_console.log')]
                    if consoles:
                        # Snowflake IDs are time-ordered; sort and take last
                        consoles.sort()
                        latest = consoles[-1]
                        # Parse: game_<snowflake>_console.log
                        return latest.split('_')[1]
            except Exception:
                pass
            return None

        if batch_index is not None and batch_index >= 0:
            # Map batch index → snowflake. The autoplay-batch logger emits
            # `[BATCH] Game N of M: <game_id>` lines somewhere; if we don't
            # have an in-memory map, fall back to chronological position
            # within today's logs.
            try:
                logs_dir = "logs"
                if os.path.isdir(logs_dir):
                    consoles = sorted(f for f in os.listdir(logs_dir)
                                       if f.startswith('game_') and f.endswith('_console.log'))
                    # 1-indexed (game 1 = first), so adjust
                    idx = batch_index - 1 if batch_index >= 1 else batch_index
                    if 0 <= idx < len(consoles):
                        return consoles[idx].split('_')[1]
            except Exception:
                pass

        return None

    async def _read_game_log_tool(self, channel_id: int, tool_input: dict) -> str:
        """Handler for the read_game_log tool. Returns formatted log excerpts
        suitable for inclusion as a tool_result content block."""
        import re
        log_type = tool_input.get('log_type', 'console')
        pattern = tool_input.get('pattern', '').strip() or None
        max_lines = min(int(tool_input.get('max_lines') or 80), 200)
        context_lines = min(int(tool_input.get('context_lines') or 0), 5)

        snowflake = await self._resolve_game_id(
            channel_id,
            game_id=tool_input.get('game_id'),
            batch_index=tool_input.get('batch_index'),
        )
        if not snowflake:
            return ("ERROR: couldn't resolve a game ID. Pass a Discord snowflake "
                    "(e.g. '1500509008510062743'), or 'recent', or a batch_index.")

        log_paths = []
        logs_dir = "logs"
        if log_type in ("console", "both"):
            log_paths.append(("console", os.path.join(logs_dir, f"game_{snowflake}_console.log")))
        if log_type in ("discord", "both"):
            log_paths.append(("discord", os.path.join(logs_dir, f"game_{snowflake}_discord.log")))

        out_blocks = []
        for kind, path in log_paths:
            if not os.path.exists(path):
                out_blocks.append(f"=== {kind} log: NOT FOUND at {path} ===")
                continue
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
            except Exception as e:
                out_blocks.append(f"=== {kind} log: read error: {e} ===")
                continue

            total = len(lines)
            if pattern:
                try:
                    rx = re.compile(pattern, re.IGNORECASE)
                except re.error as e:
                    out_blocks.append(f"=== {kind}: bad regex `{pattern}` — {e} ===")
                    continue
                matched = []
                seen_idx = set()
                for i, line in enumerate(lines):
                    if rx.search(line):
                        # Include context_lines before/after
                        for j in range(max(0, i - context_lines),
                                       min(total, i + context_lines + 1)):
                            if j not in seen_idx:
                                seen_idx.add(j)
                                matched.append((j + 1, lines[j].rstrip('\n')))
                        if len(matched) >= max_lines:
                            break
                matched.sort(key=lambda x: x[0])
                truncated = len(matched) >= max_lines
                body = "\n".join(f"{n:>5}: {t}" for n, t in matched[:max_lines])
                header = (f"=== {kind} log (game {snowflake}, {total} total lines, "
                          f"pattern=`{pattern}`, {len(matched)} matches"
                          f"{', truncated' if truncated else ''}) ===")
                out_blocks.append(f"{header}\n{body if body else '(no matches)'}")
            else:
                # No pattern: head 50 + tail 50
                head = lines[:50]
                tail = lines[-50:] if total > 100 else []
                body_head = "\n".join(f"{i+1:>5}: {l.rstrip()}" for i, l in enumerate(head))
                body_tail = ""
                if tail:
                    start = total - len(tail)
                    body_tail = "\n... [middle elided] ...\n" + "\n".join(
                        f"{start + i + 1:>5}: {l.rstrip()}" for i, l in enumerate(tail)
                    )
                header = f"=== {kind} log (game {snowflake}, {total} total lines, head+tail) ==="
                out_blocks.append(f"{header}\n{body_head}{body_tail}")

        return "\n\n".join(out_blocks) if out_blocks else "(no logs returned)"

    def _get_game_context_for_chat(self, channel_id: int) -> Optional[str]:
        """Get MTG game context for chat responses in game threads.

        May 2 audit: the bot was hallucinating about game events because the
        snapshot only included the FINAL board state — no combat history, no
        deck identity, only last-5 graveyard cards. Now also includes:
            - Each player's commander(s) (so Sythis vs Meren is visible)
            - Each player's deck name when known (autoplay sets this)
            - Final winner + how the game ended
            - Full graveyard contents (capped at 30 cards/player)
            - Command zone contents (for revealing if a commander was killed)
            - Game format + turn count
        """
        mtg_cog = self.get_cog("MTG Game")
        if not mtg_cog or not hasattr(mtg_cog, 'engine'):
            print(f"[GAME_CONTEXT] No MTG cog or engine")
            return None

        # Check for active game
        game = mtg_cog.engine.games.get(channel_id)
        print(f"[GAME_CONTEXT] channel_id={channel_id}, active_games={list(mtg_cog.engine.games.keys())}")

        # Also check for recently ended game
        if not game and hasattr(mtg_cog.engine, 'ended_games'):
            game = mtg_cog.engine.ended_games.get(channel_id)
            print(f"[GAME_CONTEXT] ended_games={list(mtg_cog.engine.ended_games.keys())}")

        if not game:
            print(f"[GAME_CONTEXT] No game found for channel {channel_id}")
            return None

        print(f"[GAME_CONTEXT] Found game! ended={game.ended}, turn={game.turn_number}")

        # Build game state description
        lines = []

        if game.ended:
            if game.winner is not None:
                winner_name = game.players[game.winner].name
                loser_name = game.players[1 - game.winner].name
                loser_life = game.players[1 - game.winner].life
                lines.append(f"**Game ended on turn {game.turn_number}** — {winner_name} won.")
                lines.append(f"({loser_name} lost at {loser_life} life — "
                             f"{'mass damage' if loser_life <= -10 else 'attrition or commander damage'})")
            else:
                reason = getattr(game, 'loss_reason', '') or 'unknown'
                lines.append(f"**Game ended on turn {game.turn_number}** — draw. ({reason})")
        else:
            lines.append(f"**Active Game** - Turn {game.turn_number}, {game.active_player.name}'s turn, phase: {game.phase.value}")
            # May 18 audit: surface channel-silence so the Q&A path can't
            # claim "no crashes detected" while the game has actually been
            # stalled for half an hour. _last_bot_message_time is stamped
            # by mtg.cog._autoplay_send on every successful Discord post.
            # Threshold matches the Discord conversation lag a human would
            # actually notice (~3 min — autoplay games normally produce
            # bot output every few seconds).
            try:
                import time as _time
                last_post = getattr(game, '_last_bot_message_time', None)
                if last_post is not None:
                    silence_s = _time.time() - last_post
                    if silence_s > 180:  # 3 minutes
                        mins = int(silence_s // 60)
                        lines.append(
                            f"⚠️ **CHANNEL STALLED** — last bot message was "
                            f"{mins} min ago. Autoplay is "
                            f"{'active' if getattr(game, 'is_autoplay', False) else 'inactive'} "
                            f"but no progress is being made. If the user asks "
                            f"\"what happened?\" or \"is the game stuck?\", DO NOT "
                            f"claim the game is healthy — flag the stall and "
                            f"suggest `!autoplay-stop`."
                        )
            except Exception as _stall_err:
                print(f"[GAME_CONTEXT] stall-check failed: {_stall_err}")

        lines.append(f"Format: {game.format}")
        # Deck names if autoplay set them (graveyard, sagas, surrak, etc.)
        deck_names = []
        for i, p in enumerate(game.players):
            dn = getattr(p, '_deck_name', None) or getattr(game, f'_deck{i}_name', None)
            if dn:
                deck_names.append(f"{p.name}={dn}")
        if deck_names:
            lines.append(f"Decks: {', '.join(deck_names)}")

        for i, player in enumerate(game.players):
            player_marker = "🤖" if player.is_claude else "👤"
            lines.append(f"\n{player_marker} **{player.name}**: {player.life} life, {player.poison} poison")
            lines.append(f"   Hand: {len(player.hand)} cards, Library: {len(player.library)} cards, "
                         f"Graveyard: {len(player.graveyard)} cards")

            # Commander(s) — critical for "what deck did I play" recall.
            cmdrs = [c.name for c in getattr(player, 'command_zone', []) or []]
            if cmdrs:
                lines.append(f"   Commander(s) in command zone: {', '.join(cmdrs)}")

            if player.battlefield:
                lands = [c.name for c in player.lands()]
                creatures = [f"{c.name} ({c.power}/{c.toughness})" for c in player.creatures()]
                other = [c.name for c in player.battlefield if not c.is_land() and not c.is_creature()]

                if lands:
                    lines.append(f"   Lands ({len(lands)}): {', '.join(lands[:10])}{'...' if len(lands) > 10 else ''}")
                if creatures:
                    lines.append(f"   Creatures ({len(creatures)}): {', '.join(creatures[:8])}{'...' if len(creatures) > 8 else ''}")
                if other:
                    lines.append(f"   Other permanents ({len(other)}): {', '.join(other[:8])}{'...' if len(other) > 8 else ''}")

            if player.graveyard:
                # Show full graveyard (capped at 30) so chat can recall key cards
                # like Sythis-killed-by-Toxic-Deluge that died early.
                gy_names = [c.name for c in player.graveyard]
                gy_display = gy_names[:30]
                more = f" + {len(gy_names) - 30} more" if len(gy_names) > 30 else ""
                lines.append(f"   Graveyard contents: {', '.join(gy_display)}{more}")

            if getattr(player, 'exile', None):
                ex_names = [c.name for c in player.exile][:15]
                if ex_names:
                    lines.append(f"   Exile: {', '.join(ex_names)}")

        return "\n".join(lines)
    
    def build_system_prompt(self, user: discord.User, distress_level: str = "none", environment: str = None, game_context: str = None, message: discord.Message = None) -> str:
        """
        Build context-aware system prompt.

        Args:
            user: Discord user
            distress_level: "none", "stressed", or "spiral"
            environment: Optional time/weather context string
            game_context: Optional MTG game state description
            message: Optional original Discord message (lets us detect
                PluralKit proxies and inject plurality awareness)
        """
        # Choose base prompt based on distress level. Built from the active
        # persona (personas/<bot_persona>.json) plus built-in capability /
        # distress-response text at runtime.
        if distress_level == "spiral":
            prompt_parts = [self.build_spiral_prompt()]
        elif distress_level == "stressed":
            prompt_parts = [self.build_support_prompt()]
        else:
            prompt_parts = [self.build_base_prompt()]

        # Add environment context (time/weather if configured).
        if environment:
            prompt_parts.append(f"\n--- Current Environment ---\n{environment}\nUse this to make your emotes and descriptions accurate to actual conditions.")
        
        # Add MTG game context if in a game thread
        if game_context:
            prompt_parts.append(f"\n--- MTG Game Context ---\nYou're in an MTG game thread. Here's the current/recent game state:\n{game_context}\nYou can comment on the game, offer strategy advice, or discuss plays. Use your MTG knowledge!")
        
        # Add memory system instructions (only for non-spiral states)
        if distress_level != "spiral":
            prompt_parts.append("""
## Memory System - IMPORTANT

You have a working memory system! Use [note: key: value] tags to remember important things.
The tags are automatically stripped from your visible response, so they don't clutter the chat.

**BEFORE finishing your response, scan what the user just said and check: did they mention anything worth a note?** This is a habit, not an exception — most conversational turns deserve at least one note. Err toward writing notes; the cost of a missed note is much higher than a redundant one.

**ACTIVELY USE NOTES when you hear about:**
- Projects/work they're doing → [note: current_project: description]
- Frustrations or challenges → [note: challenge: what they're struggling with]
- Important people mentioned → [note: person_name: relationship/context]
- Preferences or opinions → [note: preference_topic: what they like/dislike]
- Future plans or deadlines → [note: plan_or_event: details]
- Emotional state or mood → [note: mood: how they seem to be feeling]
- Facts they share about themselves, their work, their family, their interests
- New information that updates or corrects an existing memory

**Example in practice:**
User: "I'm so frustrated with my job search. Been applying to think tanks but keep getting ghosted."
Your response should include: [note: job_search: frustrated with think tank applications, getting ghosted]

**Format reminder:** lowercase `note:` exactly (not `[Note:` or `[memory:`), single colon between key and value, square brackets enclose the whole thing. Place tags anywhere in your reply — they get stripped.

Notes fade after ~48 hours unless referenced, so jot down anything that seems important for continuity!
""")
        
        # Add two-tier memory context
        memory = self.get_memory(user.id)
        memory_context = memory.get_context_string()
        if memory_context:
            prompt_parts.append(f"\n--- Remembered Context ---\n{memory_context}")
        
        # Add personal context if available (from memories/*.json files)
        user_context = self.get_user_context(user)
        if user_context:
            prompt_parts.append(f"\n--- Personal Context for {user.display_name} ---\n{user_context}")

        # === Plurality awareness (PluralKit proxies) ===
        # When a message comes through PluralKit, message.webhook_id is set
        # and the author's display name is the alter's name (not the system
        # member's Discord account). Inject context so the model treats
        # alters of the same system as conversation-continuous and uses the
        # alter's current display name when addressing them.
        if message is not None and message.webhook_id is not None:
            alter_name = user.display_name
            # known_plural_systems maps alter (display name) → system summary.
            # Config in config.json under "plural_systems": {"alex": "part of
            # a plural system", ...}. Falls back to a generic note if unmapped.
            known_systems = getattr(self, 'known_plural_systems', {}) or {}
            system_note = known_systems.get(alter_name.lower())
            if system_note:
                prompt_parts.append(
                    f"\n--- Plurality Context ---\n"
                    f"This message is from **{alter_name}**, an alter in {system_note}. "
                    f"Treat alters of the same system as the same conversation participant "
                    f"for memory continuity (they share long-term context), but address "
                    f"the alter currently speaking by their own name. Do not 'out' the "
                    f"system or comment on plurality unless they bring it up."
                )
            else:
                prompt_parts.append(
                    f"\n--- Plurality Context ---\n"
                    f"This message comes through a PluralKit webhook proxy — the speaker "
                    f"({alter_name}) is an alter in a plural system. Treat them as a real "
                    f"person; use their current display name; don't comment on plurality "
                    f"unless they raise it. If you've been talking with another alter from "
                    f"the same system in this thread, the conversation is continuous — "
                    f"they share memory and context."
                )

        return "\n".join(prompt_parts)

    # ----------------------------------------------------------------- #
    # YouTube transcription                                              #
    # ----------------------------------------------------------------- #
    def _get_youtube_transcriber(self):
        """Lazily build the YoutubeTranscriber on first access.

        Lazy because yt-dlp is an optional dependency — if it's not
        installed, we don't want startup to fail. We just degrade to
        "no 🎙️ reactions, no transcription support."
        """
        if self._yt_transcriber is not None:
            return self._yt_transcriber
        try:
            from youtube_transcribe import YoutubeTranscriber
            self._yt_transcriber = YoutubeTranscriber(
                transcripts_dir=Path("data") / "transcripts",
                allow_age_restricted=self.youtube_allow_age_restricted,
                max_duration_s=self.youtube_max_duration_s,
                whisper_model=self.youtube_whisper_model,
            )
            print(f"[YT-TRANSCRIBE] Ready — model={self.youtube_whisper_model}, "
                  f"max_duration={self.youtube_max_duration_s}s, "
                  f"age_restricted={self.youtube_allow_age_restricted}")
            return self._yt_transcriber
        except ImportError as e:
            # Cache None so we don't retry-import on every message
            print(f"[YT-TRANSCRIBE] Disabled (import failed: {e})")
            self._yt_transcriber = False  # sentinel: explicitly disabled
            return None
        except Exception as e:
            print(f"[YT-TRANSCRIBE] Init failed: {e}")
            self._yt_transcriber = False
            return None

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Trigger YouTube transcription when a user adds the 🎙️ reaction.

        May 30, 2026: uses the RAW reaction event (not on_reaction_add) so it
        fires even for messages the bot never cached — e.g. a video posted while
        the bot was OFFLINE. Add 🎙️ after the bot is back online and it
        transcribes retroactively. on_reaction_add only fires for messages in the
        bot's in-memory cache, which never includes anything from the downtime
        window, so the old handler silently ignored offline-period videos.
        """
        # Ignore our own reactions (incl. the 🎙️ the bot auto-adds in on_message).
        if payload.user_id == self.user.id:
            return
        # Only our 🎙️ — ignore everything else.
        if str(payload.emoji) != "🎙️":
            return
        transcriber = self._get_youtube_transcriber()
        if transcriber is None:
            return
        # The raw payload carries only IDs; fetch the message (it may be
        # uncached — that's the whole point — so this can hit the Discord API).
        channel = self.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(payload.channel_id)
            except discord.HTTPException as e:
                print(f"[YT-TRANSCRIBE] Couldn't fetch channel {payload.channel_id}: {e}")
                return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException as e:
            print(f"[YT-TRANSCRIBE] Couldn't fetch message {payload.message_id}: {e}")
            return
        # The reaction must be ON a message that contains a YouTube URL. This
        # filters out spurious 🎙️ reactions in unrelated conversations.
        video_id = transcriber.extract_video_id(message.content)
        if not video_id:
            return
        # Dedup: if a transcription job has already started for this message,
        # ignore. (Two users clicking 🎙️ simultaneously, etc.)
        if message.id in self._yt_jobs_started:
            return
        self._yt_jobs_started.add(message.id)

        # Cache hit — just post the existing transcript.
        existing = transcriber.existing_transcript_path(video_id)
        if existing is not None:
            try:
                await message.channel.send(
                    content=f"📜 Transcript already on file for `{video_id}`.",
                    file=discord.File(str(existing)),
                    reference=message,
                )
            except discord.HTTPException as e:
                print(f"[YT-TRANSCRIBE] Failed to post cached transcript: {e}")
            return

        # Fresh transcription. Post a thinking message and edit it as we go,
        # so spectators see progress instead of staring at a quiet channel.
        try:
            thinking = await message.channel.send(
                content=f"🎙️ Received your transcription request for `{video_id}`. Starting up…",
                reference=message,
            )
        except discord.HTTPException as e:
            print(f"[YT-TRANSCRIBE] Couldn't post thinking message: {e}")
            self._yt_jobs_started.discard(message.id)
            return

        async def progress(status: str):
            try:
                await thinking.edit(content=f"🎙️ {status}")
            except discord.HTTPException:
                # Edits can fail on Discord rate limits; ignore — the user
                # will still see the final result.
                pass

        try:
            transcript_path = await transcriber.transcribe(
                video_id, message.channel.id, on_progress=progress
            )
            # Final edit: replace the thinking text and attach the .md file.
            try:
                await thinking.edit(
                    content=f"✅ Transcript ready for `{video_id}`.",
                    attachments=[discord.File(str(transcript_path))],
                )
            except discord.HTTPException as e:
                # If editing-with-attachments fails (e.g. file too big), fall
                # back to a separate message with the file attached.
                print(f"[YT-TRANSCRIBE] Couldn't edit-attach, sending separately: {e}")
                await message.channel.send(
                    content=f"✅ Transcript ready for `{video_id}`.",
                    file=discord.File(str(transcript_path)),
                    reference=message,
                )
        except Exception as e:
            # Any user-facing transcription error becomes an edit to the
            # thinking message. We DON'T re-raise — Discord callbacks must
            # not propagate exceptions or discord.py will log a noisy traceback.
            err_msg = str(e) or e.__class__.__name__
            try:
                await thinking.edit(content=f"❌ Couldn't transcribe `{video_id}`: {err_msg}")
            except discord.HTTPException:
                pass
            # Allow re-trying later — pop the dedup entry so a second 🎙️
            # click triggers a new job.
            self._yt_jobs_started.discard(message.id)
            print(f"[YT-TRANSCRIBE] Job failed for {video_id}: {e}")

    async def on_message(self, message: discord.Message):
        """Handle incoming messages."""
        # Ignore own messages
        if message.author == self.user:
            return
        
        # Ignore DMs for now
        if not message.guild:
            return
        
        # Ignore system messages
        if message.type != discord.MessageType.default and message.type != discord.MessageType.reply:
            return
        
        # Skip excluded channels entirely (including commands!)
        channel_id = message.channel.id
        parent_id = getattr(message.channel, 'parent_id', None)
        if channel_id in self.excluded_channels or parent_id in self.excluded_channels:
            return
        
        # Process commands first
        ctx = await self.get_context(message)
        if ctx.valid:
            # This is a valid command - let the command handler deal with it
            await self.invoke(ctx)
            return
        
        # Not a command, continue with Claude response logic

        # May 24, 2026 — YouTube transcription hook. If the message contains a
        # YouTube URL, react with 🎙️ so any user can summon a transcript by
        # clicking the reaction. The actual transcription runs in
        # on_raw_reaction_add (raw so it also fires for messages the bot never
        # cached — e.g. videos posted while offline + 🎙️'d after reboot).
        # We do this BEFORE the MTG-channel check so 🎙️ also works in MTG threads.
        try:
            transcriber = self._get_youtube_transcriber()
        except Exception:
            transcriber = None
        if transcriber is not None:
            video_id = transcriber.extract_video_id(message.content)
            if video_id:
                try:
                    await message.add_reaction("🎙️")
                except discord.HTTPException as e:
                    print(f"[YT-TRANSCRIBE] Couldn't react to {message.id}: {e}")

        # Check if this is the MTG channel (or a thread in it) - respond to EVERY message there
        is_mtg_channel = (
            self.mtg_channel_id and
            (channel_id == self.mtg_channel_id or parent_id == self.mtg_channel_id)
        )
        
        # If someone typed what looks like a command (starts with !) in an MTG thread,
        # but it wasn't valid, don't respond with chat - they probably meant a game command
        if is_mtg_channel and message.content.strip().startswith('!'):
            # Check if this is a game thread by looking for an active game
            mtg_cog = self.get_cog("MTG Game")
            if mtg_cog and hasattr(mtg_cog, 'engine'):
                game = mtg_cog.engine.games.get(channel_id)
                if game:
                    # There's an active game and they typed !something invalid
                    # Give a helpful error instead of chat
                    cmd = message.content.strip().split()[0]
                    await message.channel.send(
                        f"❓ `{cmd}` isn't a game command. Try `!help` to see available commands.\n"
                        f"💡 Common commands: `!play`, `!hand`, `!state`, `!turn`, `!pass`"
                    )
                    return
        
        # Check if this message is from a monitored user (proactive-support list)
        is_monitored = message.author.id in self.monitored_users
        monitored_uid = message.author.id if is_monitored else None

        # Check for distress BEFORE deciding whether to respond
        # (needed to decide if we should proactively respond to a monitored user)
        text_content = message.content or ""
        is_distressed, distress_score, signals, is_spiral = self.distress_detector.is_distressed(
            text_content
        )

        # === Conversation buffer: captures full thread context for semantic classifier ===
        # Buffer messages from the monitored user AND from others in the same channel,
        # so Haiku can see reassurance-rejection patterns (friends comforting →
        # the monitored user arguing back = entrenchment signal).
        now = datetime.now()
        if text_content and self.monitored_users:
            author_name = message.author.display_name
            if is_monitored:
                # Monitored user's message: buffer it, update their active channel
                self.active_channels[monitored_uid] = channel_id
                # Use the configured display name from user_name_map when present
                buf_name = self.user_name_map.get(str(monitored_uid), author_name)
                self.message_buffers[monitored_uid].append((now, buf_name, text_content))
            else:
                # Someone else: buffer into ANY monitored user's active channel
                # that matches the current channel (so context flows naturally
                # in shared threads).
                for muid, active_ch in self.active_channels.items():
                    if active_ch == channel_id:
                        self.message_buffers[muid].append((now, author_name, text_content))

            # Trim to last 15 messages within 30-minute window for each user
            buffer_cutoff = now - timedelta(minutes=30)
            for muid in list(self.message_buffers.keys()):
                self.message_buffers[muid] = [
                    (t, name, txt) for t, name, txt in self.message_buffers[muid]
                    if t > buffer_cutoff
                ][-15:]

        # === Per-user: semantic classifier + sub-threshold accumulator ===
        monitored_accumulated = False
        monitored_semantic = False
        if is_monitored and text_content:

            # Check if a PREVIOUS background Haiku call flagged distress
            if self.semantic_triggered.get(monitored_uid) and not is_distressed:
                sem_score, sem_spiral, sem_reason, sem_when = self.semantic_triggered[monitored_uid]
                self.semantic_triggered[monitored_uid] = None
                # TTL: discard stale flags (e.g., flagged at 10 PM, consumed at 10 AM next day)
                sem_age_minutes = (now - sem_when).total_seconds() / 60
                if sem_age_minutes > 30:
                    print(f"[SEMANTIC] Discarding stale flag ({sem_age_minutes:.0f}m old): {sem_reason}")
                elif sem_score > distress_score:
                    # Merge: semantic score overrides if higher than keyword score
                    distress_score = sem_score
                    is_distressed = distress_score >= CONFIG.distress_threshold
                    is_spiral = sem_spiral or is_spiral
                    signals = [f"[SEMANTIC] {sem_reason}"]
                    monitored_semantic = True
                    print(f"[SEMANTIC] Proactive response triggered (score={sem_score:.1f}, reason=\"{sem_reason}\")")

            # Cross-message accumulation: sub-threshold keyword scores build up
            if distress_score > 0:
                self.score_accumulators[monitored_uid].append((now, distress_score))
                # Clean entries older than 15 minutes
                acc_cutoff = now - timedelta(minutes=15)
                self.score_accumulators[monitored_uid] = [
                    (t, s) for t, s in self.score_accumulators[monitored_uid] if t > acc_cutoff
                ]
                # Sum of recent scores, triggers at 1.0 total
                accumulated_total = sum(s for _, s in self.score_accumulators[monitored_uid])
                if accumulated_total >= 1.0 and not is_distressed:
                    monitored_accumulated = True
                    user_label = self.user_name_map.get(str(monitored_uid), str(monitored_uid))
                    print(f"💭 [ACCUMULATOR] {user_label} sub-threshold accumulation triggered "
                          f"(total: {accumulated_total:.2f} from {len(self.score_accumulators[monitored_uid])} messages)")

            # Fire background Haiku classifier when keywords found nothing
            # but we have enough messages in the buffer to analyze context
            if (distress_score == 0
                    and not monitored_accumulated
                    and not self.semantic_pending.get(monitored_uid, False)
                    and len(self.message_buffers.get(monitored_uid, [])) >= 1):
                asyncio.create_task(self._classify_distress(monitored_uid))

        # Determine if we should respond
        # Priority order:
        # 1. MTG channel: always respond
        # 2. Bot mentioned: always respond
        # 3. Thread owned by bot: always respond
        # 4. A monitored user is distressed (keyword, accumulator, or semantic): proactively respond
        # 5. Otherwise: don't respond

        is_mentioned = self.user.mentioned_in(message)
        is_bot_thread = (
            isinstance(message.channel, discord.Thread) and
            message.channel.owner_id == self.user.id
        )
        monitored_needs_support = is_monitored and (
            is_distressed or is_spiral or monitored_accumulated or monitored_semantic
        )

        should_respond = (
            is_mtg_channel or
            is_mentioned or
            is_bot_thread or
            monitored_needs_support
        )

        if not should_respond:
            return

        # === PluralKit proxy dedup ===
        # PluralKit listens for messages matching a system's proxy tags (like
        # "[message" or "alex|message"), DELETES the original, and POSTs a
        # webhook proxy in its place. on_message fires twice — once for the
        # original (which is about to vanish) and once for the webhook proxy.
        # If we respond to both, the user gets two responses. Strategy:
        #   - If the message is a webhook proxy (webhook_id is set), respond
        #     immediately — this is the canonical version that will persist.
        #   - If it's an original user message, wait ~1.2s for PluralKit to
        #     do its delete-and-replace, then try to refetch. If the message
        #     vanished, it was proxied — bail out so the proxy event handles
        #     the response. If still present, this was a normal (non-proxied)
        #     message and we respond.
        # The 1.2s budget covers typical PluralKit roundtrip (~600-900ms);
        # adjust if a future PK release changes its latency profile.
        if message.webhook_id is None:
            await asyncio.sleep(1.2)
            try:
                await message.channel.fetch_message(message.id)
            except discord.NotFound:
                print(f"🪞 [PLURALKIT] {message.author.display_name}'s message "
                      f"({message.id}) was deleted within 1.2s — assuming PK "
                      f"proxy; bailing so the webhook event handles the response")
                return
            except discord.Forbidden:
                # Can't refetch — proceed conservatively (respond once is
                # better than silently dropping).
                pass
            except Exception as e:
                print(f"🪞 [PLURALKIT] Refetch errored ({type(e).__name__}: {e}) "
                      f"— proceeding without dedup")

        # If accumulated triggered, boost the score so support level is appropriate
        if monitored_accumulated and distress_score < CONFIG.distress_threshold:
            distress_score = CONFIG.distress_threshold
            is_distressed = True
            signals = signals or ["accumulated sub-threshold distress"]

        # Log why we're responding (for debugging)
        if monitored_needs_support and not is_mentioned:
            user_label = self.user_name_map.get(str(monitored_uid), str(monitored_uid))
            print(f"💭 Proactively responding to {user_label} (distress: {distress_score:.2f})")
            # Clear accumulator and buffer for THIS user after proactive response
            if monitored_uid in self.score_accumulators:
                self.score_accumulators[monitored_uid].clear()
            if monitored_uid in self.message_buffers:
                self.message_buffers[monitored_uid].clear()
        
        # Build message content (text + attachments)
        # When mentioned, pull recent channel history so the bot can answer
        # "what do you think of that?" without needing the user to paste context.
        content_parts = await self._process_message_content(
            message, include_history=is_mentioned
        )
        if not content_parts:
            return
        
        # Determine support level with step-down logic
        thread_id = message.channel.id
        distress_level, offer_comfort, reason = self.determine_support_level(
            thread_id, distress_score, is_spiral
        )
        
        # Select model based on distress level
        if distress_level in ["spiral", "stressed"]:
            model = CONFIG.model_support
            max_tokens = CONFIG.max_tokens_support
            if distress_level == "spiral":
                print(f"🆘 SPIRAL mode (score: {distress_score:.2f}) - {reason}")
            else:
                print(f"😟 STRESSED mode (score: {distress_score:.2f}) - {reason}")
            if signals:
                print(f"   Signals: {signals}")
        else:
            model = CONFIG.model_default
            max_tokens = CONFIG.max_tokens
            if distress_score > 0:
                print(f"✅ Normal mode despite score {distress_score:.2f} - {reason}")
        
        # Build conversation context
        # Simplify if just text
        if len(content_parts) == 1 and content_parts[0]["type"] == "text":
            self.conversations[thread_id].append({
                "role": "user",
                "content": content_parts[0]["text"]
            })
        else:
            self.conversations[thread_id].append({
                "role": "user",
                "content": content_parts
            })
        
        # Trim old messages
        if len(self.conversations[thread_id]) > CONFIG.max_messages_per_thread:
            self.conversations[thread_id] = self.conversations[thread_id][-CONFIG.max_messages_per_thread:]
        
        # Fetch environment context (time/weather) - runs async, cached
        environment = await self.get_environment_context()
        
        # Get MTG game context if we're in an MTG thread
        game_context = None
        if is_mtg_channel:
            game_context = self._get_game_context_for_chat(channel_id)
        
        # Generate response
        async with message.channel.typing():
            try:
                # Run in thread pool to avoid blocking Discord's event loop
                # Tool list:
                #   - web_search (server-side, Anthropic-managed) — fetches web content
                #   - read_game_log (client-side, ours) — greps paired console+discord
                #     logs for any past MTG game so the bot can fact-check itself
                #     when asked about specific game events / bugs.
                tool_list = []
                if CONFIG.web_search_enabled:
                    tool_list.append({"type": "web_search_20250305", "name": "web_search"})
                # Game-log tool only when we're in an MTG-aware context
                if is_mtg_channel:
                    tool_list.append(self.GAME_LOG_TOOL_SCHEMA)

                response = await asyncio.to_thread(
                    self.claude.messages.create,
                    model=model,
                    max_tokens=max_tokens,
                    system=self.build_system_prompt(message.author, distress_level, environment, game_context, message=message),
                    messages=self.conversations[thread_id],
                    tools=tool_list
                )

                # Handle tool use loop — supports both server-side (web_search)
                # and client-side (read_game_log) tools.
                reply_text = ""
                urls_found = []

                while response.stop_reason == "tool_use":
                    # Collect any text so far
                    for block in response.content:
                        if hasattr(block, 'text'):
                            reply_text += block.text

                    # Extract tool use blocks
                    tool_uses = [block for block in response.content if block.type == "tool_use"]

                    # Process tool results — different tools return different content
                    tool_results = []
                    for tool_use in tool_uses:
                        if tool_use.name == "read_game_log":
                            try:
                                content = await self._read_game_log_tool(channel_id, tool_use.input)
                            except Exception as e:
                                content = f"ERROR running read_game_log: {e}"
                            # Cap content size — Discord is fine but the API
                            # bills these tokens. 12KB ≈ 3000 tokens which is
                            # plenty for a focused grep result.
                            if len(content) > 12000:
                                content = content[:12000] + "\n... [output truncated to 12KB]"
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_use.id,
                                "content": content,
                            })
                        else:
                            # Server-side tools (web_search) are completed by
                            # the API itself; we just acknowledge.
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_use.id,
                                "content": "Search completed"
                            })

                    # Track usage from this call
                    if hasattr(response, 'usage'):
                        self.total_input_tokens += response.usage.input_tokens
                        self.total_output_tokens += response.usage.output_tokens
                        self.api_calls += 1

                    # Continue conversation with tool results
                    response = await asyncio.to_thread(
                        self.claude.messages.create,
                        model=model,
                        max_tokens=max_tokens,
                        messages=[
                            {"role": "assistant", "content": response.content},
                            {"role": "user", "content": tool_results}
                        ],
                        tools=tool_list
                    )
                
                # Extract final text
                for block in response.content:
                    if hasattr(block, 'text'):
                        reply_text += block.text
                
                # Extract URLs from response for embed
                url_pattern = r'https?://[^\s\)\]<>\"\']+[^\s\.\,\)\]<>\"\':]'
                urls_found = list(set(re.findall(url_pattern, reply_text)))
                
                reply = reply_text
                
                # Track token usage
                if hasattr(response, 'usage'):
                    self.total_input_tokens += response.usage.input_tokens
                    self.total_output_tokens += response.usage.output_tokens
                    self.api_calls += 1
                    if model == CONFIG.model_support:
                        self.opus_input_tokens += response.usage.input_tokens
                        self.opus_output_tokens += response.usage.output_tokens
                    else:
                        self.sonnet_input_tokens += response.usage.input_tokens
                        self.sonnet_output_tokens += response.usage.output_tokens
                    self._save_persistent_costs()
                
                # Handle empty response
                if not reply:
                    await message.channel.send("I received an empty response. Try again?")
                    return
                
                # Extract and process working memory notes.
                # May 14 audit: user reported notes being recorded less often
                # lately. Make the matcher case-insensitive (Claude sometimes
                # emits `[Note: ...]` or `[NOTE: ...]`) and accept the common
                # variants `[note ...]`, `[memory: ...]`, `[remember: ...]`
                # so a slight formatting drift doesn't drop the capture.
                note_pattern = re.compile(
                    r'\[(?:note|memory|remember)\s*[:|]\s*([^:|]+)\s*[:|]\s*([^\]]+)\]',
                    re.IGNORECASE,
                )
                memory = self.get_memory(message.author.id)
                notes_added = []
                for match in note_pattern.finditer(reply):
                    key = match.group(1).strip()
                    value = match.group(2).strip()
                    memory.working.add(key, value)
                    notes_added.append(key)

                # Remove note markers from visible response
                reply = note_pattern.sub('', reply)
                
                # Strip timestamps Claude may have echoed back (e.g. "[Mar 22, 2:15 AM]")
                # These are for Claude's context only — Discord shows its own timestamps
                reply = re.sub(r'\[(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s*\d{1,2}:\d{2}\s*(?:AM|PM)\]\s*', '', reply)

                # Clean up any double spaces or weird formatting from removed tags
                reply = re.sub(r'\n\s*\n\s*\n', '\n\n', reply)
                reply = re.sub(r'  +', ' ', reply).strip()
                
                # Save memories if any notes were added
                if notes_added:
                    await self.save_memories_async()
                
                # Store assistant response (cleaned version, with timestamp)
                ts = _format_msg_timestamp(datetime.now(timezone.utc))
                self.conversations[thread_id].append({
                    "role": "assistant",
                    "content": f"{ts} {reply}"
                })
                
                # Extract code files if any
                reply, files = self._extract_code_files(reply)
                
                # Send reply (handle Discord character limit)
                await self._send_response(message.channel, reply, files)
                
                # Send source URL embed if any URLs were found from web search
                if urls_found:
                    embed = discord.Embed(
                        title="🔍 Sources",
                        color=discord.Color.blue()
                    )
                    for i, url in enumerate(urls_found[:CONFIG.max_search_results_in_embed], 1):
                        display_url = url[:60] + "..." if len(url) > 60 else url
                        embed.add_field(
                            name=f"Source {i}",
                            value=f"[{display_url}]({url})",
                            inline=False
                        )
                    await message.channel.send(embed=embed)
                
                # If distress is high enough, also offer comfort content
                # Uses the same image pipeline as !panda — actual pictures, not web search text walls
                if offer_comfort:
                    await asyncio.sleep(1)  # Brief pause before comfort content
                    image_url, fact, source = await self.web_search.fetch_red_panda_image()
                    if image_url:
                        embed = discord.Embed(
                            title="\U0001f43c Here's something cute",
                            color=discord.Color.orange()
                        )
                        embed.set_image(url=image_url)
                        if fact:
                            embed.description = f"*{fact}*"
                        intro = random.choice([
                            "*perks up* Oh! Here, look at this:",
                            "*nudges you gently* Hey, look:",
                            "*soft chirp* Found a friend:",
                            "*tail swishes* Here, this might help:",
                        ])
                        await message.channel.send(intro, embed=embed)
                    else:
                        # All image APIs failed — send a text fact instead of nothing
                        await message.channel.send(
                            "*nudges you gently* Red pandas wrap their fluffy tails around themselves like blankets to stay warm! \U0001f43c"
                        )
                
            except anthropic.APIError as e:
                print(f"API Error: {e}")
                await message.channel.send(f"❌ API Error: {e}")
            except Exception as e:
                print(f"Error generating response: {e}")
                await message.channel.send(
                    "Sorry, I had trouble generating a response. Try again?"
                )
    
    async def _process_message_content(
        self,
        message: discord.Message,
        *,
        include_history: bool = False,
    ) -> List[Dict]:
        """Process message text and attachments into API format."""
        content_parts = []

        # If the bot was mentioned, fetch recent channel history. Bystander
        # messages (no mention, not a monitored user, not bot thread, not MTG channel)
        # never enter self.conversations[thread_id], so without this pull the
        # bot can't see "the previous two messages" the user is asking about.
        if include_history:
            try:
                history_msgs = []
                async for hist_msg in message.channel.history(limit=10, before=message):
                    if hist_msg.type not in (
                        discord.MessageType.default,
                        discord.MessageType.reply,
                    ):
                        continue
                    history_msgs.append(hist_msg)
                history_msgs.reverse()  # chronological: oldest first

                history_lines = []
                for hist_msg in history_msgs:
                    line_parts = []
                    if hist_msg.content:
                        line_parts.append(hist_msg.content[:300])
                    for embed in hist_msg.embeds[:1]:
                        if embed.description:
                            line_parts.append(f"[Embed: {embed.description[:200]}]")
                        elif embed.title:
                            line_parts.append(f"[Embed: {embed.title}]")
                    if not line_parts:
                        continue
                    hist_ts = _format_msg_timestamp(hist_msg.created_at)
                    history_lines.append(
                        f"{hist_ts} {hist_msg.author.display_name}: {' '.join(line_parts)}"
                    )

                if history_lines:
                    content_parts.append({
                        "type": "text",
                        "text": "[Recent channel context]\n" + "\n".join(history_lines)
                    })
            except Exception as e:
                print(f"Could not fetch channel history: {e}")

        # If this is a reply, include the original message for context
        if message.reference and message.reference.message_id:
            try:
                # Try to get the referenced message
                ref_msg = message.reference.resolved
                if ref_msg is None:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                
                if ref_msg:
                    # Build context from both content and embeds
                    ref_parts = []
                    if ref_msg.content:
                        ref_parts.append(ref_msg.content[:500])
                    
                    # Include embed descriptions (important for bot responses with search results)
                    for embed in ref_msg.embeds[:3]:  # Limit to first 3 embeds
                        if embed.description:
                            ref_parts.append(f"[Embed: {embed.description[:300]}]")
                        elif embed.title:
                            ref_parts.append(f"[Embed: {embed.title}]")
                    
                    if ref_parts:
                        ref_text = " ".join(ref_parts)
                        if len(ref_text) > 800:
                            ref_text = ref_text[:800] + "..."
                        content_parts.append({
                            "type": "text",
                            "text": f"[Replying to {ref_msg.author.display_name}: \"{ref_text}\"]"
                        })
            except Exception as e:
                # If we can't fetch the reference, just continue without it
                print(f"Could not fetch referenced message: {e}")
        
        # Add text if present
        if message.content:
            # Remove bot mention from content
            text = message.content
            if self.user:
                text = text.replace(f'<@{self.user.id}>', '').replace(f'<@!{self.user.id}>', '').strip()
            if text:
                ts = _format_msg_timestamp(message.created_at)
                content_parts.append({
                    "type": "text",
                    "text": f"{ts} {message.author.display_name}: {text}"
                })
        
        # Process attachments
        for attachment in message.attachments:
            # Handle images
            if any(attachment.filename.lower().endswith(ext) for ext in CONFIG.image_types):
                if attachment.size <= CONFIG.max_image_size_mb * 1024 * 1024:
                    try:
                        image_data = await self._fetch_image_base64(attachment.url)
                        if image_data:
                            ext = attachment.filename.lower().split('.')[-1]
                            media_type = {
                                'png': 'image/png',
                                'jpg': 'image/jpeg',
                                'jpeg': 'image/jpeg',
                                'gif': 'image/gif',
                                'webp': 'image/webp'
                            }.get(ext, 'image/png')
                            
                            content_parts.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": image_data
                                }
                            })
                    except Exception as e:
                        content_parts.append({
                            "type": "text",
                            "text": f"[Image: {attachment.filename} - failed to load: {e}]"
                        })
            
            # Handle text files
            elif any(attachment.filename.lower().endswith(ext) for ext in CONFIG.text_file_types):
                if attachment.size <= 1024 * 1024:  # 1MB limit for text files
                    try:
                        file_content = await self._fetch_text_file(attachment.url)
                        if file_content:
                            content_parts.append({
                                "type": "text",
                                "text": f"\n--- File: {attachment.filename} ---\n{file_content}\n--- End of {attachment.filename} ---\n"
                            })
                    except Exception as e:
                        content_parts.append({
                            "type": "text",
                            "text": f"[File: {attachment.filename} - failed to load: {e}]"
                        })
        
        return content_parts
    
    async def _fetch_image_base64(self, url: str) -> Optional[str]:
        """Fetch image from URL and return base64 encoded."""
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return base64.b64encode(data).decode('utf-8')
        return None
    
    async def _fetch_text_file(self, url: str) -> Optional[str]:
        """Fetch text file from URL and return contents."""
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    # Try UTF-8 first, fall back to latin-1
                    try:
                        return data.decode('utf-8')
                    except UnicodeDecodeError:
                        return data.decode('latin-1')
        return None
    
    def _extract_code_files(self, response: str) -> Tuple[str, List[discord.File]]:
        """Extract code blocks with filenames and convert to Discord files."""
        files = []
        
        # Pattern for code blocks with filename: ```filename.ext\ncode\n```
        pattern = r'```(\w+\.\w+)\n(.*?)```'
        
        def replace_with_attachment_note(match):
            filename = match.group(1)
            code = match.group(2)
            
            # Only convert to file if code is long enough
            if len(code) > 500:
                file_buffer = io.BytesIO(code.encode('utf-8'))
                files.append(discord.File(file_buffer, filename=filename))
                return f"📎 *See attached file: `{filename}`*"
            else:
                # Keep short code inline
                return match.group(0)
        
        cleaned = re.sub(pattern, replace_with_attachment_note, response, flags=re.DOTALL)
        return cleaned, files
    
    async def _send_response(
        self, 
        channel: discord.abc.Messageable, 
        content: str, 
        files: List[discord.File] = None
    ) -> None:
        """Send message, chunking if over Discord's limit."""
        if not content and not files:
            return
        
        # Collapse multiple newlines to max 2 (one blank line)
        content = re.sub(r'\n{3,}', '\n\n', content)
        # Also collapse emote + multiple newlines patterns
        content = re.sub(r'(\*[^*]+\*)\n{2,}', r'\1\n', content)
        
        # If content fits in one message
        if len(content) <= 1990:
            await channel.send(content, files=files)
            return
        
        # Chunk the message
        chunks = []
        remaining = content
        while remaining:
            if len(remaining) <= 1990:
                chunks.append(remaining)
                break
            
            # Find a good break point
            break_point = remaining.rfind('\n', 0, 1990)
            if break_point == -1:
                break_point = remaining.rfind(' ', 0, 1990)
            if break_point == -1:
                break_point = 1990
            
            chunks.append(remaining[:break_point])
            remaining = remaining[break_point:].lstrip()
        
        # Send chunks (files only on first message)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await channel.send(chunk, files=files)
            else:
                await channel.send(chunk)
            await asyncio.sleep(0.3)  # Brief pause between chunks
    
    async def send_long_message(self, channel, content: str):
        """Legacy method - wraps _send_response."""
        await self._send_response(channel, content)


# =============================================================================
# COGS (Command Groups)
# =============================================================================

class TarotCog(commands.Cog, name="Tarot"):
    """MTG Tarot reading commands."""
    
    def __init__(self, bot: CompanionBot):
        self.bot = bot
    
    # All valid spread types
    VALID_SPREADS = [
        "single", "three", "five", "five_color", "celtic_cross",
        "horseshoe", "relationship", "two_paths", "elemental",
        "week", "year", "opposition",
    ]

    # Nice display names for spread titles
    SPREAD_NAMES = {
        "single": "Single Card",
        "three": "Three Card",
        "five": "Five Card",
        "five_color": "Five Color",
        "celtic_cross": "Celtic Cross (10 Card)",
        "horseshoe": "Horseshoe (7 Card)",
        "relationship": "Relationship (6 Card)",
        "two_paths": "Two Paths (7 Card)",
        "elemental": "Elemental (4 Card)",
        "week": "Week Ahead (7 Card)",
        "year": "Year Ahead (13 Card)",
        "opposition": "Opposition",
    }

    @commands.command(name="tarot", aliases=["reading", "read"])
    async def tarot_reading(self, ctx, spread: str = "three", engine: str = None):
        """Draw an MTG tarot reading with card images.

        Examples:
            !tarot              → Three card spread (Past/Present/Future)
            !tarot single       → Single card draw
            !tarot five         → Five card spread
            !tarot celtic_cross → Full 10-card Celtic Cross
            !tarot horseshoe    → 7-card arc spread
            !tarot relationship → 6-card relationship spread
            !tarot two_paths    → 7-card decision spread
            !tarot elemental    → 4-card elemental compass
            !tarot week         → 7-card week ahead
            !tarot year         → 13-card year ahead
            !tarot sephirothic  → Use harmony engine only
            !tarot qliphothic   → Use opposition engine only

        After drawing, use !interpret to get Claude's reading.
        """
        # Handle engine-as-spread-argument (e.g. "!tarot sephirothic")
        if spread in ("sephirothic", "qliphothic"):
            engine = spread
            spread = "three"

        if spread not in self.VALID_SPREADS:
            spread_list = ", ".join(f"`{s}`" for s in self.VALID_SPREADS)
            await ctx.send(f"Spread types: {spread_list}")
            return

        if engine and engine not in ["sephirothic", "qliphothic"]:
            await ctx.send("Engine types: `sephirothic`, `qliphothic`, or leave blank for mixed")
            return

        async with ctx.typing():
            if self.bot.visual_tarot:
                # Use visual engine with actual MTG card images
                engine_map = {"sephirothic": "Sephirothic", "qliphothic": "Qliphothic"}
                reading = self.bot.tarot.draw_spread(spread, engine_map.get(engine))

                # Build text summary for conversation history (so !interpret can see it)
                spread_title = self.SPREAD_NAMES.get(spread, spread.replace("_", " ").title())
                reading_text = f"MTG Tarot Reading ({spread_title} Spread):\n"
                for card in reading["cards"]:
                    reversed_str = " (Reversed)" if card.get("reversed") else ""
                    reading_text += f"- {card['position']}: {card['tarot_name']}{reversed_str} - MTG Card: {card['mtg_card']} ({card['engine']} engine, {card['energy']})\n"

                # Store in conversation so Claude can reference it later
                thread_id = ctx.channel.id
                self.bot.conversations[thread_id].append({
                    "role": "assistant",
                    "content": f"*draws tarot cards for {ctx.author.display_name}*\n\n{reading_text}\nUse !interpret to get my reading of this spread."
                })

                # Send header
                header = f"**🎴 MTG Tarot Reading ({spread_title} Spread)**"
                await ctx.send(header)

                # Check if this spread uses a visual (Pillow) layout
                if spread in self.bot.tarot.VISUAL_SPREADS:
                    # Render composite image
                    image_buf = await self.bot.tarot.create_spread_image(reading)
                    if image_buf:
                        file = discord.File(image_buf, filename=f"tarot_{spread}.png")
                        await ctx.send(file=file)

                        # Also send a text summary embed for screen readers / mobile
                        summary = discord.Embed(
                            title=f"Cards in this spread",
                            color=0x8B5CF6,
                        )
                        for card in reading["cards"]:
                            reversed_str = " ↺" if card.get("reversed") else ""
                            summary.add_field(
                                name=f"{card['emoji']} {card['position']}",
                                value=f"{card['tarot_name']}{reversed_str}\n*{card['mtg_card']}*",
                                inline=True,
                            )
                        await ctx.send(embed=summary)
                    else:
                        # PIL not available — fall back to sequential embeds
                        embeds = await self.bot.tarot.create_visual_reading(reading)
                        for embed in embeds:
                            await ctx.send(embed=embed)
                else:
                    # Linear spreads: send individual card embeds
                    embeds = await self.bot.tarot.create_visual_reading(reading)
                    for embed in embeds:
                        await ctx.send(embed=embed)

                # Send footer
                await ctx.send("*Use `!interpret` to get a reading of this spread, or `!tarot` to draw again*")
            else:
                # Fallback to basic engine
                reading = self.bot.tarot.draw_reading(spread, engine or "both")
                formatted = self.bot.tarot.format_reading(reading)
                await ctx.send(formatted)
    
    @commands.command(name="interpret")
    async def interpret_reading(self, ctx, *, question: str = None):
        """Get Claude's interpretation of your tarot reading.
        
        Examples:
            !interpret                    → General interpretation
            !interpret about my job search → Interpret with context
            !interpret context            → Interpret using recent channel messages
            !interpret context 50         → Use last 50 messages as context
        """
        thread_id = ctx.channel.id
        
        # Check if user wants channel context
        channel_context = None
        context_requested = False
        if question:
            # Parse "context" keyword and optional message count
            q_lower = question.lower().strip()
            if q_lower.startswith("context"):
                context_requested = True
                # Check for message count: "context 50", "context 30", etc.
                parts = q_lower.split()
                context_count = 30  # default
                remaining_question = None
                if len(parts) > 1:
                    try:
                        context_count = min(max(int(parts[1]), 5), 100)
                        # Any remaining text after the number is the question
                        if len(parts) > 2:
                            remaining_question = " ".join(question.split()[2:])
                    except ValueError:
                        # Not a number, treat rest as the question
                        remaining_question = " ".join(question.split()[1:])
                
                question = remaining_question  # May be None
                
                # Fetch recent channel messages
                try:
                    messages = []
                    async for msg in ctx.channel.history(limit=context_count):
                        # PluralKit-aware: proxied messages have author.bot=True
                        # because they come through a webhook, but webhook_id
                        # is set. Treat those as real user messages (which is
                        # what the alter behind the proxy semantically is).
                        is_real_bot = msg.author.bot and msg.webhook_id is None
                        if is_real_bot and msg.author != self.bot.user:
                            continue
                        author = "the bot" if msg.author == self.bot.user else msg.author.display_name
                        content = msg.content[:500]
                        if msg.embeds:
                            for embed in msg.embeds[:2]:
                                if embed.title:
                                    content += f" [embed: {embed.title}]"
                                if embed.description:
                                    content += f" [{embed.description[:200]}]"
                        if msg.attachments:
                            content += " [attachment]"
                        messages.append(f"{author}: {content}")
                    
                    messages.reverse()  # Chronological order
                    channel_context = "\n".join(messages)
                except Exception as e:
                    print(f"Error fetching channel history for interpret: {e}")
            
            # Also detect implicit context requests
            elif any(phrase in q_lower for phrase in [
                "past messages", "recent messages", "this channel",
                "what we've been talking about", "conversation so far",
                "in context of", "based on what"
            ]):
                context_requested = True
                try:
                    messages = []
                    async for msg in ctx.channel.history(limit=30):
                        # PluralKit-aware: proxied messages have author.bot=True
                        # because they come through a webhook, but webhook_id
                        # is set. Treat those as real user messages (which is
                        # what the alter behind the proxy semantically is).
                        is_real_bot = msg.author.bot and msg.webhook_id is None
                        if is_real_bot and msg.author != self.bot.user:
                            continue
                        author = "the bot" if msg.author == self.bot.user else msg.author.display_name
                        content = msg.content[:500]
                        if msg.embeds:
                            for embed in msg.embeds[:2]:
                                if embed.title:
                                    content += f" [embed: {embed.title}]"
                                if embed.description:
                                    content += f" [{embed.description[:200]}]"
                        messages.append(f"{author}: {content}")
                    messages.reverse()
                    channel_context = "\n".join(messages)
                except Exception as e:
                    print(f"Error fetching channel history for interpret: {e}")
        
        # Build interpretation prompt — include requester's name so Claude knows
        # who the querent is, even if other users' messages are in the conversation history
        requester = ctx.author.display_name
        prompt = f"Please interpret the most recent MTG tarot reading for {requester}"
        if question:
            prompt += f" in the context of this question: {question}"
        if context_requested and not question:
            prompt += " in the context of the recent channel conversation"
        
        # Add to conversation and get response
        self.bot.conversations[thread_id].append({
            "role": "user",
            "content": prompt
        })
        
        # Build the system prompt with optional channel context
        tarot_system_addition = """

When interpreting MTG tarot readings, consider:
- The color pie philosophy underlying each suit
- Sephirothic (allied pairs) represents harmony and natural flow
- Qliphothic (enemy pairs) represents productive tension and opposition
- Reversed cards suggest blocked energy, shadow aspects, or the need for reflection
- Connect the cards' themes to the querent's situation with compassion and insight
"""
        if channel_context:
            tarot_system_addition += f"""
You have access to recent channel conversation for context. Use this to make your 
interpretation specific and relevant to what's actually being discussed. Don't just 
give a generic reading — tie the cards to the real topics, projects, emotions, and 
situations visible in the conversation.

Recent channel messages:
{channel_context}
"""
        
        async with ctx.typing():
            try:
                # Get environment context
                environment = await self.bot.get_environment_context()
                
                # Run in thread pool to avoid blocking Discord's event loop
                response = await asyncio.to_thread(
                    self.bot.claude.messages.create,
                    model=CONFIG.model_default,
                    max_tokens=CONFIG.max_tokens,
                    system=self.bot.build_system_prompt(ctx.author, "none", environment) + tarot_system_addition,
                    messages=self.bot.conversations[thread_id]
                )
                
                # Track token usage
                if hasattr(response, 'usage'):
                    self.bot.total_input_tokens += response.usage.input_tokens
                    self.bot.total_output_tokens += response.usage.output_tokens
                    self.bot.sonnet_input_tokens += response.usage.input_tokens
                    self.bot.sonnet_output_tokens += response.usage.output_tokens
                    self.bot.api_calls += 1
                    self.bot._save_persistent_costs()
                
                reply = response.content[0].text
                self.bot.conversations[thread_id].append({
                    "role": "assistant",
                    "content": reply
                })
                
                await self.bot.send_long_message(ctx.channel, reply)
                
            except Exception as e:
                print(f"Error: {e}")
                await ctx.send("Sorry, I had trouble with that interpretation.")
    
    @commands.command(name="suits")
    async def show_suits(self, ctx, engine: str = "both"):
        """Show the MTG tarot suit meanings.
        
        Examples:
            !suits              → Show all suits from both engines
            !suits sephirothic  → Show harmony suits only (allied pairs)
            !suits qliphothic   → Show opposition suits only (enemy pairs)
        """
        lines = ["**🎴 MTG Tarot Suits**\n"]
        
        if self.bot.visual_tarot:
            # Use SUIT_INFO from visual engine
            from rules.tarot_visuals import SUIT_INFO
            
            if engine in ["both", "sephirothic"]:
                lines.append("**Sephirothic Engine** (Allied Pairs - Harmony)")
                for name, info in SUIT_INFO.items():
                    if info["engine"] == "Sephirothic":
                        lines.append(f"• **{name}** ({info['colors']}): {info['energy']}")
                lines.append("")
            
            if engine in ["both", "qliphothic"]:
                lines.append("**Qliphothic Engine** (Enemy Pairs - Opposition)")
                for name, info in SUIT_INFO.items():
                    if info["engine"] == "Qliphothic":
                        lines.append(f"• **{name}** ({info['colors']}): {info['energy']}")
        else:
            # Fallback to basic engine
            if engine in ["both", "sephirothic"]:
                lines.append("**Sephirothic Engine** (Allied Pairs - Harmony)")
                for name, suit in self.bot.tarot.sephirothic["suits"].items():
                    lines.append(f"• **{name}** ({suit['colors']}): {suit['energy']}")
                lines.append("")
            
            if engine in ["both", "qliphothic"]:
                lines.append("**Qliphothic Engine** (Enemy Pairs - Opposition)")
                for name, suit in self.bot.tarot.qliphothic["suits"].items():
                    lines.append(f"• **{name}** ({suit['colors']}): {suit['energy']}")
        
        await ctx.send("\n".join(lines))


class SupportCog(commands.Cog, name="Support"):
    """Emotional support and wellness commands."""
    
    def __init__(self, bot: CompanionBot):
        self.bot = bot
    
    @commands.command(name="ground")
    async def grounding_exercise(self, ctx):
        """Get a quick grounding exercise."""
        exercises = [
            "*settles next to you* **5-4-3-2-1**: Name 5 things you see, 4 you hear, 3 you can touch, 2 you smell, 1 you taste. I'll wait. 🐼",
            "*chirps softly* **Box Breathing**: Breathe in for 4 counts, hold for 4, out for 4, hold for 4. Repeat 5 times.",
            "*ear flick* **Cold Reset**: Run cold water over your wrists for 30 seconds. The temperature change interrupts the spiral. Trust me on this one.",
            "*presses warm flank against your leg* **Feet on Floor**: Press your feet firmly into the ground. Notice the pressure. You're here, right now, with me.",
            "*nudges an object toward you* **Object Focus**: Pick this up. Describe its texture, weight, temperature, color. All of it. Stay here with me.",
        ]
        await ctx.send(random.choice(exercises))
    
    @commands.command(name="breathe")
    async def breathing_exercise(self, ctx):
        """Guided box breathing exercise."""
        msg = await ctx.send("*curls up next to you*\n\nLet's breathe together.\n\n**Breathe in...** 🌬️")
        await asyncio.sleep(4)
        await msg.edit(content="*curls up next to you*\n\nLet's breathe together.\n\n**Hold...** ✨")
        await asyncio.sleep(4)
        await msg.edit(content="*curls up next to you*\n\nLet's breathe together.\n\n**Breathe out...** 🍃")
        await asyncio.sleep(4)
        await msg.edit(content="*curls up next to you*\n\nLet's breathe together.\n\n**Hold...** 💙")
        await asyncio.sleep(4)
        await msg.edit(content="*soft chirp*\n\nOne cycle complete. Type `!breathe` again if you want, or just sit here with me for a moment. 🐼💙")
    
    @commands.command(name="panda", aliases=["redpanda", "comfort"])
    async def red_panda(self, ctx, media_type: str = None):
        """Get a red panda image or gif for comfort.
        
        Usage:
            !panda      - Random image with a fact
            !panda gif  - Get a gif instead
        """
        async with ctx.typing():
            thread_id = ctx.channel.id
            
            # Store the interaction in conversation so Claude remembers what it sent
            self.bot.conversations[thread_id].append({
                "role": "user",
                "content": f"{ctx.author.display_name} requested red panda comfort content (!panda)"
            })
            
            # Try to fetch actual image/gif
            if media_type and media_type.lower() == "gif":
                gif_url, title = await self.bot.web_search.fetch_red_panda_gif()
                if gif_url:
                    embed = discord.Embed(
                        title="🐼 Red Panda Time!",
                        color=discord.Color.orange()
                    )
                    embed.set_image(url=gif_url)
                    response_text = random.choice([
                        "*bounces excitedly* Look at this!",
                        "*tail swishes happily* One of my cousins!",
                        "*chirps with delight* So fluffy!",
                        "*perks up* This one's especially cute!",
                    ])
                    await ctx.send(response_text, embed=embed)
                    self.bot.conversations[thread_id].append({
                        "role": "assistant",
                        "content": f"{response_text} [Sent red panda gif]"
                    })
                    return
            else:
                # Try to get an image + fact
                image_url, fact, source = await self.bot.web_search.fetch_red_panda_image()
                if image_url:
                    embed = discord.Embed(
                        title="🐼 Red Panda Facts!",
                        color=discord.Color.orange()
                    )
                    embed.set_image(url=image_url)
                    
                    # Add the fact if we got one (from any source)
                    if fact:
                        embed.description = f"*{fact}*"
                    
                    response_text = random.choice([
                        "*perks up excitedly* Look! More of my kind!",
                        "*soft chirp* Here's a friend!",
                        "*tail swishes proudly* We're pretty cute, aren't we?",
                        "*wiggles happily* Found one!",
                    ])
                    await ctx.send(response_text, embed=embed)
                    
                    fact_text = f" Fun fact: {fact}" if fact else ""
                    self.bot.conversations[thread_id].append({
                        "role": "assistant",
                        "content": f"{response_text} [Sent red panda image]{fact_text}"
                    })
                    return
            
            # Fallback to facts if API calls fail
            facts = [
                "*stretches proudly* Did you know we spend most of our time in trees? I even sleep in them! Very cozy. 🌳",
                "*wraps tail around paws* My tail can be up to 18 inches long! I use it as a blanket. Very practical. 🦊",
                "*yawns* We're most active at dawn and dusk - crepuscular, they call it. Fancy word for 'nap appreciator.' 🌅",
                "*tilts head* Despite our name, we're more closely related to raccoons than giant pandas! We're our own thing. 🦝",
                "*wiggles paw* I have a false thumb - an extended wrist bone - that helps me grip bamboo! Evolution is weird and cool. 🎋",
                "*soft chirp* Baby red pandas are called cubs and are born blind and deaf. We figure it out eventually! 👶",
                "*chomping noises* I can eat 200,000 bamboo leaves in a single day. ...okay that might be an exaggeration. But a lot! 🍃",
            ]
            response_text = self.bot.web_search._pick_fresh_fact(facts)
            await ctx.send(response_text)
            self.bot.conversations[thread_id].append({
                "role": "assistant",
                "content": response_text
            })
    
    @commands.command(name="search")
    async def web_search(self, ctx, *, query: str):
        """Search the web for something. (Costs a bit extra for citations.)
        
        Usage:
            !search red panda habitat
            !search MTG Modern metagame December 2025
        """
        async with ctx.typing():
            # Add search request to conversation
            thread_id = ctx.channel.id
            self.bot.conversations[thread_id].append({
                "role": "user",
                "content": f"Please search for: {query}"
            })
            
            text, embeds = await self.bot.web_search.search_with_claude(
                query=query,
                system_prompt="You are the bot (he/him), a helpful sapient red panda. Search the web and provide a clear, concise summary of what you find. Be warm but informative. IMPORTANT: Always include the full URLs of your sources in your response text (e.g., https://example.com/page). These will be extracted and shown as clickable links.",
                conversation=self.bot.conversations[thread_id]
            )
            
            # Store response
            self.bot.conversations[thread_id].append({
                "role": "assistant",
                "content": text
            })
            
            # Send results
            await self.bot.send_long_message(ctx.channel, text)
            if embeds:
                for embed in embeds:
                    await ctx.send(embed=embed)
    
    @commands.command(name="clear")
    async def clear_context(self, ctx):
        """Clear conversation history for this thread."""
        thread_id = ctx.channel.id
        self.bot.conversations[thread_id] = []
        await ctx.send("*shakes fur* Fresh start! What shall we talk about? 🌱")
    
    @commands.command(name="remember")
    async def remember(self, ctx, key: str = None, *, value: str = None):
        """Store something in long-term memory (permanent until forgotten).
        
        Usage:
            !remember favorite_color blue
            !remember birthday January 15
        """
        if not key or not value:
            await ctx.send("*tilts head* Usage: `!remember <key> <value>`\nExample: `!remember birthday January 15`")
            return
        
        memory = self.bot.get_memory(ctx.author.id)
        if memory.longterm.add(key, value):
            await self.bot.save_memories_async()
            await ctx.send(f"*ear flick* ✅ I'll remember `{key}` permanently! (Until you tell me to forget)")
        else:
            await ctx.send(
                f"*concerned chirp* Long-term memory is full ({CONFIG.max_longterm_memories} max). "
                f"Use `!forget <key>` to make room."
            )
    
    @commands.command(name="forget")
    async def forget(self, ctx, key: str = None):
        """Forget something from memory.
        
        Usage:
            !forget birthday
        """
        if not key:
            await ctx.send("*tilts head* Usage: `!forget <key>`")
            return
        
        memory = self.bot.get_memory(ctx.author.id)
        
        # Try long-term first, then working
        if memory.longterm.remove(key):
            await self.bot.save_memories_async()
            await ctx.send(f"*nods* ✅ Forgot `{key}` from long-term memory")
        elif memory.working.remove(key):
            await self.bot.save_memories_async()
            await ctx.send(f"*nods* ✅ Forgot `{key}` from working notes")
        else:
            await ctx.send(f"*ear flick* ❓ I don't have anything stored as `{key}`")
    
    @commands.command(name="keep")
    async def keep(self, ctx, key: str = None):
        """Promote a working note to permanent memory.
        
        Usage:
            !keep project_deadline
        """
        if not key:
            await ctx.send("*tilts head* Usage: `!keep <key>` - promotes a working note to permanent memory")
            return
        
        memory = self.bot.get_memory(ctx.author.id)
        
        if key not in memory.working.notes:
            await ctx.send(f"*ear flick* ❓ No working note with key `{key}`")
        elif memory.promote(key):
            await self.bot.save_memories_async()
            await ctx.send(f"*happy chirp* ✅ Promoted `{key}` to long-term memory (permanent now!)")
        else:
            await ctx.send(
                f"*concerned look* Long-term memory is full ({CONFIG.max_longterm_memories} max). "
                f"Use `!forget <key>` to make room."
            )
    
    @commands.command(name="memories")
    async def show_memories(self, ctx):
        """Show what I remember about you."""
        memory = self.bot.get_memory(ctx.author.id)
        
        lines = ["*settles down and thinks*\n"]
        
        # Long-term memories
        if memory.longterm.entries:
            lines.append("📚 **Long-term memories** (permanent):")
            for key, value in memory.longterm.entries.items():
                lines.append(f"  • `{key}`: {value}")
            lines.append("")
        else:
            lines.append("📚 **Long-term memories**: None yet")
            lines.append("*Use `!remember <key> <value>` to add some!*\n")
        
        # Working notes
        if memory.working.notes:
            lines.append("📝 **Working notes** (observations, may fade):")
            for key, note in memory.working.notes.items():
                freshness = note.freshness(CONFIG.working_memory_decay_hours)
                if freshness > 0.7:
                    indicator = "🟢"
                elif freshness > 0.3:
                    indicator = "🟡"
                else:
                    indicator = "🔴"
                lines.append(f"  {indicator} `{key}`: {note.content}")
            lines.append("")
            lines.append("*Use `!keep <key>` to make a working note permanent*")
        else:
            lines.append("📝 **Working notes**: None yet")
            lines.append("*I'll jot things down as we chat!*")
        
        # Chunk output to avoid Discord 2000 char limit
        output = "\n".join(lines)
        if len(output) <= 1900:
            await ctx.send(output)
        else:
            # Split into chunks
            chunks = []
            current_chunk = ""
            for line in lines:
                if len(current_chunk) + len(line) + 1 > 1900:
                    chunks.append(current_chunk)
                    current_chunk = line
                else:
                    current_chunk += ("\n" if current_chunk else "") + line
            if current_chunk:
                chunks.append(current_chunk)
            
            for chunk in chunks:
                await ctx.send(chunk)
    
    @commands.command(name="help_support")
    async def support_help(self, ctx):
        """Show support-related commands."""
        help_text = """*stretches and settles down to explain*

**Support Commands:**

`!ground` - I'll guide you through a grounding exercise
`!breathe` - Let's do box breathing together (4-4-4-4)
`!panda` / `!redpanda` / `!comfort` - Pictures of my kind! Always helps 🐼
`!search <query>` - I'll search the web for you (adds citation cost)
`!clear` - Fresh conversation, clean slate

**Memory Commands:**

`!memories` - See what I remember about you
`!remember <key> <value>` - Store something permanently
`!forget <key>` - Remove something from memory
`!keep <key>` - Promote a working note to permanent

**Usage Tracking:**

`!cost` - Show API usage and estimated $ since bot started
`!context` - Show current conversation size and cost estimate
`!summarize` - Get a summary of our conversation so far

**How I Work:**
- For regular chat and MTG stuff, I use my quick-thinking mode
- When I sense you're struggling, I switch to my deeper, wiser mode
- If things get really hard, I'll also fetch more red panda content automatically
- I jot down observations as we chat (they fade after ~48h unless promoted)

**I'm here for you** 💙
- You can just talk to me when I'm mentioned
- I'll notice if something's off and adjust
- Not here to judge, just here to help

*curls tail around paws*
"""
        await ctx.send(help_text)
    
    @commands.command(name="cost")
    async def show_cost(self, ctx):
        """Show API usage and estimated cost since bot started."""
        summary = self.bot.get_cost_summary()
        await ctx.send(summary)
    
    @commands.command(name="context")
    async def show_context(self, ctx):
        """Show current conversation context size and estimated cost."""
        summary = self.bot.get_context_summary(ctx.channel.id)
        await ctx.send(summary)
    
    @commands.command(name="summarize", aliases=["summary", "recap"])
    async def summarize_conversation(self, ctx, count: int = 30):
        """Get a summary of recent messages in this channel.
        
        Examples:
            !summarize      → Summarize last 30 messages
            !summarize 50   → Summarize last 50 messages
        """
        # Cap at 100 messages to avoid huge context
        count = min(max(count, 5), 100)
        
        async with ctx.typing():
            try:
                # Fetch messages from Discord directly (not bot memory)
                messages = []
                now = datetime.now(timezone.utc)
                
                async for msg in ctx.channel.history(limit=count):
                    # PluralKit-aware: webhook proxies look like bots but are
                    # really alter messages. Only skip real bots.
                    is_real_bot = msg.author.bot and msg.webhook_id is None
                    if is_real_bot and msg.author != self.bot.user:
                        continue  # Skip other bots but include the bot
                    
                    # Calculate relative time
                    age = now - msg.created_at
                    if age.total_seconds() < 60:
                        time_str = "just now"
                    elif age.total_seconds() < 3600:
                        mins = int(age.total_seconds() / 60)
                        time_str = f"{mins}m ago"
                    elif age.total_seconds() < 86400:
                        hours = int(age.total_seconds() / 3600)
                        time_str = f"{hours}h ago"
                    else:
                        days = int(age.total_seconds() / 86400)
                        time_str = f"{days}d ago"
                    
                    # Format message
                    author = "the bot" if msg.author == self.bot.user else msg.author.display_name
                    content = msg.content[:500]  # Truncate very long messages
                    if msg.attachments:
                        content += " [attachment]"
                    if msg.embeds:
                        content += " [embed]"
                    
                    messages.append(f"[{time_str}] {author}: {content}")
                
                messages.reverse()  # Chronological order
                
                if len(messages) < 3:
                    await ctx.send("*tilts head* There's not much to summarize yet!")
                    return
                
                # Build summary prompt
                conversation_text = "\n".join(messages)
                summary_messages = [{
                    "role": "user",
                    "content": f"Please summarize this conversation. Focus on key topics, decisions, and context. Keep it concise.\n\n{conversation_text}"
                }]
                
                response = await asyncio.to_thread(
                    self.bot.claude.messages.create,
                    model=CONFIG.model_default,
                    max_tokens=1024,
                    system="You are the bot (he/him), a friendly red panda. Summarize the conversation concisely but warmly. Use brief red panda mannerisms but keep the summary focused and useful. Note any ongoing topics or unresolved questions.",
                    messages=summary_messages
                )
                
                # Track usage
                if hasattr(response, 'usage'):
                    self.bot.total_input_tokens += response.usage.input_tokens
                    self.bot.total_output_tokens += response.usage.output_tokens
                    self.bot.sonnet_input_tokens += response.usage.input_tokens
                    self.bot.sonnet_output_tokens += response.usage.output_tokens
                    self.bot.api_calls += 1
                    self.bot._save_persistent_costs()
                
                summary = response.content[0].text
                await self.bot.send_long_message(ctx.channel, f"**📋 Summary of last {len(messages)} messages**\n\n{summary}")
                
            except Exception as e:
                print(f"Error summarizing: {e}")
                await ctx.send("*ear flick* Sorry, I had trouble summarizing. Try again?")


# =============================================================================
# MAIN
# =============================================================================

def main():
    # Verify environment
    if not os.getenv("DISCORD_TOKEN"):
        print("Error: DISCORD_TOKEN not set in environment")
        return
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set in environment")
        return
    
    bot = CompanionBot()
    bot.run(os.getenv("DISCORD_TOKEN"))


if __name__ == "__main__":
    main()
