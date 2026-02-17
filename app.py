import streamlit as st
import httpx
import time
from dnd_character import Character
from dnd_character.classes import CLASSES

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------
DEFAULT_WEBHOOK = "http://localhost:5678/webhook/rpg-turn"

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

    if st.button("Initialize Game"):
        st.session_state.agents = [
            Agent("Dungeon Master", "DM", dm_model),
            Agent(p1_name, "PLAYER", p1_model, p1_class)
        ]
        st.session_state.game_log = []
        st.session_state.turn_idx = 0
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
def process_turn():
    agents = st.session_state.agents
    current_agent = agents[st.session_state.turn_idx % len(agents)]
    
    # 1. Prepare Payload for n8n
    # We send the RAW model ID (e.g., 'openai/gpt-4o') so n8n just uses it
    payload = {
        "role": current_agent.role,
        "model_id": current_agent.model_key, 
        "char_name": current_agent.name,
        "char_class": current_agent.sheet.class_name if current_agent.sheet else "DM",
        "stats": current_agent.get_stats_json(),
        "context_summary": "The party has entered a dark cave. A goblin is watching.", # Simplification
        "latest_input": st.session_state.game_log[-1]["content"] if st.session_state.game_log else "Start the adventure."
    }

    # 2. Call n8n
    try:
        with st.spinner(f"{current_agent.name} is thinking..."):
            response = httpx.post(webhook_url, json=payload, timeout=60)
            if response.status_code == 200:
                content = response.json().get("content", "")
            else:
                content = f"Error from n8n: {response.status_code}"
    except Exception as e:
        content = f"Connection Failed: {str(e)}"

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