"""Lightweight helper modules used by the companion bot.

After the OSS split, this package only retains two helpers:

- ``tarot_visuals`` — visual tarot engine (used by the tarot reading commands)
- ``llm_adapter`` — OpenAI-compatible adapter for non-Anthropic models
  (DeepSeek, OpenRouter)

The full MTG rules engine that used to live here moved to the sibling
`discord-mtg-bot` repo. If the companion bot is deployed alongside the MTG
bot and the MTG package is on ``PYTHONPATH``, MTG functionality can be
opt-in loaded; otherwise the companion bot runs without it.
"""
