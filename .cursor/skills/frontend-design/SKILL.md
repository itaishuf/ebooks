---
name: frontend-design
description: Create distinctive, production-grade frontend interfaces with high design quality, and adapt them to this repo's static Alpine.js + Supabase session architecture. Use when building or refining `static/index.html`, improving UI polish, or implementing frontend auth/session flows.
license: Complete terms in LICENSE.txt
---

# Frontend Design

Use this skill for frontend work in this repo, especially changes to `static/index.html`.

The goal is not just to make the UI prettier. It should stay production-grade, cohesive, and compatible with the existing Alpine.js app, Supabase magic-link auth flow, and protected backend routes.

## Core design stance

Choose a clear aesthetic direction and execute it consistently.

- Prefer one strong visual idea over a pile of unrelated flourishes
- Keep layouts intentional, not generic card-stacks with random gradients
- Use distinctive typography and a controlled palette
- Add depth, texture, and motion only when they support the chosen feel
- Preserve clarity for search, download, auth, and job-status flows

Avoid generic AI-looking UI:

- overused fonts like Inter, Roboto, Arial, or default system stacks
- purple-on-white SaaS gradients by default
- random glassmorphism, neon, or dashboard tropes that do not fit the app
- visual churn that makes the workflow harder to scan

## Repo-specific frontend constraints

This app is a single static HTML page with Alpine.js and CDN-loaded dependencies.

- Keep frontend work inside `static/index.html` unless there is a strong reason not to
- Do not introduce a build step, framework migration, or asset pipeline unless the user explicitly asks
- Prefer CSS variables for palette, spacing, shadows, and typography tokens
- Keep changes compatible with the current security model and CSP expectations in `service.py`
- Assume the frontend talks to the backend through authenticated fetches, not query-param API keys

## Auth and session model

The current frontend flow is Supabase session-based.

- The page fetches `/auth/config` to get `supabase_url` and `supabase_publishable_key`
- Supabase magic-link auth is the sign-in mechanism
- Protected requests send `Authorization: Bearer <token>`
- Signed-out, signed-in, loading, and auth-error states are first-class UI states

When editing auth UI:

- keep the auth card and signed-in user state easy to understand
- preserve clear feedback for sending magic links, signing in, signing out, and expired sessions
- do not reintroduce shared API-key UX

## Backend contract to preserve

The frontend should stay aligned with the backend route contract.

- Public routes: `/`, `/health`, `/auth/config`
- Protected read routes: `GET /search`, `GET /jobs/{job_id}`
- Protected write routes: `POST /download`, `POST /download/md5`

If the UI changes these flows, make sure the request method, payload shape, and auth headers still match the backend.

## Alpine.js patterns for this repo

- Keep state in the main Alpine app object instead of scattering globals
- Reuse helpers like `parseError(res)` and the authenticated fetch wrapper instead of duplicating error handling
- When pushing into reactive arrays like `activeJobs`, mutate the proxied array/object reference, not a stale external object
- Keep auth, search, and job polling states explicit and easy to reason about

## Job status UX

The backend and frontend share the same ordered job steps:

`queued` -> `fetching_isbn` -> `searching` -> `downloading` -> `sending` -> `done`

Failure state: `error`

If you change the status UI, preserve these keys unless the backend is updated too.

## Design guidance for this app

This product works best when it feels warm, calm, and book-oriented rather than hyper-technical.

- favor editorial, library, storybook, or tactile reading-adjacent aesthetics
- make the auth and search surfaces feel welcoming, not enterprise-heavy
- keep status and error states readable and trustworthy
- prioritize scanning: users should immediately understand what to type, what is loading, and what to do next

## Implementation checklist

When making frontend changes:

1. Read the relevant `static/index.html` sections first instead of redesigning blindly.
2. Preserve Supabase auth bootstrapping, authenticated fetches, and protected route usage.
3. Keep Alpine state transitions coherent for signed-out, booting, loading, results, and active jobs.
4. Maintain accessibility basics: contrast, focus states, disabled states, readable copy.
5. If you touch status flows, keep the backend/frontend status keys synchronized.
6. Prefer small, cohesive refinements over broad rewrites unless the user asks for a redesign.

## Good outcomes

- A distinctive UI that still feels lightweight and maintainable
- Auth/session UX that matches the backend contract
- Search and download flows that remain obvious and dependable
- Visual polish without adding unnecessary frontend complexity
