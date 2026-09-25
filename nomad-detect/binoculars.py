"""Binoculars — the statistical detector. Two local LMs, no training, no API, no data leaving the box.

The method (Hans et al., 2024, "Spotting LLMs With Binoculars") is the strongest zero-shot detector
published, and it is the same idea every commercial checker sells: machine text is too predictable.
The trick is what it normalizes by.

Raw perplexity alone fails, because a human writing about a narrow technical subject also looks
predictable. Binoculars divides the text's perplexity under one model (the PERFORMER, an
instruction-tuned model) by the cross-perplexity between two models (the OBSERVER, the same
model before instruction tuning). The denominator answers "how surprising is this text to one
model given what ANOTHER model expected?" That cancels out the topic and leaves the machine-ness.

  score = perplexity(text | performer) / cross-perplexity(observer ‖ performer)

  LOW score  → the text sat right where a model expected it → machine-written.
  HIGH score → the text kept surprising the performer in ways the observer did not predict → human.

Both models must share a tokenizer, so the pair is one family: base + instruct. Default is the
Qwen2.5-1.5B pair (~6 GB VRAM in bf16, ~1 s for a LinkedIn post on the 5090). Set the 7B pair on a
bigger card for more separation.

Thresholds are PAIR-SPECIFIC. The published 0.9015 belongs to the Falcon-7B pair; any other pair
needs its own, which is what calibrate.py writes into thresholds.json. Until then the defaults here
are a starting point, not a verdict to trust blindly.
"""
import json
import os
import pathlib
from typing import Any

import torch
import torch.nn.functional as F
from transformers import AutoModelForCausalLM, AutoTokenizer

OBSERVER = os.environ.get("NOMAD_DETECT_OBSERVER", "Qwen/Qwen2.5-1.5B")
PERFORMER = os.environ.get("NOMAD_DETECT_PERFORMER", "Qwen/Qwen2.5-1.5B-Instruct")
DEVICE = os.environ.get("NOMAD_DETECT_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
WINDOW = int(os.environ.get("NOMAD_DETECT_WINDOW", "512"))      # tokens scored per chunk
MIN_TOKENS = int(os.environ.get("NOMAD_DETECT_MIN_TOKENS", "48"))
THRESHOLDS = pathlib.Path(__file__).with_name("thresholds.json")

# Fallback bands, replaced per model pair by calibrate.py. accuracy = balanced; low_fpr = the
# stricter line to clear when a false "AI" accusation is expensive.
DEFAULTS = {"accuracy": 0.9015, "low_fpr": 0.8536}


def thresholds(pair: str | None = None) -> dict[str, float]:
    pair = pair or f"{OBSERVER}|{PERFORMER}"
    try:
        data = json.loads(THRESHOLDS.read_text())
        t = data.get(pair)
        if t:
            return {"accuracy": float(t["accuracy"]), "low_fpr": float(t["low_fpr"]),
                    "calibrated": True, "n_samples": t.get("n_samples"),
                    "auc_by_source": t.get("auc_by_source"), "blind_to": t.get("blind_to"),
                    "style_auc_by_source": t.get("style_auc_by_source"),
                    "blind_to_both": t.get("blind_to_both"), "pair": pair}
    except Exception:
        pass
    return {**DEFAULTS, "calibrated": False, "pair": pair}


def _load(name: str, dtype, device: str):
    """transformers renamed torch_dtype→dtype in v5. Support both so this runs on either."""
    try:
        return AutoModelForCausalLM.from_pretrained(name, dtype=dtype, device_map=device).eval()
    except TypeError:
        return AutoModelForCausalLM.from_pretrained(
            name, torch_dtype=dtype, device_map=device).eval()


class Binoculars:
    """One loaded pair, reused across requests. Loading costs ~15 s; scoring costs ~1 s."""

    def __init__(self, observer: str = OBSERVER, performer: str = PERFORMER, device: str = DEVICE):
        self.pair = f"{observer}|{performer}"
        self.device = device
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
        self.tokenizer = AutoTokenizer.from_pretrained(observer)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.observer = _load(observer, dtype, device)
        self.performer = _load(performer, dtype, device)

    def _chunks(self, text: str) -> list[dict[str, torch.Tensor]]:
        ids = self.tokenizer(text, return_tensors="pt", truncation=False).input_ids[0]
        out = []
        for i in range(0, len(ids), WINDOW):
            piece = ids[i:i + WINDOW]
            if len(piece) < MIN_TOKENS and out:       # tail too short to score on its own
                break
            if len(piece) < MIN_TOKENS and not out:
                return []
            out.append({"input_ids": piece.unsqueeze(0).to(self.device),
                        "attention_mask": torch.ones_like(piece).unsqueeze(0).to(self.device)})
        return out

    @torch.no_grad()
    def _score_chunk(self, enc: dict[str, torch.Tensor]) -> float:
        obs = self.observer(**enc).logits
        per = self.performer(**enc).logits
        ids, mask = enc["input_ids"], enc["attention_mask"]

        # perplexity of the actual tokens under the performer (next-token, so shift by one)
        sl, lb = per[..., :-1, :].contiguous(), ids[..., 1:].contiguous()
        sm = mask[..., 1:].contiguous()
        ce = F.cross_entropy(sl.transpose(1, 2).float(), lb, reduction="none")
        ppl = ((ce * sm).sum(1) / sm.sum(1)).item()

        # cross-perplexity: observer's distribution as the target, performer's logits as the input
        v = obs.shape[-1]
        p = F.softmax(obs.float(), dim=-1).view(-1, v)
        xce = F.cross_entropy(per.float().view(-1, v), p, reduction="none").view(ids.shape)
        xppl = ((xce * mask).sum(1) / mask.sum(1)).item()

        del obs, per, p
        return ppl / xppl if xppl else float("nan")

    def score(self, text: str) -> dict[str, Any]:
        """Score one text. Returns the aggregate plus the per-chunk detail, because a single
        machine-written paragraph inside human text is exactly what the mean would hide."""
        chunks = self._chunks(text)
        n_tokens = len(self.tokenizer(text).input_ids)
        if not chunks:
            return {"ok": False, "error": f"too short to score ({n_tokens} tokens, need "
                                          f"{MIN_TOKENS})", "tokens": n_tokens}
        scores = [self._score_chunk(c) for c in chunks]
        agg = sum(scores) / len(scores)
        t = thresholds(self.pair)
        band = ("machine" if agg < t["low_fpr"] else
                "borderline" if agg < t["accuracy"] else "human")
        return {"ok": True, "score": round(agg, 4), "band": band,
                "chunk_scores": [round(s, 4) for s in scores],
                "most_machine_chunk": round(min(scores), 4),
                "tokens": n_tokens, "chunks": len(chunks), "thresholds": t,
                "pair": self.pair, "device": self.device}


_engine: Binoculars | None = None


def engine() -> Binoculars:
    global _engine
    if _engine is None:
        _engine = Binoculars()
    return _engine
