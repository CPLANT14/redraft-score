# cplant-model-explorer
CPLANT fantasy football model scores, by position and season.

Live site: https://cplant14.github.io/cplant-model-explorer/

Data auto-updates daily from the source Google Sheet through
`.github/workflows/update-data.yml` + `scripts/update_data.py` (Python stdlib
only). The workflow reads the sheet read-only through the credential-free
gviz JSON endpoint; the sheet ID is stored in the `MODEL_SCORES_SHEET_ID`
repository variable, not in the repo, and the sheet itself is never edited.
