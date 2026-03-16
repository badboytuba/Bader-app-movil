"""Quick test script to verify security fixes are working."""
import requests

BASE = "http://127.0.0.1:5050"

print("=" * 50)
print("TEST 1: Root URL redirects to /login")
print("=" * 50)
r = requests.get(f"{BASE}/", allow_redirects=False)
print(f"  Status: {r.status_code}")
print(f"  Location: {r.headers.get('Location', 'none')}")
assert r.status_code == 302, f"Expected 302, got {r.status_code}"
assert "/login" in r.headers.get("Location", ""), "Should redirect to /login"
print("  ✅ PASSED\n")

print("=" * 50)
print("TEST 2: Login page loads correctly")
print("=" * 50)
r = requests.get(f"{BASE}/login")
print(f"  Status: {r.status_code}")
assert r.status_code == 200, f"Expected 200, got {r.status_code}"
assert "Iniciar Sesión" in r.text, "Should show login form"
assert "csrf_token" in r.text, "Should contain CSRF token"
print("  ✅ PASSED\n")

print("=" * 50)
print("TEST 3: Wrong credentials show error")
print("=" * 50)
# First get CSRF token
s = requests.Session()
r = s.get(f"{BASE}/login")
# Extract CSRF token
import re
match = re.search(r'name="csrf_token".*?value="([^"]+)"', r.text)
csrf = match.group(1) if match else ""
r = s.post(f"{BASE}/login", data={"username": "wrong", "password": "wrong", "csrf_token": csrf}, allow_redirects=True)
print(f"  Status: {r.status_code}")
assert "incorrectos" in r.text, "Should show error message"
print("  ✅ PASSED\n")

print("=" * 50)
print("TEST 4: Correct credentials login works")
print("=" * 50)
s2 = requests.Session()
r = s2.get(f"{BASE}/login")
match = re.search(r'name="csrf_token".*?value="([^"]+)"', r.text)
csrf = match.group(1) if match else ""
r = s2.post(f"{BASE}/login", data={"username": "bader", "password": "Feira2025!", "csrf_token": csrf}, allow_redirects=True)
print(f"  Status: {r.status_code}")
assert "Buscar Cliente" in r.text, "Should show home page after login"
print("  ✅ PASSED\n")

print("=" * 50)
print("TEST 5: Protected routes redirect when not logged in")
print("=" * 50)
r = requests.get(f"{BASE}/create_presupuesto?vat=ES12345", allow_redirects=False)
print(f"  Status: {r.status_code}")
assert r.status_code == 302, f"Expected 302, got {r.status_code}"
assert "/login" in r.headers.get("Location", ""), "Should redirect to /login"
print("  ✅ PASSED\n")

print("=" * 50)
print("TEST 6: CSRF required on POST routes")
print("=" * 50)
s3 = requests.Session()
# Login first
r = s3.get(f"{BASE}/login")
match = re.search(r'name="csrf_token".*?value="([^"]+)"', r.text)
csrf = match.group(1) if match else ""
s3.post(f"{BASE}/login", data={"username": "bader", "password": "Feira2025!", "csrf_token": csrf})
# Try POST without CSRF token
r = s3.post(f"{BASE}/search", data={"query": "test"})
print(f"  Status (no CSRF): {r.status_code}")
assert r.status_code == 400, f"Expected 400 (CSRF rejected), got {r.status_code}"
print("  ✅ PASSED\n")

print("=" * 50)
print("TEST 7: DELETE route rejects GET requests")
print("=" * 50)
r = s3.get(f"{BASE}/delete_product/999")
print(f"  Status: {r.status_code}")
assert r.status_code == 405, f"Expected 405 (Method Not Allowed), got {r.status_code}"
print("  ✅ PASSED\n")

print("=" * 50)
print("ALL 7 TESTS PASSED ✅")
print("=" * 50)
