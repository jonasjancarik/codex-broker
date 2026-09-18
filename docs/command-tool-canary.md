---
read_when: validating real command execution after a Codex upgrade or broker image change.
---

# Verify a real command through the Broker API before release

`scripts/check-command-tool.py` starts one real Codex turn through the public Broker HTTP API. The turn uses the selected existing auth profile and a `workspace-write` sandbox to create a random proof artifact. The check requires a native command completion with exit code 0 and independently reads the artifact from the host or sidecar filesystem to verify its unpredictable value.

Use a dedicated empty diagnostic workspace mounted beneath an allowed broker workspace root. Run the verifier from the broker host or a sidecar that can read the same directory. If the broker and verifier see it at different paths, pass both paths explicitly.

```sh
export CODEX_BROKER_INTERNAL_KEY='…'
uv run python scripts/check-command-tool.py \
  --base-url http://127.0.0.1:3400 \
  --owner release-operator \
  --profile default \
  --workspace /workspaces/command-canary \
  --local-workspace /srv/broker-workspaces/command-canary
```

The broker credential must be supplied in `CODEX_BROKER_INTERNAL_KEY`. The command also requires an explicit owner and profile, either as arguments or through `CODEX_BROKER_OWNER_ID` and `CODEX_BROKER_PROFILE`. Use `--auth-principal-id` or `CODEX_BROKER_AUTH_PRINCIPAL_ID` where owner-to-principal policy requires it. Do not put credential values on the command line.

This canary consumes a real model turn and writes one file below `.codex-broker-command-canary/`. It interrupts only its own turn on timeout. A passing unit test does not replace this release check because unit tests use fake broker responses.
