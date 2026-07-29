# Test credentials

**API_TOKEN**: empty in `/app/backend/.env` on the preview deployment, so no
`X-API-Token` header is required for local development or test runs. If
`API_TOKEN` is later set, tests should send it via `X-API-Token: <value>` or
attach `?token=<value>` for SSE/anchor download links.

**Reviewer identity used in tests**: `test-suite@phi-console.local`

**Phase E gate**: every call to `POST /api/sessions/{sid}/human-review` MUST
include `actual_knowledge_ack: true` and a non-empty `reviewer` string per
45 CFR 164.514(b)(2)(ii); otherwise the endpoint returns HTTP 400.
