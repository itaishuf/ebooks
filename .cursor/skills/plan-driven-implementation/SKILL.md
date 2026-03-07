---
name: plan-driven-implementation
description: Execute attached plan documents in this repo while keeping existing TodoWrite items synced. Use when the user says to implement an attached `.plan.md` as specified, not edit the plan file, avoid recreating todos, and work through the plan end to end.
---

# Plan-Driven Implementation

## When to use

Use this skill when the user provides or references an attached plan document and asks to implement it directly.

Common signals:
- "Implement the plan as specified"
- "Do NOT edit the plan file itself"
- "The todos already exist"
- "Mark them as in_progress as you work"
- "Don't stop until you complete all todos"

## Workflow

1. Read the attached plan and inspect the current code surface it touches.
2. Do not edit the plan file unless the user explicitly changes that instruction.
3. Do not recreate TodoWrite items that already exist.
4. Mark the first relevant existing todo `in_progress` before substantial implementation work.
5. Keep exactly one todo `in_progress` at a time.
6. As each plan section is finished, mark that todo `completed` and move the next one to `in_progress`.
7. Implement the plan in the same order unless the code forces a small reordering.
8. Validate before finishing: run focused checks first, then broader non-E2E validation if the change touches shared paths.
9. Run `ReadLints` on the files you changed after substantive edits.

## Repo-specific guardrails

- Use `uv run` for pytest and other Python commands
- Do not start the FastAPI server, systemd service, E2E tests, or live `curl` requests unless the user explicitly asks
- Treat existing uncommitted user changes as real context; work with them instead of reverting them
- If the plan affects auth, routes, or deployment docs, include a coordination note in the final response if another surface still needs follow-up

## Final response checklist

- Summarize the implemented outcome, not the plan text
- List the validation you actually ran
- Call out anything you could not validate
- Mention any required coordination or natural next step only if it remains outside the requested scope
