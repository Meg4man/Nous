# Security Policy

## Supported Versions

Nous is currently in active prototype development. Security fixes are applied to the latest version only.

| Version | Supported |
|---|---|
| Latest (main branch) | ✅ |
| Older commits | ❌ |

---

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

If you find a security issue — especially anything that could compromise the local-first or privacy-absolute principles of Nous — report it directly:

- GitHub: [@Meg4man](https://github.com/Meg4man)

Include:
- A clear description of the vulnerability
- Steps to reproduce
- Potential impact
- Your suggested fix if you have one

I'll respond as quickly as I can and keep you updated on the fix.

---

## Security principles

Nous is built around a simple security model:

- All data stays on your hardware
- No external network calls except WireGuard / Tailscale for intentional remote access
- No telemetry, no analytics, no phoning home

Any contribution or fork that violates these principles is not Nous.
