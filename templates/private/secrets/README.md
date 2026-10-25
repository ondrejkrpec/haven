# Secret storage boundary

Configuration v1 supports optional `model.credential_ref` values using `env:VARIABLE_NAME` or `file:/absolute/path`. Validation checks their syntax and never reads the environment variable or referenced file. H1 must resolve credentials for the authorized service; secret storage and runtime loading are not implemented yet. Keep this directory free of secret values. SOPS with age remains a proposal.

The ignore rules permit future `*.sops.yaml` files but do not verify encryption. Never commit plaintext, age private keys, OAuth state, WhatsApp credentials or passwords. Decryption and recovery keys must remain outside Git and the VPS, with an independently verified recovery copy.
