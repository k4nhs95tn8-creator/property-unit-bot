# Active data source

Official Dubai Pulse/DLD dataset: https://www.dubaipulse.gov.ae/data/dld-registration/dld_units-open

Bulk resource: https://www.dubaipulse.gov.ae/dataset/85462a5b-08dc-4325-9242-676a0de4afc4/resource/7d4deadf-c9bc-47a4-85de-998d0ce38bf3/download/units.csv

The public download/conversion/weekly release architecture was inspected in NABILNET-ORG/dld-unit-finder. This bot independently implements that architecture and obtains data only from the official resource, on GitHub's runner.

Earlier local connection-code-28 failures are not evidence of a GitHub-runner failure. No GitHub cloud run has occurred yet. The local application now fetches only the resulting GitHub Release database. Authenticated DLD API and UAE Pass paths have been removed from active setup.
