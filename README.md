# Haven

Haven is a personal agent system built around Hermes. The current pilot provides deterministic owner operations through Telegram on one Ubuntu VPS. Separate worker/reviewer roles belong to H2; WhatsApp is a future transport option.

**Status: H1 is accepted for the existing deterministic pilot, 2026-11-05.** [H1.10](docs/h1.10-acceptance.adoc) verifies owner Telegram operations, recovery after a killed process and host reboot, private web access, fresh encrypted backups and monitoring. The record includes the resolved backup incident and distinguishes new checks from reused quota/recovery evidence. H2 is the next milestone. Coding dispatch stays disabled until H2; ordinary AI chat, Czech voice, WhatsApp and family sharing remain disabled or deferred. Automatic local pre-push checks replace unavailable GitHub Actions.

**Wishlist progress, 2026-11-08:** [W1 domain state](docs/wishlist-domain.adoc), [W2 Telegram ingress](docs/wishlist-service.adoc) and [W5 bounded wishlist conversation](docs/wishlist-language.adoc) are implemented. The first [W4 live slice](docs/wishlist-operations.adoc) adds isolated services, independent enrollment and verified multi-component recovery. Owner conversation exposed intake/display/removal issues; the repair is deployed with schema 4 and passes real-model reproduction; owner retry remains pending. W3 daily collection and remaining W4 dashboard/retention work follow. The coordinator retains its independent model/tool guard.

H1.6 provides [one owner's private Telegram text conversation](docs/h1.6-telegram.adoc), with live owner/denied-sender checks, blocked remote administration and verified reconnect after restart. It uses a numeric owner allowlist, protected token and outbound long polling. Agent Platform is unavailable on the pilot owner's account, so WhatsApp and [family sharing](docs/family-conversations.adoc) are deferred; the earlier Baileys and Agent Platform assessments stay inactive.

[H1.6.1 guardrails](docs/h1.6.1-capabilities.adoc) add an explicitly selected deterministic profile: status/capability replies, bounded private audit and an independent native model/tool/cron guard. Local verification and live acceptance are recorded separately in that guide.

[H1.8 web access](docs/h1.8-web-access.adoc) adds a verified read-only operational snapshot, separate private runbooks and the [public documentation site](https://ondrejkrpec.github.io/haven/). The guide records hosting, access and recovery evidence and remaining acceptance limits. Administrative dashboard controls remain deferred.

[H1.9 backup and recovery](docs/h1.9-backup-recovery.adoc) provides twice-daily encrypted Scaleway backups, exact snapshot restore verification, independent Healthchecks monitoring and separate operator retention. Production recovery was verified in a clean isolated runtime with no transport adapters or added model calls; the owner confirmed the missed-heartbeat email.

**W3 pilot progress, 2026-11-11:** [Daily collection](docs/wishlist-collection.adoc) is installed with schema 5, durable jobs, bounded HTTPS workers and price history. A real iFantazie read passed the installed-unit fixture through observation storage and Telegram detail formatting; current production entries correctly yielded no work because they were purchased/removed. Another daily cycle and owner-visible prices remain unaccepted.

**Alza remains open:** the [isolated Chromium/Xvfb path](docs/wishlist-browser.adoc) passed the workstation roundtrip and is installed on the pilot with verified file/network restrictions and normal cleanup. The VPS still receives Cloudflare verification instead of the product. Neither installing a browser nor selecting its endpoint establishes functional Alza collection.

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
