"""
Data models for the AI RPG Auto-Battler.
Wraps dnd_character objects with agent metadata for n8n integration.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Literal

from dnd_character import Character
from dnd_character.classes import (
    Barbarian,
    Bard,
    Cleric,
    Druid,
    Fighter,
    Monk,
    Paladin,
    Ranger,
    Rogue,
    Sorcerer,
    Warlock,
    Wizard,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "google/gemini-2.0-flash-lite-preview-02-05:free"

FANTASY_NAMES = [
    "Thorin", "Elara", "Grimjaw", "Lyria", "Kael", "Morgath", "Seraphina",
    "Draven", "Isolde", "Fenric", "Thalia", "Orik", "Nyssa", "Balthazar",
    "Rowena", "Zephyr", "Astrid", "Cormac", "Delphine", "Ragnar",
]

CLASS_CONSTRUCTORS = {
    "Barbarian": Barbarian,
    "Bard": Bard,
    "Cleric": Cleric,
    "Druid": Druid,
    "Fighter": Fighter,
    "Monk": Monk,
    "Paladin": Paladin,
    "Ranger": Ranger,
    "Rogue": Rogue,
    "Sorcerer": Sorcerer,
    "Warlock": Warlock,
    "Wizard": Wizard,
}

CLASS_ACTIONS: dict[str, list[str]] = {
    "Barbarian": ["Attack", "Rage", "Reckless Attack", "Dodge"],
    "Bard":      ["Attack", "Cast Spell", "Bardic Inspiration", "Hide"],
    "Cleric":    ["Attack", "Cast Spell", "Channel Divinity", "Heal"],
    "Druid":     ["Attack", "Cast Spell", "Wild Shape", "Hide"],
    "Fighter":   ["Attack", "Second Wind", "Action Surge", "Dodge"],
    "Monk":      ["Attack", "Flurry of Blows", "Dodge", "Dash"],
    "Paladin":   ["Attack", "Cast Spell", "Lay on Hands", "Smite"],
    "Ranger":    ["Attack", "Cast Spell", "Hide", "Track"],
    "Rogue":     ["Attack", "Sneak Attack", "Hide", "Dash"],
    "Sorcerer":  ["Attack", "Cast Spell", "Metamagic", "Dodge"],
    "Warlock":   ["Attack", "Cast Spell", "Eldritch Blast", "Hide"],
    "Wizard":    ["Attack", "Cast Spell", "Arcane Recovery", "Dodge"],
}

# ---------------------------------------------------------------------------
# PlayerAgent
# ---------------------------------------------------------------------------

@dataclass
class PlayerAgent:
    """Wraps a dnd_character.Character with agent/role metadata."""

    character: Character
    model_id: str = DEFAULT_MODEL
    role: Literal["DM", "PLAYER", "ENEMY"] = "PLAYER"
    webhook_url: str = ""

    # Convenience -------------------------------------------------------

    @property
    def name(self) -> str:
        return self.character.name or "Unknown"

    @property
    def class_name(self) -> str:
        return self.character.class_name or "Classless"

    @property
    def hp(self) -> int:
        return self.character.current_hp

    @property
    def max_hp(self) -> int:
        return self.character.max_hp

    @property
    def ac(self) -> int:
        return self.character.armor_class

    @property
    def level(self) -> int:
        return self.character.level

    @property
    def is_alive(self) -> bool:
        return self.character.current_hp > 0

    # Stat helpers ------------------------------------------------------

    def stat_block(self) -> dict:
        c = self.character
        return {
            "hp": c.current_hp,
            "max_hp": c.max_hp,
            "ac": c.armor_class,
            "class": c.class_name,
            "level": c.level,
            "strength": c.strength,
            "dexterity": c.dexterity,
            "constitution": c.constitution,
            "intelligence": c.intelligence,
            "wisdom": c.wisdom,
            "charisma": c.charisma,
        }

    def valid_actions(self) -> list[str]:
        return CLASS_ACTIONS.get(self.class_name, ["Attack", "Dodge", "Hide"])

    # Serialisation for n8n ---------------------------------------------

    def to_n8n_json(
        self,
        *,
        latest_event: str = "",
        history_summary: str = "",
        store_memory: bool = True,
    ) -> dict:
        """Build the payload expected by the n8n webhook."""
        return {
            "role": self.role,
            "model_id": self.model_id,
            "entity_name": self.name,
            "entity_stats": self.stat_block(),
            "latest_event": latest_event,
            "store_memory": store_memory,
            "current_state": {
                "valid_actions": self.valid_actions(),
                "history_summary": history_summary,
            },
        }

    # Mutators ----------------------------------------------------------

    def take_damage(self, amount: int) -> int:
        """Apply damage and return actual damage dealt (clamped to 0 HP)."""
        before = self.character.current_hp
        self.character.current_hp = max(0, before - abs(amount))
        return before - self.character.current_hp

    def heal(self, amount: int) -> int:
        """Heal and return actual HP restored (clamped to max_hp)."""
        before = self.character.current_hp
        self.character.current_hp = min(self.character.max_hp, before + abs(amount))
        return self.character.current_hp - before


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def _pick_unique_name(used: set[str]) -> str:
    available = [n for n in FANTASY_NAMES if n not in used]
    if not available:
        available = FANTASY_NAMES  # fallback: allow dupes
    name = random.choice(available)
    used.add(name)
    return name


def generate_party(
    class_names: list[str] | None = None,
    level: int = 1,
    webhook_url: str = "",
    model_id: str = DEFAULT_MODEL,
) -> list[PlayerAgent]:
    """Create a party of PlayerAgents with random stats."""
    if class_names is None:
        class_names = random.sample(list(CLASS_CONSTRUCTORS.keys()), 3)

    used_names: set[str] = set()
    party: list[PlayerAgent] = []

    for cls_name in class_names:
        ctor = CLASS_CONSTRUCTORS[cls_name]
        name = _pick_unique_name(used_names)
        char = ctor(name=name, level=level)
        agent = PlayerAgent(
            character=char,
            model_id=model_id,
            role="PLAYER",
            webhook_url=webhook_url,
        )
        party.append(agent)

    return party


def create_dm_agent(
    webhook_url: str = "",
    model_id: str = DEFAULT_MODEL,
) -> PlayerAgent:
    """Create a DM agent (no real character sheet needed)."""
    dm_char = Character(name="Dungeon Master")
    return PlayerAgent(
        character=dm_char,
        model_id=model_id,
        role="DM",
        webhook_url=webhook_url,
    )
