"""NOMAD Voice — the single local voice service: real-time conversation + classic TTS/STT/wake.

REAL-TIME (ChatGPT-style, the headline): audio streams both ways over WebRTC (browser echo
cancellation), Silero VAD yields on barge-in, faster-whisper transcribes, a LiteLLM model replies
(streamed), Piper speaks it (streamed).  →  open  http://127.0.0.1:8200/

CLASSIC endpoints (used by the LCARS console's push-to-talk / spoken replies / wake word):
  POST /tts  {text}              -> audio/wav    (Piper)
  POST /stt  (file=<audio blob>) -> {text}       (faster-whisper)
  WS   /wake                     -> {wake,score}  (openWakeWord "hey jarvis")
  GET  /health

Host-native (the real-time WebRTC needs direct networking); the engine/console reach it at
host.docker.internal:8200. Phase 2 swaps the real-time STT->LLM->TTS chain for Moshi (true
full-duplex) inside the same pipeline.
"""
import os
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Request, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.piper.tts import PiperTTSService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

HERE = Path(__file__).resolve().parent
VOICES = HERE / "voices"
PIPER_BIN = os.environ.get("PIPER_BIN", str(Path(sys.executable).parent / "piper"))
PIPER_VOICE = os.environ.get("PIPER_VOICE", "en_GB-alan-medium")   # calm British male — NOMAD's "Jarvis"
PIPER_MODEL = os.environ.get("PIPER_MODEL", str(VOICES / f"{PIPER_VOICE}.onnx"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WAKE_MODEL = os.environ.get("WAKE_MODEL", "hey_jarvis")
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))
LITELLM_BASE = os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000").rstrip("/")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-noop")
NOMAD_MODEL = os.environ.get("NOMAD_VOICE_MODEL", "fast")   # low-latency local default
HOST = os.environ.get("NOMAD_VOICE_HOST", "0.0.0.0")
PORT = int(os.environ.get("NOMAD_VOICE_PORT", "8200"))
SYSTEM = os.environ.get("NOMAD_VOICE_SYSTEM",
    "You are NOMAD, a calm, capable voice assistant. This is a spoken conversation, so keep replies "
    "short and natural — no lists, markdown, or emoji. Answer directly. It's fine to be interrupted.")

ice_servers = [IceServer(urls="stun:stun.l.google.com:19302")]
pcs_map: dict[str, SmallWebRTCConnection] = {}
_whisper = None

app = FastAPI(title="nomad-voice")

CLIENT_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>NOMAD Voice</title>
<style>body{font-family:system-ui,sans-serif;background:#0a0e17;color:#dbe4ff;text-align:center;
padding:48px 16px}h2{font-weight:600}#btn{font-size:18px;padding:14px 28px;border-radius:999px;
border:0;background:#3b5bdb;color:#fff;cursor:pointer}#btn:disabled{opacity:.5}#s{margin-top:20px;
color:#8b98b8;min-height:24px}.dot{display:inline-block;width:10px;height:10px;border-radius:50%;
background:#495057;margin-right:8px;vertical-align:middle}.on{background:#2f9e44;box-shadow:0 0 10px #2f9e44}</style>
</head><body>
<h2>🎙️ NOMAD Voice</h2>
<button id="btn">Connect &amp; talk</button>
<p id="s"><span class="dot" id="d"></span>idle</p>
<audio id="a" autoplay></audio>
<script>
const S=document.getElementById('s'),B=document.getElementById('btn'),A=document.getElementById('a');
function st(t,on){S.innerHTML='<span class="dot'+(on?' on':'')+'"></span>'+t;}
B.onclick=async()=>{B.disabled=true;st('requesting microphone…');
 if(!navigator.mediaDevices){st('no mic API — open via http://localhost:8200 (secure context)');B.disabled=false;return;}
 let stream;try{stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});}
 catch(e){st('mic error: '+e.name+' '+e.message);B.disabled=false;return;}
 st('mic ready — setting up…');
 const pc=new RTCPeerConnection({iceServers:[{urls:'stun:stun.l.google.com:19302'}]});
 stream.getTracks().forEach(t=>pc.addTrack(t,stream));
 pc.ontrack=e=>{A.srcObject=e.streams[0];};
 pc.onconnectionstatechange=()=>{const c=pc.connectionState;if(c==='connected')st('connected — talk! (interrupt any time)',true);else if(c==='failed'||c==='disconnected'){st(c+' — media couldn\\'t connect (WSL2 networking?)',false);B.disabled=false;}else st(c);};
 const offer=await pc.createOffer({offerToReceiveAudio:true});await pc.setLocalDescription(offer);
 st('gathering network…');
 await Promise.race([
   new Promise(r=>{if(pc.iceGatheringState==='complete')return r();const h=()=>{if(pc.iceGatheringState==='complete'){pc.removeEventListener('icegatheringstatechange',h);r();}};pc.addEventListener('icegatheringstatechange',h);}),
   new Promise(r=>setTimeout(r,2000))]);
 st('connecting to NOMAD…');
 try{const res=await fetch('/api/offer',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({sdp:pc.localDescription.sdp,type:pc.localDescription.type})});
  if(!res.ok){st('server error '+res.status);B.disabled=false;return;}
  await pc.setRemoteDescription(await res.json());st('negotiated — establishing audio…');}
 catch(e){st('connect failed: '+e.message);B.disabled=false;}
};
</script></body></html>"""


# ══ real-time voice (WebRTC + Pipecat) ═══════════════════════════════════════════════
@app.get("/", include_in_schema=False)
async def root():
    return HTMLResponse(CLIENT_HTML)


async def run_bot(webrtc_connection: SmallWebRTCConnection):
    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    stt = WhisperSTTService(model=WHISPER_MODEL, device="cpu", compute_type="int8",
                            language=Language.EN)
    tts = PiperTTSService(voice_id=PIPER_VOICE, download_dir=VOICES, use_cuda=False)
    llm = OpenAILLMService(
        model=NOMAD_MODEL, api_key=LITELLM_KEY, base_url=f"{LITELLM_BASE}/v1",
        settings=OpenAILLMService.Settings(system_instruction=SYSTEM, temperature=0.7),
    )
    context = LLMContext()
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()))
    pipeline = Pipeline([transport.input(), stt, user_agg, llm, tts,
                         transport.output(), assistant_agg])
    worker = PipelineWorker(pipeline, params=PipelineParams(
        enable_metrics=True, allow_interruptions=True))

    @transport.event_handler("on_client_connected")
    async def _connected(_t, _c):
        context.add_message({"role": "developer", "content": "Greet the user briefly and offer to help."})
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _disconnected(_t, _c):
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


@app.post("/api/offer")
async def offer(request: dict, background_tasks: BackgroundTasks):
    pc_id = request.get("pc_id")
    if pc_id and pc_id in pcs_map:
        conn = pcs_map[pc_id]
        await conn.renegotiate(sdp=request["sdp"], type=request["type"],
                               restart_pc=request.get("restart_pc", False))
    else:
        conn = SmallWebRTCConnection(ice_servers)
        await conn.initialize(sdp=request["sdp"], type=request["type"])

        @conn.event_handler("closed")
        async def _closed(c: SmallWebRTCConnection):
            pcs_map.pop(c.pc_id, None)

        background_tasks.add_task(run_bot, conn)
    answer = conn.get_answer()
    pcs_map[answer["pc_id"]] = conn
    return answer


# ══ classic TTS / STT / wake (used by the LCARS console) ═════════════════════════════
def whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        ct = "int8" if WHISPER_DEVICE == "cpu" else "float16"
        _whisper = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=ct)
    return _whisper


def _piper_tts(text: str) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        out = tf.name
    try:
        subprocess.run([PIPER_BIN, "-m", PIPER_MODEL, "-f", out],
                       input=text, text=True, capture_output=True, timeout=60, check=True)
        with open(out, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


@app.post("/tts")
async def tts_ep(req: Request):
    body = await req.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)
    try:
        wav = _piper_tts(text[:1200])
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": f"piper failed: {(e.stderr or '')[:300]}"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=500)
    return Response(content=wav, media_type="audio/wav")


@app.post("/stt")
async def stt_ep(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        return JSONResponse({"error": "empty audio"}, status_code=400)
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tf:
        tf.write(data)
        path = tf.name
    try:
        segments, _ = whisper().transcribe(
            path, language="en", vad_filter=True,
            initial_prompt="Conversation with NOMAD, the AI assistant. Hey NOMAD.")
        return {"text": "".join(s.text for s in segments).strip()}
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=500)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@app.websocket("/wake")
async def wake(websocket: WebSocket):
    await websocket.accept()
    import numpy as np
    try:
        import openwakeword
        from openwakeword.model import Model
        paths = [p for p in openwakeword.get_pretrained_model_paths()
                 if WAKE_MODEL in p.lower() or WAKE_MODEL.replace("hey_", "") in p.lower()]
        model = Model(wakeword_model_paths=paths)
    except Exception as e:
        await websocket.send_json({"error": f"wake model unavailable: {str(e)[:120]}"})
        await websocket.close()
        return
    buf = bytearray()
    cooldown = 0
    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if not data:
                continue
            buf += data
            while len(buf) >= 2560:
                frame = np.frombuffer(bytes(buf[:2560]), dtype=np.int16)
                del buf[:2560]
                if cooldown > 0:
                    cooldown -= 1
                    continue
                scores = model.predict(frame)
                if (max(scores.values()) if scores else 0.0) >= WAKE_THRESHOLD:
                    await websocket.send_json({"wake": True, "score": round(float(max(scores.values())), 3)})
                    model.reset()
                    cooldown = 25
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.get("/health")
async def health():
    return {"status": "ok", "service": "nomad-voice", "realtime": True, "model": NOMAD_MODEL,
            "stt": WHISPER_MODEL, "tts": PIPER_VOICE, "wake": WAKE_MODEL, "connections": len(pcs_map)}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    for pc in list(pcs_map.values()):
        await pc.disconnect()
    pcs_map.clear()


app.router.lifespan_context = lifespan

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
