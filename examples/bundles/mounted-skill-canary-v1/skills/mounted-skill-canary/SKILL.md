---
name: mounted-skill-canary
description: Prove that the current turn can read its attached skill snapshot and a relative fixture.
---

# Mounted Skill Canary

When an operator asks for the mounted-skill regression check, use the skill attachment supplied for the current turn. Use a sandboxed shell command to read this `SKILL.md` and `fixtures/current-snapshot.txt` relative to this skill directory. Do not discover either file through logs, events, transcripts, earlier turns, or another workspace.

Write the requested JSON proof in the current diagnostic workspace. It must contain these string fields:

- `skillPath`: absolute path of the `SKILL.md` actually read.
- `fixturePath`: absolute path of the fixture actually read.
- `skillSha256`: SHA-256 of the actual `SKILL.md` bytes.
- `fixtureSha256`: SHA-256 of the actual fixture bytes.
- `readMethod`: exactly `attached-skill-relative`.

The proof contains no other task output. Do not make network requests or modify files besides that proof.
