"""
MTG Tarot Visual Enhancement - Complete Card Mappings
======================================================

Uses a handpicked set of MTG cards from the November 2025 tarot project.
All 184 unique primary cards across both Sephirothic and Qliphothic engines.

Usage:
    from tarot_visuals import VisualTarotEngine
    
    engine = VisualTarotEngine()
    reading = engine.draw_spread('three')  # or 'celtic_cross', 'five_color'
    embeds = await engine.create_visual_reading(reading)
"""

import aiohttp
import asyncio
import discord
import io
import math
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import hashlib
import random

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# =============================================================================
# COMPLETE CARD MAPPINGS - From the November MTG Tarot Project
# =============================================================================

# Major Arcana - Sephirothic Engine (Colorless cards)
MAJOR_SEPHIROTHIC = {
    0: "Sol Ring",                      # The Spark
    1: "Karn, Silver Golem",            # The Artificer
    2: "Void Winnower",                 # The Veil
    3: "Wurmcoil Engine",               # The Garden
    4: "Platinum Angel",                # The Arbiter
    5: "Akroma's Memorial",             # The Hierarch
    6: "Batterskull",                   # The Bond
    7: "Expedition Map",                # The Journey
    8: "Sculpting Steel",               # The Shaper
    9: "Eye of Ugin",                   # The Hermit
    10: "Ulamog, the Infinite Gyre",    # The Cycle
    11: "Mindslaver",                   # The Pact
    12: "Ensnaring Bridge",             # The Hanged One
    13: "All Is Dust",                  # The Unmaking
    14: "Trading Post",                 # The Mender
    15: "Kozilek, the Great Distortion", # The Bound
    16: "Karn Liberated",               # The Shattering
    17: "Darksteel Forge",              # The Beacon
    18: "Emrakul, the Promised End",    # The Void
    19: "Mycosynth Lattice",            # The Core
    20: "Ugin, the Ineffable",          # The Calling
    21: "Planar Bridge",                # The Multiverse
}

# Major Arcana - Qliphothic Engine (Mono-colored journey through each color)
MAJOR_QLIPHOTHIC = {
    # White Path (0-4)
    0: "Soul Warden",                   # White 0: The Citizen
    1: "Knight Exemplar",               # White 1: The Knight
    2: "Elesh Norn, Grand Cenobite",    # White 2: The Absolute
    3: "Wrath of God",                  # White 3: The Purge
    4: "Selfless Spirit",               # White 4: The Martyr
    # Blue Path (5-9)
    5: "Enclave Cryptologist",          # Blue 0: The Student
    6: "Urza, Lord High Artificer",     # Blue 1: The Artificer
    7: "Thassa's Oracle",               # Blue 2: The Oracle
    8: "Vendilion Clique",              # Blue 3: The Deceiver
    9: "Omniscience",                   # Blue 4: The Infinite
    # Black Path (10-13)
    10: "Bloodghast",                   # Black 0: The Driven
    11: "Sheoldred, Whispering One",    # Black 1: The Tyrant
    12: "Griselbrand",                  # Black 2: The Isolated
    13: "Lich's Mastery",               # Black 3: The Transformed
    # Red Path (14-17)
    14: "Goblin Guide",                 # Red 0: The Hasty
    15: "Ball Lightning",               # Red 1: The Committed
    16: "Jokulhaups",                   # Red 2: The Arsonist
    17: "Rekindling Phoenix",           # Red 3: The Phoenix
    # Green Path (18-21)
    18: "Birds of Paradise",            # Green 0: The Seed
    19: "Questing Beast",               # Green 1: The Apex
    20: "Vorinclex, Voice of Hunger",   # Green 2: The Overgrowth
    21: "Worldspine Wurm",              # Green 3: The Eternal
}

# Minor Arcana - Sephirothic Engine (Allied pairs)
MINOR_SEPHIROTHIC = {
    # Edicts (WU) - Law, foresight, systematic control
    "Edicts": {
        1: "Dovin's Veto",
        2: "Council's Judgment",
        3: "Deputy of Detention",
        4: "Grand Arbiter Augustin IV",
        5: "Render Silent",
        6: "Ojutai's Command",
        7: "Sphinx's Insight",
        8: "Brago, King Eternal",
        9: "Dream Trawler",
        10: "Azor, the Lawbringer",
        11: "Deputy of Acquittals",       # Envoy
        12: "Lavinia, Azorius Renegade",  # Champion
        13: "Hanna, Ship's Navigator",    # Steward
        14: "Azor, the Lawbringer",       # Architect (duplicate intentional - it's the legacy)
    },
    
    # Secrets (UB) - Hidden knowledge, manipulation
    "Secrets": {
        1: "Thought Erasure",
        2: "Baleful Strix",
        3: "Shadowmage Infiltrator",
        4: "Ashiok, Nightmare Weaver",
        5: "Hostage Taker",
        6: "Atris, Oracle of Half-Truths",
        7: "Lazav, Dimir Mastermind",
        8: "Zareth San, the Trickster",
        9: "Yuriko, the Tiger's Shadow",
        10: "Szadek, Lord of Secrets",
        11: "Fallen Shinobi",             # Envoy
        12: "Dragonlord Silumgar",        # Champion
        13: "Vela the Night-Clad",        # Steward
        14: "Ashiok, Nightmare Muse",     # Architect
    },
    
    # Revelry (BR) - Hedonism, liberation through destruction
    "Revelry": {
        1: "Rakdos Charm",
        2: "Kroxa, Titan of Death's Hunger",
        3: "Judith, the Scourge Diva",
        4: "Rakdos, the Showstopper",
        5: "Angrath's Rampage",
        6: "Olivia Voldaren",
        7: "Master of Cruelties",
        8: "Rakdos, Lord of Riots",
        9: "Kardur, Doomscourge",
        10: "Rakdos, Patron of Chaos",
        11: "Stormfist Crusader",         # Envoy
        12: "Neheb, the Eternal",         # Champion
        13: "Anje Falkenrath",            # Steward
        14: "Xantcha, Sleeper Agent",     # Architect
    },
    
    # Wilds (RG) - Primal instinct, natural fury
    "Wilds": {
        1: "Atarka's Command",
        2: "Burning-Tree Emissary",
        3: "Rhythm of the Wild",
        4: "Domri, Anarch of Bolas",
        5: "Decimate",
        6: "Xenagos, God of Revels",
        7: "Grand Warlord Radha",
        8: "Atarka, World Render",
        9: "Rosheen Meanderer",
        10: "Borborygmos Enraged",
        11: "Zhur-Taa Goblin",            # Envoy
        12: "Stonebrow, Krosan Hero",     # Champion
        13: "Samut, Voice of Dissent",    # Steward
        14: "Thromok the Insatiable",     # Architect
    },
    
    # Groves (GW) - Community, organic growth
    "Groves": {
        1: "Selesnya Charm",
        2: "Voice of Resurgence",
        3: "Loxodon Smiter",
        4: "Trostani, Selesnya's Voice",
        5: "Dromoka's Command",
        6: "Sigarda, Host of Herons",
        7: "Mirari's Wake",
        8: "Karametra, God of Harvests",
        9: "March of the Multitudes",
        10: "Tolsimir, Friend to Wolves",
        11: "Conclave Cavalier",          # Envoy
        12: "Knight of the Reliquary",    # Champion
        13: "Saffi Eriksdotter",          # Steward
        14: "Aura Shards",                # Architect
    },
}

# Minor Arcana - Qliphothic Engine (Enemy pairs)
MINOR_QLIPHOTHIC = {
    # Debts (WB) - Power through obligation
    "Debts": {
        1: "Anguished Unmaking",
        2: "Tidehollow Sculler",
        3: "Kambal, Consul of Allocation",
        4: "Athreos, God of Passage",
        5: "Merciless Eviction",
        6: "Unburial Rites",
        7: "Sin Collector",
        8: "Vona, Butcher of Magan",
        9: "Elenda, the Dusk Rose",
        10: "Teysa Karlov",
        11: "Fiend Hunter",               # Challenger
        12: "Obzedat, Ghost Council",     # Adversary
        13: "Teysa, Orzhov Scion",        # Witness
        14: "Sorin, Lord of Innistrad",   # Alchemist
    },
    
    # Sparks (UR) - Creative destruction, volatility
    "Sparks": {
        1: "Electrolyze",
        2: "Stormchaser Mage",
        3: "Crackling Drake",
        4: "Saheeli, Sublime Artificer",
        5: "Expansion // Explosion",
        6: "Thousand-Year Storm",
        7: "Ral, Storm Conduit",
        8: "Niv-Mizzet, the Firemind",
        9: "Niv-Mizzet, Parun",
        10: "Keranos, God of Storms",
        11: "Adeliz, the Cinder Wind",    # Challenger
        12: "Tibor and Lumia",            # Adversary
        13: "Arjun, the Shifting Flame",  # Witness
        14: "Niv-Mizzet, Dracogenius",    # Alchemist
    },
    
    # Rot (BG) - Death feeding life, necessary endings
    "Rot": {
        1: "Assassin's Trophy",
        2: "Deathrite Shaman",
        3: "Slimefoot, the Stowaway",
        4: "Savra, Queen of the Golgari",
        5: "Vraska, Golgari Queen",
        6: "The Gitrog Monster",
        7: "Jarad, Golgari Lich Lord",
        8: "Meren of Clan Nel Toth",
        9: "Storrev, Devkarin Lich",
        10: "Polukranos, Unchained",
        11: "Dreg Mangler",               # Challenger
        12: "Skullbriar, the Walking Grave", # Adversary
        13: "Pharika, God of Affliction", # Witness
        14: "Izoni, Thousand-Eyed",       # Alchemist
    },
    
    # Crusades (RW) - Righteous fury, disciplined warfare
    "Crusades": {
        1: "Lightning Helix",
        2: "Swiftblade Vindicator",
        3: "Tajic, Legion's Edge",
        4: "Aurelia, the Warleader",
        5: "Deflecting Palm",
        6: "Feather, the Redeemed",
        7: "Deafening Clarion",
        8: "Razia, Boros Archangel",
        9: "Aurelia, Exemplar of Justice",
        10: "Aurelia, the Law Above",
        11: "Archwing Dragon",            # Challenger
        12: "Anax and Cymede",            # Adversary
        13: "Sunforger",                  # Witness
        14: "Chance for Glory",           # Alchemist
    },
    
    # Grafts (GU) - Evolution, nature modified
    "Grafts": {
        1: "Simic Charm",
        2: "Coiling Oracle",
        3: "Edric, Spymaster of Trest",
        4: "Zegana, Utopian Speaker",
        5: "Mystic Snake",
        6: "Momir Vig, Simic Visionary",
        7: "Uro, Titan of Nature's Wrath",
        8: "Prime Speaker Zegana",
        9: "Aesi, Tyrant of Gyre Strait",
        10: "Koma, Cosmos Serpent",
        11: "Hydroid Krasis",             # Challenger
        12: "Thrasios, Triton Hero",      # Adversary
        13: "Kiora, the Crashing Wave",   # Witness
        14: "Kumena, Tyrant of Orazca",   # Alchemist
    },
}


# =============================================================================
# SUIT METADATA
# =============================================================================

SUIT_INFO = {
    # Sephirothic (Allied pairs)
    "Edicts": {
        "colors": "WU",
        "engine": "Sephirothic",
        "theme": "Law, foresight, systematic control, institutional wisdom",
        "energy": "Structure through knowledge",
        "emoji": "⚖️",
        "embed_color": 0x7DB3E0,  # Blue-white blend
    },
    "Secrets": {
        "colors": "UB",
        "engine": "Sephirothic",
        "theme": "Hidden knowledge, strategic manipulation, covert advantage",
        "energy": "Power through what others don't know",
        "emoji": "🌑",
        "embed_color": 0x1A1A2E,  # Dark blue-black
    },
    "Revelry": {
        "colors": "BR",
        "engine": "Sephirothic",
        "theme": "Hedonism, liberation through destruction, freedom through excess",
        "energy": "Freedom through transgression",
        "emoji": "🔥",
        "embed_color": 0x8B0000,  # Dark red-black
    },
    "Wilds": {
        "colors": "RG",
        "engine": "Sephirothic",
        "theme": "Primal instinct, natural fury, honest strength",
        "energy": "Power through directness",
        "emoji": "🌲",
        "embed_color": 0x8B4513,  # Red-green earthy
    },
    "Groves": {
        "colors": "GW",
        "engine": "Sephirothic",
        "theme": "Community, organic growth, collective strength",
        "energy": "Growth through cooperation",
        "emoji": "🌻",
        "embed_color": 0x90EE90,  # Light green-white
    },
    # Qliphothic (Enemy pairs)
    "Debts": {
        "colors": "WB",
        "engine": "Qliphothic",
        "theme": "Power through obligation, ends justify means, necessary evils",
        "energy": "Control requiring sacrifice",
        "emoji": "⛓️",
        "embed_color": 0x696969,  # Gray (white-black)
    },
    "Sparks": {
        "colors": "UR",
        "engine": "Qliphothic",
        "theme": "Creative destruction, brilliant instability, innovation through chaos",
        "energy": "Breakthrough or breakdown",
        "emoji": "⚡",
        "embed_color": 0xFF4500,  # Orange-red (blue+red)
    },
    "Rot": {
        "colors": "BG",
        "engine": "Qliphothic",
        "theme": "Death feeding life, necessary endings, transformation through decay",
        "energy": "Growth through destruction",
        "emoji": "🍂",
        "embed_color": 0x2F4F2F,  # Dark green-black
    },
    "Crusades": {
        "colors": "RW",
        "engine": "Qliphothic",
        "theme": "Righteous fury, passionate order, militant idealism",
        "energy": "Justice through force",
        "emoji": "⚔️",
        "embed_color": 0xFFD700,  # Gold (red+white)
    },
    "Grafts": {
        "colors": "GU",
        "engine": "Qliphothic",
        "theme": "Nature modified, directed evolution, patient engineering",
        "energy": "Growth through design",
        "emoji": "🧬",
        "embed_color": 0x008B8B,  # Teal (green+blue)
    },
}

# Position names
POSITION_NAMES = {
    1: "Ace", 2: "Two", 3: "Three", 4: "Four", 5: "Five",
    6: "Six", 7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten",
    11: "Face (11)", 12: "Face (12)", 13: "Face (13)", 14: "Face (14)",
}

# Face card titles by engine
FACE_TITLES_SEPHIROTHIC = {11: "Envoy", 12: "Champion", 13: "Steward", 14: "Architect"}
FACE_TITLES_QLIPHOTHIC = {11: "Challenger", 12: "Adversary", 13: "Witness", 14: "Alchemist"}


# =============================================================================
# SCRYFALL CLIENT
# =============================================================================

class ScryfallClient:
    """Async client for fetching card images from Scryfall."""
    
    BASE_URL = "https://api.scryfall.com"
    
    def __init__(self):
        self._cache: Dict[str, Dict] = {}
    
    async def get_card(self, name: str) -> Optional[Dict]:
        """Get card data including image URLs."""
        if name in self._cache:
            return self._cache[name]
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.BASE_URL}/cards/named"
                params = {"fuzzy": name}
                
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._cache[name] = data
                        return data
                    else:
                        return None
        except Exception as e:
            print(f"Scryfall error for {name}: {e}")
            return None
    
    def get_image_url(self, card_data: Dict, size: str = "normal") -> Optional[str]:
        """Extract image URL from card data."""
        if not card_data:
            return None
        
        # Handle double-faced cards
        if "image_uris" in card_data:
            return card_data["image_uris"].get(size)
        elif "card_faces" in card_data and card_data["card_faces"]:
            return card_data["card_faces"][0].get("image_uris", {}).get(size)
        
        return None


# =============================================================================
# VISUAL TAROT ENGINE
# =============================================================================

class VisualTarotEngine:
    """
    Complete MTG Tarot reading engine with card images.
    
    Draws from the Sephirothic (allied pairs) or Qliphothic (enemy pairs)
    engine, with full card image support via Scryfall.
    """
    
    def __init__(self):
        self.scryfall = ScryfallClient()
        self._all_minor_cards = self._build_card_pool()
    
    def _build_card_pool(self) -> List[Tuple[str, int, str]]:
        """Build pool of all minor arcana cards: (suit, position, engine)"""
        pool = []
        
        for suit, cards in MINOR_SEPHIROTHIC.items():
            for pos in cards.keys():
                pool.append((suit, pos, "Sephirothic"))
        
        for suit, cards in MINOR_QLIPHOTHIC.items():
            for pos in cards.keys():
                pool.append((suit, pos, "Qliphothic"))
        
        return pool
    
    # Spread types that use a visual (Pillow) layout instead of sequential embeds
    VISUAL_SPREADS = {"celtic_cross", "horseshoe", "relationship", "two_paths", "elemental", "week", "year"}

    def draw_spread(
        self,
        spread_type: str = "three",
        engine: Optional[str] = None,  # "Sephirothic", "Qliphothic", or None for mixed
        include_major: bool = True,
    ) -> Dict[str, Any]:
        """
        Draw a tarot spread.

        Spread types:
        - "single": One card
        - "three": Past, Present, Future
        - "five": Self, Challenge, Subconscious, Recent Past, Potential
        - "five_color": One card from each color's suit
        - "celtic_cross": Full 10-card spread
        - "horseshoe": 7-card arc
        - "relationship": 6-card relationship spread
        - "two_paths": 5-7 card decision spread
        - "elemental": 4-card elemental spread
        - "week": 7-card week ahead
        - "year": 13-card year ahead
        - "opposition": One from each engine
        """
        if spread_type == "single":
            num_cards = 1
            positions = ["Focus"]
        elif spread_type == "three":
            num_cards = 3
            positions = ["Past", "Present", "Future"]
        elif spread_type == "five":
            num_cards = 5
            positions = ["Self", "Challenge", "Subconscious", "Recent Past", "Potential"]
        elif spread_type == "five_color":
            return self._draw_five_color_spread()
        elif spread_type == "celtic_cross":
            num_cards = 10
            positions = [
                "Present", "Challenge", "Foundation", "Past",
                "Crown", "Future", "Self", "Environment",
                "Hopes/Fears", "Outcome"
            ]
        elif spread_type == "horseshoe":
            num_cards = 7
            positions = [
                "Past", "Present", "Hidden Influences", "Obstacles",
                "Environment", "Action", "Outcome"
            ]
        elif spread_type == "relationship":
            num_cards = 6
            positions = [
                "You", "Them", "The Connection",
                "Challenges", "Strengths", "Advice"
            ]
        elif spread_type == "two_paths":
            num_cards = 7
            positions = [
                "Significator",
                "Path A: Nature", "Path A: Challenge", "Path A: Outcome",
                "Path B: Nature", "Path B: Challenge", "Path B: Outcome"
            ]
        elif spread_type == "elemental":
            num_cards = 4
            positions = ["Earth (Material)", "Air (Mental)", "Fire (Passion)", "Water (Emotional)"]
        elif spread_type == "week":
            num_cards = 7
            positions = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        elif spread_type == "year":
            num_cards = 13
            positions = [
                "Theme", "January", "February", "March", "April",
                "May", "June", "July", "August", "September",
                "October", "November", "December"
            ]
        elif spread_type == "opposition":
            return self._draw_opposition_spread()
        else:
            num_cards = 3
            positions = ["Past", "Present", "Future"]
        
        # Build card pool based on engine preference
        if engine == "Sephirothic":
            pool = [(s, p, "Sephirothic") for s, cards in MINOR_SEPHIROTHIC.items() for p in cards.keys()]
            if include_major:
                pool += [(None, p, "Major_Sephirothic") for p in MAJOR_SEPHIROTHIC.keys()]
        elif engine == "Qliphothic":
            pool = [(s, p, "Qliphothic") for s, cards in MINOR_QLIPHOTHIC.items() for p in cards.keys()]
            if include_major:
                pool += [(None, p, "Major_Qliphothic") for p in MAJOR_QLIPHOTHIC.keys()]
        else:
            pool = self._all_minor_cards.copy()
            if include_major:
                pool += [(None, p, "Major_Sephirothic") for p in MAJOR_SEPHIROTHIC.keys()]
                pool += [(None, p, "Major_Qliphothic") for p in MAJOR_QLIPHOTHIC.keys()]
        
        # Draw cards
        drawn = random.sample(pool, min(num_cards, len(pool)))
        
        cards = []
        for i, (suit, pos, eng) in enumerate(drawn):
            card_info = self._get_card_info(suit, pos, eng)
            card_info["position"] = positions[i] if i < len(positions) else f"Card {i+1}"
            card_info["reversed"] = random.random() < 0.3  # 30% chance reversed
            cards.append(card_info)
        
        return {
            "spread_type": spread_type,
            "engine": engine or "Mixed",
            "cards": cards,
        }
    
    def _draw_five_color_spread(self) -> Dict[str, Any]:
        """Draw one card from each color's primary suit."""
        color_suits = {
            "White": ("Edicts", MINOR_SEPHIROTHIC),
            "Blue": ("Secrets", MINOR_SEPHIROTHIC),
            "Black": ("Revelry", MINOR_SEPHIROTHIC),
            "Red": ("Wilds", MINOR_SEPHIROTHIC),
            "Green": ("Groves", MINOR_SEPHIROTHIC),
        }
        
        cards = []
        for color, (suit, source) in color_suits.items():
            pos = random.choice(list(source[suit].keys()))
            card_info = self._get_card_info(suit, pos, "Sephirothic")
            card_info["position"] = f"{color} Aspect"
            card_info["reversed"] = random.random() < 0.3
            cards.append(card_info)
        
        return {
            "spread_type": "five_color",
            "engine": "Sephirothic",
            "cards": cards,
        }
    
    def _draw_opposition_spread(self) -> Dict[str, Any]:
        """Draw one card from each engine to show light and shadow."""
        # Pick a random color pair
        pairs = [("Edicts", "Debts"), ("Secrets", "Sparks"), ("Revelry", "Rot"), 
                 ("Wilds", "Crusades"), ("Groves", "Grafts")]
        seph_suit, qlip_suit = random.choice(pairs)
        
        seph_pos = random.choice(list(MINOR_SEPHIROTHIC[seph_suit].keys()))
        qlip_pos = random.choice(list(MINOR_QLIPHOTHIC[qlip_suit].keys()))
        
        seph_card = self._get_card_info(seph_suit, seph_pos, "Sephirothic")
        seph_card["position"] = "Light (Harmony)"
        seph_card["reversed"] = random.random() < 0.3
        
        qlip_card = self._get_card_info(qlip_suit, qlip_pos, "Qliphothic")
        qlip_card["position"] = "Shadow (Tension)"
        qlip_card["reversed"] = random.random() < 0.3
        
        return {
            "spread_type": "opposition",
            "engine": "Both",
            "cards": [seph_card, qlip_card],
        }
    
    def _get_card_info(self, suit: Optional[str], position: int, engine: str) -> Dict:
        """Get full information about a card."""
        if engine == "Major_Sephirothic":
            mtg_card = MAJOR_SEPHIROTHIC[position]
            return {
                "tarot_name": f"Major Arcana {position}",
                "suit": "Major Arcana",
                "position_num": position,
                "engine": "Sephirothic",
                "mtg_card": mtg_card,
                "energy": "Universal archetype",
                "theme": "Transcendence",
                "emoji": "✨",
                "embed_color": 0xC0C0C0,  # Silver for colorless
            }
        elif engine == "Major_Qliphothic":
            mtg_card = MAJOR_QLIPHOTHIC[position]
            # Determine which color path
            if position < 5:
                color_theme = "White Path"
            elif position < 10:
                color_theme = "Blue Path"
            elif position < 14:
                color_theme = "Black Path"
            elif position < 18:
                color_theme = "Red Path"
            else:
                color_theme = "Green Path"
            return {
                "tarot_name": f"Major Arcana {position}",
                "suit": "Major Arcana",
                "position_num": position,
                "engine": "Qliphothic",
                "mtg_card": mtg_card,
                "energy": color_theme,
                "theme": "Shadow journey",
                "emoji": "🌑",
                "embed_color": 0x2F2F2F,  # Dark for shadow
            }
        else:
            # Minor arcana
            source = MINOR_SEPHIROTHIC if engine == "Sephirothic" else MINOR_QLIPHOTHIC
            mtg_card = source[suit][position]
            suit_info = SUIT_INFO[suit]
            
            # Get position name
            if position <= 10:
                pos_name = POSITION_NAMES[position]
            else:
                titles = FACE_TITLES_SEPHIROTHIC if engine == "Sephirothic" else FACE_TITLES_QLIPHOTHIC
                pos_name = titles[position]
            
            return {
                "tarot_name": f"{pos_name} of {suit}",
                "suit": suit,
                "position_num": position,
                "engine": engine,
                "mtg_card": mtg_card,
                "energy": suit_info["energy"],
                "theme": suit_info["theme"],
                "emoji": suit_info["emoji"],
                "embed_color": suit_info["embed_color"],
            }
    
    async def create_visual_reading(self, reading: Dict) -> List[discord.Embed]:
        """Create Discord embeds with card images for a reading."""
        embeds = []
        
        for card in reading["cards"]:
            embed = await self._create_card_embed(card)
            if embed:
                embeds.append(embed)
        
        return embeds
    
    async def _create_card_embed(self, card: Dict) -> discord.Embed:
        """Create a Discord embed for a single tarot card."""
        reversed_str = " (Reversed)" if card.get("reversed") else ""
        
        embed = discord.Embed(
            title=f"{card['emoji']} {card['position']}: {card['tarot_name']}{reversed_str}",
            description=f"**MTG Card:** {card['mtg_card']}",
            color=card["embed_color"],
        )
        
        embed.add_field(name="Engine", value=card["engine"], inline=True)
        embed.add_field(name="Energy", value=card["energy"], inline=True)
        
        if card.get("reversed"):
            embed.add_field(
                name="Reversed Meaning",
                value="The shadow of this energy — blocked, excessive, or inverted.",
                inline=False,
            )
        
        # Fetch card image
        scryfall_data = await self.scryfall.get_card(card["mtg_card"])
        if scryfall_data:
            image_url = self.scryfall.get_image_url(scryfall_data)
            if image_url:
                embed.set_thumbnail(url=image_url)
            
            # Add Scryfall link
            if "scryfall_uri" in scryfall_data:
                embed.add_field(
                    name="Card Link",
                    value=f"[View on Scryfall]({scryfall_data['scryfall_uri']})",
                    inline=False,
                )
        
        return embed
    
    def format_text_reading(self, reading: Dict) -> str:
        """Format a reading as plain text (for non-Discord use)."""
        lines = [
            f"**{reading['spread_type'].title()} Reading** ({reading['engine']} Engine)",
            "",
        ]
        
        for card in reading["cards"]:
            reversed_str = " ↺" if card.get("reversed") else ""
            lines.append(f"{card['emoji']} **{card['position']}**: {card['tarot_name']}{reversed_str}")
            lines.append(f"   MTG: *{card['mtg_card']}*")
            lines.append(f"   Energy: {card['energy']}")
            lines.append("")
        
        return "\n".join(lines)

    # =================================================================
    # PILLOW-BASED VISUAL SPREAD RENDERER
    # =================================================================

    async def create_spread_image(self, reading: Dict) -> Optional[io.BytesIO]:
        """
        Render a tarot spread as a composite Pillow image.
        Returns a BytesIO with the PNG, or None if PIL unavailable.

        Used for non-linear layouts (Celtic Cross, Horseshoe, etc.)
        that don't work well as sequential Discord embeds.
        """
        if not HAS_PIL:
            return None

        spread_type = reading["spread_type"]
        cards = reading["cards"]

        # Fetch all card images concurrently
        card_images = await self._fetch_card_images(cards)

        # Route to layout function
        layout_fn = {
            "celtic_cross": self._layout_celtic_cross,
            "horseshoe": self._layout_horseshoe,
            "relationship": self._layout_relationship,
            "two_paths": self._layout_two_paths,
            "elemental": self._layout_elemental,
            "week": self._layout_week,
            "year": self._layout_year,
        }.get(spread_type)

        if not layout_fn:
            return None

        img = layout_fn(cards, card_images)

        buf = io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        return buf

    # ---- shared constants for the renderer ----
    _CARD_W = 244
    _CARD_H = 340
    _PAD = 24
    _LABEL_H = 56  # space below each card for the label
    _BG = (25, 20, 30)            # deep purple-black
    _LABEL_COLOR = (220, 215, 225)
    _ACCENT = (180, 140, 255)      # soft purple accent
    _DIM = (100, 90, 110)          # dimmed text
    _REVERSED_TINT = (60, 20, 20)  # reddish overlay for reversed

    async def _fetch_card_images(self, cards: List[Dict]) -> List[Optional[Image.Image]]:
        """Fetch card art from Scryfall for each card, return list of PIL images."""
        cache_dir = Path(__file__).parent.parent / "data" / "card_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        async def fetch_one(card: Dict) -> Optional[Image.Image]:
            name = card["mtg_card"]
            safe = hashlib.md5(name.lower().encode()).hexdigest()
            cache_path = cache_dir / f"{safe}.png"

            if cache_path.exists():
                try:
                    return Image.open(cache_path).convert("RGB")
                except Exception:
                    cache_path.unlink(missing_ok=True)

            try:
                scryfall_data = await self.scryfall.get_card(name)
                if not scryfall_data:
                    return None
                image_url = self.scryfall.get_image_url(scryfall_data, "large")
                if not image_url:
                    return None

                async with aiohttp.ClientSession() as session:
                    async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status != 200:
                            return None
                        data = await resp.read()

                img = Image.open(io.BytesIO(data)).convert("RGB")
                img.save(cache_path, "PNG")
                return img
            except Exception as e:
                print(f"[TAROT-IMG] Failed to fetch {name}: {e}")
                return None

        # Fetch concurrently with a small delay to respect Scryfall rate limits
        results = []
        for card in cards:
            img = await fetch_one(card)
            results.append(img)
            await asyncio.sleep(0.08)
        return results

    def _get_font(self, size: int = 16) -> ImageFont.FreeTypeFont:
        """Get a font, falling back to default if needed."""
        # Try common system fonts
        for name in ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                      "C:/Windows/Fonts/arial.ttf"]:
            try:
                return ImageFont.truetype(name, size)
            except (OSError, IOError):
                continue
        return ImageFont.load_default()

    def _draw_card(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        card: Dict,
        card_img: Optional[Image.Image],
        x: int, y: int,
        font: ImageFont.FreeTypeFont,
        small_font: ImageFont.FreeTypeFont,
        rotated: bool = False,
    ):
        """Draw a single card at (x, y) with label below."""
        cw, ch = self._CARD_W, self._CARD_H
        if rotated:
            cw, ch = ch, cw  # swap for sideways card

        if card_img:
            resized = card_img.resize((cw, ch), Image.LANCZOS)
            if card.get("reversed") and not rotated:
                resized = resized.rotate(180)
            canvas.paste(resized, (x, y))
        else:
            # Placeholder rectangle
            draw.rectangle([x, y, x + cw, y + ch], fill=(50, 40, 60), outline=self._ACCENT)
            draw.text((x + 5, y + ch // 2 - 8), card["mtg_card"][:18], fill=self._LABEL_COLOR, font=small_font)

        # Reversed indicator
        if card.get("reversed"):
            rev_overlay = Image.new("RGBA", (cw, ch), (*self._REVERSED_TINT, 60))
            canvas.paste(Image.alpha_composite(
                canvas.crop((x, y, x + cw, y + ch)).convert("RGBA"),
                rev_overlay
            ).convert("RGB"), (x, y))

        # Position label below card
        label = card["position"]
        if card.get("reversed"):
            label += " (R)"
        # Center label under card
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        lx = x + (cw - tw) // 2
        draw.text((lx, y + ch + 5), label, fill=self._LABEL_COLOR, font=font)

        # Card name in smaller text
        name = card["tarot_name"]
        bbox2 = draw.textbbox((0, 0), name, font=small_font)
        tw2 = bbox2[2] - bbox2[0]
        nx = x + (cw - tw2) // 2
        draw.text((nx, y + ch + 28), name, fill=self._DIM, font=small_font)

    def _make_canvas(self, width: int, height: int, title: str) -> Tuple[Image.Image, ImageDraw.ImageDraw]:
        """Create a canvas with background and title."""
        img = Image.new("RGB", (width, height), self._BG)
        draw = ImageDraw.Draw(img)
        title_font = self._get_font(28)
        bbox = draw.textbbox((0, 0), title, font=title_font)
        tw = bbox[2] - bbox[0]
        draw.text(((width - tw) // 2, 10), title, fill=self._ACCENT, font=title_font)
        return img, draw

    # ---- CELTIC CROSS LAYOUT ----
    def _layout_celtic_cross(self, cards: List[Dict], images: List[Optional[Image.Image]]) -> Image.Image:
        """
        Classic Celtic Cross layout:

             [Crown]
        [Past] [Present+Challenge] [Future]
             [Foundation]
                                   [Self]
                                   [Environment]
                                   [Hopes/Fears]
                                   [Outcome]

        Challenge card is rotated sideways and overlaid on Present.
        """
        P = self._PAD
        CW, CH = self._CARD_W, self._CARD_H
        LH = self._LABEL_H

        # Cross section: 3 columns of cards + gaps, centered vertically
        cross_w = CW * 3 + P * 4
        cross_h = CH * 3 + LH * 3 + P * 4

        # Staff section: 4 cards stacked vertically on the right
        staff_w = CW + P * 2
        staff_h = (CH + LH + P) * 4 + P

        canvas_w = cross_w + staff_w + P * 2
        canvas_h = max(cross_h, staff_h) + 50  # 50 for title
        img, draw = self._make_canvas(canvas_w, canvas_h, "Celtic Cross Spread")
        font = self._get_font(18)
        small = self._get_font(14)

        # Card positions:
        # 0=Present, 1=Challenge(sideways), 2=Foundation, 3=Past, 4=Crown, 5=Future
        # 6=Self, 7=Environment, 8=Hopes/Fears, 9=Outcome
        top = 50
        cx = P + CW + P  # center column x (for cross)
        cy = top + CH + LH + P  # center row y

        # Present (center)
        self._draw_card(img, draw, cards[0], images[0], cx, cy, font, small)

        # Challenge (rotated, overlaid on present)
        if images[1]:
            rotated = images[1].resize((self._CARD_W, self._CARD_H), Image.LANCZOS).rotate(90, expand=True)
            # Center the rotated card on top of Present
            rx = cx + (CW - rotated.width) // 2
            ry = cy + (CH - rotated.height) // 2
            img.paste(rotated, (rx, ry))
            # Label to the side
            draw.text((cx + CW + 4, cy + CH // 2 - 8), "Challenge", fill=self._LABEL_COLOR, font=small)
            if cards[1].get("reversed"):
                draw.text((cx + CW + 4, cy + CH // 2 + 6), "(R)", fill=self._DIM, font=small)
        else:
            draw.text((cx + CW + 4, cy + CH // 2), "Challenge", fill=self._LABEL_COLOR, font=small)

        # Foundation (below center)
        self._draw_card(img, draw, cards[2], images[2], cx, cy + CH + LH + P, font, small)

        # Past (left of center)
        self._draw_card(img, draw, cards[3], images[3], P, cy, font, small)

        # Crown (above center)
        self._draw_card(img, draw, cards[4], images[4], cx, top, font, small)

        # Future (right of center)
        self._draw_card(img, draw, cards[5], images[5], cx + CW + P, cy, font, small)

        # Staff (right column, bottom to top: Self, Env, Hopes, Outcome)
        staff_x = cross_w + P
        staff_cards = [6, 7, 8, 9]
        for i, ci in enumerate(reversed(staff_cards)):
            sy = top + i * (CH + LH + P)
            self._draw_card(img, draw, cards[ci], images[ci], staff_x, sy, font, small)

        return img

    # ---- HORSESHOE LAYOUT ----
    def _layout_horseshoe(self, cards: List[Dict], images: List[Optional[Image.Image]]) -> Image.Image:
        """
        Horseshoe / arc layout (7 cards):

          [1]                 [7]
            [2]             [6]
              [3]   [4]   [5]
        """
        P = self._PAD
        CW, CH = self._CARD_W, self._CARD_H
        LH = self._LABEL_H

        canvas_w = CW * 5 + P * 6
        canvas_h = CH * 2 + LH * 2 + P * 4 + 50
        img, draw = self._make_canvas(canvas_w, canvas_h, "Horseshoe Spread")
        font = self._get_font(18)
        small = self._get_font(14)

        # Position mapping: arc from left-top to right-top with bottom center
        positions = [
            (P, 50),                                          # 0: Past (top-left)
            (P + CW // 2 + P, 50 + CH // 2 + P),            # 1: Present
            (P + CW + P, 50 + CH + LH + P),                  # 2: Hidden Influences
            (canvas_w // 2 - CW // 2, 50 + CH + LH + P),    # 3: Obstacles (bottom center)
            (canvas_w - P * 2 - CW * 2, 50 + CH + LH + P),  # 4: Environment
            (canvas_w - P - CW - CW // 2 - P, 50 + CH // 2 + P),  # 5: Action
            (canvas_w - P - CW, 50),                          # 6: Outcome (top-right)
        ]

        for i, (x, y) in enumerate(positions):
            self._draw_card(img, draw, cards[i], images[i], x, y, font, small)

        return img

    # ---- RELATIONSHIP LAYOUT ----
    def _layout_relationship(self, cards: List[Dict], images: List[Optional[Image.Image]]) -> Image.Image:
        """
        Relationship spread (6 cards):

        [You]    [Connection]    [Them]
        [Challenges] [Strengths] [Advice]
        """
        P = self._PAD
        CW, CH = self._CARD_W, self._CARD_H
        LH = self._LABEL_H

        canvas_w = CW * 3 + P * 4
        canvas_h = (CH + LH) * 2 + P * 4 + 50
        img, draw = self._make_canvas(canvas_w, canvas_h, "Relationship Spread")
        font = self._get_font(18)
        small = self._get_font(14)

        col = [P, P + CW + P, P + (CW + P) * 2]
        row1 = 50 + P
        row2 = row1 + CH + LH + P

        # Row 1: You, Connection, Them
        order_top = [0, 2, 1]  # You | Connection | Them
        for i, ci in enumerate(order_top):
            self._draw_card(img, draw, cards[ci], images[ci], col[i], row1, font, small)

        # Row 2: Challenges, Strengths, Advice
        order_bot = [3, 4, 5]
        for i, ci in enumerate(order_bot):
            self._draw_card(img, draw, cards[ci], images[ci], col[i], row2, font, small)

        # Draw a connecting line between You and Them through Connection
        mid_y = row1 + CH // 2
        draw.line([(col[0] + CW, mid_y), (col[2], mid_y)], fill=self._ACCENT, width=2)

        return img

    # ---- TWO PATHS LAYOUT ----
    def _layout_two_paths(self, cards: List[Dict], images: List[Optional[Image.Image]]) -> Image.Image:
        """
        Two Paths / decision spread (7 cards):

              [Significator]
           /                 \\
        [A Nature]        [B Nature]
        [A Challenge]     [B Challenge]
        [A Outcome]       [B Outcome]
        """
        P = self._PAD
        CW, CH = self._CARD_W, self._CARD_H
        LH = self._LABEL_H

        canvas_w = CW * 2 + P * 4
        canvas_h = (CH + LH) * 4 + P * 5 + 50
        img, draw = self._make_canvas(canvas_w, canvas_h, "Two Paths Spread")
        font = self._get_font(18)
        small = self._get_font(14)

        # Significator centered at top
        sig_x = canvas_w // 2 - CW // 2
        sig_y = 50 + P
        self._draw_card(img, draw, cards[0], images[0], sig_x, sig_y, font, small)

        # Path labels
        path_a_x = P
        path_b_x = canvas_w - P - CW
        path_start_y = sig_y + CH + LH + P * 2

        # Draw path labels
        draw.text((path_a_x + CW // 2 - 20, path_start_y - 18), "Path A", fill=self._ACCENT, font=font)
        draw.text((path_b_x + CW // 2 - 20, path_start_y - 18), "Path B", fill=self._ACCENT, font=font)

        # Forking lines from significator
        fork_y = sig_y + CH + LH
        draw.line([(sig_x + CW // 2, fork_y), (path_a_x + CW // 2, path_start_y)], fill=self._ACCENT, width=2)
        draw.line([(sig_x + CW // 2, fork_y), (path_b_x + CW // 2, path_start_y)], fill=self._ACCENT, width=2)

        # Path A: cards 1, 2, 3
        for i in range(3):
            y = path_start_y + i * (CH + LH + P)
            self._draw_card(img, draw, cards[1 + i], images[1 + i], path_a_x, y, font, small)

        # Path B: cards 4, 5, 6
        for i in range(3):
            y = path_start_y + i * (CH + LH + P)
            self._draw_card(img, draw, cards[4 + i], images[4 + i], path_b_x, y, font, small)

        return img

    # ---- ELEMENTAL LAYOUT ----
    def _layout_elemental(self, cards: List[Dict], images: List[Optional[Image.Image]]) -> Image.Image:
        """
        Elemental spread (4 cards) arranged in a compass:

              [Air]
        [Water]     [Fire]
             [Earth]
        """
        P = self._PAD
        CW, CH = self._CARD_W, self._CARD_H
        LH = self._LABEL_H

        canvas_w = CW * 3 + P * 4
        canvas_h = (CH + LH) * 2 + P * 4 + 50
        img, draw = self._make_canvas(canvas_w, canvas_h, "Elemental Spread")
        font = self._get_font(18)
        small = self._get_font(14)

        cx = canvas_w // 2 - CW // 2
        top_y = 50 + P
        mid_y = top_y + (CH + LH + P) // 2
        bot_y = top_y + CH + LH + P

        # Card positions
        fire_x = canvas_w - P - CW
        water_x = P

        # Air (top center)
        self._draw_card(img, draw, cards[1], images[1], cx, top_y, font, small)
        # Water (left)
        self._draw_card(img, draw, cards[3], images[3], water_x, mid_y, font, small)
        # Fire (right)
        self._draw_card(img, draw, cards[2], images[2], fire_x, mid_y, font, small)
        # Earth (bottom center)
        self._draw_card(img, draw, cards[0], images[0], cx, bot_y, font, small)

        # Draw connecting lines (diamond shape between card centers)
        pts = [
            (cx + CW // 2, top_y + CH),              # bottom-center of Air
            (fire_x, mid_y + CH // 2),                # left-center of Fire
            (cx + CW // 2, bot_y),                    # top-center of Earth
            (water_x + CW, mid_y + CH // 2),          # right-center of Water
        ]
        for i in range(4):
            draw.line([pts[i], pts[(i + 1) % 4]], fill=self._ACCENT, width=1)

        return img

    # ---- WEEK AHEAD LAYOUT ----
    def _layout_week(self, cards: List[Dict], images: List[Optional[Image.Image]]) -> Image.Image:
        """
        Week Ahead (7 cards) in a single row.
        """
        P = self._PAD
        CW, CH = self._CARD_W, self._CARD_H
        LH = self._LABEL_H

        canvas_w = CW * 7 + P * 8
        canvas_h = CH + LH + P * 2 + 50
        img, draw = self._make_canvas(canvas_w, canvas_h, "Week Ahead")
        font = self._get_font(17)
        small = self._get_font(13)

        for i in range(7):
            x = P + i * (CW + P)
            self._draw_card(img, draw, cards[i], images[i], x, 50 + P, font, small)

        return img

    # ---- YEAR AHEAD LAYOUT ----
    def _layout_year(self, cards: List[Dict], images: List[Optional[Image.Image]]) -> Image.Image:
        """
        Year Ahead (13 cards):
        Theme card centered on top, then 12 month cards in 2 rows of 6.
        """
        P = self._PAD
        CW, CH = self._CARD_W, self._CARD_H
        LH = self._LABEL_H

        canvas_w = CW * 6 + P * 7
        canvas_h = (CH + LH) * 3 + P * 5 + 50
        img, draw = self._make_canvas(canvas_w, canvas_h, "Year Ahead")
        font = self._get_font(17)
        small = self._get_font(13)

        # Theme card centered
        theme_x = canvas_w // 2 - CW // 2
        theme_y = 50 + P
        self._draw_card(img, draw, cards[0], images[0], theme_x, theme_y, font, small)

        # Months: 2 rows of 6
        row1_y = theme_y + CH + LH + P * 2
        row2_y = row1_y + CH + LH + P

        for i in range(6):
            x = P + i * (CW + P)
            self._draw_card(img, draw, cards[1 + i], images[1 + i], x, row1_y, font, small)

        for i in range(6):
            x = P + i * (CW + P)
            self._draw_card(img, draw, cards[7 + i], images[7 + i], x, row2_y, font, small)

        return img


# =============================================================================
# STANDALONE TEST
# =============================================================================

if __name__ == "__main__":
    import asyncio
    
    async def test():
        engine = VisualTarotEngine()
        
        print("=== Three Card Spread (Mixed) ===")
        reading = engine.draw_spread("three")
        print(engine.format_text_reading(reading))
        
        print("\n=== Five Color Spread ===")
        reading = engine.draw_spread("five_color")
        print(engine.format_text_reading(reading))
        
        print("\n=== Opposition Spread ===")
        reading = engine.draw_spread("opposition")
        print(engine.format_text_reading(reading))
        
        print("\n=== Single Card (Qliphothic only) ===")
        reading = engine.draw_spread("single", engine="Qliphothic")
        print(engine.format_text_reading(reading))
    
    asyncio.run(test())
