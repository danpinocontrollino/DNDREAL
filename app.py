import json
import random
import re

import streamlit as st
import httpx
import time
from dnd_character import Character
from dnd_character.classes import CLASSES

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------
DEFAULT_WEBHOOK = ""
DEFAULT_DM_MODEL = "anthropic/claude-3.5-sonnet"
DEFAULT_PLAYER_MODEL = "google/gemini-2.0-flash-lite-preview-02-05:free"

# Weapon damage dice by class (simplified 5e)
CLASS_DAMAGE_DICE = {
    "Barbarian": ("1d12", 12),  # Greataxe
    "Bard":      ("1d8",   8),  # Rapier
    "Cleric":    ("1d8",   8),  # Warhammer
    "Druid":     ("1d8",   8),  # Scimitar
    "Fighter":   ("1d10", 10),  # Longsword 2H
    "Monk":      ("1d6",   6),  # Unarmed / quarterstaff
    "Paladin":   ("1d10", 10),  # Longsword 2H
    "Ranger":    ("1d8",   8),  # Longbow
    "Rogue":     ("1d6",   6),  # Shortsword
    "Sorcerer":  ("1d10", 10),  # Firebolt
    "Warlock":   ("1d10", 10),  # Eldritch Blast
    "Wizard":    ("1d10", 10),  # Firebolt
}

# Monster templates for DM to spawn
MONSTER_TEMPLATES = {
    "Goblin":     {"hp": 7,  "max_hp": 7,  "ac": 15, "damage": "1d6",  "str_mod": -1, "dex_mod": 2},
    "Skeleton":   {"hp": 13, "max_hp": 13, "ac": 13, "damage": "1d6",  "str_mod": 0,  "dex_mod": 2},
    "Orc":        {"hp": 15, "max_hp": 15, "ac": 13, "damage": "1d12", "str_mod": 3,  "dex_mod": 1},
    "Dire Wolf":  {"hp": 26, "max_hp": 26, "ac": 14, "damage": "2d6",  "str_mod": 3,  "dex_mod": 2},
    "Ogre":       {"hp": 59, "max_hp": 59, "ac": 11, "damage": "2d8",  "str_mod": 4,  "dex_mod": -1},
    "Zombie":     {"hp": 22, "max_hp": 22, "ac": 8,  "damage": "1d6",  "str_mod": 1,  "dex_mod": -2},
    "Bandit":     {"hp": 11, "max_hp": 11, "ac": 12, "damage": "1d8",  "str_mod": 0,  "dex_mod": 1},
    "Troll":      {"hp": 84, "max_hp": 84, "ac": 15, "damage": "2d6",  "str_mod": 4,  "dex_mod": 1},
}


# -----------------------------------------------------------------------------
# DICE ENGINE
# -----------------------------------------------------------------------------
def roll_dice(notation: str) -> tuple[list[int], int]:
    """Roll dice from notation like '2d6', '1d20', '3d8'. Returns (individual_rolls, total)."""
    match = re.match(r"(\d+)d(\d+)", notation.strip())
    if not match:
        return [0], 0
    count, sides = int(match.group(1)), int(match.group(2))
    rolls = [random.randint(1, sides) for _ in range(count)]
    return rolls, sum(rolls)

def roll_d20():
    """Roll a d20 and return the value."""
    return random.randint(1, 20)

def get_ability_modifier(score: int) -> int:
    """D&D 5e ability modifier: (score - 10) // 2"""
    return (score - 10) // 2

def resolve_attack(attacker_name: str, attacker_mod: int, damage_notation: str,
                   target_name: str, target_ac: int) -> dict:
    """Resolve one attack: roll d20 + mod vs AC, then damage if hit."""
    d20 = roll_d20()
    attack_total = d20 + attacker_mod
    is_crit = (d20 == 20)
    is_miss = (d20 == 1)

    result = {
        "attacker": attacker_name,
        "target": target_name,
        "d20": d20,
        "modifier": attacker_mod,
        "attack_total": attack_total,
        "target_ac": target_ac,
        "hit": False,
        "crit": is_crit,
        "fumble": is_miss,
        "damage": 0,
        "damage_rolls": [],
        "damage_notation": damage_notation,
        "narrative": ""
    }

    if is_miss:
        result["narrative"] = (
            f"🎲 **{attacker_name}** rolls d20: **{d20}** (FUMBLE!) → miss!"
        )
        return result

    if is_crit or attack_total >= target_ac:
        result["hit"] = True
        rolls, total = roll_dice(damage_notation)
        if is_crit:
            crit_rolls, crit_total = roll_dice(damage_notation)
            rolls += crit_rolls
            total += crit_total
        result["damage"] = total
        result["damage_rolls"] = rolls
        crit_text = " ⚡ **CRITICAL HIT!**" if is_crit else ""
        result["narrative"] = (
            f"🎲 **{attacker_name}** rolls d20: **{d20}** +{attacker_mod} = "
            f"**{attack_total}** vs AC {target_ac} → **HIT!**{crit_text}\n"
            f"🗡️ Damage ({damage_notation}{'×2' if is_crit else ''}): "
            f"{rolls} = **{total} damage** to {target_name}"
        )
    else:
        result["narrative"] = (
            f"🎲 **{attacker_name}** rolls d20: **{d20}** +{attacker_mod} = "
            f"**{attack_total}** vs AC {target_ac} → miss."
        )

    return result


def parse_and_execute_combat(ai_text: str, agents: list, monsters: dict) -> tuple[str, list[str]]:
    """
    Parse AI narration for attack intents and resolve them with actual dice.
    Returns (modified_text, list_of_dice_log_entries).
    
    Detects patterns like:
      - 'Thorin attacks Goblin 1'
      - 'Goblin 1 attacks Thorin'
      - 'Elara casts a spell at Orc 2'
    """
    dice_log = []

    # Build lookup of all combatants
    combatants = {}
    for a in agents:
        if a.sheet:
            str_mod = get_ability_modifier(a.sheet.strength)
            dex_mod = get_ability_modifier(a.sheet.dexterity)
            class_name = a.sheet.class_name or "Fighter"
            dmg_notation = CLASS_DAMAGE_DICE.get(class_name, ("1d8", 8))[0]
            combatants[a.name.lower()] = {
                "type": "player", "ref": a,
                "mod": max(str_mod, dex_mod),  # Best of STR/DEX
                "damage": dmg_notation,
                "ac": a.sheet.armor_class,
                "hp": a.sheet.current_hp,
            }
    for mname, mdata in monsters.items():
        combatants[mname.lower()] = {
            "type": "monster", "ref_name": mname,
            "mod": max(mdata.get("str_mod", 0), mdata.get("dex_mod", 0)),
            "damage": mdata.get("damage", "1d6"),
            "ac": mdata["ac"],
            "hp": mdata["hp"],
        }

    # Pattern: "X attacks Y" / "X strikes Y" / "X casts ... at Y" / "X shoots Y"
    attack_pattern = re.compile(
        r"(\b[\w\s]+?\b)\s+(?:attacks?|strikes?|swings?\s+at|shoots?\s+(?:an?\s+arrow\s+at\s+)?|"
        r"slashes?\s+at|casts?\s+\w+\s+(?:at|on)|hurls?\s+\w+\s+at|fires?\s+(?:at)?|lunges?\s+at)\s+"
        r"([\w\s]+?)(?:\.|,|!|\n|$)",
        re.IGNORECASE
    )

    matches = attack_pattern.findall(ai_text)
    resolved_attacks = []

    for raw_attacker, raw_target in matches:
        att_key = raw_attacker.strip().lower()
        tgt_key = raw_target.strip().lower()

        # Fuzzy match combatant names
        attacker_info = None
        target_info = None
        for key, info in combatants.items():
            if key in att_key or att_key in key:
                attacker_info = info
            if key in tgt_key or tgt_key in key:
                target_info = info

        if attacker_info and target_info and attacker_info is not target_info:
            att_display = raw_attacker.strip()
            tgt_display = raw_target.strip()
            result = resolve_attack(
                att_display, attacker_info["mod"], attacker_info["damage"],
                tgt_display, target_info["ac"]
            )
            resolved_attacks.append(result)
            dice_log.append(result["narrative"])

            # Apply damage
            if result["hit"]:
                dmg = result["damage"]
                if target_info["type"] == "player":
                    agent_ref = target_info["ref"]
                    agent_ref.sheet.current_hp = max(0, agent_ref.sheet.current_hp - dmg)
                    new_hp = agent_ref.sheet.current_hp
                    max_hp = agent_ref.sheet.max_hp
                    if new_hp == 0:
                        dice_log.append(f"💀 **{tgt_display}** falls unconscious! (0/{max_hp} HP)")
                    else:
                        dice_log.append(f"❤️ {tgt_display}: {new_hp}/{max_hp} HP remaining")
                elif target_info["type"] == "monster":
                    ref_name = target_info["ref_name"]
                    monsters[ref_name]["hp"] = max(0, monsters[ref_name]["hp"] - dmg)
                    new_hp = monsters[ref_name]["hp"]
                    max_hp = monsters[ref_name]["max_hp"]
                    if new_hp == 0:
                        dice_log.append(f"☠️ **{ref_name}** is slain!")
                    else:
                        dice_log.append(f"🩸 {ref_name}: {new_hp}/{max_hp} HP remaining")

    return ai_text, dice_log


def parse_monster_spawns(ai_text: str, monsters: dict):
    """Detect when the DM introduces monsters and add them to the tracker."""
    for template_name, stats in MONSTER_TEMPLATES.items():
        # Match "two goblins", "3 orcs", "a skeleton", "the dire wolf" etc.
        patterns = [
            rf"(\d+)\s+{template_name.lower()}s?",
            rf"(a|an|the)\s+{template_name.lower()}",
            rf"(two|three|four|five)\s+{template_name.lower()}s?",
        ]
        word_to_num = {"a": 1, "an": 1, "the": 1, "two": 2, "three": 3, "four": 4, "five": 5}

        for pat in patterns:
            found = re.findall(pat, ai_text, re.IGNORECASE)
            for match in found:
                if match.isdigit():
                    count = int(match)
                else:
                    count = word_to_num.get(match.lower(), 1)

                for i in range(1, count + 1):
                    mname = f"{template_name} {i}" if count > 1 else template_name
                    if mname not in monsters:
                        monsters[mname] = dict(stats)  # Copy template


# -----------------------------------------------------------------------------
# CLASSES
# -----------------------------------------------------------------------------
class Agent:
    def __init__(self, name, role, model_key, char_class=None, is_human=False):
        self.name = name
        self.role = role          # "DM" or "PLAYER"
        self.model_key = model_key
        self.is_human = is_human  # True = waits for typed input

        # Integrate dnd-character library for Stats
        if role == "PLAYER" and char_class:
            self.sheet = Character(name=name, classs=CLASSES[char_class], level=1)
        else:
            self.sheet = None

    def get_stats_json(self):
        if not self.sheet:
            return {}
        return {
            "hp": self.sheet.current_hp,
            "max_hp": self.sheet.max_hp,
            "ac": self.sheet.armor_class,
            "str": self.sheet.strength,
            "dex": self.sheet.dexterity,
            "con": self.sheet.constitution,
            "wis": self.sheet.wisdom,
            "int": self.sheet.intelligence,
            "cha": self.sheet.charisma,
            "class": self.sheet.class_name,
        }


# -----------------------------------------------------------------------------
# APP LOGIC
# -----------------------------------------------------------------------------
st.set_page_config(page_title="⚔️ AI D&D Arena", layout="wide")

# Initialise session defaults
for key, default in [
    ("initialized", False),
    ("agents", []),
    ("game_log", []),
    ("turn_idx", 0),
    ("adventure_context", ""),
    ("monsters", {}),
    ("auto_play", False),
    ("waiting_for_human", False),
    ("human_agent_idx", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ─── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Configuration")
    webhook_url = st.text_input("n8n Webhook URL", value=DEFAULT_WEBHOOK)

    st.divider()
    # ── DM Setup ──
    st.subheader("🧙 Dungeon Master")
    dm_is_human = st.checkbox("I am the DM (human)", key="dm_human")
    dm_model = st.text_input(
        "DM Model (OpenRouter ID)", value=DEFAULT_DM_MODEL,
        placeholder="e.g. anthropic/claude-3.5-sonnet",
        disabled=dm_is_human
    )

    st.divider()
    # ── Players Setup ──
    st.subheader("🎭 Players")
    num_players = st.number_input("Number of players", min_value=1, max_value=8, value=2, step=1)

    player_configs = []
    class_list = list(CLASSES.keys())
    for i in range(int(num_players)):
        with st.expander(f"Player {i+1}", expanded=(i < 2)):
            is_human = st.checkbox("Human player", key=f"p{i}_human")
            pname = st.text_input("Name", value=f"Player {i+1}", key=f"p{i}_name")
            pclass = st.selectbox("Class", options=class_list, index=i % len(class_list), key=f"p{i}_class")
            pmodel = st.text_input(
                "Model (OpenRouter ID)", value=DEFAULT_PLAYER_MODEL,
                key=f"p{i}_model", disabled=is_human
            )
            player_configs.append({
                "name": pname, "cls": pclass, "model": pmodel, "human": is_human
            })

    st.divider()
    st.subheader("📜 Adventure Setup")
    adventure_context = st.text_area(
        "Adventure Premise",
        value=(
            "The party meets at the Rusty Dragon tavern in a small village on the edge of a dark forest. "
            "Rumors speak of an ancient tomb recently uncovered by a landslide, filled with treasure and danger. "
            "A mysterious hooded stranger approaches the party with a map and a warning."
        ),
        height=120,
        help="Describe the starting scenario. The DM AI will use this as the foundation."
    )

    if st.button("🎲 Initialize Game", type="primary"):
        agents_list = [
            Agent("Dungeon Master", "DM", dm_model if not dm_is_human else "", is_human=dm_is_human)
        ]
        for pc in player_configs:
            agents_list.append(
                Agent(pc["name"], "PLAYER", pc["model"] if not pc["human"] else "",
                      char_class=pc["cls"], is_human=pc["human"])
            )
        st.session_state.agents = agents_list
        st.session_state.game_log = []
        st.session_state.turn_idx = 0
        st.session_state.adventure_context = adventure_context
        st.session_state.monsters = {}
        st.session_state.auto_play = False
        st.session_state.waiting_for_human = False
        st.session_state.human_agent_idx = None
        st.session_state.initialized = True
        st.rerun()

# Check Initialization
if not st.session_state.initialized:
    st.info("👈 Set up your agents and click **Initialize Game** in the sidebar.")
    st.stop()

# ─── Main Area ────────────────────────────────────────────────────────────────
st.title("⚔️ AI D&D Arena")

# ── Status Bar: Players & Monsters ──
agents = st.session_state.agents
monsters = st.session_state.monsters

# Player cards
st.subheader("🎭 Party")
player_agents = [a for a in agents if a.role == "PLAYER"]
pcols = st.columns(max(len(player_agents), 1))
for i, agent in enumerate(player_agents):
    with pcols[i]:
        label = "🧑 Human" if agent.is_human else f"🤖 {agent.model_key[:25]}"
        st.markdown(f"**{agent.name}** ({agent.sheet.class_name})")
        st.caption(label)
        if agent.sheet:
            hp_pct = max(0.0, agent.sheet.current_hp / agent.sheet.max_hp)
            st.progress(hp_pct)
            st.text(f"HP: {agent.sheet.current_hp}/{agent.sheet.max_hp} | AC: {agent.sheet.armor_class}")

# Monster cards (if any)
if monsters:
    st.subheader("👹 Monsters")
    live_monsters = {k: v for k, v in monsters.items() if v["hp"] > 0}
    if live_monsters:
        mcols = st.columns(min(len(live_monsters), 4))
        for i, (mname, mdata) in enumerate(live_monsters.items()):
            with mcols[i % len(mcols)]:
                hp_pct = max(0.0, mdata["hp"] / mdata["max_hp"])
                st.markdown(f"**{mname}**")
                st.progress(hp_pct)
                st.text(f"HP: {mdata['hp']}/{mdata['max_hp']} | AC: {mdata['ac']}")

st.divider()

# ── Chat Log ──
chat_container = st.container(height=450)
for msg in st.session_state.game_log:
    with chat_container:
        icon = "🎲" if msg.get("is_dice") else ("assistant" if msg["role"] == "assistant" else "user")
        with st.chat_message(icon if isinstance(icon, str) else icon):
            st.markdown(f"**{msg['name']}:** {msg['content']}")

st.divider()

# ─── Turn Logic ───────────────────────────────────────────────────────────────

def build_history_messages(game_log, max_messages=20):
    recent = game_log[-max_messages:] if len(game_log) > max_messages else game_log
    return [{"name": m["name"], "role": m["role"], "content": m["content"]} for m in recent]


def get_current_agent():
    return agents[st.session_state.turn_idx % len(agents)]


def build_payload(current_agent, human_text=None):
    adventure = st.session_state.get("adventure_context", "A D&D adventure begins.")
    history = build_history_messages(st.session_state.game_log)

    if human_text:
        latest_input = human_text
    elif st.session_state.game_log:
        latest_input = st.session_state.game_log[-1]["content"]
    else:
        latest_input = (
            "Begin the adventure! Set the scene, describe the environment vividly, "
            "and introduce a hook that draws the players in. "
            "Address the player characters by name and involve them."
        )

    # Party stats
    party_info = []
    for a in agents:
        if a.sheet:
            party_info.append({
                "name": a.name, "class": a.sheet.class_name,
                "hp": a.sheet.current_hp, "max_hp": a.sheet.max_hp,
                "ac": a.sheet.armor_class,
            })

    # Monster stats for DM awareness
    monster_info = []
    for mname, mdata in monsters.items():
        if mdata["hp"] > 0:
            monster_info.append({
                "name": mname, "hp": mdata["hp"], "max_hp": mdata["max_hp"],
                "ac": mdata["ac"], "damage": mdata["damage"],
            })

    return {
        "role": current_agent.role,
        "model_id": current_agent.model_key,
        "char_name": current_agent.name,
        "char_class": current_agent.sheet.class_name if current_agent.sheet else "DM",
        "stats": current_agent.get_stats_json(),
        "adventure_context": adventure,
        "party_info": party_info,
        "monster_info": monster_info,
        "conversation_history": history,
        "latest_input": latest_input,
    }


def call_n8n(payload):
    """Send payload to n8n and return content string."""
    try:
        response = httpx.post(webhook_url, json=payload, timeout=90)
        if response.status_code == 200:
            try:
                data = response.json()
                content = data.get("content", "") or data.get("message", "")
                if not content:
                    content = f"⚠️ n8n returned JSON but no 'content' key. Keys: {list(data.keys())}. Data: {str(data)[:300]}"
            except (json.JSONDecodeError, ValueError):
                raw = response.text[:500]
                ct = response.headers.get("content-type", "unknown")
                content = f"⚠️ n8n returned non-JSON (content-type: {ct}): {raw}" if raw.strip() else (
                    f"⚠️ n8n returned an empty response. Check the n8n execution log."
                )
        else:
            content = f"⚠️ n8n error {response.status_code}: {response.text[:300]}"
    except httpx.TimeoutException:
        content = "⏱️ Request timed out — is the n8n workflow active?"
    except httpx.ConnectError:
        content = "🔌 Cannot connect — check your webhook URL and that n8n is running."
    except Exception as e:
        content = f"❌ Connection Failed: {str(e)}"
    return content


def append_message(name, role, content, is_dice=False):
    st.session_state.game_log.append({
        "name": name, "role": role, "content": content, "is_dice": is_dice
    })


def process_ai_turn(current_agent):
    """Process an AI-controlled turn (DM or PLAYER)."""
    if not webhook_url:
        st.error("⚠️ Please paste your n8n Webhook URL in the sidebar.")
        return

    with st.spinner(f"🧠 {current_agent.name} is thinking..."):
        payload = build_payload(current_agent)
        content = call_n8n(payload)

    # If DM, try to detect monster spawns
    if current_agent.role == "DM":
        parse_monster_spawns(content, st.session_state.monsters)

    # Parse attacks and roll dice
    _, dice_log = parse_and_execute_combat(content, agents, st.session_state.monsters)

    # Add the AI narration
    msg_role = "assistant" if current_agent.role == "DM" else "user"
    append_message(current_agent.name, msg_role, content)

    # Add dice results
    if dice_log:
        dice_text = "\n\n".join(dice_log)
        append_message("⚔️ Combat", "assistant", dice_text, is_dice=True)

    st.session_state.turn_idx += 1


def run_ai_chain():
    """Process all consecutive AI turns until the next human agent or a full round.
    This is the key fix: AI agents never require a manual click."""
    safety = 0  # prevent infinite loops
    max_chain = len(agents) + 1  # at most one full round
    while safety < max_chain:
        safety += 1
        current = get_current_agent()
        if current.is_human:
            # Stop chaining — it's a human's turn
            break
        if not webhook_url:
            st.error("⚠️ Please paste your n8n Webhook URL in the sidebar.")
            break
        time.sleep(1.5)  # small pause so chat feels natural
        process_ai_turn(current)


def process_human_input(text):
    """Handle a human player/DM submitting text."""
    current_agent = agents[st.session_state.human_agent_idx]
    msg_role = "assistant" if current_agent.role == "DM" else "user"
    append_message(current_agent.name, msg_role, text)

    # If a human player declares an attack, still roll dice
    if current_agent.role == "PLAYER":
        _, dice_log = parse_and_execute_combat(text, agents, st.session_state.monsters)
        if dice_log:
            dice_text = "\n\n".join(dice_log)
            append_message("⚔️ Combat", "assistant", dice_text, is_dice=True)

    st.session_state.turn_idx += 1
    st.session_state.waiting_for_human = False
    st.session_state.human_agent_idx = None

    # After human submits, auto-chain through all following AI turns
    run_ai_chain()


# ── Human input area ──
current_agent = get_current_agent()

if st.session_state.waiting_for_human:
    human_agent = agents[st.session_state.human_agent_idx]
    st.info(f"🧑 **{human_agent.name}** ({human_agent.role}) — your turn! Type below.")
    human_input = st.chat_input(f"What does {human_agent.name} do?")
    if human_input:
        process_human_input(human_input)
        st.rerun()
else:
    # ── Controls ──
    c1, c2, c3 = st.columns([1, 1, 3])
    if c1.button("▶️ Play Turn", type="primary"):
        if current_agent.is_human:
            st.session_state.waiting_for_human = True
            st.session_state.human_agent_idx = st.session_state.turn_idx % len(agents)
            st.rerun()
        else:
            # Process this AI turn AND all consecutive AI turns after it
            process_ai_turn(current_agent)
            run_ai_chain()  # continue through any remaining AI agents
            st.rerun()

    auto = c2.toggle("🔄 Auto-Play", value=st.session_state.auto_play, key="auto_toggle")
    st.session_state.auto_play = auto

    whose_turn = current_agent.name
    turn_type = "🧑 Human" if current_agent.is_human else "🤖 AI"
    c3.caption(f"Next: **{whose_turn}** ({current_agent.role}) — {turn_type}")

    # Auto-play logic
    if st.session_state.auto_play:
        if current_agent.is_human:
            # Pause auto-play and wait for human input
            st.session_state.waiting_for_human = True
            st.session_state.human_agent_idx = st.session_state.turn_idx % len(agents)
            st.rerun()
        else:
            time.sleep(2)
            process_ai_turn(current_agent)
            run_ai_chain()  # chain remaining AI agents
            st.rerun()