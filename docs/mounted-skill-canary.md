---
read_when: validating mounted-skill snapshots after a Codex upgrade or a broker deployment change.
---

# Run this real-model check after Codex upgrades that may affect skill attachments

`scripts/check-mounted-skill.py` is an opt-in release check for the broker's per-turn mounted-skill path. It starts two real turns in one new broker thread, so it consumes the selected existing Codex profile. It does not copy credentials, log in, or create an auth profile. Unit tests use fake broker responses and never substitute for this check.

Mount `examples/bundles/mounted-skill-canary-v1` beneath an allowed bundle root before using the command. Its manifest assumes that the broker sees the bundle at `/bundles/mounted-skill-canary-v1`; adjust the manifest and command's `--mounted-skill-path` together if your deployment uses another mount point. Also pre-provision an empty, broker-allowed diagnostic workspace. The model writes only two proof JSON files below `.codex-broker-skill-canary/` in that workspace.

Run the command from the broker host or a sidecar that can read the diagnostic workspace. If the broker sees that workspace at a different path, pass the local mount separately with `--local-workspace`.

```sh
export CODEX_BROKER_INTERNAL_KEY='…'
uv run python scripts/check-mounted-skill.py \
  --base-url http://127.0.0.1:3400 \
  --owner release-operator \
  --profile default \
  --bundle-id mounted-skill-canary-v1 \
  --workspace /workspaces/skill-canary \
  --local-workspace /srv/broker-workspaces/skill-canary \
  --fixture-dir examples/bundles/mounted-skill-canary-v1/skills/mounted-skill-canary \
  --mounted-skill-path /bundles/mounted-skill-canary-v1/skills/mounted-skill-canary
```

Credentials belong in `CODEX_BROKER_INTERNAL_KEY` or the process environment used by the normal broker client; never put them on the command line. Select an existing owner/profile explicitly with `--owner`, `--profile`, and, where required, `--auth-principal-id`.

The verifier requires both turns to complete, each proof to name the exact per-turn path ending in that broker turn ID and `.agents/skills/mounted-skill-canary/SKILL.md`, both file hashes to match the local mounted fixture, and one matching `security.bundle_skill_snapshot` audit record per turn. It also reads the completed turn's public tool events and requires shell evidence for both files while rejecting log or transcript commands and permission errors. It rejects missing proof files, a repeated overlay, and a source path that does not match the broker audit. If a turn exceeds the timeout, only that turn is interrupted and the script waits for it to become terminal before failing, including when it races to `completed` after the interrupt.
