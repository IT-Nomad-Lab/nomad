"""Deterministic style linter — the tells that mark text as machine-written.

No model, no GPU, no network. Pure regex and arithmetic, so it runs in microseconds and can be
imported anywhere (the service, the CLI, an agent, a git hook). It answers a different question
than the statistical detector next door: Binoculars says "a model probably wrote this", the linter
says "HERE is the sentence that gives it away, and here is what to do about it".

Two families of check:

  constructions  the phrasings that survived RLHF into every model's default voice — the
                 "it's not X, it's Y" antithesis, transition scaffolding, rule-of-three stacking,
                 the restating summary, the giveaway vocabulary.
  distribution   the statistics of the prose itself — sentence-length variance (burstiness),
                 paragraph uniformity, contraction rate, concrete-detail density, lexical variety.
                 Models write evenly. People do not.

Rules marked JDE come straight from prompts/JDE_STYLE.md, so a text that passes the linter is
also in-voice, not just undetectable.

  lint(text) -> {"score", "band", "metrics", "findings", "summary"}
"""
import re
import statistics
from typing import Any

# ── vocabulary ────────────────────────────────────────────────────────────────────────────────
# Words that appear far more often in model output than in human prose. The JDE set is the house
# ban list; the LLM set is the wider tell list every classifier and every editor now knows.
BANNED_JDE = [
    "leverage", "leveraging", "synergize", "synergy", "paradigm", "utilize", "utilizing",
    "ecosystem",
]
BANNED_LLM = [
    "delve", "delving", "underscore", "underscores", "pivotal", "testament", "landscape",
    "realm", "tapestry", "robust", "seamless", "seamlessly", "holistic", "comprehensive",
    "myriad", "foster", "fostering", "unlock", "unlocking", "elevate", "elevating",
    "game-changer", "game changer", "cutting-edge", "harness", "harnessing", "vibrant",
    "intricate", "nuanced", "multifaceted", "crucial", "vital", "profound", "meticulous",
    "meticulously", "resonate", "resonates", "embark", "navigating the", "dive deep",
    "deep dive", "at its core", "the truth is", "in the ever-evolving", "ever-evolving",
    "in today's", "world of", "when it comes to", "plays a key role", "plays a crucial role",
]
TRANSITIONS = [
    "moreover", "furthermore", "additionally", "in conclusion", "ultimately", "that said",
    "it's worth noting", "it is worth noting", "it's important to note",
    "it is important to note", "notably", "consequently", "nevertheless", "in essence",
    "in summary", "to sum up", "overall", "firstly", "secondly", "lastly",
]
HEDGES = [
    "arguably", "perhaps", "it seems", "somewhat", "relatively", "fairly", "generally speaking",
    "in many ways", "to some extent", "one could argue", "it may be", "might be considered",
    "can be seen as", "tends to be",
]

# ── constructions ─────────────────────────────────────────────────────────────────────────────
# (rule id, compiled pattern, severity, human-readable fix)
CONSTRUCTIONS: list[tuple[str, re.Pattern, str, str]] = [
    ("em-dash", re.compile(r"[—–]"), "high",
     "JDE hard rule: no em-dashes. Split the sentence or use a period."),
    ("antithesis", re.compile(
        r"\b(?:it'?s|this is|that'?s|we'?re)\s+not\s+(?:just\s+)?[^.!?,;]{1,60}[,.]\s*"
        r"(?:it'?s|this is|that'?s|we'?re)\s+", re.I), "high",
     "The 'it's not X, it's Y' tic is the single loudest model tell. State Y and drop X."),
    ("not-just", re.compile(r"\b(?:isn'?t|aren'?t|not)\s+just\s+about\b", re.I), "high",
     "'Not just about X' is the same antithesis in a coat. Say what it IS about."),
    ("not-only", re.compile(r"\bnot only\b[^.!?]{0,80}\bbut also\b", re.I), "medium",
     "'Not only ... but also' is textbook filler. Two sentences, or one with 'and'."),
    ("transition", re.compile(r"(?:^|(?<=[.!?]\s))\s*(" + "|".join(
        re.escape(t) for t in TRANSITIONS) + r")\b[,\s]", re.I), "medium",
     "Transition scaffolding. Cut the word; the sentence stands without it."),
    ("rhetorical-open", re.compile(
        r"(?:^|\n)\s*(?:so\s+)?(?:what|why|how|ever wonder)\b[^.!?\n]{5,90}\?\s*(?:\n|$)", re.I),
     "medium", "Rhetorical question as an opener. Make the claim instead of asking it."),
    ("fluff-open", re.compile(
        r"^\s*(?:in today'?s\b|in the world of\b|in an era\b|great question\b|"
        r"let'?s dive\b|let'?s explore\b|imagine\b[^.!?\n]{0,60}\bfor a moment\b)", re.I),
     "high", "JDE hard rule: no fluff opener. Open on the hook."),
    ("summary-close", re.compile(
        r"(?:^|\n)\s*(?:in conclusion|to conclude|in summary|to summarize|wrapping up|"
        r"all in all|at the end of the day)\b", re.I), "high",
     "Restating summary. The punchline replaces it."),
    ("rule-of-three", re.compile(
        r"\b\w+ly\b,?\s+\b\w+ly\b,?\s+and\s+\b\w+ly\b|"
        r"\b(\w+),\s+(\w+),\s+and\s+(\w+)\b(?=\s+(?:approach|solution|strategy|process|"
        r"system|platform|experience|results?|outcomes?))", re.I), "medium",
     "Rule-of-three stacking. Keep the one that carries weight."),
    ("hedge", re.compile(r"\b(" + "|".join(re.escape(h) for h in HEDGES) + r")\b", re.I),
     "medium", "JDE hard rule: no hedging. Commit to the claim or cut it."),
    ("ai-disclaimer", re.compile(
        r"\b(?:as an ai|i'?m an ai|language model|i cannot provide|it'?s important to remember)\b",
        re.I), "high", "Assistant boilerplate. Delete it."),
    ("emoji-bullet", re.compile(r"(?:^|\n)\s*(?:[\U0001F300-\U0001FAFF☀-➿])\s+\*?\*?\w",
                                re.U), "low",
     "Emoji-headed bullets read as generated listicle. Use plain bullets or none."),
]

_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_SENT_SPLIT = re.compile(r"(?<=[.!?])[\"')\]]*\s+")
_CONTRACTION = re.compile(r"\b\w+'(?:s|t|re|ve|ll|d|m)\b", re.I)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+(.*)$")
_PASSIVE = re.compile(
    r"\b(?:is|are|was|were|be|been|being)\s+(?:\w+ly\s+)?\w+(?:ed|en)\b(?:\s+by\b)?", re.I)
# Concrete detail: digits, CamelCase/product names, and spelled-out quantities. The last group
# matters — "eighty days" and "four months" are specifics, and counting only digits punished
# perfectly good prose for writing its numbers out.
_NUMWORD = (r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|forty|"
            r"fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|dozen|half|"
            r"twice|thrice")
_CONCRETE = re.compile(
    r"\b\d[\d,.:%]*\b"                                     # 30, 4.2, 90%
    r"|\b[A-Z][a-z]*[A-Z][A-Za-z0-9]*\b"                    # PostgreSQL, NocoDB, GPT4
    r"|(?i:\b(?:" + _NUMWORD + r")\s+\w{3,}s?\b)"          # four months, eighty days
    r"|(?i:\b(?:jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d|\b(?:19|20)\d\d\b)")


def _sentences(text: str) -> list[str]:
    body = re.sub(r"```.*?```", " ", text, flags=re.S)          # code blocks are not prose
    body = re.sub(r"^\s*#+\s.*$", " ", body, flags=re.M)        # nor are headings
    parts = [s.strip() for s in _SENT_SPLIT.split(body) if s.strip()]
    return [p for p in parts if len(_WORD.findall(p)) >= 2]


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _excerpt(text: str, start: int, end: int, pad: int = 34) -> str:
    a, b = max(0, start - pad), min(len(text), end + pad)
    return ("…" if a else "") + " ".join(text[a:b].split()) + ("…" if b < len(text) else "")


def metrics(text: str) -> dict[str, Any]:
    """The distributional half: how the prose is shaped, independent of what it says."""
    sents = _sentences(text)
    lens = [len(_WORD.findall(s)) for s in sents] or [0]
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    para_lens = [len(_sentences(p)) for p in paras] or [0]
    words = _WORD.findall(text)
    lower = [w.lower() for w in words]
    bullets = [m.group(1) for m in (_BULLET.match(l) for l in text.splitlines()) if m]
    bullet_lens = [len(_WORD.findall(b)) for b in bullets]

    mean_len = statistics.fmean(lens)
    sd = statistics.pstdev(lens) if len(lens) > 1 else 0.0
    # Burstiness = coefficient of variation of sentence length. Human prose swings; models cruise.
    burstiness = (sd / mean_len) if mean_len else 0.0
    bullet_cv = 0.0
    if len(bullet_lens) > 2:
        bm = statistics.fmean(bullet_lens)
        bullet_cv = (statistics.pstdev(bullet_lens) / bm) if bm else 0.0

    return {
        "words": len(words),
        "sentences": len(sents),
        "paragraphs": len(paras),
        "mean_sentence_len": round(mean_len, 1),
        "sd_sentence_len": round(sd, 1),
        "burstiness": round(burstiness, 3),
        "shortest_sentence": min(lens),
        "longest_sentence": max(lens),
        "short_sentences": sum(1 for n in lens if n <= 6),
        "paragraph_cv": round(
            (statistics.pstdev(para_lens) / statistics.fmean(para_lens))
            if len(para_lens) > 1 and statistics.fmean(para_lens) else 0.0, 3),
        "bullets": len(bullets),
        "bullet_cv": round(bullet_cv, 3),
        "contractions_per_100w": round(100 * len(_CONTRACTION.findall(text)) / max(len(words), 1), 2),
        "first_person": sum(1 for w in lower if w in ("i", "i'm", "i've", "my", "me", "we", "our")),
        "concrete_per_100w": round(100 * len(_CONCRETE.findall(text)) / max(len(words), 1), 2),
        "passive_hits": len(_PASSIVE.findall(text)),
        "type_token_ratio": round(len(set(lower)) / max(len(lower), 1), 3),
    }


def findings(text: str) -> list[dict[str, Any]]:
    """The construction half: every phrasing that gives the game away, with its location."""
    out: list[dict[str, Any]] = []
    for rule, pat, sev, fix in CONSTRUCTIONS:
        for m in pat.finditer(text):
            out.append({"rule": rule, "severity": sev, "line": _line_of(text, m.start()),
                        "excerpt": _excerpt(text, m.start(), m.end()), "fix": fix})

    for name, words, sev, fix in (
        ("banned-word-jde", BANNED_JDE, "high",
         "JDE hard rule: this word is banned in the house voice. Say the plain thing."),
        ("banned-word-llm", BANNED_LLM, "medium",
         "Classifier bait. This word is overrepresented in model output; replace it."),
    ):
        for w in words:
            pat = re.compile(r"\b" + re.escape(w).replace(r"\ ", r"\s+") + r"\b", re.I)
            for m in pat.finditer(text):
                out.append({"rule": name, "severity": sev, "line": _line_of(text, m.start()),
                            "excerpt": _excerpt(text, m.start(), m.end()),
                            "fix": f"'{m.group(0)}' → {fix}"})

    m = metrics(text)
    if m["sentences"] >= 5:
        if m["burstiness"] < 0.35:
            out.append({"rule": "flat-rhythm", "severity": "high", "line": 1,
                        "excerpt": f"burstiness {m['burstiness']} over {m['sentences']} sentences",
                        "fix": "Sentence length barely moves. Cut one sentence to three words and "
                               "let another run long."})
        elif m["burstiness"] < 0.5:
            out.append({"rule": "flat-rhythm", "severity": "medium", "line": 1,
                        "excerpt": f"burstiness {m['burstiness']}",
                        "fix": "Rhythm is too even. Add a short punch line mid-paragraph."})
        if m["short_sentences"] == 0:
            out.append({"rule": "no-short-sentence", "severity": "medium", "line": 1,
                        "excerpt": f"shortest sentence is {m['shortest_sentence']} words",
                        "fix": "Not one short sentence in the piece. Add at least one under six "
                               "words."})
    if m["bullets"] >= 3 and m["bullet_cv"] < 0.25:
        out.append({"rule": "uniform-bullets", "severity": "medium", "line": 1,
                    "excerpt": f"{m['bullets']} bullets, length CV {m['bullet_cv']}",
                    "fix": "Bullets are all the same size and shape. Vary them or write prose."})
    if m["words"] >= 150 and m["contractions_per_100w"] < 0.5:
        out.append({"rule": "no-contractions", "severity": "medium", "line": 1,
                    "excerpt": f"{m['contractions_per_100w']} contractions per 100 words",
                    "fix": "Nobody writes this formally by choice. Use don't, it's, you're."})
    if m["words"] >= 150 and m["concrete_per_100w"] < 1.0:
        out.append({"rule": "no-specifics", "severity": "high", "line": 1,
                    "excerpt": f"{m['concrete_per_100w']} numbers or names per 100 words",
                    "fix": "No numbers, tools, dates or names. Generic text reads as generated. "
                           "Put the real detail in."})
    if m["words"] >= 200 and m["first_person"] == 0:
        out.append({"rule": "no-stake", "severity": "medium", "line": 1,
                    "excerpt": "no first-person anywhere",
                    "fix": "No writer in the piece. Say what YOU did and what you think."})
    if m["passive_hits"] and m["words"] and m["passive_hits"] / m["words"] > 0.02:
        out.append({"rule": "passive-heavy", "severity": "medium", "line": 1,
                    "excerpt": f"{m['passive_hits']} passive constructions",
                    "fix": "JDE hard rule: active where active works."})

    out.sort(key=lambda f: ({"high": 0, "medium": 1, "low": 2}[f["severity"]], f["line"]))
    return out


WEIGHT = {"high": 9, "medium": 4, "low": 1}


def lint(text: str) -> dict[str, Any]:
    """Score the prose on style alone. 100 = nothing flagged. Below 70, rewrite before shipping."""
    f = findings(text)
    m = metrics(text)
    penalty = sum(WEIGHT[x["severity"]] for x in f)
    # Long pieces get more surface area for hits, so scale the penalty by length past 300 words.
    scale = max(1.0, m["words"] / 300)
    score = max(0, round(100 - penalty / scale))
    band = "human" if score >= 80 else "borderline" if score >= 60 else "machine"
    counts: dict[str, int] = {}
    for x in f:
        counts[x["rule"]] = counts.get(x["rule"], 0) + 1
    return {"score": score, "band": band, "metrics": m, "findings": f,
            "summary": {"total": len(f), "by_rule": counts,
                        "high": sum(1 for x in f if x["severity"] == "high")}}
