"""Unit tests for the relay server: tokens, authorization, room routing,
idempotency, ordering and persistence across a restart.

These are the failures that would be worst in the field and least
visible in a demo - a student able to send teacher-only controls, a cue
leaking into the wrong room, a reconnect double-cueing the student, or a
session's events quietly not surviving a restart.

Everything runs against a TEMPORARY database and a TEMPORARY key pair;
the project's own server_config.json, database and secrets/ are never
touched. No hardware and no real network is involved - FastAPI's
TestClient drives both the REST API and the WebSocket in-process.

Run from main/:  python -m pytest test-script/test_remote_guidance_server.py
"""

import sys
import time
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from server import database as dbm  # noqa: E402
from server.app import ServerContext, create_app  # noqa: E402
from server.auth import AuthError, TOKEN_TYPE_REFRESH, TokenService, hash_password, verify_password  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.database import Database  # noqa: E402
from server.schemas import PROTOCOL_VERSION, make_envelope  # noqa: E402


def temp_config(directory: Path, **overrides) -> ServerConfig:
    return ServerConfig(
        bind_host="127.0.0.1",
        port=8799,
        database_path=str(directory / "relay.db"),
        jwt_private_key_path=str(directory / "jwt_private.pem"),
        jwt_public_key_path=str(directory / "jwt_public.pem"),
        source_path=directory / "server_config.json",
        **overrides,
    )


class _ServerCase(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.dir = Path(self._dir.name)
        self.config = temp_config(self.dir)
        self.app = create_app(self.config)
        self.client = TestClient(self.app)
        self.client.__enter__()
        # Everything is torn down through addCleanup, which unittest runs
        # in reverse order *after* tearDown. Registering the client first
        # therefore closes it last - any WebSocket session a test opened
        # is closed before the client's portal goes away, which is what a
        # tearDown-based teardown would get backwards (shutting the portal
        # down with a live socket on it just blocks).
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(self.client.__exit__, None, None, None)

    # -- helpers -------------------------------------------------------

    def register(self, username: str, role: str, password: str = "a-long-enough-password") -> dict:
        response = self.client.post(
            "/api/v1/auth/register", json={"username": username, "password": password, "role": role}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    @staticmethod
    def auth(token: dict) -> dict:
        return {"Authorization": f"Bearer {token['access_token']}"}

    def make_room(self, teacher: dict, name: str = "lesson") -> dict:
        response = self.client.post("/api/v1/rooms", json={"name": name}, headers=self.auth(teacher))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def join(self, student: dict, room: dict) -> dict:
        response = self.client.post(
            "/api/v1/rooms/join", json={"join_code": room["join_code"]}, headers=self.auth(student)
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def connect(self, token: dict, room_id: str):
        """Open a socket, send the auth frame, and consume the auth reply
        plus the presence broadcast it triggers.

        The session is closed by addCleanup rather than a `with` block:
        starlette's WebSocketTestSession is entered here, and entering it
        a second time in the caller would deadlock."""
        session = self.client.websocket_connect(f"/ws/v1/rooms/{room_id}")
        socket = session.__enter__()
        self.addCleanup(session.__exit__, None, None, None)
        socket.send_json(
            make_envelope("auth", str(uuid.uuid4()), room_id=room_id, payload={"access_token": token["access_token"]})
        )
        ack = socket.receive_json()
        self.assertEqual(ack["payload"]["status"], "ok", ack)
        socket.receive_json()  # presence
        return socket

    @staticmethod
    def drain(socket, expected_type: str, limit: int = 8):
        """Next frame of a given type, skipping presence chatter."""
        for _ in range(limit):
            frame = socket.receive_json()
            if frame["type"] == expected_type:
                return frame
        raise AssertionError(f"no {expected_type} frame arrived")


# ---------------------------------------------------------------------------
# Passwords and tokens
# ---------------------------------------------------------------------------


class PasswordTests(unittest.TestCase):
    def test_hash_is_argon2id_and_verifies(self):
        digest = hash_password("a-long-enough-password")
        self.assertTrue(digest.startswith("$argon2id$"))
        self.assertNotIn("a-long-enough-password", digest)
        self.assertTrue(verify_password(digest, "a-long-enough-password"))
        self.assertFalse(verify_password(digest, "wrong"))

    def test_same_password_hashes_differently(self):
        """Per-user salt: two accounts with the same password must not
        produce the same stored value."""
        self.assertNotEqual(hash_password("a-long-enough-password"), hash_password("a-long-enough-password"))

    def test_short_password_is_refused(self):
        with self.assertRaises(AuthError):
            hash_password("short")

    def test_garbage_hash_is_a_failed_verify_not_a_crash(self):
        self.assertFalse(verify_password("not-a-hash", "a-long-enough-password"))


class TokenTests(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.dir = Path(self._dir.name)
        self.config = temp_config(self.dir)
        self.db = Database(self.config.database_file)
        self.tokens = TokenService(self.config, self.db)
        self.tokens.ensure_keys()
        self.user_id = dbm.create_user(self.db, "teacher", hash_password("a-long-enough-password"), "teacher")

    def tearDown(self):
        self._dir.cleanup()

    def test_keys_are_created_and_private_key_is_user_only(self):
        self.assertTrue(self.config.jwt_private_key_file.exists())
        self.assertTrue(self.config.jwt_public_key_file.exists())
        self.assertIn(b"PRIVATE KEY", self.config.jwt_private_key_file.read_bytes())
        if sys.platform != "win32":
            mode = self.config.jwt_private_key_file.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_issued_token_carries_every_required_claim(self):
        pair = self.tokens.issue_pair(self.user_id, "teacher")
        claims = self.tokens.verify(pair["access_token"]).raw
        for field in ("sub", "role", "iat", "nbf", "exp", "jti", "iss", "aud"):
            self.assertIn(field, claims)
        self.assertEqual(claims["iss"], self.config.issuer)
        self.assertEqual(claims["aud"], self.config.audience)
        self.assertEqual(claims["role"], "teacher")

    def test_algorithm_is_rs256(self):
        import jwt

        pair = self.tokens.issue_pair(self.user_id, "teacher")
        self.assertEqual(jwt.get_unverified_header(pair["access_token"])["alg"], "RS256")

    def test_access_and_refresh_are_not_interchangeable(self):
        pair = self.tokens.issue_pair(self.user_id, "teacher")
        with self.assertRaises(AuthError):
            self.tokens.verify(pair["refresh_token"])  # expects an access token
        with self.assertRaises(AuthError):
            self.tokens.verify(pair["access_token"], expected_type=TOKEN_TYPE_REFRESH)

    def test_expired_token_is_refused(self):
        short = temp_config(self.dir, access_token_ttl_days=0)
        service = TokenService(short, self.db)
        pair = service.issue_pair(self.user_id, "teacher")
        time.sleep(0.01)
        with self.assertRaises(AuthError) as ctx:
            service.verify(pair["access_token"])
        self.assertIn("expired", str(ctx.exception))

    def test_token_from_another_key_pair_is_refused(self):
        other_dir = Path(TemporaryDirectory().name)
        other_dir.mkdir(parents=True, exist_ok=True)
        other = temp_config(other_dir)
        other_service = TokenService(other, self.db)
        other_service.ensure_keys()
        foreign = other_service.issue_pair(self.user_id, "teacher")["access_token"]
        with self.assertRaises(AuthError):
            self.tokens.verify(foreign)

    def test_revocation_takes_effect_before_expiry(self):
        pair = self.tokens.issue_pair(self.user_id, "teacher")
        self.assertTrue(self.tokens.verify(pair["access_token"]))
        self.tokens.revoke(pair["access_token"])
        with self.assertRaises(AuthError) as ctx:
            self.tokens.verify(pair["access_token"])
        self.assertIn("revoked", str(ctx.exception))

    def test_revoking_twice_is_not_an_error(self):
        pair = self.tokens.issue_pair(self.user_id, "teacher")
        self.tokens.revoke(pair["access_token"])
        self.tokens.revoke(pair["access_token"])  # logging out twice must still work

    def test_default_ttls_are_the_long_configured_ones(self):
        pair = self.tokens.issue_pair(self.user_id, "teacher")
        access_days = (pair["expires_at"] - time.time()) / 86400
        refresh_days = (pair["refresh_expires_at"] - time.time()) / 86400
        self.assertAlmostEqual(access_days, 30, delta=0.1)
        self.assertAlmostEqual(refresh_days, 180, delta=0.1)


class AuthEndpointTests(_ServerCase):
    def test_register_login_me(self):
        token = self.register("teacher1", "teacher")
        me = self.client.get("/api/v1/auth/me", headers=self.auth(token)).json()
        self.assertEqual(me["username"], "teacher1")
        self.assertEqual(me["role"], "teacher")

        again = self.client.post(
            "/api/v1/auth/login", json={"username": "teacher1", "password": "a-long-enough-password"}
        )
        self.assertEqual(again.status_code, 200)

    def test_duplicate_username_is_refused(self):
        self.register("teacher1", "teacher")
        response = self.client.post(
            "/api/v1/auth/register",
            json={"username": "teacher1", "password": "a-long-enough-password", "role": "teacher"},
        )
        self.assertEqual(response.status_code, 409)

    def test_wrong_password_and_unknown_user_look_the_same(self):
        self.register("teacher1", "teacher")
        wrong = self.client.post("/api/v1/auth/login", json={"username": "teacher1", "password": "nope-nope"})
        missing = self.client.post("/api/v1/auth/login", json={"username": "ghost", "password": "nope-nope"})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.json()["detail"], missing.json()["detail"])

    def test_refresh_rotates_and_spends_the_old_token(self):
        token = self.register("teacher1", "teacher")
        refreshed = self.client.post("/api/v1/auth/refresh", json={"refresh_token": token["refresh_token"]})
        self.assertEqual(refreshed.status_code, 200)
        self.assertNotEqual(refreshed.json()["refresh_token"], token["refresh_token"])

        replay = self.client.post("/api/v1/auth/refresh", json={"refresh_token": token["refresh_token"]})
        self.assertEqual(replay.status_code, 401)

    def test_logout_revokes_the_access_token(self):
        token = self.register("teacher1", "teacher")
        response = self.client.post(
            "/api/v1/auth/logout", json={"refresh_token": token["refresh_token"]}, headers=self.auth(token)
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=self.auth(token)).status_code, 401)

    def test_endpoints_require_a_token(self):
        self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 401)
        self.assertEqual(self.client.post("/api/v1/rooms", json={"name": "x"}).status_code, 401)

    def test_registration_can_be_closed_after_the_first_account(self):
        config = temp_config(self.dir / "closed", allow_registration=False)
        config.database_file.parent.mkdir(parents=True, exist_ok=True)
        with TestClient(create_app(config)) as client:
            first = client.post(
                "/api/v1/auth/register",
                json={"username": "admin", "password": "a-long-enough-password", "role": "teacher"},
            )
            self.assertEqual(first.status_code, 201, "the first account must always be allowed through")
            second = client.post(
                "/api/v1/auth/register",
                json={"username": "other", "password": "a-long-enough-password", "role": "student"},
            )
            self.assertEqual(second.status_code, 403)


# ---------------------------------------------------------------------------
# Rooms and authorization
# ---------------------------------------------------------------------------


class RoomAuthorizationTests(_ServerCase):
    def setUp(self):
        super().setUp()
        self.teacher = self.register("teacher1", "teacher")
        self.other_teacher = self.register("teacher2", "teacher")
        self.student = self.register("student1", "student")
        self.outsider = self.register("student2", "student")
        self.room = self.make_room(self.teacher)
        self.join(self.student, self.room)

    def test_students_cannot_create_rooms(self):
        response = self.client.post("/api/v1/rooms", json={"name": "mine"}, headers=self.auth(self.student))
        self.assertEqual(response.status_code, 403)

    def test_join_code_is_only_returned_to_the_owner(self):
        owner_view = self.client.get(f"/api/v1/rooms/{self.room['room_id']}", headers=self.auth(self.teacher)).json()
        member_view = self.client.get(f"/api/v1/rooms/{self.room['room_id']}", headers=self.auth(self.student)).json()
        self.assertIsNotNone(owner_view["join_code"])
        self.assertIsNone(member_view["join_code"])

    def test_non_members_are_refused(self):
        room_id = self.room["room_id"]
        for path, method in (
            (f"/api/v1/rooms/{room_id}", "get"),
            (f"/api/v1/rooms/{room_id}/members", "get"),
            (f"/api/v1/rooms/{room_id}/recordings", "get"),
        ):
            response = getattr(self.client, method)(path, headers=self.auth(self.outsider))
            self.assertEqual(response.status_code, 403, path)

    def test_another_teacher_cannot_drive_this_room(self):
        """Membership is not enough - only the owning teacher."""
        room_id = self.room["room_id"]
        self.client.post(
            "/api/v1/rooms/join", json={"join_code": self.room["join_code"]}, headers=self.auth(self.other_teacher)
        )
        for path in (f"/api/v1/rooms/{room_id}/close",):
            self.assertEqual(
                self.client.post(path, headers=self.auth(self.other_teacher)).status_code, 403, path
            )
        self.assertEqual(
            self.client.post(
                f"/api/v1/rooms/{room_id}/sessions",
                json={"mode": "live", "guidance_mode": "both"},
                headers=self.auth(self.other_teacher),
            ).status_code,
            403,
        )

    def test_student_cannot_open_a_session_or_upload(self):
        room_id = self.room["room_id"]
        self.assertEqual(
            self.client.post(
                f"/api/v1/rooms/{room_id}/sessions",
                json={"mode": "live", "guidance_mode": "visual"},
                headers=self.auth(self.student),
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/rooms/{room_id}/recordings",
                json={"name": "x", "events": [{"event_order": 0, "rel_time_s": 0.0, "note": 60}]},
                headers=self.auth(self.student),
            ).status_code,
            403,
        )

    def test_bad_join_code_and_closed_room(self):
        self.assertEqual(
            self.client.post("/api/v1/rooms/join", json={"join_code": "ZZZZZZ"},
                             headers=self.auth(self.outsider)).status_code,
            404,
        )
        self.client.post(f"/api/v1/rooms/{self.room['room_id']}/close", headers=self.auth(self.teacher))
        self.assertEqual(
            self.client.post(
                "/api/v1/rooms/join", json={"join_code": self.room["join_code"]}, headers=self.auth(self.outsider)
            ).status_code,
            409,
        )

    def test_members_lists_both_roles(self):
        members = self.client.get(
            f"/api/v1/rooms/{self.room['room_id']}/members", headers=self.auth(self.teacher)
        ).json()
        self.assertEqual({m["role"] for m in members}, {"teacher", "student"})

    def test_membership_table_allows_more_than_one_student(self):
        """First version routes one student, but nothing in the schema
        forbids a second - adding students later must not need a
        migration."""
        extra = self.register("student3", "student")
        self.join(extra, self.room)
        members = self.client.get(
            f"/api/v1/rooms/{self.room['room_id']}/members", headers=self.auth(self.teacher)
        ).json()
        self.assertEqual(sum(1 for m in members if m["role"] == "student"), 2)


class RecordingAndSessionTests(_ServerCase):
    def setUp(self):
        super().setUp()
        self.teacher = self.register("teacher1", "teacher")
        self.student = self.register("student1", "student")
        self.room = self.make_room(self.teacher)
        self.join(self.student, self.room)

    def _upload(self):
        payload = {
            "name": "scale",
            "source": "sequence",
            "meta": {"title": "C scale"},
            "events": [
                {"event_order": i, "rel_time_s": i * 0.5, "duration_s": 0.4, "note": 60 + i, "finger": "R1"}
                for i in range(4)
            ],
        }
        response = self.client.post(
            f"/api/v1/rooms/{self.room['room_id']}/recordings", json=payload, headers=self.auth(self.teacher)
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_upload_and_download_a_recording(self):
        summary = self._upload()
        self.assertEqual(summary["event_count"], 4)

        detail = self.client.get(
            f"/api/v1/recordings/{summary['recording_id']}", headers=self.auth(self.student)
        ).json()
        self.assertEqual(len(detail["events"]), 4)
        self.assertEqual([e["event_order"] for e in detail["events"]], [0, 1, 2, 3])
        self.assertEqual(detail["meta"]["title"], "C scale")

    def test_recording_is_scoped_to_its_room(self):
        summary = self._upload()
        outsider = self.register("student9", "student")
        self.assertEqual(
            self.client.get(
                f"/api/v1/recordings/{summary['recording_id']}", headers=self.auth(outsider)
            ).status_code,
            403,
        )

    def test_empty_recording_is_refused(self):
        response = self.client.post(
            f"/api/v1/rooms/{self.room['room_id']}/recordings",
            json={"name": "empty", "events": []},
            headers=self.auth(self.teacher),
        )
        self.assertEqual(response.status_code, 400)

    def test_recording_session_needs_a_valid_recording(self):
        response = self.client.post(
            f"/api/v1/rooms/{self.room['room_id']}/sessions",
            json={"mode": "recording", "guidance_mode": "both"},
            headers=self.auth(self.teacher),
        )
        self.assertEqual(response.status_code, 400)

    def test_summary_names_the_student_as_the_source(self):
        """The server must never present a number it computed itself as
        the session result."""
        session = self.client.post(
            f"/api/v1/rooms/{self.room['room_id']}/sessions",
            json={"mode": "live", "guidance_mode": "both"},
            headers=self.auth(self.teacher),
        ).json()
        summary = self.client.get(
            f"/api/v1/sessions/{session['session_id']}/summary", headers=self.auth(self.teacher)
        ).json()
        self.assertIn("student_reported_summary", summary)
        self.assertIsNone(summary["summary_source"])
        self.assertEqual(summary["counts"]["performance_events"], 0)


# ---------------------------------------------------------------------------
# WebSocket relay
# ---------------------------------------------------------------------------


class WebSocketRelayTests(_ServerCase):
    def setUp(self):
        super().setUp()
        self.teacher = self.register("teacher1", "teacher")
        self.student = self.register("student1", "student")
        self.room = self.make_room(self.teacher)
        self.join(self.student, self.room)
        self.room_id = self.room["room_id"]

    def test_first_frame_must_be_auth(self):
        with self.client.websocket_connect(f"/ws/v1/rooms/{self.room_id}") as socket:
            socket.send_json(make_envelope("guidance.live", str(uuid.uuid4()), room_id=self.room_id))
            frame = socket.receive_json()
            self.assertEqual(frame["type"], "error")
            self.assertEqual(frame["payload"]["code"], "auth_required")

    def test_bad_token_is_refused(self):
        with self.client.websocket_connect(f"/ws/v1/rooms/{self.room_id}") as socket:
            socket.send_json(
                make_envelope("auth", str(uuid.uuid4()), room_id=self.room_id, payload={"access_token": "nope"})
            )
            frame = socket.receive_json()
            self.assertEqual(frame["payload"]["code"], "auth_failed")

    def test_non_member_is_refused_before_registration(self):
        outsider = self.register("student9", "student")
        with self.client.websocket_connect(f"/ws/v1/rooms/{self.room_id}") as socket:
            socket.send_json(
                make_envelope(
                    "auth", str(uuid.uuid4()), room_id=self.room_id,
                    payload={"access_token": outsider["access_token"]},
                )
            )
            frame = socket.receive_json()
            self.assertEqual(frame["payload"]["code"], "forbidden")

    def test_guidance_reaches_the_student_with_server_stamps(self):
        tws = self.connect(self.teacher, self.room_id)
        sws = self.connect(self.student, self.room_id)
        tws.receive_json()  # the presence broadcast from the student joining
        sent = make_envelope(
            "guidance.live", str(uuid.uuid4()), room_id=self.room_id, seq=1,
            payload={"actions": [{"note": 60, "finger": "R1"}], "timeout_s": 5.0},
        )
        tws.send_json(sent)

        got = self.drain(sws, "guidance.live")
        self.assertEqual(got["payload"]["actions"][0]["note"], 60)
        self.assertEqual(got["message_id"], sent["message_id"])
        # The sender's own clock value is preserved, not rewritten.
        self.assertEqual(got["sent_at_unix_ns"], sent["sent_at_unix_ns"])
        server = got["server"]
        self.assertGreater(server["receive_wall_ns"], 0)
        self.assertGreaterEqual(server["send_wall_ns"], server["receive_wall_ns"])
        self.assertEqual(server["sender_role"], "teacher")

    def test_messages_never_cross_rooms(self):
        """The failure this exists to prevent: one lesson's cues showing
        up on another student's hand."""
        other_teacher = self.register("teacher2", "teacher")
        other_student = self.register("student2", "student")
        other_room = self.make_room(other_teacher, name="other lesson")
        self.join(other_student, other_room)

        tws = self.connect(self.teacher, self.room_id)
        sws = self.connect(self.student, self.room_id)
        other = self.connect(other_student, other_room["room_id"])
        tws.receive_json()
        tws.send_json(
            make_envelope("guidance.live", str(uuid.uuid4()), room_id=self.room_id, seq=1,
                          payload={"actions": [{"note": 60, "finger": "R1"}]})
        )
        self.assertEqual(self.drain(sws, "guidance.live")["payload"]["actions"][0]["note"], 60)

        # Ask the other room's socket for something it should not have: a
        # heartbeat proves the socket is alive and that no guidance frame
        # is queued ahead of the reply.
        other.send_json(make_envelope("heartbeat", str(uuid.uuid4()), room_id=other_room["room_id"]))
        reply = other.receive_json()
        self.assertEqual(reply["type"], "heartbeat")

    def test_frame_addressed_to_another_room_is_refused(self):
        other_teacher = self.register("teacher2", "teacher")
        other_room = self.make_room(other_teacher, name="other")
        tws = self.connect(self.teacher, self.room_id)
        tws.send_json(make_envelope("guidance.live", str(uuid.uuid4()), room_id=other_room["room_id"], seq=1))
        frame = self.drain(tws, "error")
        self.assertEqual(frame["payload"]["code"], "room_mismatch")

    def test_student_cannot_send_teacher_only_controls(self):
        sws = self.connect(self.student, self.room_id)
        for message_type in (
            "guidance.live",
            "session.start",
            "session.stop",
            "recording.start",
            "latency.probe",
        ):
            sws.send_json(make_envelope(message_type, str(uuid.uuid4()), room_id=self.room_id, seq=1))
            frame = self.drain(sws, "error")
            self.assertEqual(frame["payload"]["code"], "forbidden", message_type)

    def test_duplicate_message_id_is_not_relayed_twice(self):
        """A reconnecting client replaying its outbox must not double-cue
        the student."""
        tws = self.connect(self.teacher, self.room_id)
        sws = self.connect(self.student, self.room_id)
        tws.receive_json()
        envelope = make_envelope(
            "guidance.live", str(uuid.uuid4()), room_id=self.room_id, seq=1,
            payload={"actions": [{"note": 60, "finger": "R1"}]},
        )
        tws.send_json(envelope)
        self.assertEqual(self.drain(sws, "guidance.live")["message_id"], envelope["message_id"])

        tws.send_json(envelope)  # exact replay
        refusal = self.drain(tws, "error")
        self.assertEqual(refusal["payload"]["code"], "duplicate_message")
        self.assertFalse(refusal["payload"]["fatal"])

        # The student's next frame is a *different* message, proving the
        # replay was never delivered.
        follow_up = make_envelope(
            "guidance.live", str(uuid.uuid4()), room_id=self.room_id, seq=2,
            payload={"actions": [{"note": 64, "finger": "R3"}]},
        )
        tws.send_json(follow_up)
        got = self.drain(sws, "guidance.live")
        self.assertEqual(got["message_id"], follow_up["message_id"])
        self.assertEqual(got["payload"]["actions"][0]["note"], 64)

    def test_out_of_order_seq_is_flagged_but_still_delivered(self):
        """Dropping a late cue silently would be worse than showing it
        late, so it is delivered with a flag."""
        tws = self.connect(self.teacher, self.room_id)
        sws = self.connect(self.student, self.room_id)
        tws.receive_json()
        for seq in (5, 3):
            tws.send_json(
                make_envelope("guidance.live", str(uuid.uuid4()), room_id=self.room_id, seq=seq,
                              payload={"actions": [{"note": 60}]})
            )
        first = self.drain(sws, "guidance.live")
        second = self.drain(sws, "guidance.live")
        self.assertEqual(first["seq"], 5)
        self.assertFalse(first["server"]["out_of_order"])
        self.assertEqual(second["seq"], 3)
        self.assertTrue(second["server"]["out_of_order"])

    def test_oversized_frame_is_rejected(self):
        tws = self.connect(self.teacher, self.room_id)
        envelope = make_envelope("guidance.live", str(uuid.uuid4()), room_id=self.room_id, seq=1)
        envelope["payload"] = {"junk": "x" * 100_000}
        tws.send_json(envelope)
        frame = self.drain(tws, "error")
        self.assertEqual(frame["payload"]["code"], "message_too_large")

    def test_unknown_message_type_is_rejected(self):
        tws = self.connect(self.teacher, self.room_id)
        tws.send_json(make_envelope("guidance.telepathy", str(uuid.uuid4()), room_id=self.room_id))
        self.assertEqual(self.drain(tws, "error")["payload"]["code"], "unknown_type")

    def test_protocol_version_mismatch_is_refused_at_auth(self):
        with self.client.websocket_connect(f"/ws/v1/rooms/{self.room_id}") as socket:
            envelope = make_envelope(
                "auth", str(uuid.uuid4()), room_id=self.room_id,
                payload={"access_token": self.teacher["access_token"]},
            )
            envelope["v"] = PROTOCOL_VERSION + 99
            socket.send_json(envelope)
            self.assertEqual(socket.receive_json()["payload"]["code"], "protocol_version")

    def test_latency_probe_gets_a_server_ack_and_reaches_the_student(self):
        tws = self.connect(self.teacher, self.room_id)
        sws = self.connect(self.student, self.room_id)
        tws.receive_json()
        probe = make_envelope(
            "latency.probe", str(uuid.uuid4()), room_id=self.room_id, seq=1,
            payload={"probe_id": "p1", "note": 60, "trigger_cue": False},
        )
        tws.send_json(probe)

        ack = self.drain(tws, "latency.ack")
        self.assertEqual(ack["payload"]["probe_id"], "p1")
        relayed = self.drain(sws, "latency.probe")
        self.assertEqual(relayed["payload"]["probe_id"], "p1")

    def test_heartbeat_is_echoed_with_server_stamps(self):
        tws = self.connect(self.teacher, self.room_id)
        tws.send_json(make_envelope("heartbeat", str(uuid.uuid4()), room_id=self.room_id, payload={"token": "abc"}))
        frame = self.drain(tws, "heartbeat")
        self.assertEqual(frame["payload"]["echo"], "abc")
        self.assertGreater(frame["server"]["receive_wall_ns"], 0)

    def test_presence_lists_both_endpoints(self):
        tws = self.connect(self.teacher, self.room_id)
        self.connect(self.student, self.room_id)
        frame = self.drain(tws, "presence")
        roles = {m["role"] for m in frame["payload"]["members"]}
        self.assertEqual(roles, {"teacher", "student"})


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.dir = Path(self._dir.name)
        self.config = temp_config(self.dir)

    def tearDown(self):
        self._dir.cleanup()

    def test_schema_migrates_once_and_is_idempotent(self):
        db = Database(self.config.database_file)
        self.assertEqual(db.schema_version, dbm.SCHEMA_VERSION)
        self.assertEqual(db.migrate(), dbm.SCHEMA_VERSION)  # re-running changes nothing

    def test_pragmas_are_set(self):
        db = Database(self.config.database_file)
        self.assertEqual(db.query_one("PRAGMA journal_mode")[0].lower(), "wal")
        self.assertEqual(db.query_one("PRAGMA foreign_keys")[0], 1)

    def test_data_survives_a_server_restart(self):
        """The whole point of storing sessions: closing and reopening the
        server must not lose a lesson's events."""
        app = create_app(self.config)
        with TestClient(app) as client:
            teacher = client.post(
                "/api/v1/auth/register",
                json={"username": "teacher1", "password": "a-long-enough-password", "role": "teacher"},
            ).json()
            headers = {"Authorization": f"Bearer {teacher['access_token']}"}
            room = client.post("/api/v1/rooms", json={"name": "lesson"}, headers=headers).json()
            session = client.post(
                f"/api/v1/rooms/{room['room_id']}/sessions",
                json={"mode": "live", "guidance_mode": "both"},
                headers=headers,
            ).json()
            recording = client.post(
                f"/api/v1/rooms/{room['room_id']}/recordings",
                json={"name": "scale", "events": [{"event_order": 0, "rel_time_s": 0.0, "note": 60}]},
                headers=headers,
            ).json()

        # A completely fresh app object over the same file - as if the
        # process had been restarted.
        restarted = create_app(temp_config(self.dir))
        with TestClient(restarted) as client:
            login = client.post(
                "/api/v1/auth/login", json={"username": "teacher1", "password": "a-long-enough-password"}
            )
            self.assertEqual(login.status_code, 200, "the account did not survive the restart")
            headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

            self.assertEqual(
                client.get(f"/api/v1/rooms/{room['room_id']}", headers=headers).json()["name"], "lesson"
            )
            self.assertEqual(
                client.get(f"/api/v1/sessions/{session['session_id']}", headers=headers).status_code, 200
            )
            detail = client.get(f"/api/v1/recordings/{recording['recording_id']}", headers=headers).json()
            self.assertEqual(len(detail["events"]), 1)

    def test_token_from_before_a_restart_still_verifies(self):
        """The key pair lives on disk, so an issued token must keep
        working - otherwise every restart logs everyone out."""
        app = create_app(self.config)
        with TestClient(app) as client:
            teacher = client.post(
                "/api/v1/auth/register",
                json={"username": "teacher1", "password": "a-long-enough-password", "role": "teacher"},
            ).json()

        with TestClient(create_app(temp_config(self.dir))) as client:
            response = client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {teacher['access_token']}"}
            )
            self.assertEqual(response.status_code, 200)

    def test_revocation_survives_a_restart(self):
        app = create_app(self.config)
        with TestClient(app) as client:
            teacher = client.post(
                "/api/v1/auth/register",
                json={"username": "teacher1", "password": "a-long-enough-password", "role": "teacher"},
            ).json()
            client.post(
                "/api/v1/auth/logout",
                json={"refresh_token": teacher["refresh_token"]},
                headers={"Authorization": f"Bearer {teacher['access_token']}"},
            )

        with TestClient(create_app(temp_config(self.dir))) as client:
            response = client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {teacher['access_token']}"}
            )
            self.assertEqual(response.status_code, 401)

    def test_guidance_events_are_idempotent_at_the_database_level(self):
        db = Database(self.config.database_file)
        user_id = dbm.create_user(db, "t", hash_password("a-long-enough-password"), "teacher")
        room = dbm.create_room(db, "lesson", user_id)
        session_id = dbm.create_session(db, room["id"], user_id, "live", "both")
        row = {
            "id": "fixed-message-id",
            "session_id": session_id,
            "room_id": room["id"],
            "sender_user_id": user_id,
            "seq": 1,
            "type": "guidance.live",
            "payload": {"actions": []},
        }
        self.assertTrue(dbm.insert_guidance_event(db, row))
        self.assertFalse(dbm.insert_guidance_event(db, row))
        stored = dbm.list_session_events(db, session_id)["guidance"]
        self.assertEqual(len(stored), 1)

    def test_no_video_column_exists_anywhere(self):
        """Media must never land in SQLite - a schema that could hold it
        would eventually hold it."""
        db = Database(self.config.database_file)
        tables = [r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = [r[1].lower() for r in db.query(f"PRAGMA table_info({table})")]
            for column in columns:
                self.assertNotIn("video", column, f"{table}.{column}")
                self.assertNotIn("blob", column, f"{table}.{column}")

    def test_closing_a_room_cascades_nothing_away_prematurely(self):
        db = Database(self.config.database_file)
        user_id = dbm.create_user(db, "t", hash_password("a-long-enough-password"), "teacher")
        room = dbm.create_room(db, "lesson", user_id)
        session_id = dbm.create_session(db, room["id"], user_id, "live", "both")
        dbm.close_room(db, room["id"])
        self.assertIsNotNone(dbm.get_session(db, session_id))
        self.assertIsNotNone(dbm.get_room(db, room["id"])["closed_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
