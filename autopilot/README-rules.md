# How the autopilot rules fit together

DRAFT for operator review (task #874).

Every worker run reads two files, in this order:
1. `~/nomad/docs/AUTONOMY.md`, the shared rules. Drafted here as `AUTONOMY.template.md`.
2. `<repo>/AUTONOMY.md`, the project's own file, if it has one.

The shared file sets the ceiling. A project file can only lower it.

## What a project file may do

- **Add restrictions.** Move items from "you decide" to "reserved" (RuneMatch reserves its
  assembly layering and the `RuneMerge` namespace), add never-do items, forbid paths.
- **Restate its test commands** and locks, as declared in its `autopilot:` entry in
  `projects.yaml`: fast check, pre-merge gate, milestone gate, build, known failures, flake set,
  lock wrappers, files the tests dirty and how to restore them.
- **Add project context** a worker needs: which agent roles to use, where the test docs live,
  allowed pipeline licences.

## What it may not do

- Widen anything. It can't move an item from "reserved" to "you decide", allow a command the
  permission profile denies, or relax a baseline test rule.
- Change the cycle, the result outcomes, or who merges.
- Declare a test command that isn't in `projects.yaml`. The permission profile is generated from
  that entry, so a command only in `AUTONOMY.md` would be refused anyway.

If a project file reads as widening a rule, the shared rule wins and the worker says so in its
result. Workers never edit either file; both are agent config and belong to Josias.

## Moving RuneMatch over (R13)

`RuneMatch/AUTONOMY.md` keeps only what's specific to it: the Unity compile and test commands,
the PlayMode lock, the known reds (CV5a, CV5b), the screenshot and TMP restore, the art pipeline
licences, and the layering restriction. Its cycle, merge section, weekly digest, budget and
queue-feeding sections go, because the supervisor now does those.
