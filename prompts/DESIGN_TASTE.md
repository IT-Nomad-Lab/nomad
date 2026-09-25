# DESIGN_TASTE

A reusable block for any agent that builds or reviews a screen. It exists because a UI that
compiles, renders, and returns 200 can still look like nobody was driving. Diffs don't show
taste; screens do.

Drawn from the working principles behind three commonly cited references for interface taste:
disciplined interaction/motion craft (in the tradition of Emil Kowalski's public writing on
building things like toasts and drawers), and the general "does this look considered" critique
pass that design-rule and live-editing tools exist to enforce. Apply these before calling a UI
change done.

## 1. One loudest thing per screen

If everything is emphasized, nothing is. Pick the single element the eye should land on first —
usually the thing the user came to do or the thing that needs their decision — and make
everything else visibly quieter: smaller, greyer, further down. A screen with three competing
headlines has no headline.

## 2. Motion has a reason or it doesn't exist

Animate only `transform` and `opacity` (cheap, no layout thrash). Duration scales with the
distance/size of the thing moving — a small icon settles in 100-150ms, a full panel takes longer.
Entering elements ease OUT (fast start, slow finish); exiting elements ease IN (slow start, fast
finish). Respect `prefers-reduced-motion`. If you can't say what a specific animation
communicates (arrival, causality, spatial relationship), cut it.

## 3. Every action gets a visible response

Click, submit, toggle: something on screen must change within ~100ms, even if the real work
takes longer (a pressed state, a spinner, a disabled state on the button that was just clicked).
An action with no immediate feedback reads as broken, whether or not it is.

## 4. Restraint over invention

Before adding a new color, shadow, radius, or component pattern, use the one already in the
system. A screen that reuses four things looks like a product. A screen that introduces a fifth
shadow and a new border radius looks like two people built it. Match `lcars.css` / the existing
console vocabulary before proposing a new one.

## 5. Dense is fine, cluttered is not

NOMAD's own house style is a dense HUD, not a minimal marketing page — that's correct for an
operator console. The line is whether a first-time viewer can still tell, in two seconds, what
matters most on the screen. Group related numbers, give groups a label, leave a consistent gutter
between groups. Density without grouping is noise.

## 6. Accessibility is a default, not a pass

Text contrast, visible focus rings, and keyboard reachability are checked at build time, not
bolted on when someone complains. If a control only works with a mouse, it's not done.

## 7. The taste check, before you call it done

Look at the actual rendered screen (not the diff, not the code) and answer honestly:
- What is the single loudest thing on this screen, and is it the right thing?
- Does anything move, flash, or animate without a reason you could state in one sentence?
- Is there a state (empty, loading, error, long text, zero items) you haven't looked at?
- Would this pass if you'd never seen the code, only the pixels?

If you can't answer all four, it isn't done — it's compiled.

## Confidence note

This block is written from general, publicly known interaction-design principles rather than a
byte-for-byte import of any specific external skill pack — this agent had no live fetch access
when it was written (2026-09-22). If the source skills (`taste`, `impeccable`, Emil Kowalski's
packaged skill) get pulled in later, treat this file as a draft to diff against, not a finished
transcription.
