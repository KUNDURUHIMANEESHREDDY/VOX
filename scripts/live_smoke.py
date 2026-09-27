"""Join the room with the minted token and confirm the agent is dispatched.

This is the live end-to-end check a browser performs. Connecting a participant
triggers the RoomAgentDispatch in the room config, which runs the worker
entrypoint: the agent builds a session, connects, and generates a greeting.

Three signals prove the whole path works, and none of them needs the audio
decoded:

  1. a second participant appears  -> the worker entrypoint ran
  2. it publishes an audio track   -> Cartesia TTS produced real output
  3. data arrives on the "vox" topic -> the session and its tool wiring are live

No microphone is required; we only receive.
"""
import asyncio
import json
import sys

from livekit import rtc

TOPIC = "vox"


async def main() -> int:
    with open(".data/token.json", encoding="utf-8") as fh:
        info = json.load(fh)

    room = rtc.Room()
    seen_participants: list[str] = []
    audio_tracks: list[str] = []
    events: list[str] = []

    @room.on("participant_connected")
    def _joined(p) -> None:
        seen_participants.append(p.identity or "?")

    @room.on("track_subscribed")
    def _track(track, pub, _p) -> None:
        try:
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                # RemoteTrackPublication has no track_name, and AudioTrack.name
                # is a plain attribute in 1.8 rather than a method.
                audio_tracks.append(getattr(track, "name", None) or "audio")
        except Exception as exc:  # never let a probe callback kill the run
            events.append(f"track probe error: {exc}")

    @room.on("data_received")
    def _data(pkt, *_a) -> None:
        if pkt.topic != TOPIC:
            return
        try:
            msg = json.loads(bytes(pkt.payload).decode("utf-8"))
        except Exception:
            return
        kind = msg.get("type")
        if kind == "assistant":
            events.append(f"agent said: {msg.get('text','')!r}")
        elif kind == "state":
            events.append(f"state -> {msg.get('state')}")
        elif kind == "safety":
            st = msg.get("state") or {}
            events.append(f"safety armed={st.get('armed')}")
        elif kind == "tool":
            events.append(f"tool {msg.get('tool')} [{msg.get('status')}]")

    print(f"connecting to {info['url']} room={info['room']} as {info['identity']}")
    await room.connect(info["url"], info["token"])
    print("  connected")

    # Dispatch is asynchronous, so poll rather than assume it already happened.
    for _ in range(40):
        if seen_participants and audio_tracks:
            break
        await asyncio.sleep(0.75)

    await asyncio.sleep(5)  # let the greeting finish publishing

    print(f"  agent participant joined  : {seen_participants or 'NONE'}")
    print(f"  agent audio tracks        : {audio_tracks or 'NONE'}")
    print(f"  events on the '{TOPIC}' topic:")
    for line in events[:12]:
        print(f"    - {line}")
    if not events:
        print("    (none)")

    await room.disconnect()

    ok = bool(seen_participants) and bool(audio_tracks) and bool(events)
    if ok:
        print("RESULT: PASS - agent dispatched, produced audio, and published events")
    else:
        missing = []
        if not seen_participants:
            missing.append("agent participant")
        if not audio_tracks:
            missing.append("audio track")
        if not events:
            missing.append("data events")
        print("RESULT: INCOMPLETE - missing: " + ", ".join(missing))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
