# Haven

Haven is a personal agent system built around Hermes, with WhatsApp as its planned phone interface. The first deployment will use one Ubuntu VPS and bounded automation, with separate worker and reviewer roles.

**Status: H0 local foundation accepted through a clean checkout.** Configuration v1, documentation and public checks are available. Deployment, authenticated Codex use and WhatsApp pairing remain untested; GitHub repositories are published; automatic local pre-push checks replace unavailable GitHub Actions.

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

Read the private README, edit your instance and follow [configuration setup and validation](docs/configuration.adoc). Validation requires Python 3.12+, system time-zone data and the hash-pinned dependencies in an isolated environment. It checks Haven inputs; translation to Hermes and deployment remain H1 work. The public checkout works independently of any private repository.

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

See [H0.5 acceptance](docs/h0.5-acceptance.adoc) for the clean-checkout result. Next: [H1 prerequisites](docs/h1-prerequisites.adoc), infrastructure choices and live deployment.

[H1.3 first access and security](docs/h1.3-first-access.adoc) records verified key-only SSH, a restricted service account, UFW and a tested configuration rollback on Ubuntu 26.04 LTS. [Tailscale private administration](docs/h1.3-private-access.adoc) provides verified private IPv4/IPv6 SSH with public SSH allowances removed and a tested access fallback. Updates/reboot, automated bootstrap and application deployment remain pending.

## Publication and licensing

Haven's original source, documentation and template use the [MIT License](LICENSE), selected by the owner. Copied Hermes material retains its [upstream MIT notice](research/h0.1/UPSTREAM-LICENSE), attribution and patch author metadata. The public repository is [ondrejkrpec/haven](https://github.com/ondrejkrpec/haven), with a separate private instance repository; [H1.1 publication](docs/h1.1-publication.adoc) records the current migration status.
