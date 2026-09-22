"""Byte-stable Lua state machines used by the Redis messaging backend.

Every script is one atomic transition over same-slot, generation-fenced storage.
Admission charges the prefix total together with messages, checkpoints, or records.
A generation reserves its own tombstone record, so cleanup does not need free quota.
Redis ``TIME`` is the only retention clock: active production has no deadline, terminal settlement or
owner loss starts one, and the first operation after the deadline moves ``active`` to
``expiring``. Expiration and explicit deletion retain generation tombstones so stale
handles fail instead of attaching to recreated data. The byte hashes and transition
matrix are protected by ``tests/test_retention.py`` and real Redis cases in
``tests/test_redis_backend.py``; comments stay outside Lua literals so explanation
changes cannot alter the deployed program bytes.
"""

# Notifications are advisory, bounded metadata in the same Cluster slot. Reading the
# integer back as text avoids converting its full Redis int64 range through Lua floats.
_NOTIFICATION_PREAMBLE = r"""
local function write_notification(control, generation, message_sequence, control_sequence)
    local notifications = KEYS[#KEYS - 1]
    local notification_counter = KEYS[#KEYS]
    redis.call('INCR', notification_counter)
    local notification_sequence = redis.call('GET', notification_counter)
    redis.call('XADD', notifications,
        'MAXLEN', '=', '4096', notification_sequence .. '-0',
        'scope', redis.sha1hex(control),
        'generation', generation,
        'message', message_sequence,
        'control', control_sequence)
end
"""

_WAIT_CURSOR_SCRIPT = r"""
return {
    redis.call('GET', KEYS[3]) or '',
    redis.call('HGET', KEYS[1], 'generation') or '0',
    redis.call('HGET', KEYS[1], 'state') or '',
    redis.call('HGET', KEYS[2], 'seq') or '0',
    redis.call('HGET', KEYS[1], 'signal_seq') or '0'
}
"""

# Read may only announce expiry by moving ``active`` to ``expiring``. Physical cleanup
# remains owned by the fenced expiration scripts below.
_READ_CONTROL_SCRIPT = r"""
local control = KEYS[1]
local state = redis.call('HGET', control, 'state')
if not state then
    return {'NONE'}
end
local generation = redis.call('HGET', control, 'generation') or ''
if state == 'active' then
    local deadline = tonumber(redis.call('HGET', control, 'retention_deadline_ms') or '')
    if deadline then
        local now = redis.call('TIME')
        local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
        if now_ms >= deadline then
            redis.call('HSET', control, 'state', 'expiring')
            state = 'expiring'
        end
    end
end
return {'OK', generation, state}
"""

# Prepare atomically selects owner versus attachment, validates codec/limits, and starts
# a fresh generation after deleted/expired tombstones. A new active owner clears any
# prior deadline; only terminal/owner-loss paths may start retention.
_PREPARE_SCRIPT = (
    _NOTIFICATION_PREAMBLE
    + r"""
local channel_meta = KEYS[1]
local control = KEYS[2]
local meta = KEYS[3]
local run_key = KEYS[4]
local lease_key = KEYS[5]
local key_index = KEYS[6]
local capacity = KEYS[7]
local expirations = KEYS[8]
local requested_generation = tonumber(ARGV[1])
local requested_run = ARGV[2]
local requested_codec = ARGV[3]
local requested_after = ARGV[4]
local cancellable = ARGV[5]
local recoverable = ARGV[6]
local owner_token = ARGV[7]
local lease_ms = ARGV[8]
local requested_channel = ARGV[9]
local requested_stream = ARGV[10]
local max_message_payload_bytes = ARGV[11]
local max_checkpoint_bytes = ARGV[12]
local max_thread_messages = ARGV[13]
local max_thread_payload_bytes = ARGV[14]
local retention_ms = tonumber(ARGV[15])
local max_total_bytes = ARGV[16]
local max_total_records = ARGV[17]

local function set_retention_deadline()
    if retention_ms == 0 then
        redis.call('HDEL', control, 'retention_deadline_ms')
        redis.call('ZREM', expirations, control)
        return
    end
    local retention_now = redis.call('TIME')
    local deadline = tonumber(retention_now[1]) * 1000
        + math.floor(tonumber(retention_now[2]) / 1000)
        + retention_ms
    redis.call('HSET', control, 'retention_deadline_ms', tostring(deadline))
    redis.call('ZADD', expirations, deadline, control)
end

local function write_signal()
    redis.call('HINCRBY', control, 'signal_seq', 1)
    write_notification(control, redis.call('HGET', control, 'generation'),
        '0', redis.call('HGET', control, 'signal_seq'))
end

local function initialize_lease_diagnostics()
    local lease_now = redis.call('TIME')
    redis.call('HSET', run_key,
        'lease_renew_count', '0',
        'lease_last_success_seconds', lease_now[1],
        'lease_last_success_microseconds', lease_now[2])
end

local function archive_lease_diagnostics()
    redis.call('HSET', run_key,
        'lease_previous_fence', redis.call('HGET', run_key, 'fence') or '',
        'lease_previous_renew_count', redis.call('HGET', run_key, 'lease_renew_count') or '',
        'lease_previous_last_success_seconds', redis.call('HGET', run_key, 'lease_last_success_seconds') or '',
        'lease_previous_last_success_microseconds', redis.call('HGET', run_key, 'lease_last_success_microseconds') or '')
end

local control_state = redis.call('HGET', control, 'state')
local stored_generation = tonumber(redis.call('HGET', control, 'generation') or '0')
local activate_generation = false
if control_state == 'active' then
    local deadline = tonumber(redis.call('HGET', control, 'retention_deadline_ms') or '')
    if deadline then
        local retention_now = redis.call('TIME')
        local now_ms = tonumber(retention_now[1]) * 1000
            + math.floor(tonumber(retention_now[2]) / 1000)
        if now_ms >= deadline then
            redis.call('HSET', control, 'state', 'expiring')
            return {'STREAM_EXPIRING', tostring(stored_generation)}
        end
    end
end
if not control_state then
    if requested_generation ~= 1 then
        return {'GENERATION_CHANGED'}
    end
    activate_generation = true
elseif control_state == 'deleting' then
    return {'STREAM_DELETED', tostring(stored_generation)}
elseif control_state == 'expiring' then
    return {'STREAM_EXPIRING', tostring(stored_generation)}
elseif control_state == 'deleted' or control_state == 'expired' then
    if requested_generation ~= stored_generation + 1 then
        return {'GENERATION_CHANGED'}
    end
    activate_generation = true
elseif control_state == 'active' then
    if requested_generation ~= stored_generation then
        return {'GENERATION_CHANGED'}
    end
else
    return {'INVALID_CONTROL_STATE', control_state}
end

local latest = tonumber(redis.call('HGET', meta, 'seq') or '0')
local cursor = latest
if requested_after ~= '__tail__' then
    cursor = tonumber(requested_after)
end
if cursor == nil or cursor < 0 or cursor > latest then
    return {'INVALID_CURSOR', tostring(cursor or -1), tostring(latest)}
end

local stored_codec = redis.call('HGET', channel_meta, 'codec')
if stored_codec and stored_codec ~= requested_codec then
    return {'CODEC_MISMATCH', stored_codec}
end
if stored_codec then
    if redis.call('HGET', channel_meta, 'max_message_payload_bytes') ~= max_message_payload_bytes
        or redis.call('HGET', channel_meta, 'max_checkpoint_bytes') ~= max_checkpoint_bytes
        or redis.call('HGET', channel_meta, 'max_thread_messages') ~= max_thread_messages
        or redis.call('HGET', channel_meta, 'max_thread_payload_bytes') ~= max_thread_payload_bytes
        or redis.call('HGET', channel_meta, 'max_total_bytes') ~= max_total_bytes
        or redis.call('HGET', channel_meta, 'max_total_records') ~= max_total_records then
        return {'LIMITS_MISMATCH'}
    end
    if redis.call('HGET', channel_meta, 'retention_ms') ~= tostring(retention_ms) then
        return {'RETENTION_MISMATCH'}
    end
end
if redis.call('EXISTS', capacity) == 1 then
    if redis.call('HGET', capacity, 'max_total_bytes') ~= max_total_bytes
        or redis.call('HGET', capacity, 'max_total_records') ~= max_total_records then
        return {'LIMITS_MISMATCH'}
    end
end
local new_run = redis.call('EXISTS', run_key) == 0
if new_run then
    local active_key = redis.call('HGET', meta, 'active_key')
    local active_lease = redis.call('HGET', meta, 'active_lease')
    if active_key and active_lease and redis.call('EXISTS', active_lease) == 1 then
        return {'RUN_ACTIVE', redis.call('HGET', meta, 'active_run') or ''}
    end
end
local additional_records = 0
if not stored_codec then additional_records = additional_records + 1 end
if not control_state then additional_records = additional_records + 1 end
if activate_generation then additional_records = additional_records + 1 end
if new_run then additional_records = additional_records + 1 end
local total_records = tonumber(redis.call('HGET', capacity, 'total_records') or '0')
if total_records + additional_records > tonumber(max_total_records) then
    return {'QUOTA_EXCEEDED', 'total_records', max_total_records}
end
redis.call('HSETNX', capacity, 'max_total_bytes', max_total_bytes)
redis.call('HSETNX', capacity, 'max_total_records', max_total_records)
redis.call('HSETNX', capacity, 'total_bytes', '0')
redis.call('HINCRBY', capacity, 'total_records', additional_records)
if not stored_codec then
    redis.call('HSET', channel_meta,
        'channel', requested_channel,
        'codec', requested_codec,
        'max_message_payload_bytes', max_message_payload_bytes,
        'max_checkpoint_bytes', max_checkpoint_bytes,
        'max_thread_messages', max_thread_messages,
        'max_thread_payload_bytes', max_thread_payload_bytes,
        'max_total_bytes', max_total_bytes,
        'max_total_records', max_total_records,
        'retention_ms', tostring(retention_ms))
end

if activate_generation then
    redis.call('HSET', control,
        'channel', requested_channel,
        'stream', requested_stream,
        'generation', tostring(requested_generation),
        'state', 'active',
        'retained_bytes', '0',
        'retained_records', '1')
    redis.call('HSETNX', control, 'signal_seq', '0')
end

if new_run then
    redis.call('HINCRBY', control, 'retained_records', 1)
end
redis.call('HSET', meta,
    'channel', requested_channel,
    'stream', requested_stream,
    'generation', tostring(requested_generation),
    'seq', tostring(latest))
redis.call('HSETNX', meta, 'payload_bytes', '0')
redis.call('SADD', key_index, meta)

if redis.call('EXISTS', run_key) == 1 then
    redis.call('SADD', key_index, run_key, lease_key)
    local status = redis.call('HGET', run_key, 'status')
    if status == 'completed' or status == 'cancelled' or status == 'failed' or status == 'owner_lost' then
        return {'ATTACH', tostring(cursor), status}
    end
    if redis.call('EXISTS', lease_key) == 1 then
        return {'ATTACH', tostring(cursor), status}
    end
    local settlement_started = redis.call('HGET', run_key, 'settling') or '0'
    if status == 'cancel_requested' and settlement_started == '1' then
        redis.call('HSET', run_key,
            'status', 'owner_lost',
            'end_seq', tostring(latest),
            'error_class', 'tinkerfin_messaging.OwnerLost',
            'error_message', 'producer lease expired during cancellation settlement')
        if redis.call('HGET', meta, 'active_key') == run_key then
            redis.call('HDEL', meta, 'active_run', 'active_key', 'active_lease')
        end
        write_signal()
        set_retention_deadline()
        return {'ATTACH', tostring(cursor), 'owner_lost'}
    end
    local stored_recoverable = redis.call('HGET', run_key, 'recoverable')
    if stored_recoverable == '1' and recoverable == '1' then
        local fence = redis.call('HINCRBY', meta, 'fence_counter', 1)
        local recovered_status = 'running'
        if status == 'cancel_requested' then
            recovered_status = 'cancel_requested'
        end
        archive_lease_diagnostics()
        redis.call('HSET', run_key,
            'status', recovered_status,
            'settling', '0',
            'owner_token', owner_token,
            'fence', tostring(fence),
            'cancellable', cancellable,
            'error_class', '',
            'error_message', '')
        redis.call('HSET', meta,
            'active_run', requested_run,
            'active_key', run_key,
            'active_lease', lease_key)
        initialize_lease_diagnostics()
        redis.call('HDEL', control, 'retention_deadline_ms')
        redis.call('ZREM', expirations, control)
        redis.call('SET', lease_key, owner_token .. ':' .. tostring(fence), 'PX', lease_ms)
        write_signal()
        return {
            'RECOVER', tostring(cursor), tostring(fence),
            redis.call('HGET', run_key, 'checkpoint_present') or '0',
            redis.call('HGET', run_key, 'checkpoint_position') or '',
            redis.call('HGET', run_key, 'checkpoint_message_id') or ''
        }
    end
    redis.call('HSET', run_key,
        'status', 'owner_lost',
        'end_seq', tostring(latest),
        'error_class', 'tinkerfin_messaging.OwnerLost',
        'error_message', 'producer lease expired')
    if redis.call('HGET', meta, 'active_key') == run_key then
        redis.call('HDEL', meta, 'active_run', 'active_key', 'active_lease')
    end
    write_signal()
    set_retention_deadline()
    return {'ATTACH', tostring(cursor), 'owner_lost'}
end

local active_key = redis.call('HGET', meta, 'active_key')
if active_key then
    local active_lease = redis.call('HGET', meta, 'active_lease')
    if active_lease and redis.call('EXISTS', active_lease) == 1 then
        return {'RUN_ACTIVE', redis.call('HGET', meta, 'active_run') or ''}
    end
    redis.call('HSET', active_key,
        'status', 'owner_lost',
        'end_seq', tostring(latest),
        'error_class', 'tinkerfin_messaging.OwnerLost',
        'error_message', 'producer lease expired')
    redis.call('HDEL', meta, 'active_run', 'active_key', 'active_lease')
    write_signal()
    set_retention_deadline()
end

local fence = redis.call('HINCRBY', meta, 'fence_counter', 1)
redis.call('SADD', key_index, run_key, lease_key)
redis.call('HSET', run_key,
    'run', requested_run,
    'status', 'running',
    'settling', '0',
    'publication_closed', '0',
    'publication_ready', '0',
    'start_seq', tostring(latest),
    'end_seq', tostring(latest),
    'owner_token', owner_token,
    'fence', tostring(fence),
    'cancellable', cancellable,
    'recoverable', recoverable,
    'checkpoint_present', '0',
    'error_class', '',
    'error_message', '')
redis.call('HSET', meta,
    'active_run', requested_run,
    'active_key', run_key,
    'active_lease', lease_key)
initialize_lease_diagnostics()
redis.call('HDEL', control, 'retention_deadline_ms')
        redis.call('ZREM', expirations, control)
redis.call('SET', lease_key, owner_token .. ':' .. tostring(fence), 'PX', lease_ms)
return {'START', tostring(cursor), tostring(fence)}
"""
)


# Append preserves owner token/fence, contiguous sequence, idempotent message IDs, and
# capacity accounting in one transaction; it never changes the retention deadline.
_APPEND_SCRIPT = (
    _NOTIFICATION_PREAMBLE
    + r"""
local control = KEYS[1]
local channel_meta = KEYS[2]
local meta = KEYS[3]
local run_key = KEYS[4]
local lease_key = KEYS[5]
local messages = KEYS[6]
local dedupe = KEYS[7]
local key_index = KEYS[8]
local capacity = KEYS[9]
local generation = ARGV[1]
local owner_token = ARGV[2]
local fence = ARGV[3]
local message_id = ARGV[4]
local run = ARGV[5]
local codec = ARGV[6]
local payload = ARGV[7]
local signature = ARGV[8]
local checkpoint_present = ARGV[9]
local checkpoint_position = ARGV[10]
local checkpoint_message_id = ARGV[11]
local max_thread_messages = tonumber(ARGV[12])
local max_thread_payload_bytes = tonumber(ARGV[13])
local max_total_bytes = tonumber(ARGV[14])
local max_total_records = tonumber(ARGV[15])
local external = ARGV[16] == '1'
local closes_publication = ARGV[17] == '1'
local opens_publication = ARGV[18] == '1'
local expected_owner = owner_token .. ':' .. fence

if redis.call('HGET', control, 'state') ~= 'active' or redis.call('HGET', control, 'generation') ~= generation then
    return {'STREAM_DELETED'}
end
local status = redis.call('HGET', run_key, 'status')
if not external then
    if redis.call('GET', lease_key) ~= expected_owner then
        return {'OWNERSHIP_LOST'}
    end
    if redis.call('HGET', run_key, 'owner_token') ~= owner_token or redis.call('HGET', run_key, 'fence') ~= fence then
        return {'OWNERSHIP_LOST'}
    end
    if status ~= 'running' and status ~= 'cancel_requested' then
        return {'OWNERSHIP_LOST'}
    end
end

local stored_codec = redis.call('HGET', channel_meta, 'codec')
if not stored_codec or stored_codec ~= codec then
    return {'CODEC_MISMATCH', stored_codec or ''}
end

if tonumber(redis.call('HGET', capacity, 'max_total_bytes')) ~= max_total_bytes
    or tonumber(redis.call('HGET', capacity, 'max_total_records')) ~= max_total_records then
    return {'LIMITS_MISMATCH'}
end

local existing_signature = redis.call('HGET', dedupe, 'signature')
if existing_signature then
    if existing_signature ~= signature then
        return {'MESSAGE_CONFLICT'}
    end
    return {
        'IDEMPOTENT',
        redis.call('HGET', dedupe, 'seq'),
        redis.call('HGET', dedupe, 'created_seconds'),
        redis.call('HGET', dedupe, 'created_microseconds')
    }
end

-- Observe the producer lease without acquiring or extending it. The same Lua
-- commit arbitrates publication against protocol terminals and cancellation.
if external then
    if status ~= 'running' or redis.call('HGET', run_key, 'settling') ~= '0'
        or redis.call('HGET', run_key, 'publication_closed') ~= '0' then
        return {'PUBLICATION_REJECTED', 'run_closed'}
    end
    local active_owner = redis.call('HGET', run_key, 'owner_token')
    local active_fence = redis.call('HGET', run_key, 'fence')
    if not active_owner or not active_fence or redis.call('GET', lease_key) ~= active_owner .. ':' .. active_fence then
        return {'PUBLICATION_REJECTED', 'owner_lost'}
    end
    if redis.call('HGET', run_key, 'publication_ready') ~= '1' then
        return {'PUBLICATION_REJECTED', 'run_not_ready'}
    end
end

local latest = tonumber(redis.call('HGET', meta, 'seq') or '0')
local payload_bytes = tonumber(redis.call('HGET', meta, 'payload_bytes') or '0')
if latest >= max_thread_messages then
    return {'QUOTA_EXCEEDED', 'thread_messages', tostring(max_thread_messages)}
end
if payload_bytes + string.len(payload) > max_thread_payload_bytes then
    return {'QUOTA_EXCEEDED', 'thread_payload_bytes', tostring(max_thread_payload_bytes)}
end

local checkpoint_bytes = 0
local previous_checkpoint_bytes = 0
if checkpoint_present == '1' then
    checkpoint_bytes = string.len(checkpoint_position) + string.len(checkpoint_message_id)
    previous_checkpoint_bytes = string.len(redis.call('HGET', run_key, 'checkpoint_position') or '')
        + string.len(redis.call('HGET', run_key, 'checkpoint_message_id') or '')
end
local additional_bytes = string.len(payload) + 2 * checkpoint_bytes - previous_checkpoint_bytes
local total_bytes = tonumber(redis.call('HGET', capacity, 'total_bytes'))
local total_records = tonumber(redis.call('HGET', capacity, 'total_records'))
if total_bytes + additional_bytes > max_total_bytes then
    return {'QUOTA_EXCEEDED', 'total_bytes', tostring(max_total_bytes)}
end
if total_records + 1 > max_total_records then
    return {'QUOTA_EXCEEDED', 'total_records', tostring(max_total_records)}
end
redis.call('HINCRBY', capacity, 'total_bytes', additional_bytes)
redis.call('HINCRBY', capacity, 'total_records', 1)
redis.call('HINCRBY', control, 'retained_bytes', additional_bytes)
redis.call('HINCRBY', control, 'retained_records', 1)
local seq = redis.call('HINCRBY', meta, 'seq', 1)
redis.call('HINCRBY', meta, 'payload_bytes', string.len(payload))
local now = redis.call('TIME')
local created_seconds = now[1]
local created_microseconds = now[2]
redis.call('XADD', messages, tostring(seq) .. '-0',
    'message_id', message_id,
    'run', run,
    'codec', codec,
    'payload', payload,
    'created_seconds', created_seconds,
    'created_microseconds', created_microseconds)
redis.call('HSET', dedupe,
    'signature', signature,
    'checkpoint_present', checkpoint_present,
    'checkpoint_position', checkpoint_position,
    'checkpoint_message_id', checkpoint_message_id,
    'seq', tostring(seq),
    'created_seconds', created_seconds,
    'created_microseconds', created_microseconds)
redis.call('SADD', key_index, messages, dedupe)
redis.call('HSET', run_key, 'end_seq', tostring(seq))
if closes_publication then redis.call('HSET', run_key, 'publication_closed', '1') end
if opens_publication then redis.call('HSET', run_key, 'publication_ready', '1') end
if checkpoint_present == '1' then
    redis.call('HSET', run_key,
        'checkpoint_present', '1',
        'checkpoint_position', checkpoint_position,
        'checkpoint_message_id', checkpoint_message_id)
end
write_notification(control, generation, redis.call('HGET', meta, 'seq'), '0')
return {'APPENDED', tostring(seq), created_seconds, created_microseconds}
"""
)


# Settlement fences further producer writes before terminal metadata is committed.
_BEGIN_SETTLEMENT_SCRIPT = r"""
local control = KEYS[1]
local run_key = KEYS[2]
local lease_key = KEYS[3]
local generation = ARGV[1]
local owner_token = ARGV[2]
local fence = ARGV[3]
local expected_owner = owner_token .. ':' .. fence

if redis.call('HGET', control, 'state') ~= 'active' or redis.call('HGET', control, 'generation') ~= generation then
    return {'STREAM_DELETED'}
end
if redis.call('GET', lease_key) ~= expected_owner then
    return {'OWNERSHIP_LOST'}
end
if redis.call('HGET', run_key, 'owner_token') ~= owner_token or redis.call('HGET', run_key, 'fence') ~= fence then
    return {'OWNERSHIP_LOST'}
end
local status = redis.call('HGET', run_key, 'status')
if redis.call('HGET', run_key, 'settling') == '1' then
    return {'OWNERSHIP_LOST'}
end
if status == 'cancel_requested' then
    redis.call('HSET', run_key, 'settling', '1')
    return {'CANCEL_REQUESTED'}
end
if status == 'running' then
    redis.call('HSET', run_key, 'settling', '1')
    return {'SETTLING'}
end
return {'OWNERSHIP_LOST'}
"""


# Finish records the unique producer outcome, releases the lease, and starts the
# server-time retention deadline without expiring active data early.
_FINISH_SCRIPT = (
    _NOTIFICATION_PREAMBLE
    + r"""
local control = KEYS[1]
local meta = KEYS[2]
local run_key = KEYS[3]
local lease_key = KEYS[4]
local expirations = KEYS[5]
local generation = ARGV[1]
local owner_token = ARGV[2]
local fence = ARGV[3]
local status = ARGV[4]
local error_class = ARGV[5]
local error_message = ARGV[6]
local retention_ms = tonumber(ARGV[7])
local expected_owner = owner_token .. ':' .. fence

local function write_signal()
    redis.call('HINCRBY', control, 'signal_seq', 1)
    write_notification(control, redis.call('HGET', control, 'generation'),
        '0', redis.call('HGET', control, 'signal_seq'))
end

if redis.call('HGET', control, 'state') ~= 'active' or redis.call('HGET', control, 'generation') ~= generation then
    return {'STREAM_DELETED'}
end
if redis.call('GET', lease_key) ~= expected_owner then
    return {'OWNERSHIP_LOST'}
end
if redis.call('HGET', run_key, 'owner_token') ~= owner_token or redis.call('HGET', run_key, 'fence') ~= fence then
    return {'OWNERSHIP_LOST'}
end
local current_status = redis.call('HGET', run_key, 'status')
if current_status == 'cancel_requested' and status == 'completed' then
    status = 'failed'
    error_class = 'tinkerfin_messaging.InvalidSettlement'
    error_message = 'producer completed without settling accepted cancellation'
end
local latest = redis.call('HGET', meta, 'seq') or '0'
redis.call('HSET', run_key,
    'status', status,
    'settling', '1',
    'end_seq', latest,
    'error_class', error_class,
    'error_message', error_message)
if redis.call('HGET', meta, 'active_key') == run_key then
    redis.call('HDEL', meta, 'active_run', 'active_key', 'active_lease')
end
redis.call('DEL', lease_key)
if retention_ms == 0 then
    redis.call('HDEL', control, 'retention_deadline_ms')
    redis.call('ZREM', expirations, control)
else
    local retention_now = redis.call('TIME')
    local deadline = tonumber(retention_now[1]) * 1000
        + math.floor(tonumber(retention_now[2]) / 1000)
        + retention_ms
    redis.call('HSET', control, 'retention_deadline_ms', tostring(deadline))
    redis.call('ZADD', expirations, deadline, control)
end
write_signal()
return {'OK'}
"""
)


# Cancellation is generation- and run-fenced; it signals the current owner but does not
# reinterpret abandonment as a producer failure or delete retained replay.
_CANCEL_SCRIPT = (
    _NOTIFICATION_PREAMBLE
    + r"""
local control = KEYS[1]
local run_key = KEYS[2]
local generation = ARGV[1]

local function write_signal()
    redis.call('HINCRBY', control, 'signal_seq', 1)
    write_notification(control, redis.call('HGET', control, 'generation'),
        '0', redis.call('HGET', control, 'signal_seq'))
end

if redis.call('HGET', control, 'state') ~= 'active' or redis.call('HGET', control, 'generation') ~= generation then
    return {'STREAM_DELETED'}
end
if redis.call('EXISTS', run_key) == 0 then
    return {'NOT_FOUND'}
end
local status = redis.call('HGET', run_key, 'status')
if status == 'completed' or status == 'cancelled' or status == 'failed' or status == 'owner_lost' then
    return {'FINAL'}
end
if redis.call('HGET', run_key, 'settling') == '1' then
    return {'FINAL'}
end
if redis.call('HGET', run_key, 'cancellable') ~= '1' then
    return {'UNSUPPORTED'}
end
if status == 'cancel_requested' then
    return {'DUPLICATE'}
end
redis.call('HSET', run_key, 'status', 'cancel_requested')
write_signal()
return {'REQUESTED'}
"""
)


# Snapshot returns one atomic control/run view. It can announce ``expiring`` at the
# Redis-time boundary but leaves physical cleanup to an explicit expiration claimant.
_RUN_SNAPSHOT_SCRIPT = (
    _NOTIFICATION_PREAMBLE
    + r"""
local control = KEYS[1]
local meta = KEYS[2]
local run_key = KEYS[3]
local lease_key = KEYS[4]
local messages = KEYS[5]
local channel_meta = KEYS[6]
local expirations = KEYS[7]
local generation = ARGV[1]
local requested_after = ARGV[2]
local retention_ms = tonumber(ARGV[3])

local function set_retention_deadline()
    if retention_ms == 0 then
        redis.call('HDEL', control, 'retention_deadline_ms')
        redis.call('ZREM', expirations, control)
        return
    end
    local retention_now = redis.call('TIME')
    local deadline = tonumber(retention_now[1]) * 1000
        + math.floor(tonumber(retention_now[2]) / 1000)
        + retention_ms
    redis.call('HSET', control, 'retention_deadline_ms', tostring(deadline))
    redis.call('ZADD', expirations, deadline, control)
end

local function write_signal()
    redis.call('HINCRBY', control, 'signal_seq', 1)
    write_notification(control, redis.call('HGET', control, 'generation'),
        '0', redis.call('HGET', control, 'signal_seq'))
end

if redis.call('HGET', control, 'state') ~= 'active' or redis.call('HGET', control, 'generation') ~= generation then
    return {'STREAM_DELETED'}
end
if redis.call('EXISTS', run_key) == 0 then
    return {'NOT_FOUND'}
end
local status = redis.call('HGET', run_key, 'status')
if status ~= 'running' and status ~= 'cancel_requested' and status ~= 'completed' and status ~= 'cancelled' and status ~= 'failed' and status ~= 'owner_lost' then
    return {'INVALID_STATUS', status or ''}
end
local lease_ttl_ms = -1
if status ~= 'completed' and status ~= 'cancelled' and status ~= 'failed' and status ~= 'owner_lost' then
    lease_ttl_ms = redis.call('PTTL', lease_key)
    if lease_ttl_ms < 0 then
        if status == 'cancel_requested' then
            local latest = redis.call('HGET', meta, 'seq') or '0'
            redis.call('HSET', run_key,
                'status', 'owner_lost',
                'end_seq', latest,
                'error_class', 'tinkerfin_messaging.OwnerLost',
                'error_message', 'producer lease expired during cancellation')
            if redis.call('HGET', meta, 'active_key') == run_key then
                redis.call('HDEL', meta, 'active_run', 'active_key', 'active_lease')
            end
            status = 'owner_lost'
            write_signal()
            set_retention_deadline()
        elseif redis.call('HGET', run_key, 'recoverable') ~= '1' then
            local latest = redis.call('HGET', meta, 'seq') or '0'
            redis.call('HSET', run_key,
                'status', 'owner_lost',
                'end_seq', latest,
                'error_class', 'tinkerfin_messaging.OwnerLost',
                'error_message', 'producer lease expired')
            if redis.call('HGET', meta, 'active_key') == run_key then
                redis.call('HDEL', meta, 'active_run', 'active_key', 'active_lease')
            end
            status = 'owner_lost'
            write_signal()
            set_retention_deadline()
        end
    end
end

local end_seq = redis.call('HGET', run_key, 'end_seq') or ''
local observed_at = redis.call('TIME')
local page = {}
if requested_after ~= '__none__' then
    local cursor = tonumber(requested_after)
    local parsed_end = tonumber(end_seq)
    if cursor == nil or cursor < 0 or parsed_end == nil or parsed_end < 0 then
        return {'INVALID_BOUNDARY', requested_after, end_seq}
    end
    page = redis.call(
        'XRANGE', messages,
        '(' .. tostring(cursor) .. '-0', tostring(parsed_end) .. '-0',
        'COUNT', '100')
end

return {
    'OK',
    status,
    end_seq,
    redis.call('HGET', run_key, 'error_class') or '',
    redis.call('HGET', run_key, 'error_message') or '',
    tostring(redis.call('HGET', control, 'signal_seq') or '0'),
    tostring(lease_ttl_ms),
    redis.call('HGET', run_key, 'lease_renew_count') or '',
    redis.call('HGET', run_key, 'lease_last_success_seconds') or '',
    redis.call('HGET', run_key, 'lease_last_success_microseconds') or '',
    page,
    observed_at[1],
    observed_at[2],
    redis.call('HGET', run_key, 'start_seq') or '',
    redis.call('HGET', run_key, 'settling') or '0',
    redis.call('HGET', run_key, 'cancellable') or '0',
    redis.call('HGET', run_key, 'recoverable') or '0',
    redis.call('HGET', run_key, 'owner_token') or '',
    redis.call('HGET', run_key, 'fence') or '',
    redis.call('HGET', run_key, 'checkpoint_present') or '0',
    redis.call('HGET', run_key, 'checkpoint_position') or '',
    redis.call('HGET', run_key, 'checkpoint_message_id') or '',
    redis.call('HGET', meta, 'active_run') or '',
    redis.call('HGET', meta, 'seq') or '0',
    redis.call('HGET', meta, 'payload_bytes') or '0',
    redis.call('HGET', meta, 'fence_counter') or '0',
    redis.call('HGET', channel_meta, 'codec') or '',
    redis.call('HGET', channel_meta, 'max_message_payload_bytes') or '',
    redis.call('HGET', channel_meta, 'max_checkpoint_bytes') or '',
    redis.call('HGET', channel_meta, 'max_thread_messages') or '',
    redis.call('HGET', channel_meta, 'max_thread_payload_bytes') or '',
    redis.call('HGET', channel_meta, 'retention_ms') or '',
    redis.call('HGET', run_key, 'publication_closed') or '',
    redis.call('HGET', run_key, 'publication_ready') or ''
}
"""
)


# State loading reads every requested value and the Redis clock in one script. A
# preceding control lookup only selects same-slot keys; the script rejects that lookup
# if the current generation or active-run key changed before this atomic read.
_MESSAGING_STATE_SNAPSHOT_SCRIPT = r"""
local control = KEYS[1]
local channel_meta = KEYS[2]
local meta = KEYS[3]
local target_run = KEYS[4]
local target_lease = KEYS[5]
local messages = KEYS[6]
local dedupe = KEYS[7]
local selected_tombstone = KEYS[8]
local active_run = KEYS[9]
local active_lease = KEYS[10]

local expected_generation = tonumber(ARGV[1])
local selected_generation = tonumber(ARGV[2])
local include_active_run = ARGV[3] == '1'
local expected_active_run_key = ARGV[4]
local expected_active_lease_key = ARGV[5]
local requested_message_id = ARGV[6]

local current_generation = tonumber(redis.call('HGET', control, 'generation') or '0')
if current_generation ~= expected_generation then
    return {'GENERATION_CHANGED'}
end

local observed_at = redis.call('TIME')
local state = redis.call('HGET', control, 'state') or 'none'
local control_values = redis.call('HGETALL', control)
local channel_values = redis.call('HGETALL', channel_meta)
if current_generation == 0 then
    return {
        'OK', 'missing', '0', state, observed_at[1], observed_at[2], '0',
        control_values, channel_values, {}, {}, '-2', {}, '-2', {}, {}, ''
    }
end
if state ~= 'active' and state ~= 'deleting' and state ~= 'deleted'
    and state ~= 'expiring' and state ~= 'expired' then
    return {'INVALID_CONTROL_STATE', state}
end
if selected_generation ~= current_generation then
    return {
        'OK', 'unavailable', tostring(current_generation), state,
        observed_at[1], observed_at[2], '0', control_values, channel_values,
        {}, {}, '-2', {}, '-2', {}, {}, redis.call('GET', selected_tombstone) or ''
    }
end
if state == 'deleted' or state == 'expired' then
    return {
        'OK', 'unavailable', tostring(current_generation), state,
        observed_at[1], observed_at[2], '0', control_values, channel_values,
        {}, {}, '-2', {}, '-2', {}, {}, state
    }
end
if state == 'deleting' or state == 'expiring' then
    return {
        'OK', 'sealed', tostring(current_generation), state,
        observed_at[1], observed_at[2], state == 'expiring' and '1' or '0',
        control_values, channel_values, {}, {}, '-2', {}, '-2', {}, {}, ''
    }
end

if include_active_run then
    local stored_active_run_key = redis.call('HGET', meta, 'active_key') or ''
    local stored_active_lease_key = redis.call('HGET', meta, 'active_lease') or ''
    if stored_active_run_key ~= expected_active_run_key
        or stored_active_lease_key ~= expected_active_lease_key then
        return {'ACTIVE_RUN_CHANGED'}
    end
end

local now_ms = tonumber(observed_at[1]) * 1000
    + math.floor(tonumber(observed_at[2]) / 1000)
local retention_deadline_ms = tonumber(
    redis.call('HGET', control, 'retention_deadline_ms') or '0')
local retention_expired = '0'
if retention_deadline_ms > 0 and now_ms >= retention_deadline_ms then
    retention_expired = '1'
end

local dedupe_values = {}
local matching_message = {}
if requested_message_id ~= '' then
    dedupe_values = redis.call('HGETALL', dedupe)
    local sequence = redis.call('HGET', dedupe, 'seq')
    if sequence then
        matching_message = redis.call(
            'XRANGE', messages, sequence .. '-0', sequence .. '-0', 'COUNT', '1')
    end
end

return {
    'OK', 'active', tostring(current_generation), state,
    observed_at[1], observed_at[2], retention_expired,
    control_values,
    channel_values,
    redis.call('HGETALL', meta),
    redis.call('HGETALL', target_run),
    tostring(redis.call('PTTL', target_lease)),
    include_active_run and redis.call('HGETALL', active_run) or {},
    include_active_run and tostring(redis.call('PTTL', active_lease)) or '-2',
    dedupe_values,
    matching_message,
    ''
}
"""


# Renewal succeeds only for the current owner token and fence and never extends
# terminal retention.
_RENEW_SCRIPT = r"""
local control = KEYS[1]
local run_key = KEYS[2]
local lease_key = KEYS[3]
local generation = ARGV[1]
local expected_owner = ARGV[2]
local lease_ms = ARGV[3]
if redis.call('HGET', control, 'state') ~= 'active' or redis.call('HGET', control, 'generation') ~= generation then
    return {'STREAM_DELETED'}
end
if redis.call('EXISTS', run_key) == 0 or redis.call('GET', lease_key) ~= expected_owner then
    return {'OWNERSHIP_LOST'}
end
local lease_now = redis.call('TIME')
local renew_count = redis.call('HINCRBY', run_key, 'lease_renew_count', 1)
redis.call('HSET', run_key,
    'lease_last_success_seconds', lease_now[1],
    'lease_last_success_microseconds', lease_now[2])
redis.call('PEXPIRE', lease_key, lease_ms)
return {'RENEWED', tostring(renew_count), lease_now[1], lease_now[2]}
"""


# Expiration claims only an elapsed ``expiring`` generation and assigns a cleanup token;
# concurrent readers and deleters observe the same fenced state.
_BEGIN_EXPIRATION_SCRIPT = r"""
local control = KEYS[1]
local delete_lease = KEYS[2]
local expected_generation = tonumber(ARGV[1])
local delete_owner = ARGV[2]
local delete_lease_ms = ARGV[3]

local generation = tonumber(redis.call('HGET', control, 'generation') or '0')
if generation ~= expected_generation then
    return {'RETRY'}
end
local state = redis.call('HGET', control, 'state')
if state == 'expired' then
    return {'DONE'}
end
if state ~= 'expiring' then
    return {'RETRY'}
end
local current_owner = redis.call('GET', delete_lease)
if current_owner == delete_owner then
    redis.call('PEXPIRE', delete_lease, delete_lease_ms)
    return {'OWNED'}
end
if current_owner then
    return {'WAIT'}
end
redis.call('SET', delete_lease, delete_owner, 'PX', delete_lease_ms)
return {'OWNED'}
"""


# Explicit deletion and retention expiration share batched physical cleanup but retain
# distinct terminal states (``deleted`` versus ``expired``).
_BEGIN_DELETE_SCRIPT = (
    _NOTIFICATION_PREAMBLE
    + r"""
local control = KEYS[1]
local meta = KEYS[2]
local delete_lease = KEYS[3]
local active_lease = KEYS[4]
local expected_generation = tonumber(ARGV[1])
local delete_owner = ARGV[2]
local delete_lease_ms = ARGV[3]
local expected_active_lease = ARGV[4]

local function write_signal()
    redis.call('HINCRBY', control, 'signal_seq', 1)
    write_notification(control, redis.call('HGET', control, 'generation'),
        '0', redis.call('HGET', control, 'signal_seq'))
end

local state = redis.call('HGET', control, 'state')
if not state then
    if expected_generation ~= 1 then
        return {'RETRY'}
    end
    return {'DONE'}
end

local generation = tonumber(redis.call('HGET', control, 'generation') or '0')
if generation ~= expected_generation then
    return {'RETRY'}
end
if state == 'deleted' or state == 'expired' then
    return {'DONE'}
end
if state == 'expiring' then
    return {'WAIT'}
end
if state == 'deleting' then
    local current_owner = redis.call('GET', delete_lease)
    if current_owner == delete_owner then
        redis.call('PEXPIRE', delete_lease, delete_lease_ms)
        return {'OWNED'}
    end
    if current_owner then
        return {'WAIT'}
    end
    redis.call('SET', delete_lease, delete_owner, 'PX', delete_lease_ms)
    return {'OWNED'}
end
if state ~= 'active' then
    return {'INVALID_CONTROL_STATE', state}
end

local stored_active_lease = redis.call('HGET', meta, 'active_lease') or ''
if stored_active_lease ~= expected_active_lease then
    return {'RETRY'}
end
if stored_active_lease ~= '' and redis.call('EXISTS', active_lease) == 1 then
    return {'ACTIVE', redis.call('HGET', meta, 'active_run') or ''}
end
redis.call('HSET', control, 'state', 'deleting')
redis.call('SET', delete_lease, delete_owner, 'PX', delete_lease_ms)
write_signal()
return {'OWNED'}
"""
)


# Batched cleanup is token-fenced and bounded so Redis is never blocked by an unbounded
# key deletion transaction.
_DELETE_BATCH_SCRIPT = r"""
local control = KEYS[1]
local delete_lease = KEYS[2]
local key_index = KEYS[3]
local generation = ARGV[1]
local delete_owner = ARGV[2]
local delete_lease_ms = ARGV[3]
local working_state = ARGV[4]

if redis.call('HGET', control, 'state') ~= working_state or redis.call('HGET', control, 'generation') ~= generation then
    return {'RETRY'}
end
if redis.call('GET', delete_lease) ~= delete_owner then
    return {'LEASE_LOST'}
end
redis.call('PEXPIRE', delete_lease, delete_lease_ms)
for index = 4, #KEYS do
    redis.call('UNLINK', KEYS[index])
    redis.call('SREM', key_index, KEYS[index])
end
return {'OK', tostring(redis.call('SCARD', key_index))}
"""


# Finalization removes generation data while retaining the control tombstone and next
# generation number; stale cursors cannot silently read a recreated stream.
_FINALIZE_DELETE_SCRIPT = r"""
local control = KEYS[1]
local delete_lease = KEYS[2]
local key_index = KEYS[3]
local generation = ARGV[1]
local delete_owner = ARGV[2]
local working_state = ARGV[3]
local final_state = ARGV[4]

if redis.call('HGET', control, 'state') ~= working_state or redis.call('HGET', control, 'generation') ~= generation then
    return {'RETRY'}
end
if redis.call('GET', delete_lease) ~= delete_owner then
    return {'LEASE_LOST'}
end
if redis.call('SCARD', key_index) ~= 0 then
    return {'MORE'}
end
redis.call('UNLINK', key_index)
local retained_bytes = tonumber(redis.call('HGET', control, 'retained_bytes'))
local retained_records = tonumber(redis.call('HGET', control, 'retained_records'))
redis.call('HINCRBY', KEYS[5], 'total_bytes', -retained_bytes)
redis.call('HINCRBY', KEYS[5], 'total_records', 1 - retained_records)
redis.call('HDEL', control, 'retained_bytes', 'retained_records')
redis.call('ZREM', KEYS[6], control)
redis.call('SET', KEYS[4], final_state)
redis.call('HSET', control, 'state', final_state)
redis.call('HDEL', control, 'retention_deadline_ms')
redis.call('DEL', delete_lease)
return {'DONE'}
"""


# Admission discovers a bounded page of elapsed terminal controls using server time.
# Members remain until fenced finalization, so cancellation cannot lose cleanup work.
_DUE_EXPIRATIONS_SCRIPT = r"""
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
return redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', tostring(now_ms), 'LIMIT', '0', '16')
"""
