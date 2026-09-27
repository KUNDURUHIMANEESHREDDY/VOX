"""Drive the real LLM with the real toolset, and check the safety gate.

Speech-to-text and text-to-speech are unusable right now: the Deepgram and
Cartesia keys in the pre-merge .env are both rejected (401), and the router has
no OpenAI credentials to fall back on. Neither is needed to prove the part of
this system that was actually built.

The proof is split in two, deliberately:

  1. Ask the real model, with the real toolset, and see whether it actually
     *calls* a tool. This is the failure that matters in practice: a model that
     narrates "I'll open Notepad" instead of calling the tool is useless, and
     no amount of correct tool code fixes it.

  2. Invoke the tool the model picked, through the real SafetyController, and
     show what the gate does with it.

The tool-execution loop itself belongs to AgentSession, not to LLM.chat(), so
this does not duplicate it -- the gate is the part under test here, and it is
exercised directly.

Run:  python scripts/llm_tool_probe.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from livekit.agents.llm import ChatContext  # noqa: E402

from voice_os.brain import get_brain, reset_brain  # noqa: E402
from voice_os.config import BrainConfig, Config, SafetyConfig, load_config  # noqa: E402
from voice_os.pipeline import build_llm  # noqa: E402
from voice_os.safety import get_safety, init_safety  # noqa: E402
from voice_os.tools import build_toolset  # noqa: E402

PROMPTS = [
    ("Which skills do you have for designing a landing page? Use the tools.", "read-only"),
    ("Open Notepad for me.", "desktop action"),
    ("Please remember that I always deploy on Friday afternoons.", "memory write"),
]


def _ctx(text: str) -> ChatContext:
    ctx = ChatContext.empty()
    ctx.add_message(role="user", content=text)
    return ctx


def _picked_tools(ctx: ChatContext) -> list[str]:
    """Tool names the model asked for, found in the assistant turn it produced.

    ``ChatContext.items`` is a property holding a list, not a method.
    """
    found: list[str] = []
    for item in ctx.items or []:
        for call in getattr(item, "function_calls", None) or []:
            name = getattr(call, "name", None)
            if name:
                found.append(name)
    return found


async def ask(llm, tools, text: str) -> tuple[str, list[str]]:
    """One model turn. Returns the prose it wrote and the tools it called."""
    ctx = _ctx(text)
    stream = llm.chat(chat_ctx=ctx, tools=tools)
    reply = ""
    async for chunk in stream.to_str_iterable():
        reply += chunk
    return reply.strip(), _picked_tools(ctx)


async def main() -> int:
    cfg = load_config()
    tmp = Path(tempfile.mkdtemp())
    reset_brain()
    init_safety(
        Config(
            livekit=cfg.livekit,
            llm=cfg.llm,
            stt=cfg.stt,
            tts=cfg.tts,
            turn=cfg.turn,
            safety=SafetyConfig(audit_path=tmp / "audit.jsonl", auto_disarm_minutes=0),
            brain=BrainConfig(
                skills_dir=cfg.brain.skills_dir,
                db_path=tmp / "brain.db",
                data_kernel_enabled=False,
            ),
        )
    )
    get_brain()

    tools = build_toolset(include_shell=cfg.safety.allow_shell)
    by_name = {t.id: t for t in tools}
    llm = build_llm(cfg)

    print(f"model : {cfg.llm.model} @ {cfg.llm.base_url}")
    print(f"tools : {len(tools)} registered")
    print(f"armed : {get_safety().armed}")
    problems = 0

    for text, kind in PROMPTS:
        print(f"\n{'=' * 70}\n{text}\n  (expecting: {kind})")
        reply, picked = await ask(llm, tools, text)
        print(f"  model called : {picked or 'NOTHING'}")
        print(f"  model said   : {reply[:220]}")
        if not picked:
            print("  -> the model narrated instead of calling a tool")
            problems += 1
            continue
        # Run the first tool it chose, exactly as the session would.
        name = picked[0]
        tool = by_name.get(name)
        if tool is None:
            print(f"  -> hallucinated a tool that does not exist: {name}")
            problems += 1
            continue
        result = await tool.__wrapped__()
        first = str(result).splitlines()[0] if result else "(empty)"
        print(f"  gate result  : {first[:190]}")

    print(f"\n{'=' * 70}")
    # Direct gate checks, independent of what the model happened to pick.
    print("gate, direct (no model involved):")
    arm = get_safety().arm
    dis = get_safety().disarm
    from voice_os.tools.brain import remember_this
    from voice_os.tools.apps import launch_app

    dis(source="probe")
    print(f"  disarmed, launch_app      -> {str(await launch_app.__wrapped__('notepad'))[:74]}")
    print(f"  disarmed, remember_this   -> {str(await remember_this.__wrapped__('x = 1', None))[:74]}")
    arm(source="probe")
    print(f"  ARMED,     launch_app      -> {str(await launch_app.__wrapped__('notepad'))[:74]}")
    print(f"  ARMED,     remember_this   -> {str(await remember_this.__wrapped__('x = 1', None))[:74]}")
    stored = await get_brain().recall("x = 1")
    print(f"  memory rows written       -> {len(stored)} (must be 0)")
    if stored:
        problems += 1

    reset_brain()
    print("\nRESULT:", "PASS" if problems == 0 else f"{problems} PROBLEM(S)")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
