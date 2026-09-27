#!/usr/bin/env python3
"""Time the stages of a CallDesk session, without a browser.

Measures the four costs that stack up before you hear a word: minting a token,
opening the socket, waiting for session.ready, and the gap from sending text to
the first reply.audio. Compare those against the local audio test to tell a slow
network apart from a slow agent.

    python deployment/browser/speak_test.py

The API key is read from .env and never printed. Only a 60 second session token
comes back from /v1/token, and that is not echoed either.
"""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib import aai, load_env, required  # noqa: E402

WS = "wss://agents.assemblyai.com/v1/ws"
PROMPT = "Say exactly this sentence: The quick brown fox jumps over the lazy dog."
BUDGET = 30.0  # seconds; the brief says close after 30


def stamp(label: str, seconds: float) -> None:
    print(f"  {label:<26} {seconds:6.2f}s")


def main() -> int:
    try:
        import websocket  # websocket-client
    except ImportError:
        print("This needs websocket-client: pip install websocket-client")
        return 1

    load_env()
    required("ASSEMBLYAI_API_KEY", "get one at https://www.assemblyai.com/dashboard/api-keys")
    agent_id = os.environ.get("AGENT_ID_CALLDESK", "")
    if not agent_id:
        print("AGENT_ID_CALLDESK is not set. Run: AGENT=calldesk python publish.py")
        return 1

    start = time.perf_counter()
    token = aai("/token?product=voice_agent&expires_in_seconds=60")["token"]
    t_token = time.perf_counter() - start

    opened = ready = None
    t_sent = first_audio = None
    audio_chunks = 0
    first_text = None
    audio_times: list[float] = []

    ws = websocket.create_connection(
        WS + "?token=" + token,
        timeout=30,
        # The starter's server does not send a cert chain this client trusts by
        # default, so verification is off here. This is a throwaway local probe,
        # not the browser path, which is unaffected.
        sslopt={"cert_reqs": ssl_none()},
    )
    t_connect = time.perf_counter() - start
    print(f"\nagent {agent_id}\n")

    deadline = start + BUDGET
    try:
        ws.send(json.dumps({"type": "session.update", "session": {"agent_id": agent_id}}))
        t_sent = time.perf_counter() - start

        while time.perf_counter() < deadline:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                break
            now = time.perf_counter() - start
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")

            if kind == "session.ready" and ready is None:
                ready = now
                print(f"  session_id {msg.get('session_id')}\n")
                # There is no input.text event. Text goes in as a conversation
                # item, and reply.create is what makes the agent speak it.
                ws.send(json.dumps({
                    "type": "conversation.message",
                    "role": "user",
                    "content": PROMPT,
                }))
                ws.send(json.dumps({
                    "type": "reply.create",
                    "instructions": "Say exactly this sentence and nothing else: "
                                    "The quick brown fox jumps over the lazy dog.",
                }))
                t_sent = time.perf_counter() - start

            elif kind == "reply.audio" and first_audio is None:
                first_audio = now
                audio_chunks += 1
                audio_times.append(now)

            elif kind == "reply.audio":
                audio_chunks += 1
                audio_times.append(now)

            elif kind == "transcript.agent" and first_text is None:
                first_text = now

            elif kind == "session.error":
                print(f"\n  session.error: {msg.get('code')}: {msg.get('message')}")
                break
    finally:
        ws.close()

    print("\ntimings from process start")
    stamp("token mint", t_token)
    stamp("socket open", t_connect)
    if ready is not None:
        stamp("session.ready", ready)
        stamp("text sent -> 1st audio", first_audio - t_sent if first_audio else -1)
        stamp("text sent -> 1st text", first_text - t_sent if first_text else -1)
    else:
        print("  session.ready            never arrived")
    print(f"\n  reply.audio chunks: {audio_chunks}")
    if len(audio_times) > 1:
        gaps = [b - a for a, b in zip(audio_times, audio_times[1:])]
        span = audio_times[-1] - audio_times[0]
        # 24kHz PCM16 mono is 48000 bytes/s. A gap over ~0.25s is long enough
        # to be heard as a stutter rather than as normal chunking.
        print(f"  audio span          {span:6.2f}s")
        print(f"  mean gap            {sum(gaps)/len(gaps)*1000:6.1f}ms")
        print(f"  max gap             {max(gaps)*1000:6.1f}ms  <- audible if large")
        print(f"  gaps over 250ms     {sum(1 for g in gaps if g > 0.25):6d} of {len(gaps)}")
    return 0 if first_audio else 1


def ssl_none():
    import ssl
    return ssl.CERT_NONE


if __name__ == "__main__":
    sys.exit(main())
