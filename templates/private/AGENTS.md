# Private instance instructions

This directory is for one private Haven instance. Its files may contain personal information. Read `README.md` and the public project's configuration guide before changing them.

- Keep configuration and personal instructions here. Use synthetic values when reporting issues or contributing to the public project.
- Never transfer this repository into public code, documentation, build inputs or test artifacts.
- Keep tokens, passwords, OAuth state, WhatsApp sessions, encryption keys and runtime databases outside Git. Encryption tooling and external key storage must be configured and verified before adding encrypted secrets.
- `config/instance.yaml.example` follows Haven configuration v1. Validate changes with the public `scripts/validate_config.py` and explicitly pass this instance's path. Read the public `docs/configuration.adoc`; unknown fields are rejected. Never treat the file as native Hermes configuration or deployment authorization.
- Add only necessary instance instructions. Instructions do not grant permissions; the executor and deployment runner must enforce access separately.
- Preserve personal edits during template updates. Run relevant validation once available, `git diff --check`, and inspect `git status --short` before handing off.
- Do not commit, push, publish, authenticate accounts or deploy unless the user asks.
