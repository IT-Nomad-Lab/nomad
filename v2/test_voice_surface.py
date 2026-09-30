"""V2c · the voice surface: push-to-talk, wake word, conversation mode, live voice.

Four of the eight rows V2c is porting, and the four that live mostly in the browser. The JS cannot
be executed here, so these assert two things that are still worth asserting: the server side those
four sit on, and that the page actually contains the code and the controls that drive it.

That second kind of test is weak and it is deliberate. It exists because of the failure that shape
of test catches and nothing else does: a commit that adds an endpoint and never renders it. S6 hit
exactly that, where `delivery` reached the /run payload and no version of the cockpit ever drew it,
for four months, with a commit message saying otherwise.

The one hazard here is not weak at all. COCKPIT is a plain Python string, so a lone `\\b` in a JS
regex reaches the browser as a backspace and the wake-word pattern quietly stops matching word
boundaries. Nothing fails, nothing logs, the wake word just gets worse.

Run: python3 -m pytest v2/test_voice_surface.py -q   (engine deps live in the container)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytest.importorskip("fastapi", reason="engine deps live in the container")
import server  # noqa: E402

PAGE = server.COCKPIT


def _routes():
    return {getattr(r, "path", None) for r in server.app.routes}


# ── the escapes, first, because nothing else catches them ───────────────────────────────────────

def test_the_page_contains_no_control_characters():
    """A backspace, a form feed or a vertical tab here means a Python escape ate a JS one. They
    are invisible in a diff and in a browser, and the code around them simply stops working."""
    bad = {c for c in PAGE if ord(c) < 32 and c not in "\n\r\t"}
    assert bad == set(), f"control characters in the rendered page: {[hex(ord(c)) for c in bad]}"


def test_the_wake_pattern_still_has_its_word_boundaries():
    """Without them, "jarvis" matches inside "jarvisian" and, worse, the phrase-stripping slice
    that follows takes the wrong offset and sends a truncated command."""
    i = PAGE.find("const WAKE=")
    assert i != -1
    line = PAGE[i:PAGE.index("\n", i)]
    assert line.count("\\b") == 2
    assert "\\s+" in line


# ── the server side ─────────────────────────────────────────────────────────────────────────────

def test_the_wake_socket_exists():
    assert "/ws/wake" in _routes()


def test_the_wake_socket_points_at_nomad_voice_over_ws():
    """The browser cannot reach a host-native unit on localhost, which is why this is proxied at
    all. Getting the scheme wrong would leave it dialling http:// over a websocket client."""
    up = server.VOICE_URL.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    assert up.startswith("ws")
    import inspect
    src = inspect.getsource(server.ws_wake)
    assert '"/wake"' in src or "'/wake'" in src


def test_voice_config_names_the_wake_engine_and_a_reachable_signalling_url():
    """`rt_url` is the one address the browser dials itself. WebRTC media is peer-to-peer, so it
    cannot be proxied the way `/ws/wake` and `/api/tts` are, and the page has to be told."""
    c = server.api_voice_config()
    assert c["wake"] == "openwakeword"
    assert c["label"]
    assert c["rt_url"].startswith("http")
    assert not c["rt_url"].endswith("/")


def test_voice_config_is_a_route():
    assert "/api/voice-config" in _routes()


def test_both_websocket_proxies_share_one_relay():
    """They were two copies of the same pump. A third would have been the point at which one of
    them quietly stopped closing the upstream on disconnect."""
    import inspect
    for fn in (server.ws_terminal, server.ws_wake):
        assert "_ws_relay" in inspect.getsource(fn)


# ── push-to-talk goes through the brain ─────────────────────────────────────────────────────────

def test_push_to_talk_sends_the_transcript_to_the_chat():
    """The behaviour change that matters in this batch. The mic used to POST straight to /capture,
    so a spoken "run diagnostics" was filed as a goal to do later instead of being run, and a
    spoken "approve" never reached the gate at all."""
    i = PAGE.index("pttRec.onstop")
    block = PAGE[i:i + 900]
    assert "csend(" in block
    assert "'/capture'" not in block


def test_the_chat_has_one_entry_point_that_takes_text():
    """Voice has no input box to read from. A second code path for spoken turns would be a second
    place to forget the session id, the history window or the router."""
    assert "async function csend(msg, capture, speak)" in PAGE
    assert "function csendMsg(capture){" in PAGE


def test_a_spoken_turn_is_spoken_back():
    i = PAGE.index("async function csend(")
    assert "if(speak) await vspeak(r.reply)" in PAGE[i:i + 1600]


def test_speaking_resolves_only_when_it_has_finished():
    """Conversation mode waits on this. If it resolved when playback started, NOMAD would record
    its own reply as the operator's next turn and answer itself."""
    i = PAGE.index("async function vspeak(")
    block = PAGE[i:i + 700]
    assert "await new Promise" in block and "onended" in block


def test_an_action_turn_refreshes_the_board():
    """The router files captures and clears the gate now, so a chat turn can move the pipeline.
    A page that does not re-read after that shows the operator the state from before they spoke."""
    i = PAGE.index("async function csend(")
    assert "r.intent && r.intent!=='chat'" in PAGE[i:i + 1600]


def test_silence_is_not_transcribed():
    """Whisper handed room tone will invent a sentence, and the router would act on it. Refusing
    below a size floor is cheaper than acting on a command nobody gave."""
    i = PAGE.index("async function transcribe(")
    assert "blob.size<1200" in PAGE[i:i + 400]


# ── the mic is a single shared device ───────────────────────────────────────────────────────────

def test_there_is_one_shared_mic_stream():
    """Four features want the microphone. Asking for a second stream while the first is open is
    how two of them end up holding it and both get silence."""
    assert "async function getMic()" in PAGE
    # Two, and only two. `getMic` serves push-to-talk, the wake loop and conversation mode from one
    # stream; live voice takes its own because it needs echo cancellation the others do not.
    assert PAGE.count("getUserMedia") == 2


def test_the_wake_loop_yields_the_mic_to_push_to_talk_and_conversation():
    i = PAGE.index("async function wakeLoop()")
    assert "if(pttRec||convoMode)" in PAGE[i:i + 300]


def test_live_voice_disarms_the_wake_word_before_taking_the_mic():
    """Both want the device for the length of a session. Leaving the wake word armed means the
    listen loop keeps grabbing clips underneath an open peer connection."""
    i = PAGE.index("liveBtn.onclick")
    block = PAGE[i:i + 500]
    assert "stopOWW()" in block and "wakeOn=false" in block


def test_turning_the_wake_word_off_also_ends_a_conversation():
    i = PAGE.index("wakeBtn.onclick")
    assert "convoMode=false" in PAGE[i:i + 400]


# ── the wake word, and its fallback ─────────────────────────────────────────────────────────────

def test_open_wake_word_is_tried_first_and_whisper_is_the_fallback():
    """openWakeWord fires the instant it hears the phrase; the fallback transcribes 3.5 second
    windows. The fast one is preferred and the slow one is what makes it work anywhere."""
    i = PAGE.index("wakeBtn.onclick")
    block = PAGE[i:i + 600]
    assert block.index("startOWW()") < block.index("wakeLoop()")


def test_the_operator_is_told_which_one_they_got():
    """They behave differently enough that the operator needs to know. A wake word that has
    silently fallen back to the slow path reads as a broken one."""
    i = PAGE.index("wakeBtn.onclick")
    assert "local fallback" in PAGE[i:i + 600]


def test_the_wake_socket_failing_upstream_falls_back_rather_than_going_quiet():
    i = PAGE.index("ws.onmessage=ev=>")
    assert "if(d.error){ stopOWW(); if(wakeOn) wakeLoop(); }" in PAGE[i:i + 300]


def test_the_wake_socket_is_same_origin():
    """It is a proxy through the engine precisely so the page never names the voice service."""
    i = PAGE.index("function startOWW()")
    assert "location.host+'/ws/wake'" in PAGE[i:i + 500]


def test_the_mic_is_not_echoed_to_the_speakers():
    """A ScriptProcessor has to be connected to the destination to run at all. Without a zeroed
    gain in the way, the operator hears themselves back."""
    i = PAGE.index("function startOWW()")
    assert "sink.gain.value=0" in PAGE[i:i + 900]


def test_audio_is_downsampled_to_what_the_model_expects():
    """openWakeWord takes 16 kHz mono PCM. A card running at 48 kHz would otherwise send three
    times the audio at the wrong rate, and the model simply never fires."""
    assert "function downsample(buf,inRate,outRate)" in PAGE
    i = PAGE.index("proc.onaudioprocess")
    assert "inRate===16000?" in PAGE[i:i + 400]


def test_starting_the_wake_socket_cannot_hang_forever():
    i = PAGE.index("function startOWW()")
    assert "wake ws timeout" in PAGE[i:i + 2000]


def test_stopping_tears_down_every_node_it_made():
    i = PAGE.index("function stopOWW()")
    block = PAGE[i:i + 400]
    for node in ("o.proc.disconnect", "o.sink.disconnect", "o.src.disconnect",
                 "o.ac.close", "o.ws.close"):
        assert node in block


# ── conversation mode ───────────────────────────────────────────────────────────────────────────

def test_conversation_mode_needs_the_wake_word_only_once():
    i = PAGE.index("async function onWake()")
    block = PAGE[i:i + 900]
    assert "while(convoMode)" in block
    assert "just talk, pause to end" in block


def test_a_silent_window_ends_the_conversation():
    """Ending on silence rather than on a spoken command, because a command to stop listening has
    to be heard to work, and the case you need it in is the one where hearing is the problem."""
    i = PAGE.index("async function onWake()")
    assert "if(!t) break;" in PAGE[i:i + 900]


def test_conversation_mode_waits_for_the_reply_before_listening_again():
    i = PAGE.index("async function onWake()")
    assert "await csend(t,false,true)" in PAGE[i:i + 900]


def test_a_command_spoken_with_the_wake_word_runs_before_the_conversation_opens():
    """"Hey Jarvis, run diagnostics" should run them, not answer "yes?" and wait."""
    i = PAGE.index("async function wakeLoop()")
    block = PAGE[i:i + 900]
    assert "if(cmd.length>1) await csend(cmd,false,true);" in block
    assert block.index("csend(cmd") < block.index("await onWake()")


def test_conversation_mode_does_not_start_twice():
    i = PAGE.index("async function onWake()")
    assert "if(pttRec||convoMode) return;" in PAGE[i:i + 200]


# ── live voice ──────────────────────────────────────────────────────────────────────────────────

def test_live_voice_asks_the_engine_where_to_signal():
    i = PAGE.index("async function rtConfig()")
    assert "/api/voice-config" in PAGE[i:i + 400]


def test_live_voice_has_a_fallback_address_if_the_config_call_fails():
    """One unreachable endpoint should not take out a feature that only needed an address."""
    i = PAGE.index("async function rtConfig()")
    assert "'http://localhost:8200'" in PAGE[i:i + 400]


def test_live_voice_asks_for_echo_cancellation():
    """Barge-in is the point: the operator interrupts while Piper is still speaking. Without echo
    cancellation the VAD hears the reply and treats it as an interruption of itself."""
    i = PAGE.index("async function rtConnect()")
    assert "echoCancellation:true" in PAGE[i:i + 600]


def test_ice_gathering_cannot_hang_the_connection():
    """A STUN server that does not answer would otherwise leave the button lit and nothing
    happening, with no way to tell that from a slow connection."""
    i = PAGE.index("async function rtConnect()")
    assert "setTimeout(r,2000)" in PAGE[i:i + 1600]


def test_a_failed_connection_says_push_to_talk_still_works():
    """WSL2 networking makes this the plausible failure, and the operator needs to know the other
    three voice paths are unaffected rather than assume voice is down."""
    i = PAGE.index("async function rtConnect()")
    assert "Push-to-talk still works" in PAGE[i:i + 1600]


def test_hanging_up_stops_the_tracks_and_not_just_the_connection():
    """Closing the peer connection alone leaves the microphone light on, which is the one piece
    of feedback the operator has about whether this thing is listening."""
    i = PAGE.index("function rtHangup(")
    block = PAGE[i:i + 500]
    assert "getTracks().forEach" in block and "rtPc.close()" in block


def test_the_remote_audio_has_somewhere_to_play():
    assert "<audio id=rt-audio autoplay>" in PAGE
    assert "getElementById('rt-audio')" in PAGE


# ── the controls exist and are wired ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("el", ["id=mic", "id=wake", "id=livevoice", "id=spk", "id=vstatus"])
def test_the_control_is_on_the_page(el):
    assert el in PAGE


@pytest.mark.parametrize("handler", ["mic.onclick=toggleMic", "wakeBtn.onclick=", "liveBtn.onclick="])
def test_the_control_is_wired(handler):
    assert handler in PAGE


def test_a_machine_with_no_microphone_hides_all_of_it():
    """Rather than showing four controls that do nothing when clicked."""
    i = PAGE.index("}else{\n  mic.style.display='none';")
    block = PAGE[i:i + 300]
    assert "wakeBtn.style.display='none'" in block
    assert "liveBtn.style.display='none'" in block
