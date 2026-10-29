"""Check evaluation-profile charge balance, initialization, and load switches.

These checks use actual PyBaMM solutions. The profile data is independent of
the pulse data used to identify R0/R1/C1; it is not a battery accuracy claim.
Run: python3.13 -m unittest python.tests.test_profiles -v
"""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python.simulation import profiles
from python.simulation.battery_simulation import create_battery_configuration, run_battery_simulation


class EvaluationProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = run_battery_simulation()
        cls.results = {
            name: profiles.run_test_profile(name, reference_data=cls.reference)
            for name in profiles.PROFILE_STEPS
        }
        cls.temperature_grid = {
            (soc, temperature): (
                cls.results["changing_load"] if (soc, temperature) == (0.5, 25)
                else profiles.run_test_profile(
                    "changing_load", target_soc=soc,
                    ambient_temperature_c=temperature, reference_data=cls.reference,
                )
            )
            for soc in (0.1, 0.5, 0.9)
            for temperature in (15, 25, 35)
        }

    def test_profiles_start_from_settled_half_soc_and_omit_conditioning(self):
        for name, data in self.results.items():
            with self.subTest(profile=name):
                self.assertEqual(data["time_s"][0], 0)
                self.assertAlmostEqual(data["initial_soc"], 0.5)
                self.assertAlmostEqual(data["discharge_capacity_ah"][0], 0.5 * self.reference["usable_capacity_ah"])
                self.assertAlmostEqual(data["time_s"][-1], sum(duration for _, duration in profiles.PROFILE_STEPS[name]), places=6)
                baseline = data["phase"][:data["step_end_indices"][0] + 1]
                self.assertTrue((baseline == "rest").all())
                self.assertLess(np.ptp(data["voltage_v"][:len(baseline)]), 1e-6)
                self.assertTrue((np.diff(data["time_s"]) > 0).all())
                for key in ("current_a", "voltage_v", "temperature_c", "true_soc", "discharge_capacity_ah"):
                    self.assertEqual(data[key].shape, data["time_s"].shape)
                    self.assertTrue(np.isfinite(data[key]).all())

    def test_integrated_current_matches_capacity_and_soc(self):
        for name, data in self.results.items():
            with self.subTest(profile=name):
                expected_ah = sum(current * duration / 3600 for current, duration in profiles.PROFILE_STEPS[name])
                measured_ah = data["discharge_capacity_ah"][-1] - data["discharge_capacity_ah"][0]
                integrated_ah = np.trapezoid(data["current_a"], data["time_s"]) / 3600
                self.assertAlmostEqual(measured_ah, expected_ah, places=9)
                self.assertAlmostEqual(integrated_ah, expected_ah, places=9)
                np.testing.assert_allclose(data["true_soc"], 1 - data["discharge_capacity_ah"] / data["usable_capacity_ah"], atol=1e-12)

    def test_every_switch_preserves_before_and_after_samples(self):
        for name, data in self.results.items():
            with self.subTest(profile=name):
                for step_number, ((current, _), start, end) in enumerate(zip(
                    profiles.PROFILE_STEPS[name], data["step_start_indices"], data["step_end_indices"]
                )):
                    np.testing.assert_allclose(data["current_a"][start:end + 1], current, atol=1e-12)
                    if step_number:
                        self.assertLess(data["time_s"][start] - data["time_s"][start - 1], 1e-6)
                        self.assertNotEqual(data["current_a"][start], data["current_a"][start - 1])

    def test_negative_current_increases_reference_soc(self):
        data = self.results["charge_discharge"]
        indices = np.flatnonzero(data["phase"] == "charge")
        first, last = indices[0], indices[-1]
        increase = data["true_soc"][last] - data["true_soc"][first]
        self.assertGreater(increase, 0)
        self.assertAlmostEqual(increase, 0.2 * 180 / (3600 * data["usable_capacity_ah"]), places=10)

    def test_ocv_reference_metadata_is_accepted_without_current_key(self):
        reference = {key: self.reference[key] for key in ("usable_capacity_ah", "soc_reference")}
        result = profiles.run_test_profile(reference_data=reference)
        self.assertAlmostEqual(result["initial_soc"], 0.5)
        self.assertEqual(result["reference_capacity_temperature_c"], 25)
        np.testing.assert_allclose(result["true_soc"], self.results["changing_load"]["true_soc"], atol=1e-12)

    def test_configuration_sets_initial_and_ambient_temperature_together(self):
        for temperature in (0, 15, 25, 35, 45):
            with self.subTest(temperature=temperature):
                _, parameters, specs = create_battery_configuration(ambient_temperature_c=temperature)
                self.assertEqual(parameters["Initial temperature [K]"], temperature + 273.15)
                self.assertEqual(parameters["Ambient temperature [K]"], temperature + 273.15)
                self.assertEqual(specs["initial_temperature_c"], temperature)
                self.assertEqual(specs["ambient_temperature_c"], temperature)
        _, parameters, _ = create_battery_configuration()
        self.assertEqual(parameters["Initial temperature [K]"], 298.15)
        self.assertEqual(parameters["Ambient temperature [K]"], 298.15)

    def test_soc_temperature_grid_keeps_reference_capacity_and_charge_balance(self):
        expected_ah = sum(current * duration / 3600 for current, duration in profiles.PROFILE_STEPS["changing_load"])
        duration_s = sum(duration for _, duration in profiles.PROFILE_STEPS["changing_load"])
        for (soc, temperature), data in self.temperature_grid.items():
            with self.subTest(soc=soc, temperature=temperature):
                self.assertAlmostEqual(data["initial_soc"], soc, places=12)
                self.assertEqual(data["usable_capacity_ah"], self.reference["usable_capacity_ah"])
                self.assertEqual(data["soc_reference"], self.reference["soc_reference"])
                self.assertEqual(data["reference_capacity_temperature_c"], 25)
                self.assertEqual(data["initial_temperature_c"], temperature)
                self.assertEqual(data["ambient_temperature_c"], temperature)
                # Conditioning may warm the cell; the following one-hour rest
                # returns it close to ambient, rather than resetting its state.
                self.assertAlmostEqual(data["temperature_c"][0], temperature, delta=1e-3)
                self.assertAlmostEqual(data["time_s"][-1], duration_s, places=6)
                self.assertEqual(len(data["step_start_indices"]), len(profiles.PROFILE_STEPS["changing_load"]))
                self.assertAlmostEqual(np.trapezoid(data["current_a"], data["time_s"]) / 3600, expected_ah, places=9)
                self.assertAlmostEqual(data["true_soc"][-1], soc - expected_ah / self.reference["usable_capacity_ah"], places=12)
                np.testing.assert_allclose(data["true_soc"], 1 - data["discharge_capacity_ah"] / self.reference["usable_capacity_ah"], atol=1e-12)
                for key in ("time_s", "current_a", "voltage_v", "temperature_c", "true_soc"):
                    self.assertTrue(np.isfinite(data[key]).all())
                self.assertTrue((data["true_soc"] > 0).all())
                self.assertTrue((data["true_soc"] < 1).all())
                self.assertGreater(data["voltage_v"].min(), data["minimum_voltage_v"])
                self.assertLess(data["voltage_v"].max(), data["maximum_voltage_v"])

    def test_temperature_changes_cell_response_without_changing_soc_scale(self):
        for soc in (0.1, 0.5, 0.9):
            with self.subTest(soc=soc):
                cold = self.temperature_grid[soc, 15]
                warm = self.temperature_grid[soc, 35]
                np.testing.assert_allclose(cold["true_soc"], warm["true_soc"], atol=1e-12)
                self.assertGreater(np.max(np.abs(cold["voltage_v"] - warm["voltage_v"])), 1e-5)
                self.assertGreater(np.min(warm["temperature_c"] - cold["temperature_c"]), 19)

    def test_default_capacity_reference_stays_at_25_c_for_temperature_evaluation(self):
        with patch.object(profiles, "run_battery_simulation", return_value=self.reference) as capacity_test:
            data = profiles.run_test_profile(ambient_temperature_c=35)
        capacity_test.assert_called_once_with()
        self.assertEqual(data["ambient_temperature_c"], 35)
        self.assertEqual(data["reference_capacity_temperature_c"], 25)
        self.assertEqual(data["usable_capacity_ah"], self.reference["usable_capacity_ah"])

    def test_invalid_temperature_is_rejected_before_solving(self):
        for temperature in (-0.01, 45.01, np.nan, np.inf, -np.inf, None, True):
            with self.subTest(temperature=temperature):
                with self.assertRaisesRegex(ValueError, "ambient_temperature_c"):
                    create_battery_configuration(ambient_temperature_c=temperature)
                with self.assertRaisesRegex(ValueError, "ambient_temperature_c"):
                    profiles.run_test_profile(ambient_temperature_c=temperature, reference_data=self.reference)

    def test_invalid_settings_and_exhaustion_are_rejected(self):
        cases = (
            {"profile_name": "unknown"}, {"target_soc": 0}, {"target_soc": 1.1},
            {"target_soc": np.nan}, {"sample_period_s": 0}, {"sample_period_s": np.inf},
            {"sample_period_s": 61}, {"pre_rest_duration_s": 3599},
            {"pre_rest_duration_s": np.nan}, {"target_soc": 0.001},
            {"reference_data": {"usable_capacity_ah": 0}},
        )
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    profiles.run_test_profile(**settings)

    def test_early_conditioning_cutoff_is_not_accepted_as_target_soc(self):
        reference = dict(self.reference)
        reference["usable_capacity_ah"] *= 4
        with self.assertRaisesRegex(RuntimeError, "Conditioning reached a voltage limit"):
            profiles.run_test_profile(reference_data=reference)

    def test_early_evaluation_cutoff_is_not_reported_as_complete(self):
        # Enough reference charge remains, but a large internal voltage drop
        # terminates this deliberately stressful profile before its 30 seconds.
        with patch.dict(profiles.PROFILE_STEPS, {"cutoff_fixture": ((0, 60), (4, 30), (0, 60))}):
            for temperature in (15, 25, 35):
                with self.subTest(temperature=temperature):
                    with self.assertRaisesRegex(RuntimeError, "Evaluation profile reached a voltage limit"):
                        profiles.run_test_profile(
                            "cutoff_fixture", target_soc=0.03,
                            ambient_temperature_c=temperature, reference_data=self.reference,
                        )


if __name__ == "__main__":
    unittest.main()
