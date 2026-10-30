# Contributing

Develop new features locally in a branch with a focused change. Public prose and technical documentation use English. Technical guides live in `docs/` as AsciiDoc; [documentation and public checks](docs/documentation.adoc) describes the Antora build and the shared local/CI workflow.

The default branch is `main`. Only GitHub account `ondrejkrpec` may push to or merge into the primary branches. The active server ruleset covers the default branch, `main` and `master`, with an exception only for that exact user. Contributors and agents prepare topic branches and pull requests. The owner or an agent acting through the owner account may perform a merge within work authorized by the owner, after the documented checks against the current target and a PR-head revision check. Direct owner pushes remain possible; an agent needs an explicit request for a direct primary-branch commit or push. Local checks supplement the server access rule.

Start with [local development](docs/local-development.adoc). Keep instance configuration, hostnames and personal instructions in a separate private repository. Use synthetic values in examples and tests. Store credentials and runtime data outside both source repositories.

For a change:

1. Read `AGENTS.md` and the guide affected by the change.
2. Implement the smallest complete change and update its documentation. Distinguish planned, experimentally verified and operational behavior.
3. After the documented setup, run `.venv/bin/python -m scripts.check` and review `git status --short`, including new files and ignored files when handling configuration. The command runs the same public checks as CI; it does not inspect a private instance. Use focused checks while iterating.
4. If research artifacts change, verify patch provenance, regenerate `research/h0.1/SHA256SUMS` for the intended files and explain why. Do not silently rewrite historical evidence or its counts.
5. For code changes, run the relevant checks. A new or changed integration needs evidence at the pinned version; mocks do not establish live service behavior.

Keep schema, defaults, examples, private template, format checks and documentation consistent. Validate every supplied layer as well as the merged result; changes must not silently broaden allowlists or expose values in diagnostics. See [configuration v1](docs/configuration.adoc) for dependency-lock regeneration.

For documentation, keep one canonical guide, update `docs/nav.adoc` and use repository-relative `link:` references in guides. New downloadable files need an explicit entry in `scripts/build_docs.py`; never import a private directory. The build fails on Antora warnings and the link check rejects missing local targets/fragments. External website availability is outside the offline check.

When proposing a change, describe the problem, resulting behavior, verification and remaining limits. A Hermes upgrade must rerun quota-to-review acceptance and isolation checks before replacing the pinned candidate. Read upstream repository instructions before editing or testing upstream code.

Commit or push when requested by the owner. Original Haven material uses the [MIT License](LICENSE); retain upstream attribution and notices with copied materials. Publication status and repository-name checks are recorded in [H1.1 publication](docs/h1.1-publication.adoc). Keep real provider authentication and production deployment out of foundation checks.
