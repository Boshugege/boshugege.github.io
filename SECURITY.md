# Security Policy

This repository builds the static blog at <https://parityncsvt.top> and its small comments / reading-count API.

## Reporting a vulnerability

Please report privately through **Security → Report a vulnerability** on this repository (GitHub private vulnerability reporting), or email <admin@parityncsvt.top>. Do not open a public issue for security problems.

Useful details: the affected URL or file, steps to reproduce, and the impact you expect. You should get a reply within a week.

## Scope

- The site's HTML, client scripts and build pipeline in this repository.
- The `/api/*` endpoints (comments, reading counts, site stats).
- The deployment workflow in `.github/workflows/`.

Third-party services (Cloudflare, GitHub) are out of scope; please report those to the vendor.

## What runs automatically

- Secret scanning with push protection, and Dependabot alerts.
- CodeQL code scanning and `npm audit` after every deploy and weekly (`.github/workflows/security.yml`).
- No workflow is triggered by pull requests. Runs for pull requests from outside contributors need a maintainer's approval, and the self-hosted runner refuses any job that is not a push, manual, scheduled or follow-up run on `main` of this repository.
