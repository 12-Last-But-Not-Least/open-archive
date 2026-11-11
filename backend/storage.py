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
            migrate_members = not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lab_members'").fetchone()
            db.executescript("""
                CREATE TABLE IF NOT EXISTS private_data (
                    key TEXT PRIMARY KEY, value BLOB NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS directory_entries (
                    kind TEXT NOT NULL, id TEXT NOT NULL, owner TEXT NOT NULL,
                    value TEXT NOT NULL, PRIMARY KEY(kind, id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS researcher_owner
                    ON directory_entries(owner) WHERE kind = 'researchers';
                CREATE TABLE IF NOT EXISTS removed_researchers (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lab_members (
                    lab_id TEXT NOT NULL, did TEXT NOT NULL, role TEXT NOT NULL,
                    PRIMARY KEY(lab_id, did)
                );
                CREATE TABLE IF NOT EXISTS lab_invites (
                    lab_id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS claims (
                    did TEXT PRIMARY KEY, handle TEXT NOT NULL, researcher_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS approved_researcher
                    ON claims(researcher_id) WHERE status = 'approved';
            """)
            db.execute("INSERT OR IGNORE INTO lab_members SELECT id, owner, 'owner' FROM directory_entries WHERE kind = 'labs'")
            if migrate_members:
                labs = {row["id"] for row in db.execute("SELECT id FROM directory_entries WHERE kind='labs'")}
                for row in db.execute("SELECT owner, value FROM directory_entries WHERE kind='researchers'").fetchall():
                    for lab_id in set(json.loads(row["value"]).get("labIds", [])) & labs:
                        db.execute("INSERT OR IGNORE INTO lab_members VALUES (?, ?, 'member')", (lab_id, row["owner"]))
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

    def directory_entries(self, kind):
        with self.connect() as db:
            return [json.loads(row["value"]) for row in db.execute(
                "SELECT value FROM directory_entries WHERE kind = ? ORDER BY id", (kind,))]

    def save_directory_entry(self, kind, identifier, owner, value):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT owner FROM directory_entries WHERE kind = ? AND id = ?",
                                 (kind, identifier)).fetchone()
            if current and current["owner"] != owner:
                raise ValueError("This entry already belongs to another account.")
            db.execute("INSERT INTO directory_entries VALUES (?, ?, ?, ?) ON CONFLICT(kind, id) DO UPDATE SET value=excluded.value",
                       (kind, identifier, owner, json.dumps(value)))

    def lab_members(self, lab_id=None):
        with self.connect() as db:
            if lab_id is None:
                return [dict(row) for row in db.execute("SELECT * FROM lab_members")]
            return [dict(row) for row in db.execute("SELECT * FROM lab_members WHERE lab_id = ?", (lab_id,))]

    def create_lab(self, identifier, owner, value):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM directory_entries WHERE kind='labs' AND id=?", (identifier,)).fetchone():
                raise ValueError("This lab is already listed.")
            db.execute("INSERT INTO directory_entries VALUES ('labs', ?, ?, ?)", (identifier, owner, json.dumps(value)))
            db.execute("INSERT INTO lab_members VALUES (?, ?, 'owner')", (identifier, owner))

    def manage_lab(self, lab_id, actor, action, target=None, token=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lab = db.execute("SELECT 1 FROM directory_entries WHERE kind='labs' AND id=?", (lab_id,)).fetchone()
            if not lab:
                raise ValueError("Lab not found.")
            role_row = db.execute("SELECT role FROM lab_members WHERE lab_id=? AND did=?", (lab_id, actor)).fetchone()
            role = role_row["role"] if role_row else None
            if action == "join":
                invite = db.execute("SELECT 1 FROM lab_invites WHERE lab_id=? AND token=?", (lab_id, token)).fetchone()
                if not invite:
                    raise ValueError("This invitation is invalid. Ask the lab admin for a new link.")
                db.execute("INSERT OR IGNORE INTO lab_members VALUES (?, ?, 'member')", (lab_id, actor))
                return
            if action == "invite":
                if role not in ("owner", "admin"):
                    raise ValueError("Only lab admins can create invitations.")
                db.execute("INSERT OR REPLACE INTO lab_invites VALUES (?, ?)", (lab_id, token))
                return
            member = db.execute("SELECT role FROM lab_members WHERE lab_id=? AND did=?", (lab_id, target)).fetchone()
            if not member:
                raise ValueError("Member not found.")
            if member["role"] == "owner":
                raise ValueError("The lab owner cannot be removed or demoted.")
            if action == "remove":
                if actor != target and not (role == "owner" or (role == "admin" and member["role"] == "member")):
                    raise ValueError("You cannot remove this member.")
                db.execute("DELETE FROM lab_members WHERE lab_id=? AND did=?", (lab_id, target))
            elif action in ("admin", "member"):
                if role != "owner":
                    raise ValueError("Only the lab owner can change admin permissions.")
                db.execute("UPDATE lab_members SET role=? WHERE lab_id=? AND did=?", (action, lab_id, target))
            else:
                raise ValueError("Unknown action.")

    def removed_researchers(self):
        with self.connect() as db:
            return {row["id"] for row in db.execute("SELECT id FROM removed_researchers")}

    def remove_researcher(self, did, seeded_id=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM directory_entries WHERE kind = 'researchers' AND owner = ?", (did,))
            db.execute("DELETE FROM claims WHERE did = ?", (did,))
            if seeded_id:
                db.execute("INSERT OR IGNORE INTO removed_researchers VALUES (?, ?)", (seeded_id, did))

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

    def claim_by_name(self, did, handle, researcher_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM claims WHERE status = 'approved' AND (did = ? OR researcher_id = ?)",
                          (did, researcher_id)).fetchone():
                raise ValueError("This account or profile has already been linked.")
            profile = db.execute("SELECT value FROM directory_entries WHERE kind = 'researchers' AND owner = ?", (did,)).fetchone()
            if profile:
                if not json.loads(profile["value"]).get("autoCreated"):
                    raise ValueError("This account already has a researcher profile.")
                db.execute("DELETE FROM directory_entries WHERE kind = 'researchers' AND owner = ?", (did,))
            db.execute("INSERT OR REPLACE INTO claims VALUES (?, ?, ?, 'approved', ?)",
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
