# Haven

Haven is a personal agent system built around Hermes, with WhatsApp as its planned phone interface. The first deployment will use one Ubuntu VPS and bounded automation, with separate worker and reviewer roles.

**Status: H0 local foundation accepted through a clean checkout.** Configuration v1, documentation and public checks are available. Deployment, authenticated Codex use and WhatsApp pairing remain untested; GitHub publication and the first Actions run are pending.

## Start locally

Prerequisites: a POSIX shell, Git 2.28 or newer, and `sha256sum`. Configuration validation needs Python 3.12+; the complete public checks also need Node.js/npm and Linux check tools. Podman is used only by optional H0.1 probes. Use an existing checkout, or clone a copy once a remote is available:

```sh
git clone <repository-url> haven
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

## Publication and licensing

Haven's original source, documentation and template use the [MIT License](LICENSE), selected by the owner. Copied Hermes material retains its [upstream MIT notice](research/h0.1/UPSTREAM-LICENSE), attribution and patch author metadata. The proposed GitHub names are `haven` and `haven-private`, under the owner's personal account; [H1.1 publication](docs/h1.1-publication.adoc) records the current migration status.
