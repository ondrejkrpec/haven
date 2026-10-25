# Haven repository instructions

This repository contains Haven's public source, English documentation, configuration validator, synthetic examples and H0.1 feasibility probes. Read `README.md` and `docs/local-development.adoc` before changing it. Deployment and the executor adapter are future work.

- Keep technical documentation in AsciiDoc under `docs/`; README, contribution rules and agent instructions use Markdown. Do not add a documentation generator, dependency framework or CLI without a concrete task requiring it.
- Keep each installation in a separate private repository created from `templates/private/`. Do not read a neighboring private repository unless the task needs it. Never copy its contents into public sources or build inputs.
- Public examples use synthetic values. Do not include credentials, session data, QR codes, phone numbers, personal paths or runtime databases. `.gitignore` is a convenience, not a security boundary or secret scanner.
- H0.3 implements configuration v1 in `schemas/`, `config/`, `scripts/validate_config.py` and `tests/`. Read `docs/configuration.adoc`; keep examples and the private template aligned. Validate each explicit layer and the effective result, replace lists completely, and never resolve or print credentials during validation.
- H0.4 adds Antora and public checks. Read `docs/documentation.adoc`; run `.venv/bin/python -m scripts.check` after the documented setup. Canonical guides retain their `docs/` paths; the exporter rewrites local links and copies only declared public attachments. Reject symlinks, undeclared targets and includes rather than expanding build inputs. Do not add private sources or publishing steps to the public workflow.
- H0.5 accepts the local foundation through a clean clone and fresh dependency installation. Read `docs/h0.5-acceptance.adoc` and `docs/h1-prerequisites.adoc` before deployment work. Retain the distinction between disposable fixture revisions and real project history; GitHub execution, Ubuntu/VPS and authenticated integrations remain unverified.
- Hermes is an exact-version upstream dependency. Retain patch author metadata and the MIT notice. Read `research/h0.1/README.adoc` before touching pins, patches or probes. Upstream tests must follow upstream AGENTS.md and its canonical runner.
- Profiles and the board are not authorization boundaries. The full lower-trust executor must be isolated from the controller's database, credentials and personal data. The probes use synthetic executors and do not implement the production adapter.
- Keep recovery independent of Nora's model. Retries must bind results to the current run and reconcile uncertain external effects before repeating them.
- Use targeted checks justified by the change. Before handing off, run `git diff --check`, inspect `git status --short`, and verify changed documentation links. Never overwrite unrelated changes.
- Do not commit, push, create remotes, publish or deploy unless the user asks. Disposable Git snapshots used only to test cloning must remain outside the working repositories and must not be pushed.

Record significant architectural decisions as short ADRs when made. Record experiments with the exact revision, date, commands, result and scope. Keep planned behavior separate from verified behavior.
