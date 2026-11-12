"""Wrong-password limits: per account and per IP, on the login page and on the change-password page."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.models import User
from app.ratelimit import FailureLimiter
from app.services import check_password
from tests.test_http import Client, add_user, app, client  # noqa: F401  (fixtures)


def from_ip(app, ip):
    c = Client(app)
    c.c = TestClient(app, client=(ip, 50000))
    return c


def test_login_limited_per_ip_across_usernames(client):
    app = client.app
    for name in ("u1", "u2", "u3", "u4", "u5"):
        add_user(client, name, "accountant")
    attacker = from_ip(app, "10.0.0.66")
    for name in ("u1", "u2", "u3", "u4", "u5"):  # 4 tries each: under the per-account limit of 5
        for _ in range(4):
            assert attacker.login(name, "guess-guess").status_code == 401
    # 20 failures from this IP: blocked now, even with a right password for an untouched account
    assert attacker.login("admin", "password123").status_code == 429
    # another IP is not affected
    assert from_ip(app, "10.0.0.7").login("admin", "password123").status_code == 200


def test_successful_login_does_not_reset_the_ip_counter(client):
    app = client.app
    for name in ("u1", "u2", "u3", "u4", "u5"):
        add_user(client, name, "accountant")
    attacker = from_ip(app, "10.0.0.66")
    for i in range(19):
        attacker.login(("u1", "u2", "u3", "u4", "u5")[i % 5], "guess-guess")
        if i == 10:
            assert attacker.login("admin", "password123").status_code == 200  # his own account
    attacker.login("u1", "guess-guess")
    assert attacker.login("u2", "password123").status_code == 429


def test_spoofed_x_forwarded_for_does_not_escape_the_ip_limit(client):
    # as deployed: nginx appends the real address, uvicorn trusts only 127.0.0.1
    wrapped = ProxyHeadersMiddleware(client.app, trusted_hosts="127.0.0.1")
    for name in ("u1", "u2", "u3", "u4", "u5"):
        add_user(client, name, "accountant")
    c = Client(client.app)
    c.c = TestClient(wrapped, client=("127.0.0.1", 50000))
    n = 0
    for name in ("u1", "u2", "u3", "u4", "u5"):
        for _ in range(4):
            n += 1
            c.c.headers["x-forwarded-for"] = f"203.0.113.{n}, 198.51.100.9"  # forged part, then nginx's
            assert c.login(name, "guess-guess").status_code == 401
    c.c.headers["x-forwarded-for"] = "203.0.113.200, 198.51.100.9"
    assert c.login("admin", "password123").status_code == 429


def test_change_password_page_limited_per_account(client):
    c = client  # logged in as admin
    for _ in range(5):
        r = c.post("/account/password", {"current": "wrong-guess", "new": "newpass123", "new2": "newpass123"},
                   page="/account")
        assert "flash error" in r.text
    # now even the right current password is refused
    r = c.post("/account/password", {"current": "password123", "new": "newpass123", "new2": "newpass123"},
               page="/account")
    assert "محاولات كثيرة" in r.text
    s = c.db()
    assert check_password("password123", s.scalar(select(User.password_hash).where(User.username == "admin")))
    s.close()


def test_change_password_failures_count_toward_the_ip_limit(client):
    app = client.app
    add_user(client, "acc", "accountant")
    c = from_ip(app, "10.0.0.66")
    c.login("acc")
    for _ in range(4):  # under the per-account limit
        c.post("/account/password", {"current": "wrong-guess", "new": "newpass123", "new2": "newpass123"},
               page="/account")
    for name in ("x1", "x2", "x3", "x4"):
        for _ in range(4):
            c.login(name, "guess-guess")
    assert c.login("admin", "password123").status_code == 429


def test_right_current_password_works_and_resets(client):
    c = client
    for _ in range(3):
        c.post("/account/password", {"current": "wrong-guess", "new": "newpass123", "new2": "newpass123"},
               page="/account")
    r = c.post("/account/password", {"current": "password123", "new": "newpass123", "new2": "newpass123"},
               page="/account")
    assert "flash ok" in r.text


def test_limiter_window_and_flooding_does_not_clear_a_target():
    now = [0.0]
    lim = FailureLimiter(limit=3, window=300, max_keys=50, clock=lambda: now[0])
    for _ in range(3):
        lim.fail("login:admin")
    assert lim.blocked("login:admin")
    for i in range(500):  # flood with other keys
        lim.fail(f"login:junk{i}")
    lim.fail("login:admin")  # attacker keeps going: still counted, still blocked
    assert lim.blocked("login:admin")
    now[0] = 301
    assert not lim.blocked("login:admin")
