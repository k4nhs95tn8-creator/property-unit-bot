# Active data source

Official Dubai Pulse/DLD dataset: https://www.dubaipulse.gov.ae/data/dld-registration/dld_units-open

Bulk resource: https://www.dubaipulse.gov.ae/dataset/85462a5b-08dc-4325-9242-676a0de4afc4/resource/7d4deadf-c9bc-47a4-85de-998d0ce38bf3/download/units.csv

The public download/conversion/weekly release architecture was inspected in NABILNET-ORG/dld-unit-finder. This bot independently implements that architecture and obtains data only from the official resource, on GitHub's runner.

Earlier local connection-code-28 failures are not evidence of a GitHub-runner failure. Two real GitHub cloud runs have now failed to connect to the official source (connection code 28). The local application now fetches only the resulting GitHub Release database. Authenticated DLD API and UAE Pass paths have been removed from active setup.


## Cloud execution result

GitHub authorization completed; repository and weekly workflow are active. Both official-source cloud downloads failed with connection code 28. The second allowed 120 seconds to connect. No CSV, database release or real-data candidate report was produced. No deployment occurred.

[Second cloud run](https://github.com/k4nhs95tn8-creator/property-unit-bot/actions/runs/34975602386)
