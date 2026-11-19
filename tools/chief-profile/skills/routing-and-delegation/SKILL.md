---
name: routing-and-delegation
description: Route work to the right lane, whole, and track it home
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [chief, routing, delegation, team]
    category: team
    requires_toolsets: [terminal]
---

# Routing and delegation

## When to Use

Whenever a request is not yours to execute: library work, server
work, or anything a specialist owns (see the lane table in
AGENTS.md).

## Procedure

1. **Name the lane out loud** in your reply to Yan ("That's Media
   Manager's — handing it over"). Silent routing reads as ignoring.
2. **Package the task whole**: the goal in Yan's words, the target
   (channel / system / file), constraints he stated (budget,
   approvals, deadlines), and what "done" looks like. A specialist
   that has to ask for the basics was routed badly.
3. **Hand over through the team's channel** (the board / task
   system the gateway provides). If no mechanical channel exists
   for a lane, tell Yan the handoff is manual — he messages that
   bot — and give him the packaged task text to paste. Never
   pretend a handoff happened that didn't.
4. **Track it**: the routed task joins your open-items list with
   its lane and its expected report (digest / proposal / count).
5. **Close it**: when the lane reports, relay the outcome to Yan in
   digest form (counts first, link where the lane provided one) and
   strike the item. A task that never reports is escalated per
   AGENTS.md, not forgotten.

## Decision table

| Request shape | Lane |
|---|---|
| Fix / identify / organise library files | Media Manager |
| "What's waiting / what got fixed" in the library | Media Manager (point Yan there) |
| Server down, code, installs, Hermes itself broken | Sira — tell Yan |
| Question, draft, lookup, reminder, summary | You |
| Unclear ownership | Ask Yan one question; default to you for a first look, never to a specialist's internals |

## Pitfalls

- **Half-routing**: sending the task but not the constraints —
  the specialist then acts inside the wrong permissions.
- **Doing it yourself "because it's quick"**: quick specialist work
  by the chief is how libraries get wrong captions. Route it.
- **Relaying stale status**: a lane's numbers from an old session
  are not its status. Fresh check or "can't see it".

## Verification

- Every routed task is either reported back, still open on your
  list with its lane named, or escalated. None are in none of those
  states.
