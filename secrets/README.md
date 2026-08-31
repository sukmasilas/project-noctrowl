# secrets/

This folder holds credential files that shouldn't be committed (the Google Cloud service
account JSON, primarily). Everything in this folder except this README is gitignored.

Expected contents once milestone 3 starts:
- `google-service-account.json` — Drive API scope only (Sheets API is not needed for this project).
