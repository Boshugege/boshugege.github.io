# PNC Comment API

Small Node service that fronts Directus for public blog comments.

Required environment:

- `DIRECTUS_URL`
- `DIRECTUS_TOKEN` or `DIRECTUS_TOKEN_FILE`
- `TURNSTILE_SITE_KEY`
- `TURNSTILE_SECRET_KEY`
- `COMMENT_HASH_SECRET`

Every secret also supports a matching `_FILE` variable for Compose secrets. The static
Directus token belongs to the least-privilege `pnc-comment-api` service account and
can only read visible comments or create a visible comment.

The public browser should only call this service. Do not expose Directus write
permissions directly.
