# Schema changes

New schema changes belong here as numbered migration files (for example, `002_add_run_label.sql`). Keep each change small and ordered. The existing baseline schema is created by the idempotent `ensure_schema` functions in Lynewright services; migrations added here apply only to changes after that baseline.
