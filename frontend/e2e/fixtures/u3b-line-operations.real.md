# U3b production-line operations fixture

`u3b-line-operations.real.json` is captured by `scripts/run-u3-screens.ps1` from the running backend route `GET /api/production-lines/operations?project_id=…`. The screenshot setup refreshes the signed local catalogue, installs the native example through the catalogue install route, enables it, and captures rows for that package and the bootstrapped Demo Writer. It is response data from the backend, not a hand-authored API shape.
