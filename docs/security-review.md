# Security review: projectnous.eu

Date: 2026-11-03
Reviewer: Ninurta van Mierlo (self-review, not an independent audit)

This covers the Nous website and the setup around it. It does not cover the Nous system itself (server, models, apps), which is still in development.

## Scope

In scope:

- The site source in this repository (`index.html`, `fonts/`)
- The DNS records for projectnous.eu
- The repository history
- The accounts that control the domain and the repository

Out of scope: GitHub's infrastructure and the DNS provider's infrastructure. I couldn't find a safe-harbor rule that covers GitHub Pages sites, so I did not run scanners against the live site. Live checks were single requests, the same load as visiting the page. Scans ran against a local copy.

## Summary

The site is static: no forms, no input handling, no cookies, no storage, no network calls. The attack surface is small, so most findings were about privacy and about the accounts around the site, not the page itself.

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | Fonts were loaded from Google's servers, so every visitor's IP went to a third party | Low (privacy) | Fixed: fonts are now self-hosted |
| 2 | No SPF or DMARC on a domain with no mail, so anyone could forge sender addresses | Low | Fixed: SPF `-all` and DMARC `p=reject` |
| 3 | Author email address in git commit metadata | Low | Fixed: history rewritten to GitHub's noreply address, pushes that expose a private address are now blocked |
| 4 | No clear private channel for vulnerability reports | Low | Fixed: private vulnerability reporting, `security.txt`, SECURITY.md updated |
| 5 | External links had no `rel="noopener noreferrer"` | Info | Fixed |
| 6 | No HSTS, CSP, X-Frame-Options or nosniff headers | Info | Accepted: GitHub Pages doesn't allow custom headers |
| 7 | DNSSEC not available | Info | Accepted: the DNS host doesn't support it |

Ratings are my own judgment for this site.

## What came back clean

- Page source: no `innerHTML`, `eval`, `document.write`, inline event handlers, forms, cookies, storage or network calls. The only third-party reference was Google Fonts (finding 1).
- Repository: no tokens, keys or passwords in any commit. Only the files meant to be public are published, and `/.git/` returns 404 on the live site.
- HTTP redirects to HTTPS. The certificate covers the apex domain and `www`, and renews automatically.
- DNS: only the records GitHub Pages needs. No leftover subdomains.
- Response headers: the server header reveals no versions. `access-control-allow-origin: *` is set by GitHub Pages and is harmless for public static content.

## Scanner results

I ran nikto against a local copy of the site. It reported three things, and none of them applies to the live site:

- Missing `X-Frame-Options`: Python's dev server sends no security headers. The site has no forms or actions to hijack.
- "SimpleHTTP appears outdated": a false positive. It compared Python's server version against a different product's numbering.
- `/.git/index` exposed: this appeared only because I served the repository folder locally. GitHub Pages doesn't publish `.git`, and `/.git/index` and `/.git/config` both return 404 on the live site.

## Decisions

**Response headers.** GitHub Pages can't set custom headers, so HSTS, CSP and the others aren't possible here. The risk is low for a static brochure site. This changes when the Nous dashboard is hosted on my own server.

**DNSSEC.** The DNS host's own nameservers don't support it, and entering keys without a signed zone would make the domain fail for validating resolvers. I left it off. Moving DNS to a provider that signs zones is an option if it becomes worth the extra moving parts.

## Revisit when the dashboard goes live

- Set HSTS, CSP, X-Frame-Options and nosniff on my own server.
- Serve a build folder, never the repository root, and block `/.git/` in the server config.
- Reconsider DNSSEC.
- Renew the `Expires` date in `security.txt` before 2027-10-01.

## Limits of this review

- It is a single point in time.
- The source review was pattern-based, not a full manual audit.
- The nikto version available in Ubuntu is old, so a clean run means it found nothing obvious.
- I reviewed my own work.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md) or `https://projectnous.eu/.well-known/security.txt`.
