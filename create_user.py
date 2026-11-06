import hashlib
import json
import os
import secrets

USERS_FILE = "users.json"

def _read_users():
    if not os.path.exists(USERS_FILE):
        return []
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _write_users(users):
    temp_file = USERS_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)
    os.replace(temp_file, USERS_FILE)

def register_user(username, password):
    if not username or not password:
        print("Username and password cannot be empty.")
        return

    users = _read_users()

    # Check for existing user
    for u in users:
        if u.get("username", "").lower() == username.lower():
            print(f"Error: User '{username}' already exists!")
            return

    # Generate 16-byte salt and 32-byte session token (cookie)
    salt = secrets.token_hex(16)
    session_cookie = secrets.token_hex(32)

    # Hash password using PBKDF2-HMAC-SHA256
    hashed_pw = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        bytes.fromhex(salt),
        600000
    ).hex()

    users.append({
        "username": username,
        "hashed_pw": hashed_pw,
        "salt": salt,
        "cookie": session_cookie,
        "role": "admin",
        "prefs": {"advanced_mode": False}
    })

    _write_users(users)

    print(f"User '{username}' created successfully!")
    print(f"Assigned Session Cookie: {session_cookie}")

if __name__ == "__main__":
    print("=== Admin Account Generator ===")
    u = input("Enter username: ").strip()
    p = input("Enter password: ").strip()
    register_user(u, p)
