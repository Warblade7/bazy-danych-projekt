"""Configurable PostgreSQL public transport data generator (Python 3.11+)."""
from __future__ import annotations

import argparse
import csv
import heapq
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
    for key in ('vehicle_equipment', 'vehicle_events'):
        lookups.setdefault(key, [])
    for key in ('first_names', 'last_names', 'line_names', 'stop_names', 'vehicle_makes', 'vehicle_models',
                'vehicle_equipment', 'vehicle_events'):
        values = lookups.get(key)
        if not isinstance(values, list) or (not values and key not in ('vehicle_equipment', 'vehicle_events')):
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
    g.setdefault('start_date', date(2024, 1, 1))
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
    for key in ('stops', 'lines', 'trips', 'drivers', 'vehicles', 'execution_days'):
        if not isinstance(g[key], int) or g[key] < 0:
            raise ValueError(f'generation.{key} must be a nonnegative integer')
    if not isinstance(g['start_date'], date) or isinstance(g['start_date'], datetime):
        raise ValueError('generation.start_date must be a date in YYYY-MM-DD format')
    if g['stops'] < 2 or g['lines'] < 1 or g['trips'] < g['lines']:
        raise ValueError('Need >=2 stops, >=1 lines, and trips >= lines')
    bounds = g['connections_per_trip']
    if not 1 <= bounds['min'] <= bounds['max']:
        raise ValueError('connections_per_trip requires 1 <= min <= max')
    if g['execution_days'] and (not g['drivers'] or not g['vehicles']):
        raise ValueError('Executions require drivers and vehicles')
    for key, default in (('layover_minutes', 5), ('max_driver_minutes_per_day', 540),
                         ('driver_daily_rest_minutes', 660), ('max_shift_span_minutes', 720),
                         ('driver_days_off_per_week', 2)):
        r.setdefault(key, default)
    for key in ('layover_minutes', 'max_driver_minutes_per_day', 'driver_daily_rest_minutes',
                'max_shift_span_minutes'):
        if not isinstance(r[key], int) or r[key] < 0:
            raise ValueError(f'realism.{key} must be a nonnegative integer')
    if r['driver_days_off_per_week'] not in range(7):
        raise ValueError('realism.driver_days_off_per_week must be between 0 and 6')
    for key, default in (('vehicle_inspection_interval_days', 30), ('vehicle_repairs_per_year', 2),
                         ('vehicle_repair_days', {'min': 2, 'max': 10}), ('driver_summer_leave_days', 14),
                         ('driver_other_leave_blocks', [7, 5]), ('driver_sick_days_per_year', 8)):
        r.setdefault(key, default)
    lengths = [r['vehicle_inspection_interval_days'], r['vehicle_repairs_per_year'], r['driver_summer_leave_days'],
               r['driver_sick_days_per_year'], *r['driver_other_leave_blocks']]
    if any(not isinstance(v, int) or v < 0 for v in lengths) or any(v > 92 for v in r['driver_other_leave_blocks']) \
            or r['driver_summer_leave_days'] > 92:
        raise ValueError('Maintenance and absence settings must be nonnegative integers (leave blocks up to 92 days)')
    if not 1 <= r['vehicle_repair_days']['min'] <= r['vehicle_repair_days']['max'] <= 92:
        raise ValueError('realism.vehicle_repair_days requires 1 <= min <= max <= 92')
    for key in ('weekday_weights', 'weekend_weights'):
        if len(r[key]) != 24 or any(x < 0 for x in r[key]) or sum(r[key]) == 0:
            raise ValueError(f'{key} must contain 24 nonnegative weights with positive sum')
    r.setdefault('public_holidays', True)
    r.setdefault('unexecuted_trip_fraction', 0.0)
    for key in ('tram_fraction', 'express_fraction', 'on_request_fraction', 'screen_fraction',
                'low_floor_fraction', 'on_time_probability', 'unexecuted_trip_fraction'):
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


def polish_holidays(year):
    """Statutory public holidays in Poland (Easter via the anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    easter = date(year, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)
    days = {date(year, 1, 1), date(year, 1, 6), easter, easter + timedelta(days=1),
            date(year, 5, 1), date(year, 5, 3), easter + timedelta(days=49), easter + timedelta(days=60),
            date(year, 8, 15), date(year, 11, 1), date(year, 11, 11), date(year, 12, 25), date(year, 12, 26)}
    if year >= 2025:  # Christmas Eve became a public holiday in 2025
        days.add(date(year, 12, 24))
    return days


def vehicle_note(rng, lookups, produced, last_year, low_floor):
    """Free-text description for vehicles.notes; about one vehicle in five has none."""
    if rng.random() < 0.2:
        return None
    pool = lookups['vehicle_equipment']
    equipment = rng.sample(pool, min(len(pool), rng.randint(1, 4)))
    if low_floor:
        equipment.append('wheelchair ramp')
    parts = []
    if equipment:
        listed = equipment[0] if len(equipment) == 1 else ', '.join(equipment[:-1]) + ' and ' + equipment[-1]
        parts.append(f'Equipped with {listed}.')
    events = [e for e in lookups['vehicle_events'] if '{year}' not in e or produced < last_year]
    for event in rng.sample(events, min(len(events), rng.randint(0, 2))):
        parts.append(event.format(year=rng.randint(produced + 1, last_year)) if '{year}' in event else event)
    return ' '.join(parts) or None


def spell(rng, first, last, length):
    """Day ordinals of a block of `length` days starting at a random day in [first, last - length + 1]."""
    start = rng.randint(first.toordinal(), max(first.toordinal(), last.toordinal() - length + 1))
    return range(start, start + length)


def vehicle_downtime(rng, vehicles, years, r):
    """Days each vehicle is out of service: periodic one-day inspections plus random multi-day repairs."""
    down = defaultdict(set)
    interval, repair = r['vehicle_inspection_interval_days'], r['vehicle_repair_days']
    for v in vehicles:
        if interval:
            offset = rng.randrange(interval)
            down[v].update(o for y in years for o in range(date(y, 1, 1).toordinal(), date(y, 12, 31).toordinal() + 1)
                           if (o - offset) % interval == 0)
        for y in years:
            for _ in range(rng.randint(0, 2 * r['vehicle_repairs_per_year'])):
                down[v].update(spell(rng, date(y, 1, 1), date(y, 12, 31), rng.randint(repair['min'], repair['max'])))
    return down


def driver_absences(rng, drivers, years, r):
    """Days each driver is away: a summer holiday, other leave blocks and sick leave spells."""
    away = defaultdict(set)
    for x in drivers:
        for y in years:
            if r['driver_summer_leave_days']:
                away[x].update(spell(rng, date(y, 6, 1), date(y, 8, 31), r['driver_summer_leave_days']))
            for length in r['driver_other_leave_blocks']:
                away[x].update(spell(rng, date(y, 1, 1), date(y, 12, 31), length))
            sick = rng.randint(0, 2 * r['driver_sick_days_per_year'])
            while sick > 0:
                length = min(sick, rng.randint(1, 5))
                away[x].update(spell(rng, date(y, 1, 1), date(y, 12, 31), length))
                sick -= length
    return away


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
    base = g['start_date']
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
    # On a weekday public holiday the weekday service is removed and the weekend/holiday service runs instead.
    holidays = set()
    if r['public_holidays'] and dates:
        holidays = {h for y in range(base.year, end_date.year + 1) for h in polish_holidays(y)
                    if base <= h <= end_date and h.weekday() < 5}
    rows_copy(conn, 'calendar_dates', (row for h in sorted(holidays) for row in ((1, h, 2), (2, h, 1))), batch)

    def service_calendar(d):
        return 2 if d.weekday() >= 5 or d in holidays else 1

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
    # The schema has no vehicle type, so the fleet is split in proportion to tram/bus trips
    # and the fleet number prefix (T-/B-) tells trams and buses apart.
    tram_share = sum(line_types[t[1]] == 'TRAM' for t in trip_specs) / len(trip_specs)
    trams = round(g['vehicles'] * tram_share)
    if 0 < tram_share < 1:
        trams = min(max(trams, 1), g['vehicles'] - 1)
    vehicle_pools = {'TRAM': range(1, trams + 1), 'BUS': range(trams + 1, g['vehicles'] + 1)}
    if g['execution_days'] and any(not vehicle_pools[line_types[t[1]]] for t in trip_specs):
        raise ValueError('Not enough vehicles to cover both bus and tram lines')

    def make_vehicles():
        last_year = (dates[-1] if dates else base).year
        for i in range(1, g['vehicles'] + 1):
            make, model = rng.choice(lookups['vehicle_makes']), rng.choice(lookups['vehicle_models'])
            produced, capacity = rng.randint(2005, 2025), rng.randint(60, 180)
            low_floor = rng.random() < r['low_floor_fraction']
            yield (i, f"{'T' if i <= trams else 'B'}-{i:07d}", make, model, produced, f'DW-{i:07d}',
                   capacity, low_floor, vehicle_note(rng, lookups, produced, last_year, low_floor))

    rows_copy(conn, 'vehicles', make_vehicles(), batch)

    def make_executions():
        """Run the full timetable: every trip on every day its calendar (with exceptions) is in service."""
        if not dates:
            return
        # Some trips stay planned only (no executions yet), e.g. newly added services.
        unexecuted = set(rng.sample([t[0] for t in trip_specs],
                                    round(len(trip_specs) * r['unexecuted_trip_fraction'])))
        by_calendar = defaultdict(list)
        for t in trip_specs:
            if t[0] not in unexecuted:
                by_calendar[t[5]].append(t)
        all_drivers = range(1, g['drivers'] + 1)
        years = range(dates[0].year, dates[-1].year + 1)
        vehicle_down = vehicle_downtime(rng, range(1, g['vehicles'] + 1), years, r)
        driver_away = driver_absences(rng, all_drivers, years, r)
        driver_free = dict.fromkeys(all_drivers, 0)
        vehicle_free = dict.fromkeys(range(1, g['vehicles'] + 1), 0)
        produced = 0
        for d in dates:
            day_abs = d.toordinal() * 1440
            # Draw delays first so resources are booked for the actual (delayed) times.
            runs = []
            for t in by_calendar[service_calendar(d)]:
                delay = 0 if rng.random() < r['on_time_probability'] else min(
                    r['max_delay_minutes'], round(rng.expovariate(1 / max(r['average_delay_minutes'], 0.01))))
                runs.append((day_abs + t[3] + delay, day_abs + t[4] + delay, delay, t))
            runs.sort(key=lambda run: run[0])
            if not r['enforce_resource_conflicts']:
                for start_abs, end_abs, delay, t in runs:
                    produced += 1
                    yield (produced, t[0], rng.randint(1, g['drivers']), rng.choice(vehicle_pools[line_types[t[1]]]),
                           d, timestamp(d, t[3] + delay), timestamp(d, t[4] + delay))
                continue
            # Each resource waits in a heap until it is free; a freed one goes to an idle stack.
            # Everyone free at midnight starts level (shuffled), so work rotates across the whole pool.
            # Taking the most recently freed (LIFO) keeps a driver on consecutive trips, i.e. a shift.
            today = d.toordinal()
            on_duty = [x for x in all_drivers
                       if (today + x) % 7 >= r['driver_days_off_per_week'] and today not in driver_away[x]]
            rng.shuffle(on_duty)
            driver_wait = [(max(driver_free[x], day_abs), i, x) for i, x in enumerate(on_duty)]
            heapq.heapify(driver_wait)
            driver_idle, driver_load, driver_first_start, driver_last_end = [], defaultdict(int), {}, {}
            vehicle_wait, vehicle_idle = {}, {}
            for kind, pool in vehicle_pools.items():
                order = [v for v in pool if today not in vehicle_down[v]]
                rng.shuffle(order)
                vehicle_wait[kind] = [(max(vehicle_free[v], day_abs), i, v) for i, v in enumerate(order)]
                heapq.heapify(vehicle_wait[kind])
                vehicle_idle[kind] = []
            for start_abs, end_abs, delay, t in runs:
                kind, minutes = line_types[t[1]], end_abs - start_abs
                while driver_wait and driver_wait[0][0] <= start_abs:
                    driver_idle.append(heapq.heappop(driver_wait)[2])
                while vehicle_wait[kind] and vehicle_wait[kind][0][0] <= start_abs:
                    vehicle_idle[kind].append(heapq.heappop(vehicle_wait[kind])[2])
                # A driver over the daily driving limit or the shift span is done for the day.
                while driver_idle and (
                        driver_load[driver_idle[-1]] + minutes > r['max_driver_minutes_per_day']
                        or end_abs - driver_first_start.get(driver_idle[-1], start_abs) > r['max_shift_span_minutes']):
                    driver_idle.pop()
                if not driver_idle or not vehicle_idle[kind]:
                    missing = 'drivers' if not driver_idle else f'{kind.lower()} vehicles'
                    raise ValueError(f'Not enough {missing} on {d} at {clock(start_abs - day_abs)} to run the full '
                                     'timetable. Increase generation.drivers / generation.vehicles.')
                driver_id, vehicle_id = driver_idle.pop(), vehicle_idle[kind].pop()
                free_at = end_abs + r['layover_minutes']
                driver_free[driver_id] = vehicle_free[vehicle_id] = free_at
                driver_load[driver_id] += minutes
                driver_first_start.setdefault(driver_id, start_abs)
                driver_last_end[driver_id] = end_abs
                heapq.heappush(driver_wait, (free_at, produced, driver_id))
                heapq.heappush(vehicle_wait[kind], (free_at, produced, vehicle_id))
                produced += 1
                yield (produced, t[0], driver_id, vehicle_id, d,
                       timestamp(d, t[3] + delay), timestamp(d, t[4] + delay))
            # A driver who worked today gets the daily rest before the next shift.
            for driver_id, last_end in driver_last_end.items():
                driver_free[driver_id] = last_end + r['driver_daily_rest_minutes']

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
          f"execution days={g['execution_days']:,}")
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
