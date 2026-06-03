"""Grant party guest user access to the Home group player."""
import sqlite3, json, secrets, hashlib

db = sqlite3.connect("/data/library.db")

# The Home group player ID from the logs
home_group_id = "51d7858a-cde3-4df0-956c-073e773e25b2"

# Find party guest user
row = db.execute("SELECT id, player_filter FROM users WHERE username = ?", ("party_guest",)).fetchone()
if row:
    user_id = row[0]
    player_filter = json.loads(row[3]) if row[3] else []
    print(f"User ID: {user_id}")
    print(f"Current player_filter: {player_filter}")
    if home_group_id not in player_filter:
        player_filter.append(home_group_id)
        db.execute("UPDATE users SET player_filter = ? WHERE id = ?", (json.dumps(player_filter), user_id))
        db.commit()
        print(f"Added {home_group_id} to player_filter")
    else:
        print("Home group already in player_filter")
else:
    print("No party_guest user found — creating one with Home group access...")
    # Create the party guest user with player_filter pre-set
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = __import__("datetime").datetime.utcnow().isoformat()
    player_filter = [home_group_id]
    db.execute(
        "INSERT INTO users (username, role, enabled, created_at, display_name, player_filter) VALUES (?, ?, 1, ?, ?, ?)",
        ("party_guest", "guest", now, "Party Guest", json.dumps(player_filter))
    )
    db.commit()
    print(f"Created party_guest user with player_filter = {player_filter}")

db.close()