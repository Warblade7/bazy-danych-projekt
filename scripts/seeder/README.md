# Public Transport Data Generator

Python 3.13+ generator for synthetic PostgreSQL public transport data. Uses `uv` for dependency and environment management.

## Setup and run

```bash
uv sync
uv run generate.py --config config.yaml --dry-run
uv run generate.py --config config.yaml
```

Set `POSTGRES_PASSWORD` in the environment, or provide `database.password` in the YAML config. PowerShell:

```powershell
$env:POSTGRES_PASSWORD = "postgres_dev"
```

## Database behavior

Apply `../../schema.sql` before running the generator. The target tables must be empty unless `generation.truncate_existing` is `true`; when enabled, all 10 tables are truncated. Inserts run in one transaction, so a failed generation is rolled back.

Data is loaded with PostgreSQL `COPY FROM STDIN` in batches. `performance.use_copy` must remain `true`. `--dry-run` validates the configuration and lookup data without connecting to the database; it does not check whether the requested number of executions can be scheduled.

## Configuration and lookups

`config.yaml` controls table sizes, geography, departure distributions, travel times, delays, and execution generation. `target_trip_executions` is a requested count: generation fails if the configured dates, trips, drivers, vehicles, or conflict rules cannot produce that many executions. Setting `realism.enforce_resource_conflicts` to `false` allows driver and vehicle assignments to overlap.

`lookups.yaml` contains names for drivers, lines, and stops, plus vehicle makes and models. Lists must contain unique, non-empty strings. Line and stop names are unique; suffixes are added if a list is exhausted. Trip headsigns use the destination stop name. Pass `--lookups path/to/file.yaml` to use another lookup file.

Generated schedules and geographic coordinates are synthetic. Each line has a fixed route. Connection times use the configured travel-time bounds; the schema stores times without a date offset, so after-midnight segments cannot represent the following day explicitly. Vehicle records have no vehicle-type field, so tram/bus line types are not matched to vehicle types.