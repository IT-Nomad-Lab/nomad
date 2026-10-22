# Vendored skills (read and vetted 2026-10-15, #1323)

| Skill | Upstream | Licence | Commit |
|---|---|---|---|
| taste-skill | github.com/Leonxlnx/taste-skill (`skills/taste-skill`) | MIT | f624f37 |
| web-design-guidelines | github.com/vercel-labs/agent-skills | see upstream | 063bee9 |
| playwright-cli | github.com/microsoft/playwright-cli (`skills/playwright-cli`) | Apache-2.0 | b85c7a7 |

| image-to-code | github.com/Leonxlnx/taste-skill (`skills/image-to-code-skill`) | MIT | f624f37 |
| awesome-design-md | github.com/VoltAgent/awesome-design-md (`design-md/`, plus our SKILL.md wrapper) | MIT | 3b60532 |

Kept for future projects even when a given site does not use them.
- image-to-code is written for Codex and expects an image generator (nomad-image MCP or
  ElevenLabs `creative_generate_image`). Use it when there is no design; with a Figma design,
  Figma is the source of truth.
- awesome-design-md is reference only: take principles, never another brand's identity.

When to use which:
- A Figma/Penpot design exists: build to it with the Figma MCP, then check with
  web-design-guidelines and playwright-cli. Do not run taste-skill's direction draw: it would
  override the design. Its `inspect`/critique step is fine.
- No design yet: taste-skill picks the direction, then the same checks.
- Update: re-copy from upstream, re-read the diff, bump the commit above.
