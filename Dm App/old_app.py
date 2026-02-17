"""
⚔️ AI RPG Auto-Battler — Streamlit + dnd-character + n8n
Run:  streamlit run app.py
"""

from __future__ import annotations

import json
import re
import time
from textwrap import shorten

import httpx
import streamlit as st

from models import (
    CLASS_CONSTRUCTORS,
    DEFAULT_MODEL,
    PlayerAgent,
    create_dm_agent,
    generate_party,
)

# ──────────────────────────────────────────────────────────────────────────────
# Page config
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="⚔️ AI RPG Auto-Battler", layout="wide")

# ──────────────────────────────────────────────────────────────────────────────
# Custom CSS
# ──────────────────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
    .hp-bar-bg  { background:#333; border-radius:6px; height:18px; width:100%; }
    .hp-bar-fg  { border-radius:6px; height:18px; text-align:center;
                  font-size:12px; color:#fff; line-height:18px; }
    .stat-label { font-size:11px; color:#888; text-align:center; }
    .stat-value { font-size:18px; font-weight:700; text-align:center; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ──────────────────────────────────────────────────────────────────────────────
# Session state defaults
# ──────────────────────────────────────────────────────────────────────────────
_DEFAULTS: dict = {
    "party": [],           # list[PlayerAgent]
    "dm": None,            # PlayerAgent
    "history": [],         # list[dict]  — chat log entries
    "turn_index": 0,
    "game_active": False,
    "auto_play": False,
    "webhook_url": "",
}
for key, val in _DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = val

# ──────────────────────────────────────────────────────────────────────────────
# Sidebar — Settings
# ──────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Settings")

    st.session_state["webhook_url"] = st.text_input(
        "n8n Webhook URL",
        value=st.session_state["webhook_url"],
        placeholder="https://your-n8n.app/webhook/xxx",
    )

    st.divider()
    st.subheader("🤖 Model per Agent")
    model_options = [
        "google/gemini-2.0-flash-lite-preview-02-05:free",
        "google/gemini-2.0-flash-001",
        "openai/gpt-4o-mini",
        "anthropic/claude-3-haiku",
        "mistralai/mistral-7b-instruct:free",
    ]

    dm_model = st.selectbox("DM Model", model_options, index=0, key="dm_model_sel")

    # Per-character model selectors (shown after party exists)
    char_models: dict[int, str] = {}
    if st.session_state["party"]:
        for i, agent in enumerate(st.session_state["party"]):
            char_models[i] = st.selectbox(
                f"{agent.name} ({agent.class_name})",
                model_options,
                index=0,
                key=f"model_sel_{i}",
            )

    st.divider()
    st.subheader("🎲 Party Generation")
    chosen_classes = st.multiselect(
        "Pick 3 classes (or leave empty for random)",
        options=list(CLASS_CONSTRUCTORS.keys()),
        max_selections=3,
    )
    if st.button("🎲 Generate Party", use_container_width=True):
        cls_list = chosen_classes if len(chosen_classes) == 3 else None
        st.session_state["party"] = generate_party(
            class_names=cls_list,
            webhook_url=st.session_state["webhook_url"],
            model_id=dm_model,
        )
        st.session_state["dm"] = create_dm_agent(
            webhook_url=st.session_state["webhook_url"],
            model_id=dm_model,
        )
        st.session_state["history"] = []
        st.session_state["turn_index"] = 0
        st.session_state["game_active"] = True
        st.rerun()


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _history_summary(history: list[dict], max_chars: int = 600) -> str:
    """Condense chat history into a short string for the n8n payload."""
    lines = [f"[{h['sender']}] {h['text']}" for h in history[-8:]]
    return shorten("\n".join(lines), width=max_chars, placeholder="…")


def _parse_stat_changes(text: str) -> list[dict]:
    """
    Scan AI narration for damage/healing cues.
    Returns a list of  {"target": str, "type": "damage"|"heal", "amount": int}.
    """
    changes: list[dict] = []

    # Pattern: "X takes/receives N damage"
    for m in re.finditer(
        r"(\w[\w\s]{0,20}?)\s+(?:takes?|receives?|suffers?)\s+(\d+)\s+(?:points?\s+of\s+)?damage",
        text,
        re.IGNORECASE,
    ):
        changes.append({"target": m.group(1).strip(), "type": "damage", "amount": int(m.group(2))})

    # Pattern: "X heals/recovers/regains N hp/hit points"
    for m in re.finditer(
        r"(\w[\w\s]{0,20}?)\s+(?:heals?|recovers?|regains?)\s+(\d+)\s+(?:hit\s*points?|hp)",
        text,
        re.IGNORECASE,
    ):
        changes.append({"target": m.group(1).strip(), "type": "heal", "amount": int(m.group(2))})

    return changes


def _apply_stat_changes(changes: list[dict], party: list[PlayerAgent]) -> list[str]:
    """Apply parsed changes and return human-readable log lines."""
    logs: list[str] = []
    name_map = {a.name.lower(): a for a in party}
    for ch in changes:
        target_key = ch["target"].lower()
        agent = name_map.get(target_key)
        if agent is None:
            # fuzzy: check if target is a substring of any name
            for key, a in name_map.items():
                if target_key in key or key in target_key:
                    agent = a
                    break
        if agent is None:
            continue
        if ch["type"] == "damage":
            dealt = agent.take_damage(ch["amount"])
            logs.append(f"💥 {agent.name} took {dealt} damage → {agent.hp}/{agent.max_hp} HP")
        else:
            healed = agent.heal(ch["amount"])
            logs.append(f"💚 {agent.name} healed {healed} HP → {agent.hp}/{agent.max_hp} HP")
    return logs


def _send_to_n8n(payload: dict, webhook_url: str) -> dict:
    """POST payload to the n8n webhook and return parsed JSON response."""
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(webhook_url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        return {"narration": f"⚠️ Webhook HTTP error: {e.response.status_code}", "action": "error"}
    except httpx.RequestError as e:
        return {"narration": f"⚠️ Could not reach webhook: {e}", "action": "error"}
    except Exception as e:
        return {"narration": f"⚠️ Unexpected error: {e}", "action": "error"}


# ──────────────────────────────────────────────────────────────────────────────
# Run Turn
# ──────────────────────────────────────────────────────────────────────────────

def run_turn() -> None:
    """Execute one turn of the game loop."""
    party: list[PlayerAgent] = st.session_state["party"]
    dm: PlayerAgent = st.session_state["dm"]
    history: list[dict] = st.session_state["history"]
    turn: int = st.session_state["turn_index"]
    webhook_url: str = st.session_state["webhook_url"]

    if not webhook_url:
        st.error("Please set the n8n Webhook URL in the sidebar.")
        return

    living = [a for a in party if a.is_alive]
    if not living:
        st.session_state["game_active"] = False
        st.warning("☠️ All party members have fallen! Game Over.")
        return

    # Determine whose turn it is: DM on even turns, party members on odd
    is_dm_turn = turn % 2 == 0
    if is_dm_turn:
        agent = dm
        agent.model_id = st.session_state.get("dm_model_sel", DEFAULT_MODEL)
    else:
        idx = ((turn // 2) % len(living))
        agent = living[idx]
        # sync model from sidebar
        full_idx = party.index(agent)
        agent.model_id = st.session_state.get(f"model_sel_{full_idx}", DEFAULT_MODEL)
        agent.webhook_url = webhook_url

    latest_event = history[-1]["text"] if history else "The adventure begins…"
    summary = _history_summary(history)

    payload = agent.to_n8n_json(
        latest_event=latest_event,
        history_summary=summary,
    )

    with st.spinner(f"🧠 {agent.name} is thinking…"):
        result = _send_to_n8n(payload, webhook_url)

    narration = result.get("narration", result.get("message", str(result)))
    action = result.get("action", "")

    # Record in history
    history.append({
        "sender": agent.name,
        "role": agent.role,
        "text": narration,
        "action": action,
        "turn": turn,
    })

    # Parse and apply stat changes from the narration
    changes = _parse_stat_changes(narration)
    change_logs = _apply_stat_changes(changes, party)
    if change_logs:
        history.append({
            "sender": "⚙️ System",
            "role": "SYSTEM",
            "text": "\n".join(change_logs),
            "action": "",
            "turn": turn,
        })

    st.session_state["turn_index"] = turn + 1


# ──────────────────────────────────────────────────────────────────────────────
# UI — Title
# ──────────────────────────────────────────────────────────────────────────────
st.title("⚔️ AI RPG Auto-Battler")

if not st.session_state["party"]:
    st.info("👈 Use the sidebar to generate a party and configure the webhook, then start playing!")
    st.stop()

# ──────────────────────────────────────────────────────────────────────────────
# UI — Two-column layout
# ──────────────────────────────────────────────────────────────────────────────
chat_col, sheet_col = st.columns([3, 2])

# ── Left: Chat Log ──────────────────────────────────────────────────────────
with chat_col:
    st.subheader("📜 Adventure Log")

    chat_container = st.container(height=500)
    with chat_container:
        if not st.session_state["history"]:
            st.caption("_The story has yet to begin…_")
        for entry in st.session_state["history"]:
            role = entry["role"]
            if role == "DM":
                avatar = "🧙"
            elif role == "SYSTEM":
                avatar = "⚙️"
            else:
                avatar = "🗡️"
            with st.chat_message(name=entry["sender"], avatar=avatar):
                st.markdown(entry["text"])
                if entry.get("action"):
                    st.caption(f"Action: *{entry['action']}*")

# ── Right: Character Sheets ─────────────────────────────────────────────────
with sheet_col:
    st.subheader("🛡️ Party Status")
    for agent in st.session_state["party"]:
        with st.container(border=True):
            alive_icon = "💀" if not agent.is_alive else "❤️"
            st.markdown(f"### {alive_icon} {agent.name}  —  {agent.class_name} Lv.{agent.level}")

            # HP Bar
            hp_pct = (agent.hp / agent.max_hp * 100) if agent.max_hp else 0
            bar_color = "#4caf50" if hp_pct > 50 else "#ff9800" if hp_pct > 25 else "#f44336"
            st.markdown(
                f"""
                <div class="hp-bar-bg">
                    <div class="hp-bar-fg" style="width:{hp_pct:.0f}%; background:{bar_color};">
                        {agent.hp} / {agent.max_hp}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.caption(f"AC: {agent.ac}")

            # Ability scores in columns
            stats = agent.stat_block()
            cols = st.columns(6)
            for col, (label, key) in zip(
                cols,
                [("STR", "strength"), ("DEX", "dexterity"), ("CON", "constitution"),
                 ("INT", "intelligence"), ("WIS", "wisdom"), ("CHA", "charisma")],
            ):
                with col:
                    st.markdown(
                        f'<div class="stat-label">{label}</div>'
                        f'<div class="stat-value">{stats[key]}</div>',
                        unsafe_allow_html=True,
                    )

# ──────────────────────────────────────────────────────────────────────────────
# UI — Bottom Controls
# ──────────────────────────────────────────────────────────────────────────────
st.divider()
ctrl_left, ctrl_mid, ctrl_right = st.columns([2, 2, 1])

with ctrl_left:
    play_disabled = not st.session_state["game_active"]
    if st.button("▶️  Play Next Turn", use_container_width=True, disabled=play_disabled):
        run_turn()
        st.rerun()

with ctrl_mid:
    auto = st.checkbox("🔄 Auto-Play", key="auto_play_cb", value=st.session_state["auto_play"])
    st.session_state["auto_play"] = auto

with ctrl_right:
    if st.button("🗑️ Reset", use_container_width=True):
        for key, val in _DEFAULTS.items():
            st.session_state[key] = val
        st.rerun()

# Auto-play loop (runs one turn per rerun cycle)
if st.session_state["auto_play"] and st.session_state["game_active"]:
    time.sleep(1.5)
    run_turn()
    st.rerun()
