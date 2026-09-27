"""Entry point.

``python -m voice_os worker``  run the agent (the normal mode)
``python -m voice_os doctor``  check configuration and exit
``python -m voice_os client``  only start the control server and open the UI

The worker registers with LiveKit and waits to be dispatched into a room. The
control server mints the tokens that let a browser reach it, so the two are
started together in worker mode.
"""

from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from pathlib import Path

from livekit.agents import (
    AgentServer,
    JobContext,
    WorkerOptions,
    cli,
)
from livekit.agents.voice.room_io import RoomOptions

from .agent import AGENT_NAME, build_agent, build_session
from .config import Config, env_file_path, load_config
from .control_server import start_control_server
from .pipeline import Pipeline, PipelineError, build_pipeline
from .safety import init_safety

logger = logging.getLogger("voice_os")

#: Held so the control server is not garbage collected while the worker runs.
_CONTROL_SERVER: object | None = None

#: Shown to the operator when configuration is incomplete.
_ENV_PATH = env_file_path()


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # These are chatty at DEBUG and drown out the tool activity we care about.
    for noisy in ("livekit.agents.telemetry", "httpcore", "httpx", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_worker_options(cfg: Config, pipeline: Pipeline) -> WorkerOptions:
    agent = build_agent(cfg)

    async def entrypoint(ctx: JobContext) -> None:
        logger.info("job started in room %s", ctx.room.name)
        # Every session starts disarmed. A headset session should never inherit
        # permissions from the previous one.
        init_safety(cfg)
        session = await build_session(ctx, cfg, pipeline)
        # RoomOptions, not RoomInputOptions. In livekit-agents 1.8
        # session.start() validates this argument and rejects the old type with
        # "expected RoomOptions, got RoomInputOptions", so every job died right
        # here before the agent ever joined. The defaults are what we want
        # (close_on_disconnect, no participant filter), so an explicit empty
        # RoomOptions is equivalent to omitting it.
        await session.start(
            agent,
            room=ctx.room,
            room_options=RoomOptions(),
        )
        try:
            await ctx.connect()
        except Exception as exc:  # pragma: no cover - transport failure
            logger.error("could not connect to the room: %s", exc)
            raise
        logger.info("agent ready")
        await session.generate_reply(instructions="Say one short greeting.")

    options = WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME)
    return options


def cmd_doctor(cfg: Config) -> int:
    """Report configuration problems and sanity-check what can be checked."""
    print("VOX - configuration check")
    print("=" * 60)

    problems = cfg.problems()
    if problems:
        print("\nBLOCKING problems:\n")
        for problem in problems:
            print(f"  x {problem}")
    else:
        print("\nAll required settings are present.")

    print("\nResolved configuration:")
    print(f"  LiveKit URL        : {cfg.livekit.url or '(unset)'}")
    print(f"  Agent name         : {cfg.livekit.agent_name}")
    print(f"  LLM                : {cfg.llm.provider} / {cfg.llm.model}")
    print(f"  LLM base URL       : {cfg.llm.base_url}")
    print(f"  LLM API key set    : {bool(cfg.llm.api_key)}")
    print(f"  STT                : {cfg.stt.provider} / {cfg.stt.model} ({cfg.stt.language})")
    print(f"  STT API key set    : {bool(cfg.stt.api_key)}")
    print(f"  TTS                : {cfg.tts.provider} / {cfg.tts.model}")
    print(f"  TTS API key set    : {bool(cfg.tts.api_key)}")
    print(f"  Turn detection     : {cfg.turn.turn_detection}")
    print(f"  Shell commands     : {'ENABLED' if cfg.safety.allow_shell else 'disabled'}")
    print(f"  Auto-disarm        : {cfg.safety.auto_disarm_minutes:g} min idle")
    print(f"  Control server     : http://{cfg.control_host}:{cfg.control_port}")

    # The brain is optional, so report it separately from the blocking
    # problems: a missing skills directory is worth knowing about but must not
    # stop the agent from controlling the desktop.
    if cfg.brain.enabled:
        try:
            from .brain import get_brain

            brain = get_brain(cfg.brain)
            print(f"  Brain              : OK  {brain.describe()}")
            if brain.skills.count() == 0:
                print(
                    f"                      no skill packs found in {cfg.brain.skills_dir}. "
                    "Memory recall still works."
                )
        except Exception as exc:
            print(f"  Brain              : FAILED  {type(exc).__name__}: {exc}")
            print("                      The desktop tools still work without it.")
    else:
        print("  Brain              : disabled (BRAIN_ENABLED=0)")

    # The LLM key defaults to a placeholder string, so "is it set" is the wrong
    # question -- the question that actually matches the documented failure is
    # "did they replace the placeholder".
    if cfg.llm.api_key in {"9router", ""}:
        print(
            "\n  ! LLM_API_KEY is still the placeholder. /v1/models is often public, so a "
            "working\n    model list does not prove the key is right; a wrong key only shows up "
            "as a 401\n    once the agent tries to speak."
        )

    # Try to build the parts that need no credentials to construct. The VAD is
    # excluded on purpose: it is loaded in prewarm, so it reports as 'pending'.
    print("\nRuntime checks:")
    if problems:
        # Building the pipeline without its keys just reproduces the list above
        # as a raw provider exception, which is less useful, not more.
        print("  pipeline           : SKIPPED  needs the keys listed above")
    else:
        try:
            pipeline = build_pipeline(cfg)
            print(f"  pipeline           : OK  {pipeline.describe()}")
        except PipelineError as exc:
            print(f"  pipeline           : FAILED  {exc}")
            return 1
        except Exception as exc:
            print(f"  pipeline           : FAILED  {type(exc).__name__}: {exc}")
            return 1

    # Probe the LLM endpoint. A wrong base_url is the most common misconfiguration.
    try:
        import httpx

        response = httpx.get(
            f"{cfg.llm.base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {cfg.llm.api_key}"},
            timeout=6.0,
        )
        if response.status_code < 400:
            print(f"  LLM endpoint       : OK  (HTTP {response.status_code})")
            try:
                models = response.json().get("data", [])
                names = [m.get("id") for m in models[:8] if m.get("id")]
                if names:
                    print(f"  models available   : {', '.join(names)}")
                    if cfg.llm.model not in {m.get("id") for m in models}:
                        print(
                            f"  ! LLM_MODEL {cfg.llm.model!r} is not in that list. "
                            "Set LLM_MODEL to one of the ids above."
                        )
            except Exception:
                pass
        else:
            print(
                f"  LLM endpoint       : FAILED  HTTP {response.status_code} "
                f"from {cfg.llm.base_url}"
            )
    except Exception as exc:
        print(f"  LLM endpoint       : UNREACHABLE  {cfg.llm.base_url} ({type(exc).__name__})")
        print("                      Is 9Router running? Try: npx 9router")

    if problems:
        print("\nFix the blocking problems above, then run doctor again.")
        return 1
    print("\nReady. Start the agent with:  python -m voice_os worker")
    return 0


def _should_open_browser() -> bool:
    """run.ps1 -NoBrowser sets this so the console does not steal focus."""
    import os

    return os.environ.get("VOICE_OS_NO_BROWSER", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }


def cmd_worker(
    cfg: Config, verbose: bool, passthrough: list[str] | None = None, mode: str = "worker"
) -> int:
    init_safety(cfg)
    logger.info(
        "desktop control starts DISARMED; shell commands %s",
        "enabled" if cfg.safety.allow_shell else "disabled",
    )

    problems = cfg.problems()

    # Always bring the console up, even when the agent cannot start. The console
    # is what explains the missing keys, so refusing to serve it would hide the
    # very thing the operator needs to see.
    global _CONTROL_SERVER
    _CONTROL_SERVER = start_control_server(cfg)
    url = f"http://{cfg.control_host}:{cfg.control_port}"
    # flush because these prints matter most when stdout is redirected, which
    # makes it block-buffered and would otherwise hide them.
    print(f"\n  Operator console:  {url}", flush=True)
    print(f"  The agent is disarmed. Read-only questions work; nothing else does.\n", flush=True)
    if _should_open_browser():
        webbrowser.open(url)

    if problems:
        print("  Cannot start the agent yet. Missing configuration:\n", flush=True)
        for problem in problems:
            print(f"    x {problem}", flush=True)
        print(
            f"\n  The console is running and shows the same list.\n"
            f"  Add the keys to {_ENV_PATH}, then run this again.\n",
            flush=True,
        )
        logger.error("configuration incomplete; serving the console only")
        _wait_forever()
        return 1

    try:
        # Built once, on the main thread, at CLI scope: the VAD import and the
        # turn-detector load both refuse to run on a job-runner thread.
        pipeline = build_pipeline(cfg)
        logger.info("pipeline ready: %s", pipeline.describe())
    except Exception as exc:
        logger.error("could not build the speech pipeline: %s", exc)
        for problem in cfg.problems():
            logger.error("  - %s", problem)
        # Keep the console up so the failure is inspectable.
        _wait_forever()
        return 1

    options = build_worker_options(cfg, pipeline)

    # cli.run_app drives the LiveKit worker CLI, which parses sys.argv itself and
    # only understands its own subcommands. Hand it the right one plus whatever
    # the caller forwarded, so `worker` behaves like `start` and `-- --log-level
    # DEBUG` still reaches the framework.
    subcommand = WORKER_SUBCOMMANDS.get(mode, "start")
    sys.argv = [sys.argv[0], subcommand, *(passthrough or [])]
    try:
        cli.run_app(AgentServer.from_server_options(options))
    except KeyboardInterrupt:
        logger.info("interrupted")
    return 0


def cmd_client(cfg: Config) -> int:
    init_safety(cfg)
    start_control_server(cfg)
    url = f"http://{cfg.control_host}:{cfg.control_port}"
    print(f"Control server running at {url}. Press Ctrl+C to stop.")
    if _should_open_browser():
        webbrowser.open(url)
    try:
        _wait_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def _wait_forever() -> None:
    import threading

    threading.Event().wait()


#: How our modes map onto the LiveKit worker CLI's own subcommands. ``run_app``
#: parses ``sys.argv`` itself, so the mode has to be translated, not passed.
WORKER_SUBCOMMANDS = {"worker": "start", "dev": "dev", "console": "console"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Anything after "--" is forwarded verbatim to the LiveKit worker CLI.
    passthrough: list[str] = []
    if "--" in argv:
        index = argv.index("--")
        argv, passthrough = argv[:index], argv[index + 1 :]

    parser = argparse.ArgumentParser(
        prog="voice_os",
        description=__doc__,
        epilog="Forward worker-CLI flags after --, e.g. "
        "python -m voice_os worker -- --log-level DEBUG",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="worker",
        choices=["worker", "dev", "console", "doctor", "client", "overlay"],
        help="worker: run the agent (default). dev: reload on change. "
        "console: watch a session. doctor: check config. client: UI only. "
        "overlay: the always-on-top corner orb (needs the agent running).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    configure_logging(args.verbose)
    cfg = load_config()

    if args.mode == "doctor":
        return cmd_doctor(cfg)
    if args.mode == "client":
        return cmd_client(cfg)
    if args.mode == "overlay":
        from .overlay import run as run_overlay

        return run_overlay(cfg)
    return cmd_worker(cfg, args.verbose, passthrough, args.mode)


if __name__ == "__main__":
    sys.exit(main())
