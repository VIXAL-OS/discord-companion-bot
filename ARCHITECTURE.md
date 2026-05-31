# ARCHITECTURE

Contributor-facing technical overview of `discord-companion-bot`. Read this before working on the support / memory / persona systems. For setup / how-to-use, see [README.md](README.md).

## Project layout

```
discord-companion-bot/
├── bot.py                  # Discord bot + all subsystems (distress, memory, tarot, persona, chat)
├── youtube_transcribe.py   # Whisper-based YouTube transcription
├── personas/               # Character-layer JSON files (config-swappable)
│   ├── plain.json          # Default (no roleplay)
│   ├── ressapanda.json     # Whimsical red panda example
│   └── README.md
├── rules/                  # Lightweight helpers
│   ├── tarot_visuals.py    # Visual tarot engine (MTG-card-based)
│   └── llm_adapter.py      # OpenAI-compatible adapter (DeepSeek/OpenRouter)
├── memories/               # Per-user JSON (gitignored)
├── docs/tarot/             # Long-form docs on the tarot system
├── config.json.example
└── requirements.txt
```

This repo is the **companion-bot** half of a split. The MTG engine half lives at [`discord-mtg-bot`](https://github.com/VIXAL-OS/discord-mtg-bot). This bot can optionally import the MTG engine — if it's on PYTHONPATH, `bot.py:setup_hook` opt-in loads the `mtg.cog` extension and you get both bots in one process.

## Subsystem map

### 1. Distress detection (3-tier)

Layered so each tier catches what the previous misses:

1. **Keyword detector** (`DistressDetector` class). Fast, signal-weighted dictionary lookup. Runs on every message. Score ≥ `distress_threshold` → switches to Opus + support prompt.
2. **Sub-threshold accumulator** (per-user). Keyword scores below threshold accumulate over a 15-minute rolling window; ≥ 1.0 total triggers proactive support.
3. **Semantic classifier** (`_classify_distress` method). Background Haiku call runs when keywords find nothing but the buffer has recent context. Reads `distress_context` from the user's `memories/<name>.json` if present (optional per-user calibration). Catches indirect distress language: novel metaphors, self-punishment fantasies, subtle self-worth negation.

All three gated on `monitored_users` (config). Empty list = no proactive monitoring; the bot only responds when mentioned, in MTG channel (if MTG engine is loaded), or in threads it owns.

The Haiku classifier prompt has built-in framing for **distinguishing real grievances from anxiety spirals** — important because deploying grounding techniques during a real grievance is invalidating. The system is designed primarily for autistic users where pattern-matching to "spiral" can misfire when the situation actually IS bad.

### 2. Two-tier memory

Per-user storage:

- **Working notes** (`WorkingMemory`) — Claude's scratch space. Up to 10 notes, 48-hour decay. The bot creates these automatically via `[note: key: value]` tags in its responses.
- **Long-term entries** (`LongTermMemory`) — user-controlled. Up to 25 entries. Created via `!remember <text>`, removed via `!forget <pattern>`. Promoted from working notes via `!keep <pattern>`.

Storage: `memories/<name>.json` per user (file name comes from `user_name_map` config). Working notes are persisted alongside long-term entries; the decay is applied on load.

### 3. Persona layer

`personas/<name>.json` files describe the character layer. The bot reads `bot_persona` from `config.json` at startup and composes three system prompts (`build_base_prompt`, `build_support_prompt`, `build_spiral_prompt`) from the persona fields plus built-in capability/behavior text. See [`personas/README.md`](personas/README.md) for the schema.

The persona affects only how the bot **speaks** — name, pronouns, mannerisms, personality traits, comfort actions. All capabilities (memory, distress detection, tarot, transcription) are built into the code and stay the same regardless of persona.

### 4. Tarot engine

`MTGTarotEngine` + `VisualTarotEngine` (in `rules/tarot_visuals.py`). Custom dual-engine spread (Sephirothic + Qliphothic) using Magic: The Gathering's color pie. `!tarot` pulls a 3-card spread; `!interpret` runs an AI interpretation. Not connected to the rest of the bot's logic — purely a fun feature.

### 5. YouTube transcription

`youtube_transcribe.py` — yt-dlp pulls the audio, local Whisper transcribes, Claude summarizes. React 🎙️ to any message with a YouTube URL to summon. The whole flow respects `youtube_allow_age_restricted`, `youtube_max_duration_s`, and `youtube_whisper_model` from config.

### 6. PluralKit handling

When PluralKit detects a system member's proxy tag in a message, it deletes the original and reposts via webhook. The bot's `on_message` handles both events:

1. If it's the user's original (webhook_id is None), sleeps 1.2s and tries to refetch — if the message vanished, PluralKit took it; bail and let the webhook event respond.
2. If it's the webhook proxy, responds immediately.

The `plural_systems` config maps alter display names → system context, injected into the prompt so the bot understands "Alex" and "Sam" might be alters of the same person.

### 7. Optional MTG integration

`bot.py:setup_hook` does:

```python
try:
    await self.load_extension("mtg.cog")
    print("✅ MTG game engine loaded (optional sibling-repo extension)")
except (ImportError, ModuleNotFoundError):
    print("ℹ️  MTG game engine not installed — companion-only mode.")
```

If the sibling `discord-mtg-bot` is installed alongside (or has its `mtg/` package on PYTHONPATH), the MTG game cog auto-loads. Same for `cube_draft`. The companion bot doesn't depend on either — they're pure extensions.

## Console logging tags

```
[SEMANTIC]    Background Haiku distress classifier — score + reason
[ACCUMULATOR] Sub-threshold accumulation triggered (per-user)
[PLURALKIT]   PluralKit proxy detected, dedup behavior
[YOUTUBE]     Transcription pipeline events
[MEMORY]      Working notes / long-term memory operations
```

## Model selection

| Model | Role | Trigger |
|---|---|---|
| Sonnet | Default chat | Anyone, no distress detected |
| Opus | Emotional support | Distress detected (keyword or semantic) |
| Haiku | Background classifier | Every monitored-user message where keywords found nothing |

`!cost` shows lifetime usage; persisted in `data/api_costs.json`.

## Common pitfalls

1. **`monitored_users` is sensitive.** Don't add a user without their explicit consent — the bot will proactively respond to them based on distress signals.
2. **Memory files are user-specific and gitignored.** A `memories/alice.json` file describing what triggers Alice should never enter git. The `.gitignore` already handles this; don't override.
3. **The distress framework assumes English-language signals.** Adding other languages requires expanding `BotConfig.distress_signals` and possibly the Haiku classifier prompt.
4. **The "real grievance vs spiral" framework is the core of `build_spiral_prompt`.** Be careful changing it — getting it wrong (deploying grounding during real grievances, or validating spirals when grounding is needed) defeats the bot's purpose.

## Where to ask questions

- Open a GitHub Discussion for design questions, especially around the distress detection / memory architecture
- Open a GitHub Issue for bug reports
- For sensitive issues (privacy, abuse patterns, etc.), reach out via the maintainer's profile email rather than a public issue
