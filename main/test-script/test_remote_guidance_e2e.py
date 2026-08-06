"""End-to-end integration: one relay, a fake teacher and a fake student.

Drives a whole lesson through the real server, the real envelope
protocol and the real StudentSession - live guidance, a pre-recorded
sequence, latency probes and the provisional/final result split - with
fakes only where hardware would be. Nothing here opens a camera, a MIDI
port, a serial port or a real socket.

What it is really checking is that the pieces agree with each other:
that a cue sent by a teacher arrives, is acknowledged before it is
presented, is scored on the student's own clock, and comes back to the
teacher as a result the server stored but did not compute.

Run from main/:  python -m pytest test-script/test_remote_guidance_e2e.py
"""

import sys
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.finger_matching import FingerMatch  # noqa: E402
from app.quiz import CueOutput, summarize  # noqa: E402
from remote_guidance.cue_outputs import CompositeCueOutput, LedKeyCue  # noqa: E402
from remote_guidance.protocol import (  # noqa: E402
    STAGE_FINAL,
    STAGE_PROVISIONAL,
    TYPE_GUIDANCE_LIVE,
    TYPE_GUIDANCE_PRESENTED,
    TYPE_GUIDANCE_RECEIVED,
    TYPE_LATENCY_PRESENTED,
    TYPE_LATENCY_PROBE,
    TYPE_LATENCY_RECEIVED,
    TYPE_PERFORMANCE_RESPONSE,
    TYPE_RECORDING_START,
    TYPE_SESSION_FINISHED,
    TYPE_SESSION_START,
    GuidanceAction,
    guidance_payload,
)
from remote_guidance.student.session import LocalRecordingScheduler, StudentSession  # noqa: E402
from remote_guidance.timing import mono_ns, wall_ns  # noqa: E402
from server.app import create_app  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.schemas import make_envelope  # noqa: E402

PASSWORD = "a-long-enough-password"


class RecordingCue(CueOutput):
    """A cue channel with no hardware behind it."""

    def __init__(self):
        self.targets = []
        self.cleared = 0
        self.closed = 0

    def show_target(self, note, finger):
        self.targets.append((note, finger))

    def show_message(self, text):
        pass

    def clear(self):
        self.cleared += 1

    def close(self):
        self.closed += 1


class RecordingMapper:
    def __init__(self):
        self.lit = []

    def light_key(self, note):
        self.lit.append(note)
        return True

    def clear_key(self, note):
        return True


class FakeEndpoint:
    """A WebSocket endpoint driven by starlette's TestClient, with the
    same auth handshake and sequence numbering a real client uses."""

    def __init__(self, case, token: dict, room_id: str, session_id=None):
        self.case = case
        self.room_id = room_id
        self.session_id = session_id
        self.inbox = []
        self._seq = 0

        session = case.client.websocket_connect(f"/ws/v1/rooms/{room_id}")
        self.socket = session.__enter__()
        case.addCleanup(session.__exit__, None, None, None)
        self.socket.send_json(
            make_envelope("auth", str(uuid.uuid4()), room_id=room_id, payload={"access_token": token["access_token"]})
        )
        ack = self.socket.receive_json()
        assert ack["payload"]["status"] == "ok", ack
        self.socket.receive_json()  # presence

    def send(self, type_, payload=None, message_id=None):
        self._seq += 1
        envelope = make_envelope(
            type_,
            message_id or str(uuid.uuid4()),
            room_id=self.room_id,
            session_id=self.session_id,
            seq=self._seq,
            payload=payload or {},
        )
        self.socket.send_json(envelope)
        return envelope

    def receive(self, expected_type, limit=12):
        for _ in range(limit):
            frame = self.socket.receive_json()
            self.inbox.append(frame)
            if frame["type"] == expected_type:
                return frame
        raise AssertionError(f"no {expected_type} arrived; saw {[f['type'] for f in self.inbox]}")


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        directory = Path(self._dir.name)
        self.config = ServerConfig(
            bind_host="127.0.0.1",
            port=8798,
            database_path=str(directory / "relay.db"),
            jwt_private_key_path=str(directory / "jwt_private.pem"),
            jwt_public_key_path=str(directory / "jwt_public.pem"),
            source_path=directory / "server_config.json",
        )
        self.client = TestClient(create_app(self.config))
        self.client.__enter__()
        # See test_remote_guidance_server._ServerCase: cleanups run in
        # reverse, so the client registered first is closed last, after
        # every socket.
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(self.client.__exit__, None, None, None)

        self.teacher = self._register("teacher1", "teacher")
        self.student = self._register("student1", "student")
        self.room = self.client.post(
            "/api/v1/rooms", json={"name": "lesson"}, headers=self._auth(self.teacher)
        ).json()
        self.client.post(
            "/api/v1/rooms/join",
            json={"join_code": self.room["join_code"]},
            headers=self._auth(self.student),
        )
        self.room_id = self.room["room_id"]

    # -- helpers -------------------------------------------------------

    def _register(self, username, role):
        response = self.client.post(
            "/api/v1/auth/register", json={"username": username, "password": PASSWORD, "role": role}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    @staticmethod
    def _auth(token):
        return {"Authorization": f"Bearer {token['access_token']}"}

    def _open_session(self, mode="live", guidance_mode="both", **extra):
        body = {"mode": mode, "guidance_mode": guidance_mode, **extra}
        response = self.client.post(
            f"/api/v1/rooms/{self.room_id}/sessions", json=body, headers=self._auth(self.teacher)
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _student_session(self, cue, on_send, resolve_finger=None):
        return StudentSession(
            timeout_s=5.0,
            present=lambda event: cue.show_target(event.target.note, event.target.finger),
            clear_cue=cue.clear,
            send_received=lambda event, accepted: on_send(
                TYPE_GUIDANCE_RECEIVED, event.received_payload(accepted)
            ),
            send_presented=lambda event: on_send(TYPE_GUIDANCE_PRESENTED, event.presented_payload()),
            send_response=lambda event: on_send(
                TYPE_PERFORMANCE_RESPONSE, event.response_payload(STAGE_PROVISIONAL)
            ),
            resolve_finger=resolve_finger,
        )

    # ------------------------------------------------------------------

    def test_live_guidance_round_trip(self):
        """A teacher plays a key; the student cues it, plays it, and the
        teacher sees the result - through the real relay."""
        session = self._open_session()
        teacher = FakeEndpoint(self, self.teacher, self.room_id, session["session_id"])
        student = FakeEndpoint(self, self.student, self.room_id, session["session_id"])
        teacher.receive("presence")

        led, visual, haptic = RecordingMapper(), RecordingCue(), RecordingCue()
        cue = CompositeCueOutput(led=LedKeyCue(led), visual=visual, haptic=haptic, guidance_mode="both")
        student_session = self._student_session(
            cue,
            student.send,
            resolve_finger=lambda note: FingerMatch(
                note=note, key_id=1, finger="R1", point=(5, 5), distance_px=0.0, inside=True,
                probability=0.8, probabilities={"R1": 0.8, "R2": 0.2},
            ),
        )
        student_session.start()

        teacher.send(TYPE_SESSION_START, {"mode": "live", "guidance_mode": "both"})
        sent = teacher.send(
            TYPE_GUIDANCE_LIVE,
            guidance_payload([GuidanceAction(note=60, finger="R1", note_name="C4")], timeout_s=5.0),
        )

        # Student side: take it off the wire and run it through the real
        # session logic.
        arrived = student.receive(TYPE_GUIDANCE_LIVE)
        self.assertEqual(arrived["message_id"], sent["message_id"])
        student_session.accept(arrived)
        student_session.tick()
        student_session.on_note(60)

        # All three channels fired.
        self.assertEqual(led.lit, [60])
        self.assertEqual(visual.targets, [(60, "R1")])
        self.assertEqual(haptic.targets, [(60, "R1")])

        received = teacher.receive(TYPE_GUIDANCE_RECEIVED)
        self.assertTrue(received["payload"]["accepted"])
        self.assertEqual(received["payload"]["guidance_message_id"], sent["message_id"])

        presented = teacher.receive(TYPE_GUIDANCE_PRESENTED)
        timings = presented["payload"]["timings"]
        self.assertEqual(timings["timing_kind"], "software_dispatch_render")
        self.assertIsNotNone(timings["cue_ready_monotonic_ns"])

        response = teacher.receive(TYPE_PERFORMANCE_RESPONSE)
        payload = response["payload"]
        self.assertEqual(payload["stage"], STAGE_PROVISIONAL)
        self.assertTrue(payload["note_correct"])
        self.assertTrue(payload["finger_correct"])
        self.assertGreater(payload["reaction_time_ns"], 0)

    def test_reaction_time_is_not_the_teacher_to_student_delay(self):
        """The rule that matters most end to end: the number the teacher
        reads as the student's reaction time must not contain the network
        path."""
        session = self._open_session(guidance_mode="visual")
        teacher = FakeEndpoint(self, self.teacher, self.room_id, session["session_id"])
        student = FakeEndpoint(self, self.student, self.room_id, session["session_id"])
        teacher.receive("presence")

        cue = CompositeCueOutput(visual=RecordingCue(), guidance_mode="visual")
        student_session = self._student_session(cue, student.send)
        student_session.start()

        teacher.send(TYPE_GUIDANCE_LIVE, guidance_payload([GuidanceAction(note=60, finger="R1")]))
        arrived = student.receive(TYPE_GUIDANCE_LIVE)
        event = student_session.accept(arrived)
        student_session.tick()
        student_session.on_note(60)

        timings = event.timings
        reaction = event.reaction_time_ns
        self.assertEqual(reaction, timings.student_response_monotonic_ns - timings.cue_ready_monotonic_ns)

        # The transport stamps are all present and all different from it.
        self.assertIsNotNone(timings.teacher_send_wall_ns)
        self.assertIsNotNone(timings.server_receive_wall_ns)
        self.assertIsNotNone(timings.student_receive_wall_ns)
        for stamp in (
            timings.teacher_send_wall_ns,
            timings.server_receive_wall_ns,
            timings.student_receive_wall_ns,
        ):
            self.assertNotEqual(reaction, timings.student_response_wall_ns - stamp)

    def test_received_reaches_the_teacher_before_presented(self):
        session = self._open_session(guidance_mode="visual")
        teacher = FakeEndpoint(self, self.teacher, self.room_id, session["session_id"])
        student = FakeEndpoint(self, self.student, self.room_id, session["session_id"])
        teacher.receive("presence")

        cue = CompositeCueOutput(visual=RecordingCue(), guidance_mode="visual")
        student_session = self._student_session(cue, student.send)
        student_session.start()

        teacher.send(TYPE_GUIDANCE_LIVE, guidance_payload([GuidanceAction(note=60, finger="R1")]))
        student_session.accept(student.receive(TYPE_GUIDANCE_LIVE))
        student_session.tick()

        order = []
        for _ in range(2):
            frame = teacher.socket.receive_json()
            if frame["type"] in (TYPE_GUIDANCE_RECEIVED, TYPE_GUIDANCE_PRESENTED):
                order.append(frame["type"])
        self.assertEqual(order, [TYPE_GUIDANCE_RECEIVED, TYPE_GUIDANCE_PRESENTED])

    def test_queue_pressure_is_reported_not_hidden(self):
        session = self._open_session(guidance_mode="visual")
        teacher = FakeEndpoint(self, self.teacher, self.room_id, session["session_id"])
        student = FakeEndpoint(self, self.student, self.room_id, session["session_id"])
        teacher.receive("presence")

        cue = CompositeCueOutput(visual=RecordingCue(), guidance_mode="visual")
        student_session = StudentSession(
            queue_size=1,
            present=lambda event: cue.show_target(event.target.note, event.target.finger),
            send_received=lambda event, accepted: student.send(
                TYPE_GUIDANCE_RECEIVED, event.received_payload(accepted)
            ),
        )
        student_session.start()

        for note in (60, 62, 64):
            teacher.send(TYPE_GUIDANCE_LIVE, guidance_payload([GuidanceAction(note=note, finger="R1")]))
        for _ in range(3):
            student_session.accept(student.receive(TYPE_GUIDANCE_LIVE))

        acknowledgements = [teacher.receive(TYPE_GUIDANCE_RECEIVED)["payload"] for _ in range(3)]
        self.assertEqual(sum(1 for a in acknowledgements if a["accepted"]), 1)
        refused = [a for a in acknowledgements if not a["accepted"]]
        self.assertEqual(len(refused), 2)
        self.assertIn("queue is full", refused[0]["refused_reason"])

    def test_pre_recorded_sequence_is_downloaded_then_scheduled_locally(self):
        """The asynchronous mode: after the trigger, no cue waits on the
        network."""
        events = [
            {"event_order": i, "rel_time_s": i * 0.5, "duration_s": 0.4, "note": 60 + i, "finger": "R1"}
            for i in range(3)
        ]
        recording = self.client.post(
            f"/api/v1/rooms/{self.room_id}/recordings",
            json={"name": "scale", "source": "sequence", "meta": {}, "events": events},
            headers=self._auth(self.teacher),
        ).json()
        session = self._open_session(
            mode="recording", guidance_mode="visual", playback_mode="paced", recording_id=recording["recording_id"]
        )

        teacher = FakeEndpoint(self, self.teacher, self.room_id, session["session_id"])
        student = FakeEndpoint(self, self.student, self.room_id, session["session_id"])
        teacher.receive("presence")

        teacher.send(
            TYPE_RECORDING_START,
            {
                "recording_id": recording["recording_id"],
                "playback_mode": "paced",
                "start_at_unix_ns": wall_ns() + 100_000_000,
            },
        )
        trigger = student.receive(TYPE_RECORDING_START)

        # The student downloads the whole list before playing anything.
        detail = self.client.get(
            f"/api/v1/recordings/{trigger['payload']['recording_id']}", headers=self._auth(self.student)
        ).json()
        self.assertEqual(len(detail["events"]), 3)

        cue = CompositeCueOutput(visual=RecordingCue(), guidance_mode="visual")
        student_session = self._student_session(cue, student.send)
        student_session.start()
        scheduler = LocalRecordingScheduler(detail["events"], "paced", mono_ns())

        played = []
        for _ in range(3):
            due = scheduler.due(session_idle=student_session.current is None and student_session.pending == 0)
            self.assertIsNotNone(due)
            student_session.accept(
                {
                    "message_id": f"local-{due['event_order']}",
                    "seq": due["event_order"],
                    "sent_at_unix_ns": None,
                    "payload": {
                        "actions": [{"note": due["note"], "finger": due["finger"]}],
                        "timeout_s": 5.0,
                    },
                    "server": {},
                }
            )
            student_session.tick()
            played.append(student_session.current.target.note)
            student_session.on_note(due["note"])

        self.assertEqual(played, [60, 61, 62])
        self.assertTrue(scheduler.finished)
        # Locally scheduled events carry no teacher/server stamps, which
        # is exactly right: they never crossed the network.
        self.assertIsNone(student_session.events[0].timings.teacher_send_wall_ns)
        self.assertIsNotNone(student_session.events[0].timings.cue_ready_monotonic_ns)

    def test_latency_probe_answers_in_two_stages(self):
        session = self._open_session()
        teacher = FakeEndpoint(self, self.teacher, self.room_id, session["session_id"])
        student = FakeEndpoint(self, self.student, self.room_id, session["session_id"])
        teacher.receive("presence")

        sent_mono = mono_ns()
        teacher.send(TYPE_LATENCY_PROBE, {"probe_id": "p1", "note": 60, "trigger_cue": True})

        # The relay acknowledges the hop it handled.
        ack = teacher.receive("latency.ack")
        self.assertEqual(ack["payload"]["probe_id"], "p1")

        probe = student.receive(TYPE_LATENCY_PROBE)
        receive_mono = mono_ns()
        student.send(
            TYPE_LATENCY_RECEIVED,
            {
                "probe_id": probe["payload"]["probe_id"],
                "student_receive_wall_ns": wall_ns(),
                "student_receive_monotonic_ns": receive_mono,
            },
        )
        cue = CompositeCueOutput(led=LedKeyCue(RecordingMapper()), guidance_mode="visual")
        dispatch = cue.show_target(60, "R1")
        student.send(
            TYPE_LATENCY_PRESENTED,
            {
                "probe_id": probe["payload"]["probe_id"],
                "dispatch_ns": dispatch.local_dispatch_ns,
                "timing_kind": "software_dispatch_render",
            },
        )

        received = teacher.receive(TYPE_LATENCY_RECEIVED)
        rtt_ns = mono_ns() - sent_mono
        self.assertGreater(rtt_ns, 0)
        # The student's monotonic value came back untouched, and is not
        # something the teacher may subtract from its own clock.
        self.assertEqual(received["payload"]["student_receive_monotonic_ns"], receive_mono)

        presented = teacher.receive(TYPE_LATENCY_PRESENTED)
        self.assertEqual(presented["payload"]["timing_kind"], "software_dispatch_render")
        self.assertGreaterEqual(presented["payload"]["dispatch_ns"], 0)

    def test_provisional_then_final_results_are_stored_and_distinguished(self):
        session = self._open_session(guidance_mode="visual")
        session_id = session["session_id"]
        teacher = FakeEndpoint(self, self.teacher, self.room_id, session_id)
        student = FakeEndpoint(self, self.student, self.room_id, session_id)
        teacher.receive("presence")

        cue = CompositeCueOutput(visual=RecordingCue(), guidance_mode="visual")
        student_session = self._student_session(
            cue,
            student.send,
            # The live pass gets the finger wrong - the hand was half out
            # of frame, which is exactly why the offline pass exists.
            resolve_finger=lambda note: FingerMatch(
                note=note, key_id=1, finger="R3", point=(9, 9), distance_px=40.0, inside=False,
                probability=0.7, probabilities={"R3": 0.7, "R1": 0.3},
            ),
        )
        student_session.start()

        teacher.send(TYPE_GUIDANCE_LIVE, guidance_payload([GuidanceAction(note=60, finger="R1")]))
        student_session.accept(student.receive(TYPE_GUIDANCE_LIVE))
        student_session.tick()
        student_session.on_note(60)

        provisional = teacher.receive(TYPE_PERFORMANCE_RESPONSE)
        self.assertEqual(provisional["payload"]["stage"], STAGE_PROVISIONAL)
        self.assertFalse(provisional["payload"]["finger_correct"])

        # Offline pass overturns it, and the final frame says so.
        updated = student_session.apply_offline_match(
            0,
            FingerMatch(note=60, key_id=1, finger="R1", point=(1, 1), distance_px=0.0, inside=True,
                        probability=0.95, probabilities={"R1": 0.95, "R2": 0.05}),
        )
        student.send(TYPE_PERFORMANCE_RESPONSE, updated.response_payload(STAGE_FINAL))
        final = teacher.receive(TYPE_PERFORMANCE_RESPONSE)
        self.assertEqual(final["payload"]["stage"], STAGE_FINAL)
        self.assertTrue(final["payload"]["finger_correct"])

        summary = summarize(student_session.results())
        student.send(
            TYPE_SESSION_FINISHED,
            {"stage": STAGE_FINAL, "session_name": "e2e", "event_count": 1, "summary": summary},
        )
        teacher.receive(TYPE_SESSION_FINISHED)

        # Both stages reached the database, and the stored summary is
        # labelled as the student's, not the server's.
        self.client.portal.call(self.client.app.state.ctx.drain)
        stored = self.client.get(
            f"/api/v1/sessions/{session_id}/events", headers=self._auth(self.teacher)
        ).json()
        stages = [event["stage"] for event in stored["performance"]]
        self.assertIn(STAGE_PROVISIONAL, stages)
        self.assertIn(STAGE_FINAL, stages)

        stored_summary = self.client.get(
            f"/api/v1/sessions/{session_id}/summary", headers=self._auth(self.teacher)
        ).json()
        self.assertEqual(stored_summary["summary_source"], "student")
        self.assertEqual(stored_summary["student_reported_summary"]["note_accuracy"], 1.0)
        self.assertEqual(stored_summary["counts"]["final"], 1)

    def test_session_state_follows_the_control_messages(self):
        session = self._open_session()
        session_id = session["session_id"]
        teacher = FakeEndpoint(self, self.teacher, self.room_id, session_id)
        FakeEndpoint(self, self.student, self.room_id, session_id)
        teacher.receive("presence")

        self.assertEqual(
            self.client.get(f"/api/v1/sessions/{session_id}", headers=self._auth(self.teacher)).json()["state"],
            "created",
        )
        teacher.send(TYPE_SESSION_START, {"mode": "live"})
        self.client.portal.call(self.client.app.state.ctx.drain)
        self.assertEqual(
            self.client.get(f"/api/v1/sessions/{session_id}", headers=self._auth(self.teacher)).json()["state"],
            "running",
        )

        teacher.send("session.stop", {})
        self.client.portal.call(self.client.app.state.ctx.drain)
        self.assertEqual(
            self.client.get(f"/api/v1/sessions/{session_id}", headers=self._auth(self.teacher)).json()["state"],
            "stopped",
        )

    def test_events_survive_a_relay_restart(self):
        """A lesson's record must outlive the process that relayed it."""
        session = self._open_session()
        session_id = session["session_id"]
        teacher = FakeEndpoint(self, self.teacher, self.room_id, session_id)
        FakeEndpoint(self, self.student, self.room_id, session_id)
        teacher.receive("presence")

        for note in (60, 62, 64):
            teacher.send(TYPE_GUIDANCE_LIVE, guidance_payload([GuidanceAction(note=note, finger="R1")]))
        self.client.portal.call(self.client.app.state.ctx.drain)

        restarted = ServerConfig(
            bind_host=self.config.bind_host,
            port=self.config.port,
            database_path=self.config.database_path,
            jwt_private_key_path=self.config.jwt_private_key_path,
            jwt_public_key_path=self.config.jwt_public_key_path,
            source_path=self.config.source_path,
        )
        with TestClient(create_app(restarted)) as client:
            login = client.post("/api/v1/auth/login", json={"username": "teacher1", "password": PASSWORD}).json()
            events = client.get(
                f"/api/v1/sessions/{session_id}/events",
                headers={"Authorization": f"Bearer {login['access_token']}"},
            ).json()
            notes = [
                event["payload"]["actions"][0]["note"]
                for event in events["guidance"]
                if event["type"] == TYPE_GUIDANCE_LIVE
            ]
            self.assertEqual(sorted(notes), [60, 62, 64])


if __name__ == "__main__":
    unittest.main(verbosity=2)
