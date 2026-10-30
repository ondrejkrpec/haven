# Haven

Haven is a personal agent system built around Hermes. Telegram is the first phone interface; WhatsApp is a future transport option. The first deployment uses one Ubuntu VPS and bounded automation, with separate worker and reviewer roles.

**Status: H1.6.1 deterministic Telegram operations verified on the Ubuntu pilot.** Owner status/capability replies, refusal, zero model usage, native execution guards, rollback, restart and idempotent deployment are accepted. Private administration and supervised recovery work. Coding dispatch stays disabled until H2; Czech voice work is deferred by the owner, and H1.9 off-site backup/recovery is the next planned operational milestone. Automatic local pre-push checks replace unavailable GitHub Actions.

H1.6 provides [one owner's private Telegram text conversation](docs/h1.6-telegram.adoc), with live owner/denied-sender checks, blocked remote administration and verified reconnect after restart. It uses a numeric owner allowlist, protected token and outbound long polling. Agent Platform is unavailable on the pilot owner's account, so WhatsApp and [family sharing](docs/family-conversations.adoc) are deferred; the earlier Baileys and Agent Platform assessments stay inactive.

[H1.6.1 guardrails](docs/h1.6.1-capabilities.adoc) add an explicitly selected deterministic profile: status/capability replies, bounded private audit and an independent native model/tool/cron guard. Local verification and live acceptance are recorded separately in that guide.

[H1.8 web access](docs/h1.8-web-access.adoc) adds a verified read-only operational snapshot, separate private runbooks and the [public documentation site](https://ondrejkrpec.github.io/haven/). The guide records hosting, access and recovery evidence and remaining acceptance limits. Administrative dashboard controls remain deferred.

[H1.9 preparation](docs/h1.9-backup-recovery.adoc) adds a metadata-only inventory and an opt-in synthetic encrypted backup/restore experiment. [Storage and monitoring options](docs/h1.9-provider-options.adoc) remain unselected; production off-site backup and external alerts are not enabled.

## Start locally

Prerequisites: a POSIX shell, Git 2.28 or newer, and `sha256sum`. Configuration validation needs Python 3.12+; the complete public checks also need Node.js/npm and Linux check tools. Podman is used only by optional H0.1 probes. Use an existing checkout, or clone the public repository:

```sh
git clone https://github.com/ondrejkrpec/haven.git haven
cd haven
```

Verify the retained research files and create a separate private workspace. The destination must not already exist:

```sh
set -eu
(cd research/h0.1 && sha256sum -c SHA256SUMS)
test ! -e ../haven-private
cp -R templates/private ../haven-private
git -C ../haven-private init -b main
cp ../haven-private/config/instance.yaml.example ../haven-private/config/instance.yaml
```

Read the private README, edit your instance and follow [configuration setup and validation](docs/configuration.adoc). Validation requires Python 3.12+, system time-zone data and the hash-pinned dependencies in an isolated environment. It checks Haven inputs; the separate [H1.4 guide](docs/h1.4-model-configuration.adoc) covers the bounded subscription-provider renderer and live setup. The public checkout works independently of any private repository.

Enable the documented local push pipeline with `git config --local core.hooksPath .githooks` after [tool setup](docs/documentation.adoc). It checks the clean commit being pushed; configure each new clone separately.

## Project files

| Path | Purpose |
| --- | --- |
| [Local development](docs/local-development.adoc) | Working locally, private setup and optional upstream reproduction |
| [Configuration](docs/configuration.adoc) | Versioned YAML, defaults, overrides, validation and secret references |
| [Architecture](docs/architecture.adoc) | Planned roles, isolation and milestone boundaries |
| [Documentation and checks](docs/documentation.adoc) | Build the Antora site and run the local/CI checks |
| [H0.1 research](research/h0.1/README.adoc) | Exact candidate, patch attribution, results and limits |
| [Private template](templates/private/README.md) | A starting point for each user's own instance |
| [Contributing](CONTRIBUTING.md) | Change and verification workflow |
| [Agent instructions](AGENTS.md) | Rules for coding agents working in this repository |

See [H0.5 acceptance](docs/h0.5-acceptance.adoc) for the clean-checkout result and [H1 acceptance](docs/h1-prerequisites.adoc) for live progress and remaining work.

[H1.3 first access and security](docs/h1.3-first-access.adoc) records verified key-only SSH, a restricted service account, UFW and a tested configuration rollback on Ubuntu 26.04 LTS. [Tailscale private administration](docs/h1.3-private-access.adoc) provides verified private IPv4/IPv6 SSH with public SSH allowances removed and a tested access fallback. [Repeatable host bootstrap and restart](docs/h1.3-bootstrap.adoc) are verified for the existing secured pilot. [H1.4 configuration and authentication](docs/h1.4-model-configuration.adoc) adds the pinned core runtime, independent OAuth login, credential failure checks and one native request. [H1.5 service and recovery](docs/h1.5-service-recovery.adoc) adds the bounded systemd coordinator, a required worker user bus, tested application rollback and recovery after a killed process/reboot. OS updates remain deferred by the owner.

## Publication and licensing

Haven's original source, documentation and template use the [MIT License](LICENSE), selected by the owner. Copied Hermes material retains its [upstream MIT notice](research/h0.1/UPSTREAM-LICENSE), attribution and patch author metadata. The public repository is [ondrejkrpec/haven](https://github.com/ondrejkrpec/haven), with a separate private instance repository; [H1.1 publication](docs/h1.1-publication.adoc) records the current migration status.
