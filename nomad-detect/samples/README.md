# calibration samples

Two piles of text. The detector fits its thresholds to the gap between them, so what goes in here
decides whether the numbers mean anything.

```
human/   real human writing. YOURS, ideally — your posts, emails, docs.
ai/      the same kind of thing, written by the models you actually use.
```

`human/` is gitignored. Your own writing stays on this box.

## What ships in the repo

`ai/` holds a seed set generated on 2026-09-10 through the LiteLLM gateway, named by source so
`calibrate.py` can report per-model separation:

| prefix | model | why it is here |
|---|---|---|
| `fast_` | llama3.1:8b (local Ollama) | the local workhorse |
| `gpt_` | gpt-4o | the cloud default |
| `opus_` | Claude Opus | the hard case, and the model Josias actually writes with |
| `published_` | the content-creation pipeline (Opus in JDE_STYLE) | the output this service actually exists to audit |

`published_*` is every piece in `~/content-creation` that was published between 2026-03 and
2026-09: 41 LinkedIn posts and 10 articles, NFKC-normalised (LinkedIn "bold" is
mathematical-alphanumeric codepoints) with the production markers — `[COVER IMAGE]`, `[DIAGRAM: …]`
— stripped. They belong in `ai/`, not `human/`. `master_index.json` records how each one was made:
Claude drafted it in the house voice from Josias's lived experience, and Josias edited lightly
before publishing (post_041: he bolded one line and joined two others). Putting them in `human/`
would fit the thresholds so that the pipeline's own output reads as human by definition — the one
mistake that makes a detector worse than no detector.

The `human/` seed used to fit the first thresholds was twelve essays by Paul Graham and Joel
Spolsky, published between 2000 and 2015 — long before an LLM could have written them. They are
NOT committed, and they are a stand-in, not the target. Professional essays are not LinkedIn posts,
and neither is your voice.

## Replace the seed

`WRITING_TASKS.md` (next to this file, gitignored) holds twenty writing tasks: twelve posts, three
article sections, two emails, a leadership message, a decision record and one free topic, all in
the JDE voice so the human pile matches the published one. Write between the markers, then:

```bash
cd nomad-detect
.venv/bin/python import_writing.py              # filled pieces → human/josias_NN.txt + JDE report
.venv/bin/python import_writing.py --drop-seed  # at 20: move the stand-ins to human-seed/
.venv/bin/python calibrate.py --human samples/human --ai samples/ai
```

The JDE report is for fixing lines by hand. A model that touches a piece makes it a mixed sample.

Everything the detector says about your drafts gets more accurate the moment you do this. Until
then `/health` reports `calibrated: false` and every verdict carries the caveat.

## What the 2026-09-18 refit found

Adding `published_` answered the question the service was built for, and the answer is bad:

| source | Binoculars AUC | style AUC |
|---|---|---|
| gpt-4o | 0.983 | 0.996 |
| llama3.1:8b | 0.817 | 0.996 |
| Claude Opus (raw) | 0.367 | 0.833 |
| **published (Opus in JDE_STYLE)** | **0.415** | **0.516** |

51 of 51 published pieces land in band `human` on BOTH channels. The statistical channel was
already blind to Opus; the style channel — the documented fallback for exactly that case — is a
coin flip here, because the pipeline runs `content-creation/tools/style_check.py` on every draft
before it ships. The linter is inside the loop it is being asked to judge, so it cannot judge it.

That is now recorded rather than implied: `calibrate.py` scores both channels and writes
`style_auc_by_source` + `blind_to_both`, and a verdict on this class of text says so outright —
*"NEITHER channel separates published from human writing; a pass on that kind of text means
nothing, only a human read does."* The thresholds themselves did not move (0.9822 / 0.9562); the
FPR cap pins them to the human pile.

The fix is not a better threshold. It is a real `human/` pile — twenty pieces Josias wrote himself,
unedited by a model — which is still the open item under "Replace the seed" above.
