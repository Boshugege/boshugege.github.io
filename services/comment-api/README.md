# PNC Comment API

Small Node service that fronts Directus for public blog comments.

Required environment:

- `DIRECTUS_URL`
- `DIRECTUS_EMAIL`
- `DIRECTUS_PASSWORD`
- `TURNSTILE_SITE_KEY`
- `TURNSTILE_SECRET_KEY`
- `COMMENT_HASH_SECRET`

The public browser should only call this service. Do not expose Directus write permissions directly.
