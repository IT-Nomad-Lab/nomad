How to work (NOMAD SDLC, from the nomad-operator plugin). The full rules: `git -C ~/nomad show master:docs/SDLC.md` in WSL (from Windows: `wsl.exe -e git -C /home/jdelimam/nomad show master:docs/SDLC.md`). Read them from master before planning or starting work, because other sessions switch the ~/nomad checkout. The short version:

- Green, just do it: anything local you can undo. Edit, test, commit on a feature branch, create branches and worktrees, update nomad-pm tasks.
- Yellow, do it and then tell the operator: merge into the local default branch once the work is done and the tests pass, or restart a shared service. Post the results on the task.
- Red, ask first: `git push`, PRs, anything sent to a person, deleting data, prod targets, spending money, widening network exposure, secrets, editing the guardrails. Never force-push.
- Every piece of work gets its own branch off the default branch. Never build on master/main.
- Size the work first. Small (a fix): one task, no PRD. Medium: a short PRD and an Epic. Large (a new project or service): full PRD with the operator's OK, then a roadmap in nomad-pm.
- Every piece of work has a task in nomad-pm (MCP server `nomad-pm`), and its status matches reality. If the tools are missing, say so. Never skip the tracking without saying so.
- The operator can waive any step or move any action between tiers for one piece of work.
