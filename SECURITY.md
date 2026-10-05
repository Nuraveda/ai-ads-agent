# Security Policy

## Reporting a vulnerability

Email `help.nuraveda@gmail.com` with the subject line `[security] ai-ads-agent: <short summary>`.

Please do not open a public issue for security reports. We aim to acknowledge within 72 hours.

If you'd prefer encrypted reporting, request our PGP key in your initial email and we'll respond with the public key + fingerprint.

## Scope

This policy covers the open-source code in this repository. Out of scope:

- Vulnerabilities in upstream dependencies (report to the vendor first; we'll bump on disclosure).
- Vulnerabilities in the platform APIs we integrate with (Meta, Google, etc.) — report to those vendors directly.
- Configuration mistakes in your own deployment (overly permissive API keys, weak Discord webhooks, etc.).

## Supported versions

The `main` branch is supported. Older tags receive security backports only when the fix is trivial.

## Coordinated disclosure

We follow standard coordinated disclosure: report → acknowledge → fix in private → release → public advisory. Researchers who follow this process are credited in the advisory unless they prefer anonymity.

## Out-of-scope deployment notes

Operators are responsible for:

- Rotating Meta / Google / TikTok / Amazon / LinkedIn API credentials regularly.
- Locking down the Discord HITL gateway to a private server.
- Not committing `.env` files (the `.gitignore` covers `.env` by default).
