"""Comprehensive edge-case and concurrency test suite for CoWork API.

Tests domain rules, edge cases, error codes, and thread-safety invariants.
"""
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.database import SessionLocal
from app.models import Booking, RefundLog, Room, User, Organization

client = TestClient(app)


def _future(hours: int, tz_offset: int = 0) -> str:
    """Helper to return an ISO 8601 string in the future, optionally with a timezone offset."""
    tz = timezone(timedelta(hours=tz_offset)) if tz_offset != 0 else timezone.utc
    dt = datetime.now(tz) + timedelta(hours=hours)
    # Clear minutes/seconds to keep it clean
    dt = dt.replace(minute=0, second=0, microsecond=0)
    return dt.isoformat()


def test_auth_token_edge_cases():
    """Test token lifetime, revocation, refresh token single-use, and logout."""
    org_name = f"org-auth-{time.time()}"
    
    # 1. Register admin
    r = client.post("/auth/register", json={"org_name": org_name, "username": "admin", "password": "password"})
    assert r.status_code == 201
    
    # 2. Login
    r = client.post("/auth/login", json={"org_name": org_name, "username": "admin", "password": "password"})
    assert r.status_code == 200
    tokens = r.json()
    access = tokens["access_token"]
    refresh = tokens["refresh_token"]
    
    # 3. Refresh token (first use should work)
    r = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200
    new_tokens = r.json()
    new_access = new_tokens["access_token"]
    new_refresh = new_tokens["refresh_token"]
    
    # 4. Refresh token single-use (reuse of first refresh token should fail with 401)
    r = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 401
    
    # 5. Access token logout / revocation
    # Access with current access token
    r = client.get("/rooms", headers={"Authorization": f"Bearer {new_access}"})
    assert r.status_code == 200
    
    # Logout
    r = client.post("/auth/logout", headers={"Authorization": f"Bearer {new_access}"})
    assert r.status_code == 200
    
    # Access with revoked access token must fail
    r = client.get("/rooms", headers={"Authorization": f"Bearer {new_access}"})
    assert r.status_code == 401


def test_registration_conflict():
    """Test duplicate registration returns 409 USERNAME_TAKEN."""
    org_name = f"org-reg-{time.time()}"
    
    # Register first user (admin)
    r = client.post("/auth/register", json={"org_name": org_name, "username": "user1", "password": "password"})
    assert r.status_code == 201
    
    # Register second user with different username in same org (member)
    r = client.post("/auth/register", json={"org_name": org_name, "username": "user2", "password": "password"})
    assert r.status_code == 201
    assert r.json()["role"] == "member"
    
    # Register duplicate username in same org -> 409 USERNAME_TAKEN
    r = client.post("/auth/register", json={"org_name": org_name, "username": "user1", "password": "password"})
    assert r.status_code == 409
    assert r.json()["code"] == "USERNAME_TAKEN"


def test_timezone_normalization():
    """Test offset-aware input datetimes are converted to UTC before storage/display."""
    org_name = f"org-tz-{time.time()}"
    client.post("/auth/register", json={"org_name": org_name, "username": "admin", "password": "password"})
    tokens = client.post("/auth/login", json={"org_name": org_name, "username": "admin", "password": "password"}).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    
    room = client.post("/rooms", json={"name": "Room TZ", "capacity": 5, "hourly_rate_cents": 1000}, headers=headers).json()
    
    # Booking with +05:00 offset start and +05:00 offset end
    # E.g. start at 2030-06-01T15:00:00+05:00. In UTC, this is 10:00:00.
    start_str = "2030-06-01T15:00:00+05:00"
    end_str = "2030-06-01T17:00:00+05:00"
    
    r = client.post("/bookings", json={"room_id": room["id"], "start_time": start_str, "end_time": end_str}, headers=headers)
    assert r.status_code == 201
    b = r.json()
    
    # Check return fields are in UTC format
    assert b["start_time"] == "2030-06-01T10:00:00+00:00"
    assert b["end_time"] == "2030-06-01T12:00:00+00:00"


def test_booking_validation_edge_cases():
    """Test duration checks, past start times, zero/negative durations, pricing cents."""
    org_name = f"org-val-{time.time()}"
    client.post("/auth/register", json={"org_name": org_name, "username": "admin", "password": "password"})
    tokens = client.post("/auth/login", json={"org_name": org_name, "username": "admin", "password": "password"}).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    
    room = client.post("/rooms", json={"name": "Val Room", "capacity": 5, "hourly_rate_cents": 1000}, headers=headers).json()
    room_id = room["id"]
    
    # 1. Past start_time -> 400
    r = client.post("/bookings", json={"room_id": room_id, "start_time": _future(-2), "end_time": _future(1)}, headers=headers)
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"
    
    # 2. end_time <= start_time -> 400
    r = client.post("/bookings", json={"room_id": room_id, "start_time": _future(5), "end_time": _future(4)}, headers=headers)
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"
    
    # 3. Non-whole hours -> 400
    # E.g. 1 hour 30 mins
    start = _future(5)
    end = (datetime.fromisoformat(start) + timedelta(hours=1, minutes=30)).isoformat()
    r = client.post("/bookings", json={"room_id": room_id, "start_time": start, "end_time": end}, headers=headers)
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"
    
    # 4. Duration > 8 hours -> 400
    r = client.post("/bookings", json={"room_id": room_id, "start_time": _future(5), "end_time": _future(14)}, headers=headers)
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"
    
    # 5. Pricing accuracy: rate * duration
    r = client.post("/bookings", json={"room_id": room_id, "start_time": _future(5), "end_time": _future(7)}, headers=headers)
    assert r.status_code == 201
    assert r.json()["price_cents"] == 2000  # 1000 * 2 hours


def test_booking_overlap_back_to_back():
    """Test overlapping bookings are rejected, but back-to-back bookings are allowed."""
    org_name = f"org-overlap-{time.time()}"
    client.post("/auth/register", json={"org_name": org_name, "username": "admin", "password": "password"})
    tokens = client.post("/auth/login", json={"org_name": org_name, "username": "admin", "password": "password"}).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    
    room = client.post("/rooms", json={"name": "Overlap Room", "capacity": 5, "hourly_rate_cents": 1000}, headers=headers).json()
    room_id = room["id"]
    
    # Create booking for hours [10, 12]
    start = _future(10)
    end = _future(12)
    r = client.post("/bookings", json={"room_id": room_id, "start_time": start, "end_time": end}, headers=headers)
    assert r.status_code == 201
    
    # 1. Back-to-back immediately after (hours [12, 14]) -> Allowed
    r = client.post("/bookings", json={"room_id": room_id, "start_time": _future(12), "end_time": _future(14)}, headers=headers)
    assert r.status_code == 201
    
    # 2. Back-to-back immediately before (hours [8, 10]) -> Allowed
    r = client.post("/bookings", json={"room_id": room_id, "start_time": _future(8), "end_time": _future(10)}, headers=headers)
    assert r.status_code == 201
    
    # 3. Direct overlap (hours [11, 13]) -> 409 ROOM_CONFLICT
    r = client.post("/bookings", json={"room_id": room_id, "start_time": _future(11), "end_time": _future(13)}, headers=headers)
    assert r.status_code == 409
    assert r.json()["code"] == "ROOM_CONFLICT"
    
    # 4. Nested overlap (hours [10, 11]) -> 409 ROOM_CONFLICT
    r = client.post("/bookings", json={"room_id": room_id, "start_time": _future(10), "end_time": _future(11)}, headers=headers)
    assert r.status_code == 409
    assert r.json()["code"] == "ROOM_CONFLICT"


def test_booking_quota_limit():
    """Test rolling 24-hour quota (max 3 bookings starting within [now, now+24h])."""
    org_name = f"org-quota-{time.time()}"
    client.post("/auth/register", json={"org_name": org_name, "username": "admin", "password": "password"})
    
    # Register member
    client.post("/auth/register", json={"org_name": org_name, "username": "member1", "password": "password"})
    tokens = client.post("/auth/login", json={"org_name": org_name, "username": "member1", "password": "password"}).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    
    # Admin creates rooms
    adm_tokens = client.post("/auth/login", json={"org_name": org_name, "username": "admin", "password": "password"}).json()
    adm_headers = {"Authorization": f"Bearer {adm_tokens['access_token']}"}
    room1 = client.post("/rooms", json={"name": "Room 1", "capacity": 5, "hourly_rate_cents": 1000}, headers=adm_headers).json()
    
    # Create 3 valid bookings in 24h window
    r = client.post("/bookings", json={"room_id": room1["id"], "start_time": _future(2), "end_time": _future(3)}, headers=headers)
    assert r.status_code == 201
    
    r = client.post("/bookings", json={"room_id": room1["id"], "start_time": _future(4), "end_time": _future(5)}, headers=headers)
    assert r.status_code == 201
    
    r = client.post("/bookings", json={"room_id": room1["id"], "start_time": _future(6), "end_time": _future(7)}, headers=headers)
    assert r.status_code == 201
    
    # 4th booking in 24h window -> 409 QUOTA_EXCEEDED
    r = client.post("/bookings", json={"room_id": room1["id"], "start_time": _future(8), "end_time": _future(9)}, headers=headers)
    assert r.status_code == 409
    assert r.json()["code"] == "QUOTA_EXCEEDED"
    
    # Booking outside 24h window (e.g. 26 hours in future) -> Allowed (doesn't count towards the rolling 24h limit)
    r = client.post("/bookings", json={"room_id": room1["id"], "start_time": _future(26), "end_time": _future(27)}, headers=headers)
    assert r.status_code == 201


def test_rate_limiting():
    """Test user booking rate limiting (max 20 requests per rolling 60 seconds)."""
    org_name = f"org-rate-{time.time()}"
    client.post("/auth/register", json={"org_name": org_name, "username": "admin", "password": "password"})
    tokens = client.post("/auth/login", json={"org_name": org_name, "username": "admin", "password": "password"}).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    
    room = client.post("/rooms", json={"name": "Rate Room", "capacity": 5, "hourly_rate_cents": 1000}, headers=headers).json()
    room_id = room["id"]
    
    # Make 20 rapid requests (even invalid ones count towards rate limiting)
    for i in range(20):
        # We can just send requests. Some can succeed, some fail, but they all count.
        # To avoid making many db entries or conflicts, we can send invalid requests (e.g. duration too long).
        r = client.post("/bookings", json={"room_id": room_id, "start_time": _future(5), "end_time": _future(20)}, headers=headers)
        # Verify it wasn't rate limited
        assert r.status_code == 400
    
    # The 21st request must be rate limited with 429
    r = client.post("/bookings", json={"room_id": room_id, "start_time": _future(5), "end_time": _future(6)}, headers=headers)
    assert r.status_code == 429
    assert r.json()["code"] == "RATE_LIMITED"


def test_cancellation_refunds():
    """Test refund tiers, rounding half-up, and cancel access checks."""
    org_name = f"org-cancel-{time.time()}"
    
    # Register Admin & Member
    client.post("/auth/register", json={"org_name": org_name, "username": "admin", "password": "password"})
    client.post("/auth/register", json={"org_name": org_name, "username": "member1", "password": "password"})
    client.post("/auth/register", json={"org_name": org_name, "username": "member2", "password": "password"})
    
    tokens_adm = client.post("/auth/login", json={"org_name": org_name, "username": "admin", "password": "password"}).json()
    tokens_m1 = client.post("/auth/login", json={"org_name": org_name, "username": "member1", "password": "password"}).json()
    tokens_m2 = client.post("/auth/login", json={"org_name": org_name, "username": "member2", "password": "password"}).json()
    
    headers_adm = {"Authorization": f"Bearer {tokens_adm['access_token']}"}
    headers_m1 = {"Authorization": f"Bearer {tokens_m1['access_token']}"}
    headers_m2 = {"Authorization": f"Bearer {tokens_m2['access_token']}"}
    
    room = client.post("/rooms", json={"name": "Cancel Room", "capacity": 5, "hourly_rate_cents": 151}, headers=headers_adm).json()
    room_id = room["id"]
    
    # 1. Notice >= 48 hours -> 100% refund
    b1 = client.post("/bookings", json={"room_id": room_id, "start_time": _future(50), "end_time": _future(51)}, headers=headers_m1).json()
    r = client.post(f"/bookings/{b1['id']}/cancel", headers=headers_m1)
    assert r.status_code == 200
    assert r.json()["refund_percent"] == 100
    assert r.json()["refund_amount_cents"] == 151
    
    # Already cancelled -> 409
    r = client.post(f"/bookings/{b1['id']}/cancel", headers=headers_m1)
    assert r.status_code == 409
    assert r.json()["code"] == "ALREADY_CANCELLED"
    
    # 2. 24 <= Notice < 48 hours -> 50% refund (with half-cent rounding check)
    # price_cents = 151. 50% refund = 75.5 -> half-up rounds to 76 cents.
    b2 = client.post("/bookings", json={"room_id": room_id, "start_time": _future(30), "end_time": _future(31)}, headers=headers_m1).json()
    r = client.post(f"/bookings/{b2['id']}/cancel", headers=headers_m1)
    assert r.status_code == 200
    assert r.json()["refund_percent"] == 50
    assert r.json()["refund_amount_cents"] == 76
    
    # 3. Notice < 24 hours -> 0% refund
    b3 = client.post("/bookings", json={"room_id": room_id, "start_time": _future(5), "end_time": _future(6)}, headers=headers_m1).json()
    r = client.post(f"/bookings/{b3['id']}/cancel", headers=headers_m1)
    assert r.status_code == 200
    assert r.json()["refund_percent"] == 0
    assert r.json()["refund_amount_cents"] == 0
    
    # 4. Member 2 trying to cancel Member 1's booking -> 404
    b4 = client.post("/bookings", json={"room_id": room_id, "start_time": _future(50), "end_time": _future(51)}, headers=headers_m1).json()
    r = client.post(f"/bookings/{b4['id']}/cancel", headers=headers_m2)
    assert r.status_code == 404
    
    # 5. Admin can cancel any booking in the org -> Allowed
    r = client.post(f"/bookings/{b4['id']}/cancel", headers=headers_adm)
    assert r.status_code == 200


def test_multi_tenancy_and_visibility():
    """Test cross-org isolation and member-visibility rules."""
    org1 = f"org1-{time.time()}"
    org2 = f"org2-{time.time()}"
    
    client.post("/auth/register", json={"org_name": org1, "username": "admin", "password": "password"})
    client.post("/auth/register", json={"org_name": org1, "username": "member", "password": "password"})
    tokens_m1 = client.post("/auth/login", json={"org_name": org1, "username": "member", "password": "password"}).json()
    tokens_a1 = client.post("/auth/login", json={"org_name": org1, "username": "admin", "password": "password"}).json()
    
    client.post("/auth/register", json={"org_name": org2, "username": "admin", "password": "password"})
    tokens_a2 = client.post("/auth/login", json={"org_name": org2, "username": "admin", "password": "password"}).json()
    
    headers_m1 = {"Authorization": f"Bearer {tokens_m1['access_token']}"}
    headers_a1 = {"Authorization": f"Bearer {tokens_a1['access_token']}"}
    headers_a2 = {"Authorization": f"Bearer {tokens_a2['access_token']}"}
    
    room_org1 = client.post("/rooms", json={"name": "Room O1", "capacity": 5, "hourly_rate_cents": 1000}, headers=headers_a1).json()
    
    # 1. Member 1 books room in Org 1
    booking = client.post("/bookings", json={"room_id": room_org1["id"], "start_time": _future(10), "end_time": _future(11)}, headers=headers_m1).json()
    
    # 2. Admin 2 (from Org 2) tries to get Room from Org 1 -> 404
    r = client.get(f"/rooms/{room_org1['id']}/stats", headers=headers_a2)
    assert r.status_code == 404
    
    # 3. Admin 2 tries to get Booking from Org 1 -> 404
    r = client.get(f"/bookings/{booking['id']}", headers=headers_a2)
    assert r.status_code == 404


def test_pagination_and_sorting():
    """Test pagination limit, offset, and sorting ascending by start_time."""
    org_name = f"org-pag-{time.time()}"
    client.post("/auth/register", json={"org_name": org_name, "username": "admin", "password": "password"})
    tokens = client.post("/auth/login", json={"org_name": org_name, "username": "admin", "password": "password"}).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    
    room = client.post("/rooms", json={"name": "Pag Room", "capacity": 5, "hourly_rate_cents": 1000}, headers=headers).json()
    room_id = room["id"]
    
    # Create bookings out of order chronologically
    t5 = _future(5)
    t10 = _future(10)
    t30 = _future(30)
    t35 = _future(35)
    
    for h in [10, 5, 30, 35]:
        client.post("/bookings", json={"room_id": room_id, "start_time": _future(h), "end_time": _future(h+1)}, headers=headers)
        
    # Get bookings with limit=2, page=1
    r = client.get("/bookings?page=1&limit=2", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["page"] == 1
    assert data["limit"] == 2
    assert data["total"] == 4
    
    # Verify sorted ascending by start_time
    items = data["items"]
    assert len(items) == 2
    assert items[0]["start_time"] == t5
    assert items[1]["start_time"] == t10
    
    # Get page 2
    r = client.get("/bookings?page=2&limit=2", headers=headers)
    data = r.json()
    items = data["items"]
    assert len(items) == 2
    assert items[0]["start_time"] == t30
    assert items[1]["start_time"] == t35


def test_concurrency_double_booking():
    """Concurrently booking the same slot; only one must succeed."""
    org_name = f"org-conc-book-{time.time()}"
    client.post("/auth/register", json={"org_name": org_name, "username": "admin", "password": "password"})
    
    # Register 4 members to book concurrently
    members = []
    for i in range(4):
        username = f"member-{i}"
        client.post("/auth/register", json={"org_name": org_name, "username": username, "password": "password"})
        tokens = client.post("/auth/login", json={"org_name": org_name, "username": username, "password": "password"}).json()
        members.append({"username": username, "token": tokens["access_token"]})
        
    adm_tokens = client.post("/auth/login", json={"org_name": org_name, "username": "admin", "password": "password"}).json()
    adm_headers = {"Authorization": f"Bearer {adm_tokens['access_token']}"}
    room = client.post("/rooms", json={"name": "Conc Room", "capacity": 5, "hourly_rate_cents": 1000}, headers=adm_headers).json()
    room_id = room["id"]
    
    start_time = _future(12)
    end_time = _future(14)
    
    results = []
    def make_booking(member_info):
        c = TestClient(app)
        try:
            r = c.post(
                "/bookings", 
                json={"room_id": room_id, "start_time": start_time, "end_time": end_time},
                headers={"Authorization": f"Bearer {member_info['token']}"}
            )
            results.append((r.status_code, r.json()))
        except Exception as e:
            results.append((999, str(e)))
            
    threads = [threading.Thread(target=make_booking, args=(m,)) for m in members]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    success = [r for r in results if r[0] == 201]
    conflict = [r for r in results if r[0] == 409]
    
    assert len(success) == 1, f"Expected exactly 1 success, got {len(success)}: {results}"
    assert len(conflict) == 3, f"Expected exactly 3 conflicts, got {len(conflict)}: {results}"


def test_concurrency_cancellation():
    """Concurrently cancelling the same booking; only one must write a refund log."""
    org_name = f"org-conc-cancel-{time.time()}"
    client.post("/auth/register", json={"org_name": org_name, "username": "admin", "password": "password"})
    tokens = client.post("/auth/login", json={"org_name": org_name, "username": "admin", "password": "password"}).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    
    room = client.post("/rooms", json={"name": "Room", "capacity": 5, "hourly_rate_cents": 1000}, headers=headers).json()
    booking = client.post("/bookings", json={"room_id": room["id"], "start_time": _future(50), "end_time": _future(51)}, headers=headers).json()
    
    results = []
    def make_cancel():
        c = TestClient(app)
        try:
            r = c.post(f"/bookings/{booking['id']}/cancel", headers={"Authorization": f"Bearer {tokens['access_token']}"})
            results.append((r.status_code, r.json()))
        except Exception as e:
            results.append((999, str(e)))
            
    threads = [threading.Thread(target=make_cancel) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    success = [r for r in results if r[0] == 200]
    conflict = [r for r in results if r[0] == 409]
    
    assert len(success) == 1, f"Expected 1 success, got {len(success)}: {results}"
    assert len(conflict) == 3, f"Expected 3 conflicts, got {len(conflict)}: {results}"
    
    db = SessionLocal()
    refund_logs = db.query(RefundLog).filter(RefundLog.booking_id == booking["id"]).all()
    assert len(refund_logs) == 1
    db.close()


def test_admin_export_cross_org():
    """Test that /admin/export fails with 404 when querying a room belonging to another organization."""
    org1 = f"org-exp-1-{time.time()}"
    org2 = f"org-exp-2-{time.time()}"
    
    # Org 1: Admin & Room
    client.post("/auth/register", json={"org_name": org1, "username": "admin", "password": "password"})
    t_a1 = client.post("/auth/login", json={"org_name": org1, "username": "admin", "password": "password"}).json()["access_token"]
    room_o1 = client.post("/rooms", json={"name": "Room O1", "capacity": 5, "hourly_rate_cents": 1000}, headers={"Authorization": f"Bearer {t_a1}"}).json()
    
    # Org 2: Admin
    client.post("/auth/register", json={"org_name": org2, "username": "admin", "password": "password"})
    t_a2 = client.post("/auth/login", json={"org_name": org2, "username": "admin", "password": "password"}).json()["access_token"]
    
    # Admin 2 tries to export room from Org 1 -> 404 ROOM_NOT_FOUND
    r = client.get(f"/admin/export?room_id={room_o1['id']}&include_all=true", headers={"Authorization": f"Bearer {t_a2}"})
    assert r.status_code == 404
    assert r.json()["code"] == "ROOM_NOT_FOUND"


def test_concurrency_registration():
    """Test concurrent registration of the same username in the same organization; only one should succeed, others return 409."""
    org_name = f"org-conc-reg-{time.time()}"
    username = "user-reg"
    password = "password"
    
    # Pre-create the organization to ensure both concurrent requests try to register the user in the same org
    client.post("/auth/register", json={"org_name": org_name, "username": "admin", "password": "password"})
    
    results = []
    def make_registration():
        c = TestClient(app)
        try:
            r = c.post("/auth/register", json={"org_name": org_name, "username": username, "password": password})
            results.append((r.status_code, r.json()))
        except Exception as e:
            results.append((999, str(e)))
            
    threads = [threading.Thread(target=make_registration) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    success = [r for r in results if r[0] == 201]
    conflict = [r for r in results if r[0] == 409]
    
    assert len(success) == 1, f"Expected exactly 1 success, got {len(success)}: {results}"
    assert len(conflict) == 4, f"Expected 4 conflicts, got {len(conflict)}: {results}"
    assert all(c[1]["code"] == "USERNAME_TAKEN" for c in conflict)

