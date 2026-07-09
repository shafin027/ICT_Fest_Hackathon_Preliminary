# Bug Report — CoWork API

## Bug 1: Access token lifetime is 900× too long (Hard)

- **File**: `app/auth.py`, line 50
- **Bug**: `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)` multiplies 15 minutes by 60, yielding 900 minutes (54,000 seconds) instead of the required 900 seconds (15 minutes).
- **Rule violated**: Rule 8 — "Access tokens expire in exactly 900 seconds"
- **Fix**: Changed to `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` (15 min = 900 sec).

## Bug 2: Token revocation checks user ID instead of token JTI (Hard)

- **File**: `app/auth.py`, line 97
- **Bug**: `payload.get("sub") in _revoked_tokens` checks the user's ID (`sub` claim) against the revoked set, but `revoke_access_token` stores the token's `jti`. This means the check always fails (wrong key type), so revoked tokens are never actually rejected.
- **Rule violated**: Rule 8 — "Logout immediately invalidates the presented access token"
- **Fix**: Changed to `payload.get("jti") in _revoked_tokens`.

## Bug 3: Refresh tokens are not single-use (Hard)

- **File**: `app/routers/auth.py`, lines 82–93; `app/auth.py`
- **Bug**: The refresh endpoint decodes and uses the refresh token but never records its JTI as consumed. The same refresh token can be reused indefinitely.
- **Rule violated**: Rule 8 — "Refresh tokens are single-use; reuse → 401"
- **Fix**: Added `_revoked_refresh` set in `auth.py` with `revoke_refresh_token()` and `is_refresh_revoked()`. The refresh endpoint now checks for prior use and revokes the token's JTI before issuing new tokens.

## Bug 4: Deadlock in notifications (ABBA lock ordering) (Hard)

- **File**: `app/services/notifications.py`, lines 24–35
- **Bug**: `notify_created` acquires `_email_lock` then `_audit_lock`; `notify_cancelled` acquires `_audit_lock` then `_email_lock`. If a create and cancel run concurrently, they can deadlock (thread A holds email, waits for audit; thread B holds audit, waits for email).
- **Rule violated**: Rule 16 — "no combination of concurrent valid requests may hang the service"
- **Fix**: Removed lock nesting. Both functions now acquire locks sequentially (email first, then audit), never nested.

## Bug 5: Reference code race condition (Hard)

- **File**: `app/services/reference.py`, lines 17–21
- **Bug**: `next_reference_code()` reads the counter, sleeps 0.12s, then increments. Under concurrent requests, multiple threads read the same value before any increment → duplicate reference codes.
- **Rule violated**: Rule 7 — "Every booking's reference code is unique, including under concurrent creation"
- **Fix**: Wrapped the entire read-increment-return sequence in a `threading.Lock`.

## Bug 6: Stats service race condition (Hard)

- **File**: `app/services/stats.py`, lines 15–26
- **Bug**: `record_create` and `record_cancel` read the current dict, sleep 0.1s, then write a new dict. Concurrent calls read stale values and overwrite each other's updates (classic lost-update race).
- **Rule violated**: Rule 14 — "always consistent, including after bursts of concurrent activity"
- **Fix**: Protected all `record_create`, `record_cancel`, and `get` with a `threading.Lock`.

## Bug 7: Rate limiter race condition (Hard)

- **File**: `app/services/ratelimit.py`, lines 18–26
- **Bug**: The bucket is read, trimmed, slept (0.1s), appended, then stored — all without any lock. Concurrent requests can read the same bucket size and both pass the limit check.
- **Rule violated**: Rule 5 — "Must hold under concurrent requests"
- **Fix**: Wrapped the entire trim-sleep-append-check sequence in a `threading.Lock`.

## Bug 8: UTC offset not converted (Medium)

- **File**: `app/timeutils.py`, lines 11–13
- **Bug**: `dt.replace(tzinfo=None)` strips the timezone metadata without actually converting the time value. An input like `2025-01-01T10:00:00+05:30` is stored as `2025-01-01T10:00:00` instead of `2025-01-01T04:30:00`.
- **Rule violated**: Rule 1 — "Input datetimes carrying a UTC offset must be converted to UTC"
- **Fix**: Changed to `dt.astimezone(timezone.utc).replace(tzinfo=None)`.

## Bug 9: Start time validation has 5-minute grace window (Medium)

- **File**: `app/routers/bookings.py`, line 86
- **Bug**: `start <= now - timedelta(seconds=300)` allows booking start times up to 5 minutes in the past.
- **Rule violated**: Rule 2 — "start_time must be strictly in the future at request time — no grace window"
- **Fix**: Changed to `start <= now`.

## Bug 10: Missing duration minimum and end>start validation (Medium)

- **File**: `app/routers/bookings.py`, lines 89–94
- **Bug**: No check for `end <= start` or `duration_hours < 1`. Zero-duration or negative-duration bookings could be created.
- **Rule violated**: Rule 2 — "minimum 1 hour, end_time must be strictly after start_time"
- **Fix**: Added `if end <= start` check and `if duration_hours < MIN_DURATION_HOURS` check.

## Bug 11: Cancellation refund tiers are wrong (Medium)

- **File**: `app/routers/bookings.py`, lines 200–206
- **Bug**: Three issues: (a) `int(notice.total_seconds() // 3600)` truncates fractional hours; (b) `notice_hours > 48` should be `>=`; (c) the `< 24h` case returns 50% instead of 0%.
- **Rule violated**: Rule 6 — "notice >= 48h -> 100%; 24h <= notice < 48h -> 50%; notice < 24h -> 0%"
- **Fix**: Compare timedelta directly: `notice >= timedelta(hours=48)` -> 100%, `notice >= timedelta(hours=24)` -> 50%, else 0%.

## Bug 12: get_booking overwrites start_time with created_at (Medium)

- **File**: `app/routers/bookings.py`, line 166
- **Bug**: `response["start_time"] = iso_utc(booking.created_at)` replaces the correct start_time with the booking's creation timestamp.
- **Rule violated**: API contract — booking response must contain actual start_time
- **Fix**: Removed the line.

## Bug 13: get_booking doesn't enforce member visibility (Medium)

- **File**: `app/routers/bookings.py`, lines 150–175
- **Bug**: The endpoint checks org-level access via Room join but doesn't restrict members to their own bookings. Any member can view any booking in the org.
- **Rule violated**: Rule 10 — "Members may read only their own bookings"
- **Fix**: Added `if user.role != "admin" and booking.user_id != user.id: raise 404`.

## Bug 14: Overlap check uses <= instead of < (Medium)

- **File**: `app/routers/bookings.py`, line 50
- **Bug**: `b.start_time <= end and start <= b.end_time` rejects back-to-back bookings (e.g., 10:00–11:00 and 11:00–12:00) because `<=` treats touching boundaries as overlap.
- **Rule violated**: Rule 3 — "existing.start < new.end AND new.start < existing.end. Back-to-back bookings are allowed"
- **Fix**: Changed to strict `<`: `b.start_time < end and start < b.end_time`.

## Bug 15: Registration returns existing user instead of 409 (Easy)

- **File**: `app/routers/auth.py`, lines 37–43
- **Bug**: When a username already exists in the org, the code returns the existing user's data with HTTP 200 instead of raising a 409 error.
- **Rule violated**: Rule 15 — "A duplicate username within the org -> 409 USERNAME_TAKEN"
- **Fix**: Changed to `raise AppError(409, "USERNAME_TAKEN", "Username already taken")`.

## Bug 16: list_bookings — wrong sort, wrong offset, hardcoded limit (Easy)

- **File**: `app/routers/bookings.py`, lines 137–139
- **Bug**: Three bugs in one query: (a) sort is `desc()` instead of `asc()`; (b) offset is `page * limit` instead of `(page - 1) * limit` (page 1 skips all results); (c) `.limit(10)` is hardcoded instead of using the `limit` parameter.
- **Rule violated**: Rule 11 — "sorted ascending by start_time. Sequential pages never skip or repeat items"
- **Fix**: Changed to `.order_by(Booking.start_time.asc(), Booking.id.asc()).offset((page - 1) * limit).limit(limit)`.

## Bug 17: Refund amount uses int() truncation instead of rounding (Easy)

- **File**: `app/services/refunds.py`, lines 15–17
- **Bug**: Converts price to dollars and back, then truncates with `int()`. E.g., 50% of 151 cents = 75.5 -> `int(0.755 * 100)` = 75 instead of 76 (half-up).
- **Rule violated**: Rule 6 — "Refund amount rounds to the nearest cent, half-cents rounding up"
- **Fix**: Changed to `math.floor(booking.price_cents * percent / 100 + 0.5)` for half-up rounding.

## Bug 18: Export filters by admin's own user_id (Easy)

- **File**: `app/services/export.py`, lines 53–54
- **Bug**: When `include_all=False`, `_fetch_scoped` is called with the admin's `user_id`, filtering to only their own bookings instead of all org bookings.
- **Rule violated**: API contract — admin export should include all bookings in the org
- **Fix**: Pass `None` for `user_id` in the non-`include_all` branch.

## Bug 19: Stale caches prevent immediate consistency (Easy)

- **Files**: `app/routers/rooms.py` (lines 69–71, 99), `app/routers/admin.py` (lines 25–27, 61)
- **Bug**: Availability cache is not invalidated on booking cancellation. Usage report cache is not invalidated on new bookings. Stale cached results are served.
- **Rule violated**: Rules 12, 13 — "Must reflect the current state immediately"
- **Fix**: Disabled caching entirely for both endpoints. The queries are simple enough that caching is unnecessary and correctness is paramount.
