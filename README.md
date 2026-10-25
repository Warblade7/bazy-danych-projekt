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

`config.yaml` controls table sizes, geography, departure distributions, travel times, delays, and execution generation. Executions follow the full timetable: every trip runs on every day its calendar is in service, and weekday public holidays (Poland) run the weekend schedule via `calendar_dates`. `realism.unexecuted_trip_fraction` leaves some trips without executions. Drivers and vehicles are assigned in shifts with no overlaps, limited by `layover_minutes`, `max_driver_minutes_per_day`, `max_shift_span_minutes`, `driver_daily_rest_minutes` and `driver_days_off_per_week`. Vehicles are out of service for inspections and repairs (`vehicle_inspection_interval_days`, `vehicle_repairs_per_year`, `vehicle_repair_days`) and drivers for leave and sick days (`driver_summer_leave_days`, `driver_other_leave_blocks`, `driver_sick_days_per_year`); these absences are not stored in tables and show only as days without trips; generation fails with a clear message if `generation.drivers` or `generation.vehicles` is too small. Set `realism.enforce_resource_conflicts` to `false` to assign them at random instead.

`lookups.yaml` contains driver names, line and stop names, vehicle makes and models, and the optional equipment and event phrases used for the free-text `vehicles.notes`. Lists must contain unique, non-empty strings. Line and stop names are unique; numeric suffixes are added when a list runs out. Trip headsigns use the destination stop name. Use `--lookups path/to/file.yaml` to load a different lookup file.

The target tables must be empty unless `generation.truncate_existing` is `true`, which truncates all 10 tables. Generation runs in one transaction and rolls back on failure. Data is loaded in batches using PostgreSQL `COPY FROM STDIN`; `performance.use_copy` must remain `true`.

`--dry-run` validates the config and lookup file without connecting to PostgreSQL. It does not check whether the requested execution count can be scheduled.

Generated schedules and coordinates are synthetic. Each line has a fixed route. The schema stores connection times without a date offset, so after-midnight segments cannot explicitly indicate the next day. Vehicles have no type field, so the generator splits the fleet in proportion to tram and bus trips and marks it in the fleet number (`T-` trams, `B-` buses); executions only use vehicles matching the line type.