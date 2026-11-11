"""Convert cumulative kernel counters into exact, recoverable history deltas."""

import time

from .nft import device_counter_id

MIN_WALL_MS = 1577836800000
MAX_WALL_MS = 253402300800000
MAX_SPLIT_MS = 86400000
CLOCK_TOLERANCE_MS = 2000
INT64_MAX = (1 << 63) - 1
UINT64_MAX = (1 << 64) - 1

UNCERTAIN = 1
CLOCK = 2
GAP = 4
TIMEZONE = 8
RECOVERY = 16


def wall_valid(value):
    return type(value) is int and MIN_WALL_MS <= value < MAX_WALL_MS


def _text(value, capacity, empty=False):
    return (isinstance(value, str) and (empty or bool(value)) and
            '\0' not in value and len(value.encode('utf-8')) < capacity)


def _label(second):
    local = time.localtime(second)
    return time.strftime('%Y-%m-%dT%H%z', local), local


def bucket_at(wall_ms):
    """Find a local-hour interval, keeping repeated/truncated DST hours distinct."""
    second = wall_ms // 1000
    label, local = _label(second)
    day = time.strftime('%Y-%m-%d', local)
    elapsed = local.tm_min * 60 + local.tm_sec
    low, high = second - elapsed, second
    if _label(low)[0] == label:
        start = low
    else:
        while high - low > 1:
            middle = (low + high) // 2
            if _label(middle)[0] == label:
                high = middle
            else:
                low = middle
        start = high
    low, high = second, second + 3600 - elapsed
    if _label(high)[0] == label:
        raise ValueError('Cannot determine local hour boundary')
    while high - low > 1:
        middle = (low + high) // 2
        if _label(middle)[0] == label:
            low = middle
        else:
            high = middle
    return start, high * 1000, day


def half_hour_at(wall_ms):
    hour, boundary, day = bucket_at(wall_ms)
    start = hour + ((wall_ms // 1000 - hour) // 1800) * 1800
    return start, min(boundary, (start + 1800) * 1000), day


def build_sample(previous, current, inventory, boot_id, wall_ms, mono_ms,
                 timezone, max_gap_ms, resumed):
    """Build without mutation; adopt checkpoints only after Store.stage succeeds.

    The caller sets TZ and calls time.tzset() before sampling. Pending deltas
    remain represented by the old baseline when a stage operation fails.
    """
    if (not current.exists or not current.owned or not _text(boot_id, 37) or
            len(boot_id) != 36 or not _text(current.generation, 37) or
            len(current.generation) != 36 or not _text(timezone, 128) or
            type(mono_ms) is not int or mono_ms < 0 or mono_ms > INT64_MAX or
            type(wall_ms) is not int or not -(1 << 63) <= wall_ms <= INT64_MAX or
            type(max_gap_ms) is not int or max_gap_ms <= 0):
        raise ValueError('Invalid sample inputs')
    previous = previous or {}
    for name, checkpoint in previous.items():
        if (name != checkpoint.get('counter') or not _text(name, 32) or
                not all(_text(checkpoint.get(key), capacity, True) for key, capacity in
                        (('boot_id', 37), ('generation', 37), ('timezone', 128)))):
            raise ValueError('Invalid previous checkpoint')
    for name, counter in current.counters.items():
        if (not _text(name, 32) or counter.get('name', name) != name or
                any(type(counter.get(key)) is not int or not 0 <= counter[key] <= UINT64_MAX
                    for key in ('handle', 'bytes', 'packets'))):
            raise ValueError('Invalid counter fields')

    clock_valid = wall_valid(wall_ms)
    batch = {'rows': [], 'checkpoints': {}, 'events': [], 'inventory': inventory,
             'sample_wall_ms': wall_ms if clock_valid else 0}
    result = {'batch': batch, 'rates': {}, 'clock_valid': clock_valid,
              'quality_flags': 0 if clock_valid else UNCERTAIN | CLOCK,
              'deferred_counters': 0}
    max_gap_ms = min(max_gap_ms, MAX_SPLIT_MS)

    def event(counter, kind, old_wall):
        batch['events'].append({'wall_ms': wall_ms if clock_valid else 0,
                                'kind': kind, 'counter': counter,
                                'detail': f'previous_wall_ms={old_wall}; current_wall_ms={wall_ms}'})

    detail_slots = {}

    def detail_slot(when):
        if when not in detail_slots:
            detail_slots[when] = half_hour_at(when)
        return detail_slots[when]

    def add_detail(name, when, byte_count, packet_count, flags, coverage):
        start, _, day = detail_slot(when)
        batch['rows'].append({'counter': name, 'bucket_start': start, 'day': day,
            'timezone': timezone, 'bytes': byte_count, 'packets': packet_count,
            'flags': flags, 'coverage_ms': coverage})

    for name, counter in current.counters.items():
        old = previous.get(name)
        next_checkpoint = {'counter': name, 'handle': counter['handle'],
                           'bytes': counter['bytes'], 'packets': counter['packets'],
                           'boot_id': boot_id, 'generation': current.generation,
                           'timezone': timezone, 'wall_ms': wall_ms, 'mono_ms': mono_ms}
        batch['checkpoints'][name] = next_checkpoint
        rate = {'bytes_per_second': None, 'packets_per_second': None}
        result['rates'][name] = rate

        reset = None
        if old is None:
            reset = 'initial_baseline'
        elif old['boot_id'] != boot_id:
            reset = 'boot_changed'
        elif old['generation'] != current.generation:
            reset = 'generation_changed'
        elif old['handle'] != counter['handle']:
            reset = 'counter_replaced'
        elif counter['bytes'] < old['bytes'] or counter['packets'] < old['packets']:
            reset = 'counter_reset'
        if reset:
            if old is not None:
                result['quality_flags'] |= UNCERTAIN | GAP
            event(name, reset, old['wall_ms'] if old else 0)
            continue

        if not clock_valid:
            # Keep unassigned traffic recoverable from kernel counters until time is valid.
            batch['checkpoints'][name] = dict(old, wall_ms=0)
            result['deferred_counters'] += 1
            continue
        byte_count = counter['bytes'] - old['bytes']
        packet_count = counter['packets'] - old['packets']
        if byte_count > INT64_MAX or packet_count > INT64_MAX:
            raise OverflowError('History delta exceeds the signed 64-bit range')
        mono_valid = old['mono_ms'] >= 0 and mono_ms > old['mono_ms']
        elapsed = mono_ms - old['mono_ms'] if mono_valid else 0
        previous_clock_valid = wall_valid(old['wall_ms'])
        wall_elapsed = wall_ms - old['wall_ms'] if previous_clock_valid else 0
        flags = RECOVERY if resumed else 0
        if (not mono_valid or not previous_clock_valid or wall_elapsed <= 0 or
                abs(wall_elapsed - elapsed) > CLOCK_TOLERANCE_MS):
            flags |= UNCERTAIN | CLOCK
        if elapsed > max_gap_ms or wall_elapsed > max_gap_ms:
            flags |= UNCERTAIN | GAP
        if old['timezone'] != timezone:
            flags |= UNCERTAIN | TIMEZONE
        result['quality_flags'] |= flags
        if mono_valid and previous_clock_valid and not resumed and elapsed <= max_gap_ms:
            rate['bytes_per_second'] = byte_count * 1000.0 / elapsed
            rate['packets_per_second'] = packet_count * 1000.0 / elapsed
        identity = device_counter_id(name)
        # 继续更新检查点以正确计算重连增量, 但不为离线设备生成新的零用量明细.
        if (identity and inventory is not None and identity[0] not in inventory.devices
                and not byte_count and not packet_count):
            continue
        if flags & UNCERTAIN:
            add_detail(name, wall_ms, byte_count, packet_count, flags, 0)
            if byte_count or packet_count or wall_ms != old['wall_ms']:
                event(name, 'time_uncertain', old['wall_ms'])
        if not flags & UNCERTAIN:
            cursor, allocated_bytes, allocated_packets = old['wall_ms'], 0, 0
            while cursor < wall_ms:
                _, boundary, _ = detail_slot(cursor)
                next_time = min(boundary, wall_ms)
                through = next_time - old['wall_ms']
                through_bytes = byte_count * through // wall_elapsed
                through_packets = packet_count * through // wall_elapsed
                add_detail(name, cursor, through_bytes - allocated_bytes,
                           through_packets - allocated_packets, flags, next_time - cursor)
                allocated_bytes, allocated_packets = through_bytes, through_packets
                cursor = next_time
    return result
