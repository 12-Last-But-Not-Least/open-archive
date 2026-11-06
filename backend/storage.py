"""Small SQLite store. OAuth credentials are encrypted and never sent to the browser."""

import json
import os
import sqlite3
import time
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from atproto_oauth.models import OAuthSession, OAuthState
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization


class Database:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key_path = directory / "session.key"
        if not key_path.exists():
            try:
                with key_path.open("xb") as key_file:
                    os.chmod(key_path, 0o600)
                    key_file.write(Fernet.generate_key())
            except FileExistsError:
                pass
        self.cipher = Fernet(key_path.read_bytes())
        self.path = directory / "archive.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS private_data (
                    key TEXT PRIMARY KEY, value BLOB NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                    did TEXT PRIMARY KEY, handle TEXT NOT NULL, researcher_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS approved_researcher
                    ON claims(researcher_id) WHERE status = 'approved';
            """)
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def put(self, key, value, ttl=7 * 86400):
        encoded = self.cipher.encrypt(json.dumps(value).encode())
        with self.connect() as db:
            db.execute("DELETE FROM private_data WHERE expires < ?", (time.time(),))
            db.execute("INSERT OR REPLACE INTO private_data VALUES (?, ?, ?)", (key, encoded, time.time() + ttl))

    def get(self, key, *, consume=False):
        with self.connect() as db:
            if consume:
                row = db.execute("DELETE FROM private_data WHERE key = ? RETURNING *", (key,)).fetchone()
            else:
                row = db.execute("SELECT * FROM private_data WHERE key = ?", (key,)).fetchone()
        if not row or row["expires"] < time.time():
            return None
        return json.loads(self.cipher.decrypt(row["value"]))

    def delete(self, key):
        with self.connect() as db:
            db.execute("DELETE FROM private_data WHERE key = ?", (key,))

    def claims(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM claims ORDER BY created")]

    def request_claim(self, did, handle, researcher_id):
        with self.connect() as db:
            current = db.execute("SELECT status FROM claims WHERE did = ?", (did,)).fetchone()
            if current and current["status"] == "approved":
                raise ValueError("This account already has a researcher profile.")
            db.execute("INSERT OR REPLACE INTO claims VALUES (?, ?, ?, 'pending', ?)",
                       (did, handle, researcher_id, time.time()))


class OAuthStore:
    """One OAuth grant per browser session, including separate DPoP keys."""

    def __init__(self, database, browser_key):
        self.database, self.prefix = database, "oauth:" + browser_key + ":"

    def save(self, key, value, ttl):
        data = {field.name: getattr(value, field.name) for field in fields(value)}
        data["dpop_private_key"] = data["dpop_private_key"].private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        for field in ("created_at", "expires_at"):
            if data.get(field):
                data[field] = data[field].isoformat()
        self.database.put(self.prefix + key, data, ttl)

    def load(self, key, model):
        data = self.database.get(self.prefix + key)
        if data is None:
            return None
        data["dpop_private_key"] = serialization.load_pem_private_key(data["dpop_private_key"].encode(), password=None)
        for field in ("created_at", "expires_at"):
            if data.get(field):
                data[field] = datetime.fromisoformat(data[field])
        return model(**data)

    async def save_state(self, state):
        self.save("state:" + state.state, state, 600)

    async def get_state(self, state_key):
        return self.load("state:" + state_key, OAuthState)

    async def delete_state(self, state_key):
        self.database.delete(self.prefix + "state:" + state_key)

    async def save_session(self, session):
        self.save("session:" + session.did, session, 7 * 86400)

    async def get_session(self, did):
        return self.load("session:" + did, OAuthSession)

    async def delete_session(self, did):
        self.database.delete(self.prefix + "session:" + did)
