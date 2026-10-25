"""Configurable PostgreSQL public transport data generator (Python 3.11+)."""
from __future__ import annotations

import argparse
import csv
import io
import math
import os
import random
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

TABLES = ['stops', 'lines', 'calendar', 'exception_types', 'calendar_dates',
          'trips', 'connections', 'drivers', 'vehicles', 'trip_executions']
COLS = {
    'stops': 'id name lat lon has_screen is_on_request',
    'lines': 'id number type is_express',
    'calendar': 'id monday tuesday wednesday thursday friday saturday sunday start_date end_date',
    'exception_types': 'id description',
    'calendar_dates': 'id date exception_type',
    'trips': 'id line_id start_stop_id end_stop_id headsign calendar_id',
    'connections': 'id start_stop_id end_stop_id departure_time arrival_time trip_id sequence_num',
    'drivers': 'id first_name last_name birth_date license_number is_active',
    'vehicles': 'id fleet_number make model production_year registration_number capacity is_low_floor notes',
    'trip_executions': 'id trip_id driver_id vehicle_id service_date actual_start_time actual_end_time',
}


def load_lookups(path):
    """Load editable vocabulary and reject invalid lists before touching the database."""
    lookups = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    if not isinstance(lookups, dict):
        raise ValueError('Lookups must be a YAML mapping')
    for key in ('first_names', 'last_names', 'line_names', 'stop_names', 'vehicle_makes', 'vehicle_models'):
        values = lookups.get(key)
        if not isinstance(values, list) or not values:
            raise ValueError(f'lookups.{key} must be a nonempty list')
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f'lookups.{key} must contain nonempty strings')
        if len(values) != len(set(values)):
            raise ValueError(f'lookups.{key} must contain unique values')
    return lookups


def unique_names(rng, names, count):
    """Shuffle fictional names; number further batches without name collisions."""
    used = set()
    batch = 0
    while len(used) < count:
        pool = rng.sample(names, len(names))
        for name in pool:
            candidate = name if batch == 0 else f'{name} {batch + 1}'
            if candidate in used:
                continue
            used.add(candidate)
            yield candidate
            if len(used) == count:
                return
        batch += 1



def normalize_config(cfg):
    """Translate the descriptive YAML sections to the generator's internal settings."""
    g, r = cfg['generation'], cfg['realism']
    if 'days_of_executions' in g:
        g['execution_days'] = g.pop('days_of_executions')
    geography = r.pop('geography', {})
    for k in ('center_lat', 'center_lon', 'radius_km'):
        if k in geography:
            r[k] = geography[k]
    departures = r.pop('departures', {})
    weights = departures.get('hourly_weights')
    if weights is not None:
        if isinstance(weights, dict):
            weights = [weights.get(h, weights.get(str(h), 0)) for h in range(24)]
        r['weekday_weights'] = weights
    for source, target in (('weekday_weights', 'weekday_weights'),
                           ('weekend_weights', 'weekend_weights'),
                           ('rush_hour_multiplier', 'rush_hour_multiplier'),
                           ('night_traffic_multiplier', 'night_traffic_multiplier')):
        if source in departures:
            r[target] = departures[source]
    connections = r.pop('connections', {})
    if 'average_speed_kmh' in connections:
        r['average_speed_kmh'] = connections['average_speed_kmh']
    travel = connections.get('travel_minutes', {})
    if 'min' in travel:
        r['min_segment_minutes'] = travel['min']
    if 'max' in travel:
        r['max_segment_minutes'] = travel['max']
    dwell = connections.get('dwell_seconds')
    if isinstance(dwell, dict):
        r['dwell_seconds'] = round((dwell['min'] + dwell['max']) / 2)
    elif dwell is not None:
        r['dwell_seconds'] = dwell
    delays = r.pop('delays', {})
    for key in ('on_time_probability', 'average_delay_minutes', 'max_delay_minutes'):
        if key in delays:
            r[key] = delays[key]
    if cfg['performance'].get('use_copy') is False:
        raise ValueError('performance.use_copy=false is not supported; COPY is required')
    return cfg


def validate(c):
    g, r, p = c['generation'], c['realism'], c['performance']
    for key in ('stops', 'lines', 'trips', 'drivers', 'vehicles', 'execution_days', 'target_trip_executions'):
        if not isinstance(g[key], int) or g[key] < 0:
            raise ValueError(f'generation.{key} must be a nonnegative integer')
    if g['stops'] < 2 or g['lines'] < 1 or g['trips'] < g['lines']:
        raise ValueError('Need >=2 stops, >=1 lines, and trips >= lines')
    bounds = g['connections_per_trip']
    if not 1 <= bounds['min'] <= bounds['max']:
        raise ValueError('connections_per_trip requires 1 <= min <= max')
    if g['target_trip_executions'] and (not g['drivers'] or not g['vehicles'] or not g['execution_days']):
        raise ValueError('Executions require drivers, vehicles and days')
    for key in ('weekday_weights', 'weekend_weights'):
        if len(r[key]) != 24 or any(x < 0 for x in r[key]) or sum(r[key]) == 0:
            raise ValueError(f'{key} must contain 24 nonnegative weights with positive sum')
    for key in ('tram_fraction', 'express_fraction', 'on_request_fraction', 'screen_fraction',
                'low_floor_fraction', 'on_time_probability'):
        if not 0 <= r[key] <= 1:
            raise ValueError(f'realism.{key} must be in [0,1]')
    if r['min_segment_minutes'] < 1 or r['max_segment_minutes'] < r['min_segment_minutes']:
        raise ValueError('Invalid segment duration bounds')
    if r['average_delay_minutes'] < 0 or r['max_delay_minutes'] < 0:
        raise ValueError('Delay settings must be nonnegative')
    if r['rush_hour_multiplier'] < 0 or r['night_traffic_multiplier'] < 0:
        raise ValueError('Traffic multipliers must be nonnegative')
    if p['batch_size'] < 1:
        raise ValueError('batch_size must be positive')


def rows_copy(conn, table, rows, batch_size):
    columns = COLS[table].split()
    statement = f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT CSV, NULL '\\N')"
    count = 0
    with conn.cursor() as cur:
        with cur.copy(statement) as copy:
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator='\n')
            for row in rows:
                writer.writerow(['\\N' if v is None else v for v in row])
                count += 1
                if count % batch_size == 0:
                    copy.write(buf.getvalue().encode())
                    buf.seek(0)
                    buf.truncate()
            if buf.tell():
                copy.write(buf.getvalue().encode())
    print(f'{table:18s} {count:>12,}')
    return count


def clock(minute):
    return f'{(minute // 60) % 24:02d}:{minute % 60:02d}:00'


def timestamp(d, minutes):
    return (datetime.combine(d, time()) + timedelta(minutes=minutes)).isoformat(sep=' ')


def choose_departure(rng, r, weekend):
    weights = list(r['weekend_weights'] if weekend else r['weekday_weights'])
    for h in range(24):
        if h in (7, 8, 15, 16, 17):
            weights[h] *= r['rush_hour_multiplier']
        if h <= 4 or h >= 23:
            weights[h] *= r['night_traffic_multiplier']
    if sum(weights) <= 0:
        raise ValueError('All hourly weights are zero after applying multipliers')
    hour = rng.choices(range(24), weights=weights)[0]
    return hour * 60 + rng.randrange(60)


def stops_data(rng, n, r, names):
    for i, name in enumerate(unique_names(rng, names, n), 1):
        radius = r['radius_km'] * math.sqrt(rng.random())
        angle = rng.random() * 2 * math.pi
        lat = r['center_lat'] + radius * math.sin(angle) / 111.1
        lon = r['center_lon'] + radius * math.cos(angle) / (111.1 * math.cos(math.radians(r['center_lat'])))
        yield (i, name, round(lat, 6), round(lon, 6),
               rng.random() < r['screen_fraction'], rng.random() < r['on_request_fraction'])


def create_data(conn, cfg, lookups):
    rng = random.Random(cfg['seed'])
    g, r, batch = cfg['generation'], cfg['realism'], cfg['performance']['batch_size']
    base = date.today()
    dates = [base + timedelta(days=i) for i in range(g['execution_days'])]
    stop_coords = []
    stop_names = []

    def make_stops():
        for row in stops_data(rng, g['stops'], r, lookups['stop_names']):
            stop_coords.append((row[2], row[3]))
            stop_names.append(row[1])
            yield row

    rows_copy(conn, 'stops', make_stops(), batch)
    line_types = {}
    routes = {}
    def make_lines():
        for i, name in enumerate(unique_names(rng, lookups['line_names'], g['lines']), 1):
            kind = 'TRAM' if rng.random() < r['tram_fraction'] else 'BUS'
            line_types[i] = kind
            k = min(g['stops'], rng.randint(g['connections_per_trip']['min'] + 1,
                                            g['connections_per_trip']['max'] + 1))
            # Nearest-neighbour route yields spatially plausible adjacent stops.
            available = set(range(1, g['stops'] + 1))
            first = rng.choice(tuple(available))
            route = [first]
            available.remove(first)
            while len(route) < k:
                lat, lon = stop_coords[route[-1] - 1]
                near = rng.sample(tuple(available), min(30, len(available)))
                nxt = min(near, key=lambda j: (stop_coords[j-1][0] - lat)**2 +
                          (stop_coords[j-1][1] - lon)**2)
                route.append(nxt)
                available.remove(nxt)
            routes[i] = route
            yield (i, name, kind, rng.random() < r['express_fraction'])

    rows_copy(conn, 'lines', make_lines(), batch)
    end_date = dates[-1] if dates else base
    calendar_rows = [(1, True, True, True, True, True, False, False, base, end_date),
                     (2, False, False, False, False, False, True, True, base, end_date)]
    rows_copy(conn, 'calendar', calendar_rows, batch)
    rows_copy(conn, 'exception_types', [(1, 'Added service'), (2, 'Removed service')], batch)
    # Explicit exception example, but do not remove service to avoid surprising capacity loss.
    rows_copy(conn, 'calendar_dates', [], batch)

    # Each trip stores its scheduled departure, end, and calendar for execution generation.
    trip_specs = []
    def make_trips():
        for i in range(1, g['trips'] + 1):
            line_id = (i - 1) % g['lines'] + 1
            route = routes[line_id]
            weekend = rng.random() < 0.27
            departure = choose_departure(rng, r, weekend)
            duration = sum(rng.randint(r['min_segment_minutes'], r['max_segment_minutes'])
                           for _ in range(len(route)-1))
            trip_specs.append((i, line_id, route, departure, departure + duration, 2 if weekend else 1))
            yield (i, line_id, route[0], route[-1], stop_names[route[-1] - 1], 2 if weekend else 1)

    rows_copy(conn, 'trips', make_trips(), batch)

    def make_connections():
        idx = 0
        for trip_id, line_id, route, start, _, calendar_id in trip_specs:
            current = start
            for seq, (a, b) in enumerate(zip(route, route[1:]), 1):
                idx += 1
                # Distances influence time, capped to user-specified bounds.
                la, loa = stop_coords[a-1]
                lb, lob = stop_coords[b-1]
                dist = math.hypot((la-lb)*111.1, (loa-lob)*111.1*math.cos(math.radians(la)))
                minutes = max(r['min_segment_minutes'], min(r['max_segment_minutes'],
                              round(dist / max(r['average_speed_kmh'], 1) * 60) + 1))
                end = current + minutes
                yield (idx, a, b, clock(current), clock(end), trip_id, seq)
                current = end + (1 if r['dwell_seconds'] >= 30 else 0)

    # Reconcile duration with actual connection durations to avoid scheduling overlaps.
    # Compute trip duration deterministically with same calculation as make_connections.
    fixed = []
    for trip_id, line_id, route, start, _, cal in trip_specs:
        elapsed = 0
        for a, b in zip(route, route[1:]):
            la, loa = stop_coords[a-1]; lb, lob = stop_coords[b-1]
            dist = math.hypot((la-lb)*111.1, (loa-lob)*111.1*math.cos(math.radians(la)))
            elapsed += max(r['min_segment_minutes'], min(r['max_segment_minutes'],
                           round(dist / max(r['average_speed_kmh'], 1) * 60) + 1))
            elapsed += (1 if r['dwell_seconds'] >= 30 else 0)
        fixed.append((trip_id, line_id, route, start, start+elapsed, cal))
    trip_specs = fixed
    rows_copy(conn, 'connections', make_connections(), batch)

    rows_copy(conn, 'drivers', ((i, rng.choice(lookups['first_names']), rng.choice(lookups['last_names']),
               date(rng.randint(1960, 2004), rng.randint(1,12), rng.randint(1,28)),
               f'DRV-{i:09d}', True) for i in range(1, g['drivers']+1)), batch)
    rows_copy(conn, 'vehicles', ((i, f'F-{i:07d}', rng.choice(lookups['vehicle_makes']),
               rng.choice(lookups['vehicle_models']), rng.randint(2005, 2025),
               f'DW-{i:07d}', rng.randint(60, 180), rng.random() < r['low_floor_fraction'], None)
               for i in range(1, g['vehicles']+1)), batch)

    def make_executions():
        if not dates or not g['target_trip_executions']:
            return
        # Use day-indexed candidates; repeated trip per day is disallowed.
        candidates = [(d, t) for d in dates for t in trip_specs
                      if (d.weekday() >= 5) == (t[5] == 2)]
        rng.shuffle(candidates)
        driver_busy = defaultdict(list)
        vehicle_busy = defaultdict(list)
        produced = 0
        for d, t in candidates:
            if produced >= g['target_trip_executions']:
                break
            tid, lid, route, start, end, cal = t
            start_abs = d.toordinal()*1440 + start
            end_abs = d.toordinal()*1440 + end
            if r['enforce_resource_conflicts']:
                # Randomized bounded search; favors scalability over optimal packing.
                driver_id = next((x for x in rng.sample(range(1, g['drivers']+1),
                    min(g['drivers'], 80)) if all(end_abs <= a or start_abs >= b
                    for a,b in driver_busy[x])), None)
                vehicle_id = next((x for x in rng.sample(range(1, g['vehicles']+1),
                    min(g['vehicles'], 80)) if all(end_abs <= a or start_abs >= b
                    for a,b in vehicle_busy[x])), None)
                if driver_id is None or vehicle_id is None:
                    continue
                driver_busy[driver_id].append((start_abs, end_abs))
                vehicle_busy[vehicle_id].append((start_abs, end_abs))
            else:
                driver_id = rng.randint(1, g['drivers'])
                vehicle_id = rng.randint(1, g['vehicles'])
            delay = 0 if rng.random() < r['on_time_probability'] else min(
                r['max_delay_minutes'], round(rng.expovariate(1 / max(r['average_delay_minutes'], 0.01))))
            produced += 1
            yield (produced, tid, driver_id, vehicle_id, d,
                   timestamp(d, start+delay), timestamp(d, end+delay))
        if produced < g['target_trip_executions']:
            raise ValueError(f'Could generate only {produced:,} of {g["target_trip_executions"]:,} requested '
                             'executions. Increase days/trips/resources or disable conflict checking.')

    rows_copy(conn, 'trip_executions', make_executions(), batch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--lookups', type=Path, default=Path(__file__).with_name('lookups.yaml'),
                        help='Vocabulary YAML (defaults to lookups.yaml beside this script)')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding='utf-8'))
    cfg = normalize_config(cfg)
    validate(cfg)
    lookups = load_lookups(args.lookups)
    print('Configuration OK')
    g = cfg['generation']
    print(f"Stops={g['stops']:,}, lines={g['lines']:,}, trips={g['trips']:,}, "
          f"target executions={g['target_trip_executions']:,}")
    if args.dry_run:
        return
    import psycopg
    db = cfg['database']
    password = db.get("password") or os.getenv(
        db.get("password_env", "POSTGRES_PASSWORD")
    )
    if not password:
        raise RuntimeError('Set environment variable ' + db.get('password_env', 'POSTGRES_PASSWORD'))
    with psycopg.connect(host=db['host'], port=db['port'], dbname=db['name'],
                         user=db['user'], password=password) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                if g.get('truncate_existing', False):
                    cur.execute('TRUNCATE TABLE ' + ', '.join(TABLES) + ' RESTART IDENTITY CASCADE')
                else:
                    for table in TABLES:
                        cur.execute(f'SELECT EXISTS (SELECT 1 FROM {table} LIMIT 1)')
                        if cur.fetchone()[0]:
                            raise ValueError(f'Table {table} is not empty. Set truncate_existing: true '
                                             'to replace existing data.')
            create_data(conn, cfg, lookups)
    print('Committed successfully.')


if __name__ == '__main__':
    main()
