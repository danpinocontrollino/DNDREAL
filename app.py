import json

import streamlit as st
import httpx
import time
from dnd_character import Character
from dnd_character.classes import CLASSES

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------
DEFAULT_WEBHOOK = ""

# Default model ID (paste any OpenRouter model ID)
DEFAULT_DM_MODEL = "anthropic/claude-3.5-sonnet"
DEFAULT_PLAYER_MODEL = "google/gemini-2.0-flash-lite-preview-02-05:free"

# -----------------------------------------------------------------------------
# CLASSES
# -----------------------------------------------------------------------------
class Agent:
    def __init__(self, name, role, model_key, char_class=None):
        self.name = name
        self.role = role  # "DM" or "PLAYER"
        self.model_key = model_key
        
        # Integrate dnd-character library for Stats
        if role == "PLAYER" and char_class:
            # Create a Level 1 character of specific class
            self.sheet = Character(name=name, classs=CLASSES[char_class], level=1)
        else:
            self.sheet = None

    def get_stats_json(self):
        """Extract stats from the library object for the AI"""
        if not self.sheet:
            return {}
        return {
            "hp": self.sheet.current_hp,
            "ac": self.sheet.armor_class,
            "str": self.sheet.strength,
            "dex": self.sheet.dexterity,
            "int": self.sheet.intelligence,
            "class": self.sheet.class_name
        }

# -----------------------------------------------------------------------------
# APP LOGIC
# -----------------------------------------------------------------------------
st.set_page_config(page_title="n8n RPG Bridge", layout="wide")

# Sidebar Setup
with st.sidebar:
    st.header("⚙️ Configuration")
    webhook_url = st.text_input("n8n Webhook URL", value=DEFAULT_WEBHOOK)
    
    st.divider()
    st.subheader("🤖 Agent Setup")
    
    # DM Setup
    dm_model = st.text_input("DM Model (OpenRouter ID)", value=DEFAULT_DM_MODEL,
                             placeholder="e.g. anthropic/claude-3.5-sonnet")
    
    # Player 1 Setup
    p1_name = st.text_input("Player 1 Name", "Thorin")
    p1_class = st.selectbox("Class", options=list(CLASSES.keys()), index=0)
    p1_model = st.text_input("Player 1 Model (OpenRouter ID)", value=DEFAULT_PLAYER_MODEL,
                              placeholder="e.g. openai/gpt-4o")

    st.divider()
    st.subheader("📜 Adventure Setup")
    adventure_context = st.text_area(
        "Adventure Premise",
        value="The party meets at the Rusty Dragon tavern in a small village on the edge of a dark forest. "
              "Rumors speak of an ancient tomb recently uncovered by a landslide, filled with treasure and danger. "
              "A mysterious hooded stranger approaches the party with a map and a warning.",
        height=120,
        help="Describe the starting scenario. The DM AI will use this as the foundation."
    )

    if st.button("Initialize Game"):
        st.session_state.agents = [
            Agent("Dungeon Master", "DM", dm_model),
            Agent(p1_name, "PLAYER", p1_model, p1_class)
        ]
        st.session_state.game_log = []
        st.session_state.turn_idx = 0
        st.session_state.adventure_context = adventure_context
        st.session_state.initialized = True
        st.rerun()

# Check Initialization
if "initialized" not in st.session_state:
    st.info("👈 Please setup your agents and click Initialize Game in the sidebar.")
    st.stop()

# -----------------------------------------------------------------------------
# GAME LOOP
# -----------------------------------------------------------------------------
st.title("⚔️ Modular AI RPG")

# Display Stats
cols = st.columns(len(st.session_state.agents))
for i, agent in enumerate(st.session_state.agents):
    with cols[i]:
        st.subheader(f"{agent.name} ({agent.role})")
        st.caption(f"🧠 {agent.model_key}")
        if agent.sheet:
            st.progress(agent.sheet.current_hp / agent.sheet.max_hp)
            st.text(f"HP: {agent.sheet.current_hp}/{agent.sheet.max_hp} | AC: {agent.sheet.armor_class}")

st.divider()

# Chat Area
chat_container = st.container(height=400)
for msg in st.session_state.game_log:
    with chat_container:
        with st.chat_message(msg["role"]):
            st.write(f"**{msg['name']}:** {msg['content']}")

# Turn Execution
def build_history_messages(game_log, max_messages=20):
    """Convert game log into chat-style message history for AI memory."""
    recent = game_log[-max_messages:] if len(game_log) > max_messages else game_log
    messages = []
    for msg in recent:
        messages.append({
            "name": msg["name"],
            "role": msg["role"],
            "content": msg["content"]
        })
    return messages

def process_turn():
    agents = st.session_state.agents
    current_agent = agents[st.session_state.turn_idx % len(agents)]
    adventure = st.session_state.get("adventure_context", "A D&D adventure begins.")

    if not webhook_url:
        st.error("⚠️ Please paste your n8n Webhook URL in the sidebar.")
        return
    
    # Build conversation history (memory)
    history = build_history_messages(st.session_state.game_log)
    
    # Build the latest input — what happened last, or a kickoff prompt
    if st.session_state.game_log:
        latest_input = st.session_state.game_log[-1]["content"]
    else:
        latest_input = (
            f"Begin the adventure! Set the scene, describe the environment vividly, "
            f"and introduce a hook that draws the players in. "
            f"Address the player characters by name and involve them."
        )

    # Gather ALL party stats for context
    party_info = []
    for a in agents:
        if a.sheet:
            party_info.append({
                "name": a.name,
                "class": a.sheet.class_name,
                "hp": a.sheet.current_hp,
                "max_hp": a.sheet.max_hp,
                "ac": a.sheet.armor_class
            })

    # 1. Prepare Payload for n8n
    payload = {
        "role": current_agent.role,
        "model_id": current_agent.model_key,
        "char_name": current_agent.name,
        "char_class": current_agent.sheet.class_name if current_agent.sheet else "DM",
        "stats": current_agent.get_stats_json(),
        "adventure_context": adventure,
        "party_info": party_info,
        "conversation_history": history,
        "latest_input": latest_input
    }

    # 2. Call n8n
    try:
        with st.spinner(f"🧠 {current_agent.name} is thinking..."):
            response = httpx.post(webhook_url, json=payload, timeout=90)
            
            if response.status_code == 200:
                # Safely parse JSON
                try:
                    data = response.json()
                    content = data.get("content", "") or data.get("message", "")
                    if not content:
                        content = f"⚠️ n8n returned JSON but no 'content' key. Keys: {list(data.keys())}. Data: {str(data)[:300]}"
                except (json.JSONDecodeError, ValueError):
                    # n8n returned non-JSON — show debug info
                    raw = response.text[:500]
                    ct = response.headers.get('content-type', 'unknown')
                    if not raw.strip():
                        content = (
                            f"⚠️ n8n returned an empty response (content-type: {ct}). "
                            f"This usually means the workflow errored before reaching the Respond node. "
                            f"Check the n8n execution log for errors."
                        )
                    else:
                        content = f"⚠️ n8n returned non-JSON (content-type: {ct}): {raw}"
            else:
                raw = response.text[:300]
                content = f"⚠️ n8n error {response.status_code}: {raw}"
    except httpx.TimeoutException:
        content = "⏱️ Request timed out — is the n8n workflow active?"
    except httpx.ConnectError:
        content = "🔌 Cannot connect — check your webhook URL and that n8n is running."
    except Exception as e:
        content = f"❌ Connection Failed: {str(e)}"

    # 3. Update State
    st.session_state.game_log.append({
        "name": current_agent.name,
        "role": "assistant" if current_agent.role == "DM" else "user",
        "content": content
    })
    st.session_state.turn_idx += 1
    st.rerun()

# Controls
c1, c2 = st.columns([1, 4])
if c1.button("▶️ Play Turn", type="primary"):
    process_turn()

auto = c2.checkbox("Auto-Play Loop")
if auto:
    time.sleep(2)
    process_turn()