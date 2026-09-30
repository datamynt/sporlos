# Databehandleravtale (DPA) — Sporløs

The published agreement lives in **`app/dpa.py`** and is served at
**https://sporlos.no/databehandleravtale** (version 1, valid from 2026-09-30).

It is accepted electronically when an account is created (GDPR art. 28 nr. 9); the
accepted version and time are stored on the tenant (`tenants.dpa_version`,
`tenants.dpa_accepted_at`). Existing accounts accept it from the account page.

Changing the text means a new `VERSION` in `app/dpa.py`, never an in-place edit of a
published version. Customers who need a signed copy ask at post@sporlos.no; a
DPA both parties have signed (for example KS' standard agreement) takes precedence.
