# Private Telegram DLD unit candidate bot

A local Telegram bot backed by official public Dubai Land Department unit data.

## Current architecture

1. A **public GitHub repository** runs `.github/workflows/dld-bulk-cloud.yml` on a standard Ubuntu runner, manually and every Sunday at 03:00 UTC.
2. Only the runner downloads the official Dubai Pulse `dld_units-open` CSV. The bot and setup page never download from Dubai Pulse or use authenticated DLD APIs.
3. The importer retains every CSV record, including duplicate property IDs. All source columns are retained as TEXT in `source_units`; `units` provides normalized matching fields and indexes. Malformed rows fail the refresh rather than silently disappearing.
4. The runner checks SQLite integrity and row counts, tests the Seslia listing, compresses the database, and publishes checksums, provenance and all compatible candidates in a versioned GitHub Release. A draft is published only after all uploads succeed. Previous good releases remain available.
5. The bot checks the public release at startup and hourly. It validates compressed and uncompressed checksums, source provenance, row counts, complete raw rows, and SQLite integrity before atomically replacing its database. Failed updates preserve the old database.

Based on the public data architecture inspected in [NABILNET-ORG/dld-unit-finder](https://github.com/NABILNET-ORG/dld-unit-finder), independently implemented here. No DXB Hawk account or service is involved.

## Accounts and cost

The only remaining setup requirement is GitHub sign-in/authorization to create your repository and run its workflow. No DLD credentials, UAE Pass or manual CSV download is required.

Use a public repository and standard GitHub-hosted runner, which [GitHub provides free](https://docs.github.com/en/billing/concepts/product-billing/github-actions). Databases are [Release assets](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases), not billed Actions artifacts or caches. No paid runner, server or subscription is configured. The Telegram bot runs on your Mac while the app is running and the Mac is awake. Always-on external hosting is not deployed.

GitHub may delay scheduled jobs and disables scheduled workflows in public repositories after 60 days without repository activity. If disabled, open Actions → Weekly official DLD unit database → Enable workflow. A successful cloud build does not guarantee the upstream dataset itself is fresh.

## Confidence and candidate reports

Size/bedroom similarity alone never proves an individual unit. Missing attributes remain possible rivals. A project can span buildings. Download time is not the source record date. Exact identifier status requires a supplied genuine DLD property ID, globally unique matching record, corroborated building/area, compatible details and a known fresh snapshot. The bulk snapshot has unknown record age, so remains cautious.

The Seslia test uses reviewed visible listing facts: studio, 397 sqft (36.88250688 sqm), Seslia Tower. It does not treat the agent reference as a DLD unit number. `seslia-result.json` includes every compatible candidate with unit numbers/IDs clearly labelled as unverified alternatives. Compact Telegram messages show a limited summary.

## Local operation

Python 3.11+ and curl required; no Python packages needed.

- `python3 setup_app.py` serves the loopback setup page at http://127.0.0.1:8765/.
- `python3 -m unittest discover -s tests` checks local behavior with synthetic fixtures.
- `python3 update_data.py` downloads the published GitHub database, using `data/github_source.json` or `DLD_GITHUB_REPO=owner/repo`.
- `python3 check_listing.py` writes the complete local Seslia result after real data is loaded.

No Telegram token is needed by the cloud workflow. `data/`, `.tools/`, environment files and local output are excluded from publication. Do not commit them.

## Access boundary

Official public HTTPS dataset only. An authentication page, CAPTCHA, denial or rate-limit response stops the download. No bypass or alternate private endpoint is used.
