"""Regression checks for the usable-capacity SOC reference.

This file runs battery_simulation.py and ocv_soc.py, then checks their capacity,
SOC calculations, voltage cutoff, and rested-voltage results for consistency.
unittest.main() at the bottom automatically finds the test class and runs its
test_ methods. setUpClass() runs the shared simulations once before the tests.
Passing checks confirm code behavior; matching the real battery needs measurements.

Run from the repository root with:
    python -m unittest python.tests.test_simulation -v

Or run this file directly from an editor or terminal:
    python python/tests/test_simulation.py -v

These tests print pass/fail results in the terminal. To display a graph, run
python/simulation/ocv_soc.py or python/simulation/battery_simulation.py instead.
"""

from pathlib import Path
import sys
import unittest
import warnings
from unittest.mock import patch

import numpy as np
import pybamm

# Direct execution puts python/tests on the import path, rather than the
# repository root. Add the root so the same project imports work in both modes.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python.simulation import battery_simulation, ocv_soc


class UsableCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Run each complete experiment once, then share its results across the
        # checks. These test the actual solver output rather than invented data.
        cls.discharge = battery_simulation.run_battery_simulation()
        cls.curve = ocv_soc.generate_ocv_soc_curve()

    def test_capacity_test_reaches_voltage_cutoff(self):
        data = self.discharge
        self.assertAlmostEqual(data["voltage_v"][-1], data["minimum_voltage_v"], places=5)
        self.assertAlmostEqual(data["true_soc"][0], 1.0)
        self.assertAlmostEqual(data["true_soc"][-1], 0.0)
        self.assertEqual(data["nominal_capacity_ah"], 2.0)
        self.assertGreater(data["usable_capacity_ah"], 0)

    def test_discharge_charge_accounting(self):
        data = self.discharge
        # Independent integration of current must reproduce the reported Ah.
        charge_ah = np.trapezoid(data["current_a"], data["time_s"]) / 3600
        self.assertAlmostEqual(charge_ah, data["usable_capacity_ah"], places=7)
        np.testing.assert_allclose(
            data["true_soc"],
            1 - data["discharge_capacity_ah"] / data["usable_capacity_ah"],
            atol=1e-12,
        )

    def test_ocv_and_discharge_share_the_capacity_reference(self):
        data = self.curve
        self.assertAlmostEqual(data["usable_capacity_ah"], self.discharge["usable_capacity_ah"])
        self.assertEqual(data["nominal_capacity_ah"], 2.0)
        np.testing.assert_allclose(data["soc_points"], np.linspace(0, 1, 51), atol=1e-10)
        np.testing.assert_allclose(
            data["soc_points"],
            1 - data["removed_capacity_ah"] / self.discharge["usable_capacity_ah"],
            atol=1e-10,
        )
        self.assertAlmostEqual(data["removed_capacity_ah"][0], data["usable_capacity_ah"])
        self.assertFalse(data["stopped_at_voltage_limit"])

    def test_ocv_steps_use_reference_charge_and_settle(self):
        data = self.curve
        # The default 2% grid must remove 2% of the defined reference capacity.
        step_charge_ah = data["discharge_current_a"] * data["discharge_duration_s"] / 3600
        self.assertAlmostEqual(step_charge_ah, 0.02 * self.discharge["usable_capacity_ah"])
        self.assertTrue(np.isfinite(data["ocv_points_v"]).all())
        self.assertTrue((np.diff(data["ocv_points_v"]) > 0).all())
        self.assertTrue(data["settled_points"].all())
        self.assertTrue((data["rest_drift_v"] <= data["settling_tolerance_v"]).all())

    def test_finer_sampling_preserves_model_voltages(self):
        # Finer sampling should reveal the model's knee without moving the
        # previously sampled voltages. Compare independent solver runs at their
        # shared SOC points, allowing 1 mV for history/numerical differences.
        coarse = ocv_soc.generate_ocv_soc_curve(soc_step=0.1)
        np.testing.assert_allclose(self.curve["soc_points"][::5], coarse["soc_points"], atol=1e-10)
        np.testing.assert_allclose(self.curve["ocv_points_v"][::5], coarse["ocv_points_v"], atol=0.001, rtol=0)

    def test_time_limited_discharge_is_not_accepted_as_capacity(self):
        # Supply a real but deliberately incomplete discharge to represent a
        # capacity test ending at its time guard before the voltage cutoff.
        model, parameters, _ = battery_simulation.create_battery_configuration()
        short_solution = pybamm.Simulation(
            model,
            parameter_values=parameters,
            experiment=pybamm.Experiment([
                "Rest for 10 minutes", "Discharge at 0.4 A for 60 seconds"
            ]),
        ).solve(initial_soc=1.0)
        with patch.object(battery_simulation.pybamm, "Simulation") as simulation:
            simulation.return_value.solve.return_value = short_solution
            with self.assertRaisesRegex(RuntimeError, "did not reach"):
                battery_simulation.run_battery_simulation()

    def test_early_cutoff_keeps_a_partial_soc_range(self):
        # An overlarge reference forces cutoff before all requested SOC steps.
        # The generator must preserve actual SOC, not relabel the last point 0%.
        reference = dict(self.discharge)
        reference["usable_capacity_ah"] *= 2
        with patch.object(ocv_soc, "run_battery_simulation", return_value=reference):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                data = ocv_soc.generate_ocv_soc_curve(soc_step=0.1)
        self.assertTrue(data["stopped_at_voltage_limit"])
        self.assertGreater(data["soc_points"][0], 0)
        self.assertLess(len(data["soc_points"]), 11)
        self.assertTrue(any("voltage cutoff" in str(item.message) for item in caught))

    def test_invalid_soc_grid_is_rejected(self):
        for step in (0, -0.1, 0.3, 0.1000001, np.nan):
            with self.subTest(soc_step=step):
                with self.assertRaises(ValueError):
                    ocv_soc.generate_ocv_soc_curve(soc_step=step)


if __name__ == "__main__":
    unittest.main()
