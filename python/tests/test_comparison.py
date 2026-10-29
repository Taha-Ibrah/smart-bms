"""Check comparison metrics and the shared PyBaMM-to-estimator pipeline.

Small examples with known errors verify units, timing, convergence, and drift.
The integration checks replay one real profile, without requiring any estimator
to win: accuracy is a measured result, rather than a condition built into tests.

Run from the repository root with either:
    python -m unittest python.tests.test_comparison -v
    python python/tests/test_comparison.py -v
"""

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import matplotlib.pyplot as plt
import numpy as np

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python.evaluation import compare_estimators
from python.database.database import load_comparison, save_comparison
from python.simulation.sensor_noise import add_sensor_noise
from python.visualization.plots import plot_estimator_comparison


class SOCMetricTests(unittest.TestCase):
    def metrics(self, time_s, error_pp, **settings):
        # Error examples are written in percentage points for readability, but
        # the public function accepts SOC fractions like the actual estimators.
        reference_soc = np.full(len(time_s), 0.5)
        estimated_soc = reference_soc + np.asarray(error_pp) / 100
        return compare_estimators.calculate_soc_metrics(
            time_s, reference_soc, estimated_soc, **settings
        )

    def test_constant_signed_error_has_known_magnitude_and_zero_drift(self):
        result = self.metrics([100, 200, 500, 1000], [-3, -3, -3, -3])
        self.assertAlmostEqual(result["rmse_pp"], 3)
        self.assertAlmostEqual(result["max_error_pp"], 3)
        self.assertAlmostEqual(result["initial_error_pp"], -3)
        self.assertAlmostEqual(result["final_error_pp"], -3)
        self.assertAlmostEqual(result["drift_pp_per_hour"], 0)
        self.assertIsNone(result["convergence_time_s"])

    def test_rmse_is_time_weighted_for_nonuniform_samples(self):
        result = self.metrics([0, 1, 10], [0, 2, 4])
        # Integrating squared error gives (0+4)/2*1 + (4+16)/2*9
        # = 92 pp^2 seconds. Divide by the full 10-second span, then sqrt.
        self.assertAlmostEqual(result["rmse_pp"], np.sqrt(9.2))
        self.assertAlmostEqual(result["max_error_pp"], 4)

    def test_duplicate_switch_times_are_allowed_without_extra_duration(self):
        result = self.metrics([0, 1, 1, 3], [0, 1, 2, 3])
        # The instantaneous switch contributes no elapsed time of its own.
        self.assertAlmostEqual(result["rmse_pp"], np.sqrt(4.5))
        self.assertAlmostEqual(result["max_error_pp"], 3)

    def test_linear_error_has_exact_drift_slope_with_interpolated_window(self):
        time_s = [0, 30, 180, 350, 600]
        error_pp = 1.5 + 0.002 * np.asarray(time_s)
        result = self.metrics(time_s, error_pp, drift_window_s=300)
        # The requested tail starts at 300 seconds, between recorded samples.
        self.assertAlmostEqual(result["drift_pp_per_hour"], 7.2)

    def test_drift_uses_the_tail_instead_of_initial_soc_correction(self):
        result = self.metrics(
            [0, 50, 100, 200, 400, 600], [20, 10, 0, 1, 3, 5],
            drift_window_s=300,
        )
        # The last 300 seconds follow error_pp = 0.01*time_s - 1.
        # The earlier correction from +20 pp must not dominate this rate.
        self.assertAlmostEqual(result["drift_pp_per_hour"], 36)

    def test_drift_regression_weights_irregular_samples_by_elapsed_time(self):
        result = self.metrics([0, 1, 3], [0, 0, 3], drift_window_s=10)
        # Trapezoidal weights are 0.5, 1.5, and 1 second. The weighted
        # covariance is 4.5 and variance 3.75, giving 1.2 pp/second.
        self.assertAlmostEqual(result["drift_pp_per_hour"], 4320)

    def test_drift_includes_an_interpolated_tail_boundary(self):
        result = self.metrics([0, 2, 5], [0, 2, 2], drift_window_s=4)
        # The tail is (t,error) = (1,1), (2,2), (5,2). With time weights
        # 0.5, 2, 1.5, covariance/variance = 1/10 pp/second.
        self.assertAlmostEqual(result["drift_pp_per_hour"], 360)

    def test_convergence_ignores_an_early_short_lived_visit_inside_band(self):
        result = self.metrics(
            [10, 20, 40, 50, 60, 90, 130], [4, 1, 1, 4, 1, 1, 1],
            convergence_tolerance_pp=2, convergence_hold_s=60,
        )
        # The first qualifying uninterrupted run starts at t=60, or 50 seconds
        # after the first sample. Its hold requirement is verified at t=130.
        self.assertEqual(result["convergence_time_s"], 50)

    def test_convergence_is_none_when_the_remaining_record_is_too_short(self):
        result = self.metrics(
            [0, 30, 60], [4, 1, 1], convergence_hold_s=60
        )
        self.assertIsNone(result["convergence_time_s"])

    def test_zero_hold_reports_first_in_band_sample_in_elapsed_seconds(self):
        result = self.metrics(
            [100, 110, 150], [4, 2, 0],
            convergence_tolerance_pp=2, convergence_hold_s=0,
        )
        self.assertEqual(result["convergence_time_s"], 10)

    def test_an_already_converged_record_starts_at_zero_elapsed_time(self):
        result = self.metrics([100, 140, 200], [0, 0, 0])
        self.assertEqual(result["convergence_time_s"], 0)

    def test_invalid_measurement_arrays_are_rejected(self):
        cases = (
            ([], [], []),
            ([0], [0.5], [0.5]),
            ([0, 0], [0.5, 0.5], [0.5, 0.5]),
            ([1, 0], [0.5, 0.5], [0.5, 0.5]),
            ([0, 1], [0.5], [0.5, 0.5]),
            ([[0, 1]], [0.5, 0.5], [0.5, 0.5]),
            ([0, 1], [[0.5, 0.5]], [0.5, 0.5]),
            ([0, 1], [0.5, 0.5], [[0.5, 0.5]]),
            ([0, np.inf], [0.5, 0.5], [0.5, 0.5]),
            ([0, 1], [np.nan, 0.5], [0.5, 0.5]),
            ([0, 1], [0.5, 0.5], [0.5, np.inf]),
        )
        for index, arrays in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    compare_estimators.calculate_soc_metrics(*arrays)

    def test_invalid_metric_settings_are_rejected(self):
        cases = (
            {"convergence_tolerance_pp": 0},
            {"convergence_tolerance_pp": -1},
            {"convergence_tolerance_pp": np.nan},
            {"convergence_tolerance_pp": np.inf},
            {"convergence_hold_s": -1},
            {"convergence_hold_s": np.nan},
            {"convergence_hold_s": np.inf},
            {"drift_window_s": 0},
            {"drift_window_s": -1},
            {"drift_window_s": np.nan},
            {"drift_window_s": np.inf},
        )
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    self.metrics([0, 100], [0, 0], **settings)


class SensorNoiseConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.clean = {
            "voltage_v": np.array([3.7, 3.6, 3.5]),
            "current_a": np.array([0.0, 0.2, -0.1]),
            "temperature_c": np.array([25.0, 25.1, 25.2]),
        }

    def test_zero_noise_adds_only_configured_bias_and_records_settings(self):
        result = add_sensor_noise(
            self.clean, seed=7,
            voltage_noise_std_v=0, voltage_bias_v=-0.003,
            current_noise_std_a=0, current_bias_a=0.05,
            temp_noise_std_c=0, temp_bias_c=0.4,
        )
        for measured, clean, bias in (
            ("measured_voltage_v", "voltage_v", -0.003),
            ("measured_current_a", "current_a", 0.05),
            ("measured_temp_c", "temperature_c", 0.4),
        ):
            np.testing.assert_allclose(result[measured], self.clean[clean] + bias, atol=1e-14)
        self.assertEqual(result["noise_settings"]["seed"], 7)
        self.assertEqual(result["noise_settings"]["current_bias_a"], 0.05)
        self.assertEqual(result["noise_settings"]["voltage_noise_std_v"], 0)

    def test_fixed_seed_is_repeatable_and_input_arrays_are_preserved(self):
        original = {name: values.copy() for name, values in self.clean.items()}
        first = add_sensor_noise(self.clean, seed=42)
        second = add_sensor_noise(self.clean, seed=42)
        for name in ("measured_voltage_v", "measured_current_a", "measured_temp_c"):
            np.testing.assert_array_equal(first[name], second[name])
        for name, expected in original.items():
            np.testing.assert_array_equal(self.clean[name], expected)

    def test_invalid_noise_settings_and_seeds_are_rejected(self):
        cases = (
            {"voltage_noise_std_v": -0.001},
            {"current_noise_std_a": -0.001},
            {"temp_noise_std_c": -0.1},
            {"voltage_bias_v": np.inf},
            {"current_bias_a": np.nan},
            {"temp_bias_c": np.inf},
            {"seed": -1}, {"seed": 1.5}, {"seed": True},
        )
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    add_sensor_noise(self.clean, **settings)


class ComparisonPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Run real simulations and estimators once. Wrapping the estimator
        # functions records their inputs without replacing their calculations.
        with (
            patch.object(
                compare_estimators, "estimate_soc_coulomb_counting",
                wraps=compare_estimators.estimate_soc_coulomb_counting,
            ) as counter,
            patch.object(
                compare_estimators, "estimate_soc_kalman_filter",
                wraps=compare_estimators.estimate_soc_kalman_filter,
            ) as kf,
            patch.object(
                compare_estimators, "estimate_soc_ekf",
                wraps=compare_estimators.estimate_soc_ekf,
            ) as ekf,
        ):
            cls.result = compare_estimators.run_comparison(
                profile_names=("changing_load",), seed=42
            )
        cls.estimator_calls = {
            "Coulomb counting": counter.call_args_list,
            "KF": kf.call_args_list,
            "EKF": ekf.call_args_list,
        }
        cls.scenarios = {
            scenario["scenario_name"]: scenario
            for scenario in cls.result["scenarios"]
        }

    def test_comparison_returns_three_scenarios_and_their_settings(self):
        self.assertEqual(
            set(self.scenarios), {"nominal", "wrong_initial_soc", "current_bias"}
        )
        self.assertIn("metric_settings", self.result)
        self.assertIn("estimator_settings", self.result)
        self.assertIn("fit", self.result["characterization"])
        self.assertIn("ocv_curve", self.result["characterization"])

    def test_all_estimates_are_finite_bounded_and_aligned_to_measurements(self):
        for scenario_name, scenario in self.scenarios.items():
            data = scenario["data"]
            time_s = data["time_s"]
            self.assertGreater(time_s.size, 2)
            self.assertGreater(time_s[-1] - time_s[0], 0)
            self.assertTrue((np.diff(time_s) >= 0).all())
            self.assertEqual(
                set(scenario["estimates"]), {"Coulomb counting", "KF", "EKF"}
            )
            self.assertEqual(set(scenario["metrics"]), set(scenario["estimates"]))
            for name, estimated_soc in scenario["estimates"].items():
                with self.subTest(scenario=scenario_name, estimator=name):
                    self.assertEqual(estimated_soc.shape, time_s.shape)
                    self.assertTrue(np.isfinite(estimated_soc).all())
                    self.assertTrue(((estimated_soc >= 0) & (estimated_soc <= 1)).all())
                    metrics = scenario["metrics"][name]
                    for key in (
                        "rmse_pp", "max_error_pp", "initial_error_pp",
                        "final_error_pp", "drift_pp_per_hour",
                    ):
                        self.assertTrue(np.isfinite(metrics[key]), key)
                    self.assertGreaterEqual(metrics["rmse_pp"], 0)
                    self.assertGreaterEqual(metrics["max_error_pp"], metrics["rmse_pp"])
                    convergence = metrics["convergence_time_s"]
                    if convergence is not None:
                        self.assertGreaterEqual(convergence, 0)
                        self.assertLessEqual(convergence, time_s[-1] - time_s[0])

    def test_wrong_initial_soc_changes_only_the_supplied_initial_estimate(self):
        nominal = self.scenarios["nominal"]
        wrong = self.scenarios["wrong_initial_soc"]
        self.assertIs(nominal["data"], wrong["data"])
        self.assertNotEqual(nominal["initial_soc"], wrong["initial_soc"])
        for scenario in (nominal, wrong):
            self.assertAlmostEqual(
                scenario["estimates"]["Coulomb counting"][0], scenario["initial_soc"]
            )

    def test_seed_reproduces_the_same_noise_without_rerunning_the_pipeline(self):
        nominal_data = self.scenarios["nominal"]["data"]
        repeated = add_sensor_noise(nominal_data, seed=42)
        for name in ("measured_current_a", "measured_voltage_v", "measured_temp_c"):
            np.testing.assert_array_equal(repeated[name], nominal_data[name])

    def test_current_bias_scenario_keeps_voltage_temperature_and_random_noise_fixed(self):
        nominal = self.scenarios["nominal"]["data"]
        biased = self.scenarios["current_bias"]["data"]
        for name in (
            "time_s", "true_soc", "current_a", "voltage_v", "temperature_c",
            "measured_voltage_v", "measured_temp_c",
        ):
            np.testing.assert_array_equal(biased[name], nominal[name])
        np.testing.assert_allclose(
            biased["measured_current_a"] - nominal["measured_current_a"],
            0.04, atol=1e-14, rtol=0,
        )

    def test_estimators_receive_measured_arrays_and_the_shared_capacity_reference(self):
        capacity_ah = self.result["characterization"]["ocv_curve"]["usable_capacity_ah"]
        for estimator_name, calls in self.estimator_calls.items():
            self.assertEqual(len(calls), 3)
            for call, scenario in zip(calls, self.result["scenarios"]):
                with self.subTest(estimator=estimator_name, scenario=scenario["scenario_name"]):
                    data, arguments = scenario["data"], call.kwargs
                    np.testing.assert_array_equal(arguments["time_s"], data["time_s"])
                    np.testing.assert_array_equal(
                        arguments["measured_current_a"], data["measured_current_a"]
                    )
                    if estimator_name != "Coulomb counting":
                        np.testing.assert_array_equal(
                            arguments["measured_voltage_v"], data["measured_voltage_v"]
                        )
                    self.assertAlmostEqual(arguments["nominal_capacity_ah"], capacity_ah)
                    self.assertAlmostEqual(arguments["initial_soc"], scenario["initial_soc"])
                    self.assertNotIn("true_soc", arguments)
                    self.assertNotIn("reference_soc", arguments)


class OperatingConditionGridTests(unittest.TestCase):
    """Exercise fixed characterization across SOC and temperature conditions.

    The comparison may expose poor estimates. These tests require correct data
    flow, scoring, and persistence without requiring a particular winner.
    """

    @classmethod
    def setUpClass(cls):
        with (
            patch.object(compare_estimators, "run_ecm_fit", wraps=compare_estimators.run_ecm_fit) as fit,
            patch.object(compare_estimators, "estimate_soc_kalman_filter", wraps=compare_estimators.estimate_soc_kalman_filter) as kf,
            patch.object(compare_estimators, "estimate_soc_ekf", wraps=compare_estimators.estimate_soc_ekf) as ekf,
        ):
            cls.result = compare_estimators.run_comparison(
                profile_names=("changing_load",), initial_socs=(0.1, 0.5, 0.9),
                ambient_temperatures_c=(15, 25, 35), seed=42,
            )
        cls.fit_call_count = fit.call_count
        cls.filter_calls = {"KF": kf.call_args_list, "EKF": ekf.call_args_list}
        cls.conditions = {}
        for scenario in cls.result["scenarios"]:
            cls.conditions.setdefault(scenario["condition_name"], {})[scenario["scenario_name"]] = scenario

    def test_grid_has_all_nine_conditions_and_three_scenarios_per_condition(self):
        self.assertEqual(len(self.result["scenarios"]), 27)
        self.assertEqual(len(self.conditions), 9)
        self.assertEqual(
            {(scenario["target_soc"], scenario["ambient_temperature_c"])
             for scenario in self.result["scenarios"]},
            {(soc, temperature) for soc in (0.1, 0.5, 0.9) for temperature in (15, 25, 35)},
        )
        for name, scenarios in self.conditions.items():
            with self.subTest(condition=name):
                self.assertEqual(set(scenarios), {"nominal", "wrong_initial_soc", "current_bias"})
                nominal, wrong = scenarios["nominal"], scenarios["wrong_initial_soc"]
                self.assertIs(nominal["data"], wrong["data"])
                self.assertEqual(nominal["initial_soc"], nominal["target_soc"])
                self.assertEqual(wrong["initial_soc"], max(0, nominal["target_soc"] - 0.1))
                np.testing.assert_allclose(
                    scenarios["current_bias"]["data"]["measured_current_a"] - nominal["data"]["measured_current_a"],
                    0.04, rtol=0, atol=1e-14,
                )

    def test_temperature_and_initial_soc_metadata_match_actual_profile_physics(self):
        reference_capacity = self.result["characterization"]["ocv_curve"]["usable_capacity_ah"]
        for scenario in self.result["scenarios"]:
            with self.subTest(condition=scenario["condition_name"], scenario=scenario["scenario_name"]):
                data = scenario["data"]
                self.assertEqual(data["target_soc"], scenario["target_soc"])
                self.assertAlmostEqual(data["initial_soc"], scenario["target_soc"], places=12)
                self.assertAlmostEqual(data["true_soc"][0], scenario["target_soc"], places=12)
                self.assertEqual(data["ambient_temperature_c"], scenario["ambient_temperature_c"])
                self.assertEqual(data["initial_temperature_c"], scenario["ambient_temperature_c"])
                self.assertAlmostEqual(data["temperature_c"][0], scenario["ambient_temperature_c"], delta=1e-3)
                self.assertEqual(data["reference_capacity_temperature_c"], 25)
                self.assertEqual(data["usable_capacity_ah"], reference_capacity)

    def test_characterization_and_filter_tuning_stay_fixed_across_the_grid(self):
        self.assertEqual(self.fit_call_count, 1)
        settings = self.result["evaluation_settings"]
        self.assertTrue(settings["calibration_is_fixed"])
        self.assertEqual(settings["calibration_soc"], 0.5)
        self.assertEqual(settings["calibration_temperature_c"], 25)
        self.assertEqual(settings["capacity_reference_temperature_c"], 25)
        fit = self.result["characterization"]["fit"]
        capacity = self.result["characterization"]["ocv_curve"]["usable_capacity_ah"]
        for estimator, calls in self.filter_calls.items():
            self.assertEqual(len(calls), 27)
            for call in calls:
                self.assertEqual(call.kwargs["nominal_capacity_ah"], capacity)
                for name, value in self.result["estimator_settings"][estimator].items():
                    self.assertEqual(call.kwargs[name], value)
                if estimator == "KF":
                    self.assertEqual(call.kwargs["internal_resistance_ohm"], fit["r0_ohm"])
                    for name in ("ocv_at_empty_v", "ocv_at_full_v"):
                        self.assertEqual(call.kwargs[name], self.result["linear_ocv_model"][name])
                else:
                    for name in ("r0_ohm", "r1_ohm", "c1_f"):
                        self.assertEqual(call.kwargs[name], fit[name])

    def test_every_scenario_includes_a_closed_kf_model_error_decomposition(self):
        contribution_names = (
            "ocv_nonlinearity_mean_pp", "unmodeled_dynamics_mean_pp",
            "voltage_sensor_error_mean_pp", "current_sensor_error_mean_pp",
        )
        for scenario in self.result["scenarios"]:
            with self.subTest(condition=scenario["condition_name"], scenario=scenario["scenario_name"]):
                diagnostic = scenario["kf_model_diagnostics"]
                self.assertTrue(all(np.isfinite(value) for value in diagnostic.values()))
                self.assertAlmostEqual(
                    sum(diagnostic[name] for name in contribution_names),
                    diagnostic["voltage_implied_soc_error_mean_pp"], places=10,
                )
                self.assertLess(diagnostic["decomposition_residual_max_abs_pp"], 1e-10)
                self.assertAlmostEqual(diagnostic["reference_soc_min"], scenario["data"]["true_soc"].min())
                self.assertAlmostEqual(diagnostic["reference_soc_max"], scenario["data"]["true_soc"].max())

    def test_whole_grid_round_trips_through_sqlite_and_preserves_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grid.sqlite3"
            comparison_id = save_comparison(path, self.result)
            restored = load_comparison(path, comparison_id)
        self.assertEqual(len(restored["scenarios"]), 27)
        self.assertEqual(restored["evaluation_settings"], self.result["evaluation_settings"])
        self.assertEqual(restored["estimator_settings"], self.result["estimator_settings"])
        self.assertEqual(restored["software_versions"], self.result["software_versions"])
        for original, loaded in zip(self.result["scenarios"], restored["scenarios"]):
            for name in ("condition_name", "target_soc", "ambient_temperature_c", "scenario_name", "initial_soc"):
                self.assertEqual(loaded[name], original[name])
            self.assertEqual(loaded["kf_model_diagnostics"], original["kf_model_diagnostics"])
            self.assertEqual(loaded["data"]["reference_capacity_temperature_c"], 25)
            np.testing.assert_array_equal(loaded["data"]["true_soc"], original["data"]["true_soc"])
        # Recompute several cases from reloaded measurements and estimates;
        # storage must preserve enough precision to reproduce the same scores.
        for scenario in (restored["scenarios"][0], restored["scenarios"][13], restored["scenarios"][-1]):
            for estimator, estimate in scenario["estimates"].items():
                recomputed = compare_estimators.calculate_soc_metrics(
                    scenario["data"]["time_s"], scenario["data"]["true_soc"], estimate,
                    **restored["metric_settings"],
                )
                for name, value in recomputed.items():
                    if name == "drift_interval_s":
                        np.testing.assert_allclose(scenario["metrics"][estimator][name], value, atol=1e-12)
                    elif value is None:
                        self.assertIsNone(scenario["metrics"][estimator][name])
                    else:
                        self.assertAlmostEqual(scenario["metrics"][estimator][name], value, places=12)

    def test_plots_group_by_condition_and_save_unique_files(self):
        selected = {**self.result, "scenarios": self.result["scenarios"][:6]}
        figures = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                figures = plot_estimator_comparison(selected, save_dir=directory, show=False)
                files = list(Path(directory).glob("*_comparison.png"))
                self.assertEqual(len(figures), 2)
                self.assertEqual(len(files), 2)
                self.assertEqual(len({path.name for path in files}), 2)
                self.assertTrue(all(path.stat().st_size > 0 for path in files))
                for figure in figures:
                    self.assertEqual(len(figure.axes), 6)
                    self.assertEqual(sum(axis.get_ylabel() == "SOC (%)" for axis in figure.axes), 3)
                    self.assertIn("changing load", figure._suptitle.get_text())
                    self.assertIn("15°C", figure._suptitle.get_text())
        finally:
            for figure in figures:
                plt.close(figure)


class OperatingConditionArgumentTests(unittest.TestCase):
    def test_invalid_grids_are_rejected_before_characterization(self):
        cases = (
            {"initial_socs": ()}, {"initial_socs": (0.5, 0.5)},
            {"initial_socs": (0,)}, {"initial_socs": (-0.1,)},
            {"initial_socs": (1.1,)}, {"initial_socs": (np.nan,)},
            {"initial_socs": (np.inf,)}, {"initial_socs": 0.5},
            {"initial_socs": ((0.1, 0.5),)},
            {"ambient_temperatures_c": ()}, {"ambient_temperatures_c": (25, 25)},
            {"ambient_temperatures_c": (-1,)}, {"ambient_temperatures_c": (46,)},
            {"ambient_temperatures_c": (np.nan,)}, {"ambient_temperatures_c": (np.inf,)},
            {"ambient_temperatures_c": 25}, {"ambient_temperatures_c": ((15, 25),)},
        )
        for settings in cases:
            with self.subTest(settings=settings), patch.object(compare_estimators, "run_ecm_fit") as fit:
                with self.assertRaises(ValueError):
                    compare_estimators.run_comparison(profile_names=("changing_load",), **settings)
                fit.assert_not_called()

    def test_cli_passes_explicit_grid_flags_to_comparison(self):
        argv = [
            "compare_estimators", "--profiles", "changing_load", "--soc-starts", ".1", ".9",
            "--temperatures", "15", "35", "--no-store", "--no-show",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(compare_estimators, "run_comparison", return_value={}) as run,
            patch.object(compare_estimators, "print_comparison_summary"),
            patch.object(compare_estimators, "save_comparison") as save,
            patch.object(compare_estimators, "plot_estimator_comparison") as plot,
        ):
            compare_estimators.main()
        run.assert_called_once_with(
            ["changing_load"], seed=42, initial_socs=[0.1, 0.9], ambient_temperatures_c=[15, 35],
        )
        save.assert_not_called()
        plot.assert_not_called()

    def test_loading_rejects_grid_overrides_before_loading_or_simulating(self):
        for flag, values in (("--soc-starts", [".1", ".9"]), ("--temperatures", ["15", "35"])):
            with (
                self.subTest(flag=flag),
                patch.object(sys, "argv", ["compare_estimators", "--load-id", "1", flag, *values]),
                patch.object(sys, "stderr", new_callable=io.StringIO) as stderr,
                patch.object(compare_estimators, "load_comparison") as load,
                patch.object(compare_estimators, "run_comparison") as run,
            ):
                with self.assertRaises(SystemExit) as error:
                    compare_estimators.main()
                self.assertEqual(error.exception.code, 2)
                self.assertIn("--load-id uses stored settings", stderr.getvalue())
                load.assert_not_called()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
