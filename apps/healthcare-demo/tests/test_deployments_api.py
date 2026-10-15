"""The routes that add a hospital to a running consortium, and the flight they share with the pilot.

A pilot and the addition of a hospital share one :class:`SingleFlight`, so the
two never run at the same time, and a refused request says which one runs.
"""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import unittest

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.api.limits import SingleFlight  # noqa: E402


class SingleFlightTests(unittest.TestCase):
    def test_one_holder_at_a_time_and_its_name(self) -> None:
        flight = SingleFlight()
        self.assertFalse(flight.busy)
        self.assertIsNone(flight.holder)
        self.assertTrue(flight.try_enter("pilot"))
        self.assertTrue(flight.busy)
        self.assertEqual("pilot", flight.holder)
        self.assertFalse(flight.try_enter("deployment"))
        self.assertEqual("pilot", flight.holder)
        flight.leave()
        self.assertFalse(flight.busy)
        self.assertIsNone(flight.holder)
        self.assertTrue(flight.try_enter("deployment"))
        self.assertEqual("deployment", flight.holder)
        flight.leave()

    def test_the_holder_defaults_to_no_name(self) -> None:
        flight = SingleFlight()
        self.assertTrue(flight.try_enter())
        self.assertEqual("", flight.holder)
        flight.leave()

    def test_two_threads_never_both_enter(self) -> None:
        flight = SingleFlight()
        entered: list[str] = []
        start = threading.Barrier(8)

        def attempt(name: str) -> None:
            start.wait()
            if flight.try_enter(name):
                entered.append(name)

        threads = [threading.Thread(target=attempt, args=(f"caller-{n}",)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, len(entered))
        self.assertEqual(entered[0], flight.holder)
        flight.leave()


class PilotBusyMessageTests(unittest.TestCase):
    """The pilot route names what holds the flight."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(main.app)

    def refused(self, holder: str) -> dict:
        self.assertTrue(main.pilot_flight.try_enter(holder))
        try:
            response = self.client.post("/api/v1/pilot:run", json={"round_prefix": "ui"})
        finally:
            main.pilot_flight.leave()
        self.assertEqual(409, response.status_code, response.text)
        return response.json()

    def test_a_running_pilot(self) -> None:
        self.assertEqual("A pilot is already running. Try again when it finishes.", self.refused("pilot")["detail"])

    def test_a_hospital_being_added(self) -> None:
        self.assertEqual(
            "A hospital is being added. Run the pilot when it has joined.", self.refused("deployment")["detail"]
        )

    def test_a_holder_that_left_reads_as_a_pilot(self) -> None:
        """The holder is read after the refusal; if it left, the pilot's own message is used."""
        self.assertTrue(main.pilot_flight.try_enter("deployment"))
        main.pilot_flight._holder = None  # the holder left between the refusal and the read
        try:
            response = self.client.post("/api/v1/pilot:run", json={"round_prefix": "ui"})
        finally:
            main.pilot_flight.leave()
        self.assertEqual("A pilot is already running. Try again when it finishes.", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
