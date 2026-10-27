"""Regression checks for the usable-capacity SOC reference.

This file runs the discharge, OCV, and pulse simulations, then checks their
capacity, SOC calculations, voltage cutoff, and voltage results for consistency.
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

from python.simulation import battery_simulation, ocv_soc, pulse_test
from python.estimation.ekf import estimate_soc_ekf
from python.estimation.kalman_filter import estimate_soc_kalman_filter


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

    def test_generated_models_work_with_existing_filters(self):
        # A rested battery at constant SOC has zero current and no polarization.
        # This checks both existing filter interfaces without inventing R0/R1/C1
        # for a loaded-cell validation. The RC values here are test fixtures only.
        data = self.curve
        linear = ocv_soc.fit_linear_ocv_model(data["soc_points"], data["ocv_points_v"])
        ocv, derivative = ocv_soc.build_nonlinear_ocv_model(
            data["soc_points"], data["ocv_points_v"]
        )
        np.testing.assert_allclose(ocv(data["soc_points"]), data["ocv_points_v"], atol=1e-12)
        for soc in (0.0, 0.5, 1.0):
            time_s = [0.0, 1.0, 2.0]
            current_a = np.zeros(3)
            kf = estimate_soc_kalman_filter(
                time_s, current_a,
                np.full(3, linear["ocv_intercept_v"] + linear["ocv_slope_v_per_soc"] * soc),
                nominal_capacity_ah=data["usable_capacity_ah"], initial_soc=soc,
                ocv_at_empty_v=linear["ocv_at_empty_v"],
                ocv_at_full_v=linear["ocv_at_full_v"],
            )
            ekf = estimate_soc_ekf(
                time_s, current_a, np.full(3, ocv(soc)),
                nominal_capacity_ah=data["usable_capacity_ah"], initial_soc=soc,
                ocv_function=ocv, ocv_derivative_function=derivative,
                r0_ohm=0.01, r1_ohm=0.02, c1_f=1000.0,
            )
            np.testing.assert_allclose(kf, soc, atol=1e-12)
            np.testing.assert_allclose(ekf, soc, atol=1e-12)


class PulseTestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = pulse_test.run_pulse_test()

    def test_pulse_uses_the_reference_soc_and_preserves_switch_samples(self):
        data = self.data
        self.assertAlmostEqual(data["initial_soc"], 0.5)
        self.assertAlmostEqual(data["discharge_capacity_ah"][0], 0.5 * data["usable_capacity_ah"])
        np.testing.assert_allclose(
            data["true_soc"], 1 - data["discharge_capacity_ah"] / data["usable_capacity_ah"],
            atol=1e-12,
        )
        self.assertEqual(data["time_s"][0], 0)
        self.assertAlmostEqual(data["time_s"][-1], 3600 + 60 + 1800)
        self.assertTrue((np.diff(data["time_s"]) > 0).all())
        for name in ("current_a", "voltage_v", "temperature_c", "true_soc", "discharge_capacity_ah"):
            self.assertEqual(data[name].shape, data["time_s"].shape)
            self.assertTrue(np.isfinite(data[name]).all())
        for index in (data["pulse_start_index"], data["recovery_start_index"]):
            # Both sides of the switch must survive; one is at zero current
            # and the other under load, at effectively the same physical time.
            self.assertLess(data["time_s"][index] - data["time_s"][index - 1], 1e-6)
            self.assertNotEqual(data["current_a"][index], data["current_a"][index - 1])

    def test_pulse_charge_balance_and_rest_currents(self):
        data = self.data
        for phase, expected_current in (("pre_rest", 0), ("pulse", 0.4), ("recovery", 0)):
            mask = data["phase"] == phase
            np.testing.assert_allclose(data["current_a"][mask], expected_current, atol=1e-12)
            if phase != "pulse":
                self.assertLess(np.ptp(data["discharge_capacity_ah"][mask]), 1e-10)
        # The pulse is one minute at 0.4 A, independently predicting its Ah.
        expected_charge_ah = 0.4 / 60
        removed_ah = data["discharge_capacity_ah"][-1] - data["discharge_capacity_ah"][0]
        self.assertAlmostEqual(removed_ah, expected_charge_ah, places=9)
        self.assertAlmostEqual(
            np.trapezoid(data["current_a"], data["time_s"]) / 3600,
            expected_charge_ah, places=9,
        )
        self.assertAlmostEqual(data["true_soc"][-1], 0.5 - expected_charge_ah / data["usable_capacity_ah"])

    def test_pulse_voltage_drop_and_recovery(self):
        data = self.data
        voltage = data["voltage_v"]
        start, recovery = data["pulse_start_index"], data["recovery_start_index"]
        self.assertLess(voltage[start], voltage[start - 1])
        self.assertLess(voltage[recovery - 1], voltage[start])
        self.assertGreater(voltage[recovery], voltage[recovery - 1])
        self.assertGreater(voltage[-1], voltage[recovery])
        # Charge was removed, so the final rested voltage need not return to
        # the original rested value even when the transient has relaxed.
        self.assertLess(voltage[-1], voltage[start - 1])

    def test_invalid_pulse_settings_are_rejected(self):
        invalid_settings = (
            {"target_soc": 0}, {"target_soc": 1.01}, {"target_soc": np.nan},
            {"pulse_current_a": 0}, {"pulse_current_a": -0.4},
            {"pulse_current_a": 4.1}, {"pulse_current_a": np.inf},
            {"pulse_duration_s": 0}, {"pre_rest_duration_s": -1},
            {"recovery_duration_s": np.nan}, {"sample_period_s": 0},
            {"sample_period_s": 61}, {"target_soc": 0.001},
        )
        for settings in invalid_settings:
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    pulse_test.run_pulse_test(**settings)

    def test_voltage_cutoff_cannot_be_reported_as_a_complete_test(self):
        # This pulse has sufficient reference charge available, but its large
        # voltage drop makes the real solver reach cutoff before 30 seconds.
        with self.assertRaisesRegex(RuntimeError, "pulse reached a voltage limit"):
            pulse_test.run_pulse_test(target_soc=0.03, pulse_current_a=4.0, pulse_duration_s=30)

        # An inflated reference also makes conditioning reach cutoff before
        # the requested charge has been removed. Detect that stage separately.
        reference = {
            "usable_capacity_ah": 4 * self.data["usable_capacity_ah"],
            "discharge_current_a": 0.4,
        }
        with patch.object(pulse_test, "run_battery_simulation", return_value=reference):
            with self.assertRaisesRegex(RuntimeError, "Conditioning reached a voltage limit"):
                pulse_test.run_pulse_test()

    def test_pulse_can_start_from_full_without_conditioning(self):
        data = pulse_test.run_pulse_test(target_soc=1.0)
        self.assertAlmostEqual(data["initial_soc"], 1.0)
        self.assertAlmostEqual(data["discharge_capacity_ah"][0], 0.0)
        self.assertAlmostEqual(data["true_soc"][-1], 1 - (0.4 / 60) / data["usable_capacity_ah"])


class OCVModelTests(unittest.TestCase):
    """Small independent examples check the fit, interpolation, and derivative."""

    def test_linear_fit_matches_known_line(self):
        model = ocv_soc.fit_linear_ocv_model([0, 0.25, 0.75, 1], [3.2, 3.4, 3.8, 4.0])
        self.assertAlmostEqual(model["ocv_intercept_v"], 3.2)
        self.assertAlmostEqual(model["ocv_slope_v_per_soc"], 0.8)
        self.assertAlmostEqual(model["ocv_at_empty_v"], 3.2)
        self.assertAlmostEqual(model["ocv_at_full_v"], 4.0)
        self.assertLess(model["max_error_v"], 1e-12)

    def test_linear_fit_uses_all_samples_not_just_endpoints(self):
        # For this three-point example the least-squares line is 3.1 + 1.0*SOC.
        # Its errors are +0.1, -0.2, +0.1 V, rather than zero at both endpoints.
        model = ocv_soc.fit_linear_ocv_model([0, 0.5, 1], [3.0, 3.8, 4.0])
        self.assertAlmostEqual(model["ocv_at_empty_v"], 3.1)
        self.assertAlmostEqual(model["ocv_at_full_v"], 4.1)
        self.assertAlmostEqual(model["rmse_v"], np.sqrt(0.02))
        self.assertAlmostEqual(model["max_error_v"], 0.2)

    def test_nonlinear_lookup_preserves_points_and_shape(self):
        soc = np.array([0, 0.1, 0.3, 0.8, 1])
        voltage = np.array([3.0, 3.45, 3.55, 3.7, 4.2])
        ocv, derivative = ocv_soc.build_nonlinear_ocv_model(soc, voltage)
        np.testing.assert_allclose(ocv(soc), voltage, atol=1e-12)
        for index in range(len(soc) - 1):
            query = np.linspace(soc[index], soc[index + 1], 101)
            values = ocv(query)
            self.assertTrue((values >= voltage[index] - 1e-12).all())
            self.assertTrue((values <= voltage[index + 1] + 1e-12).all())
            self.assertTrue((derivative(query) >= -1e-12).all())

    def test_derivative_agrees_with_voltage_changes(self):
        ocv, derivative = ocv_soc.build_nonlinear_ocv_model(
            [0, 0.1, 0.3, 0.8, 1], [3.0, 3.45, 3.55, 3.7, 4.2]
        )
        step = 1e-6
        for soc in (0.0, 0.05, 0.1, 0.3, 0.5, 0.8, 0.9, 1.0):
            # Use one-sided differences at endpoints to stay inside the table.
            if soc == 0:
                numerical = (-3 * ocv(0) + 4 * ocv(step) - ocv(2 * step)) / (2 * step)
            elif soc == 1:
                numerical = (3 * ocv(1) - 4 * ocv(1 - step) + ocv(1 - 2 * step)) / (2 * step)
            else:
                numerical = (ocv(soc + step) - ocv(soc - step)) / (2 * step)
            self.assertAlmostEqual(float(derivative(soc)), float(numerical), delta=1e-4)

    def test_invalid_or_partial_samples_are_rejected(self):
        invalid_samples = (
            ([], []), ([0], [3]), ([[0, 1]], [[3, 4]]),
            ([0, 1], [3]), ([0, np.nan, 1], [3, 3.5, 4]),
            ([0, 1], [3, np.inf]), ([0, 0.5, 0.5, 1], [3, 3.5, 3.6, 4]),
            ([0, 0.8, 0.2, 1], [3, 3.8, 3.2, 4]),
            ([0.1, 1], [3, 4]), ([0, 0.9], [3, 4]),
            ([0, 50, 100], [3, 3.5, 4]),
        )
        for builder in (ocv_soc.fit_linear_ocv_model, ocv_soc.build_nonlinear_ocv_model):
            for soc, voltage in invalid_samples:
                with self.subTest(builder=builder.__name__, soc=soc):
                    with self.assertRaises(ValueError):
                        builder(soc, voltage)
        with self.assertRaisesRegex(ValueError, "positive"):
            ocv_soc.fit_linear_ocv_model([0, 1], [4, 3])
        with self.assertRaisesRegex(ValueError, "positive"):
            ocv_soc.fit_linear_ocv_model([0, 0.5, 1], [3.7, 3.7, 3.7])

    def test_lookup_rejects_extrapolation_and_nonfinite_soc(self):
        ocv, derivative = ocv_soc.build_nonlinear_ocv_model([0, 1], [3, 4])
        for function in (ocv, derivative):
            for soc in (-0.01, 1.01, np.nan, np.inf, [0.5, 1.01]):
                with self.subTest(soc=soc):
                    with self.assertRaises(ValueError):
                        function(soc)

    def test_endpoint_roundoff_is_tolerated_without_changing_inputs(self):
        soc = np.array([1e-12, 0.5, 1 - 1e-12])
        original = soc.copy()
        ocv, derivative = ocv_soc.build_nonlinear_ocv_model(soc, [3, 3.5, 4])
        np.testing.assert_allclose(ocv([0, 1]), [3, 4], atol=1e-12)
        self.assertTrue(np.isfinite(derivative([0, 1])).all())
        np.testing.assert_array_equal(soc, original)


if __name__ == "__main__":
    unittest.main()
