"""Lua scripts for atomic Redis operations.

Shipped as module-level string constants and registered through
``redis.asyncio.Redis.register_script()`` from the consuming service. Keeping
them here separates the multi-line Lua source from regular Python code so it
stays diffable and reviewable in isolation.
"""

from __future__ import annotations

# UPDATE_ORDER_LUA — atomic CAS-aware order patch with status-index swap.
#
# Why Lua: the order hash, an optional CAS check on `status`, and the
# orders:by_status:{old} / orders:by_status:{new} index update must all
# happen in one indivisible step so a concurrent reader never sees a
# half-applied transition (e.g. HGET says "open" but the order is missing
# from orders:by_status:open).
#
# KEYS:
#   [1] order:{order_id}
#
# ARGV:
#   [1] order_id (used as member in orders:by_status:* index swap)
#   [2] '1' to perform CAS check on status, '0' to skip
#   [3] expected old_status (only consulted when ARGV[2] == '1')
#   [4] new_status — the *target* status when the patch changes status,
#       or empty string when the patch leaves status alone
#   [5..N] alternating field, value pairs to HSET on the order hash
#          (must be even count; may be empty)
#
# Returns:
#   1 on success (patch applied + index swap if applicable)
#   0 when the order doesn't exist OR the CAS check failed
#
# Cluster note: this script accesses keys with two different prefixes
# (``order:*`` and ``orders:by_status:*``). Redis Cluster requires all
# touched keys to be declared in KEYS and to hash to the same slot. The
# project runs single-instance Redis (D-006), so we skip the hash-tag
# dance — revisit if Cluster is ever introduced.
UPDATE_ORDER_LUA = """
local order_key = KEYS[1]
local order_id = ARGV[1]
local check_cas = ARGV[2]
local expected = ARGV[3]
local new_status = ARGV[4]

if redis.call('EXISTS', order_key) == 0 then
  return 0
end

local cur_status = redis.call('HGET', order_key, 'status')

if check_cas == '1' then
  if cur_status ~= expected then
    return 0
  end
end

local i = 5
while i < #ARGV do
  redis.call('HSET', order_key, ARGV[i], ARGV[i + 1])
  i = i + 2
end

if new_status ~= '' and cur_status ~= new_status then
  if cur_status and cur_status ~= '' then
    redis.call('SREM', 'orders:by_status:' .. cur_status, order_id)
  end
  redis.call('SADD', 'orders:by_status:' .. new_status, order_id)
end

return 1
"""


# CASCADE_LOCK_LUA — atomic single-trigger guard for cascade close orchestrator.
#
# Step 4.8 — the 5 cascade-close trigger paths (A/B/C/D/E) can fire on the
# same order_id near-simultaneously (operator clicks UI Close while an FTMO
# SL hit fires). The cascade orchestrator must produce ONE outbound cmd per
# leg per order regardless. This script claims a per-order lock atomically;
# losers see EXISTS==1 and abort idempotently.
#
# KEYS:
#   [1] cascade_lock:{order_id}
#
# ARGV:
#   [1] trigger_path tag — "A" | "B" | "C" | "D" | "E" | "cancel_late_fill".
#       Stored as the lock value so a post-mortem `GET cascade_lock:{id}`
#       reveals which path actually advanced.
#   [2] ttl_seconds — 30s in production; auto-releases on
#       HedgeService crash. Caller still releases explicitly on terminal
#       transition (closed | close_failed) so a re-trigger on the same
#       order doesn't have to wait the TTL.
#
# Returns:
#   1 — acquired (caller proceeds).
#   0 — already locked (caller aborts).
CASCADE_LOCK_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
return 1
"""
