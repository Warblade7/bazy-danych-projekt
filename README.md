# Public Transport Data Generator

Python 3.13+ generator for synthetic PostgreSQL public transport data. Dependencies and the virtual environment are managed with `uv`.

## Setup and run

Run the commands from the repository root unless noted otherwise:

```bash
docker compose up -d
docker exec -i public_transport psql -U postgres -d public_transport < scripts/schema.sql
cd scripts/seeder
uv sync
uv run generate.py --config config.yaml
```

The Compose service exposes PostgreSQL on `localhost:5434` and creates the `public_transport` database with user `postgres`. The schema command initializes the tables; run it once for a fresh database.

## Database password

The Compose file sets the database password to `postgres_dev`. The generator first uses a non-empty `database.password` from `config.yaml`. Only when that value is missing, `null`, or empty does it read the environment variable named by `database.password_env`, which defaults to `POSTGRES_PASSWORD`.

The checked-in `config.yaml` currently sets `database.password: postgres_dev`, so setting `POSTGRES_PASSWORD` alone does not override it. To use an environment variable, remove `database.password` or set it to `null`. Then set the variable before running the generator, for example:

```powershell
$env:POSTGRES_PASSWORD = "postgres_dev"
uv run generate.py --config config.yaml
```

On Linux/macOS:

```bash
export POSTGRES_PASSWORD=postgres_dev
uv run generate.py --config config.yaml
```

If you change the password, keep the Compose database password and the generator password in sync. You can select another environment variable in the config:

```yaml
database:
  password: null
  password_env: MY_DATABASE_PASSWORD
```

## Configuration and loading

`config.yaml` controls table sizes, geography, departure distributions, travel times, delays, and execution generation. `target_trip_executions` is a requested count; generation fails if dates, trips, drivers, vehicles, or conflict rules cannot produce that many executions. Set `realism.enforce_resource_conflicts` to `false` to allow overlapping driver and vehicle assignments.

`lookups.yaml` contains driver names, line and stop names, and vehicle makes and models. Lists must contain unique, non-empty strings. Line and stop names are unique; numeric suffixes are added when a list runs out. Trip headsigns use the destination stop name. Use `--lookups path/to/file.yaml` to load a different lookup file.

The target tables must be empty unless `generation.truncate_existing` is `true`, which truncates all 10 tables. Generation runs in one transaction and rolls back on failure. Data is loaded in batches using PostgreSQL `COPY FROM STDIN`; `performance.use_copy` must remain `true`.

`--dry-run` validates the config and lookup file without connecting to PostgreSQL. It does not check whether the requested execution count can be scheduled.

Generated schedules and coordinates are synthetic. Each line has a fixed route. The schema stores connection times without a date offset, so after-midnight segments cannot explicitly indicate the next day. Vehicles have no type field, so bus and tram line types are not matched to vehicles.