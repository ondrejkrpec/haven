# Private Haven instance

This template belongs in its own **private repository**, outside the public Haven checkout. It contains synthetic values. Copying it prepares a workspace; it does not install or start Haven.

The original Haven template is MIT-licensed; retain [TEMPLATE-LICENSE](TEMPLATE-LICENSE) with copied template material. Keep your instance repository private.

If created using the public README, `config/instance.yaml` already exists. Otherwise, create the configuration once:

```sh
set -eu
test ! -e config/instance.yaml
cp config/instance.yaml.example config/instance.yaml
```

Edit the instance name, time zone, host and model in your copy. The example host and model are placeholders. This is Haven configuration v1; it is not native Hermes configuration, and runtime translation/deployment remain H1 work. From the public Haven checkout, follow `docs/configuration.adoc` to prepare its Python environment, then validate:

```sh
.venv/bin/python scripts/validate_config.py ../haven-private/config/instance.yaml
```

Public defaults are applied first. Additional YAML files can be named with repeated `--override`; each must declare `schema_version: 1`. Mappings merge by key; lists replace completely. No private files are discovered automatically. Validation requires no real credentials and does not print your configuration.

| Path | Purpose |
| --- | --- |
| `config/instance.yaml.example` | Synthetic configuration v1 example retained for comparison |
| `config/instance.yaml` | Your instance's configuration; intended for this private Git repository |
| [Instructions](instructions/README.md) | Instance-specific personal and role instructions |
| [Runbooks](runbooks/README.md) | Private operational notes when verified |
| [Secrets](secrets/README.md) | Future encrypted-file boundary; no keys or plaintext secrets |

Store hostnames and allowlists in the supported YAML fields; personal instructions belong here too. WhatsApp is disabled by default and can be enabled only with a non-empty effective allowlist. Optional model credentials use an `env:VARIABLE_NAME` or `file:/absolute/path` reference, never a value. References are not resolved during validation. Sessions, runtime databases and backups belong outside Git. Ignore rules reduce accidental additions; they do not scan secrets or protect already tracked files.

Keep this repository private when a remote is created. Record the matching public Haven commit after project history exists. Review changes to the public template manually before applying them to your instance.
