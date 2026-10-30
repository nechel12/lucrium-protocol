# Security policy

## Release signing

Release tags `v*` are signed with the project SSH signing key:

- fingerprint: `SHA256:RKtM0n6ob0U7GcFKq9FVum3BifnM/pPvaZf+Vsn8OxU`
- public key: [`security/release-signing-key.pub`](security/release-signing-key.pub)

Verify a tag:

```bash
git -c gpg.ssh.allowedSignersFile=security/allowed_signers verify-tag v0.1.0
```

Only maintainers should create `v*` tags; GitHub tag rulesets block updates and
deletions of existing release tags.

## Reporting

Please report vulnerabilities privately via GitHub Security Advisories
("Report a vulnerability") rather than opening a public issue.
