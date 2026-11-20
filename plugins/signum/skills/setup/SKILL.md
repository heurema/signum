---
name: setup
description: Welcome someone who selected Signum post-install setup, or initialize a specific project when they explicitly ask to use Signum there.
---

# Set up Signum

Reply in the user's language. During post-install onboarding, briefly explain that Signum is available in Codex and each project opts in separately. Ask which project to connect, with the option to skip. If a workspace is already selected, name it in the question. The installation or the click to open this conversation is not project consent. If the user already explicitly requested initialization for a known project, proceed directly. An unrelated message does not trigger onboarding.

In the selected existing project root run `python3 -B <this-skill>/../project-intent/scripts/signum.py --project ROOT init`. Use the named project and check the returned absolute path. Show where its decisions and delivery records live. The root AGENTS.md cue helps future chats discover the opt-in. Existing project instructions are retained. Initialization is safe to repeat.

When a project already has its own journal, “let’s start this project again” normally asks for [project orientation](../project-intent/SKILL.md#starting-a-project-or-deciding-where-to-begin). Give the owner a short finding and a recommended next step grounded in the existing materials. Reserve technical initialization for an explicit setup request. Create a Rust workspace when the owner selects that step. An owner who skips setup leaves every project as it was.
