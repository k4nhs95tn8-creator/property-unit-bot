# Local verification — GitHub architecture

45 tests passed on 2026-09-15, including complete source-column preservation, all-candidate export, conservative matching, gzip round-trip, checksum corruption rejection, decompression bounds, provenance checks, GitHub release version consistency, rollback, Telegram pairing and setup-page request protection.

All automated database tests use synthetic fixtures. GitHub is authorized. Two cloud runs passed the 45 tests but timed out connecting to the official source; no official dataset is loaded. No deployment has occurred.


## Cloud execution result

GitHub authorization completed; repository and weekly workflow are active. Both official-source cloud downloads failed with connection code 28. The second allowed 120 seconds to connect. No CSV, database release or real-data candidate report was produced. No deployment occurred.

[Second cloud run](https://github.com/k4nhs95tn8-creator/property-unit-bot/actions/runs/34975602386)
