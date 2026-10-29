"""Verify complete, repeatable SQLite saves without changing the experiment.

Small fixtures cover timing switches, metadata, isolation, and failed writes.
One real PyBaMM comparison is also saved and replayed to check that storage
preserves the readings and SOC estimates used by the accuracy calculations.
All databases live in temporary directories and are removed after the tests.
"""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python.database.database import (
    initialize_database, list_comparisons, load_comparison, save_comparison,
)
from python.evaluation import compare_estimators


def sample_comparison():
    """Build a small experiment containing the same kinds of data as PyBaMM."""
    # A current switch can have duplicate timestamps or a separation far below
    # the normal one-second sampling period. Neither sample may be discarded.
    time = np.array([0.0, 1.0, 1.0, 1.0 + 1e-12, 61.0, 121.0])
    reference = np.array([0.5, 0.5, 0.5, 0.5, 0.499, 0.498])
    settings = {
        "convergence_tolerance_pp": 2.0,
        "convergence_hold_s": 60.0,
        "drift_window_s": 60.0,
    }
    data = {
        "time_s": time,
        "current_a": np.array([0.0, 0.2, 0.4, 0.4, 0.0, 0.0]),
        "voltage_v": np.array([3.8, 3.79, 3.78, 3.78, 3.79, 3.795]),
        "temperature_c": np.array([25.0, 25.01, 25.01, 25.01, 25.02, 25.01]),
        "discharge_capacity_ah": (1 - reference) * 2.153329048,
        "true_soc": reference,
        "measured_current_a": np.array([0.01, 0.225, 0.415, 0.405, 0.002, 0.01]),
        "measured_voltage_v": np.array([3.805, 3.791, 3.782, 3.78, 3.792, 3.797]),
        "measured_temp_c": np.array([25.1, 25.2, 25.2, 25.1, 25.3, 25.2]),
        "phase": np.array(["rest", "discharge", "discharge", "discharge", "rest", "rest"]),
        "step_start_indices": np.array([0, 1, 4], dtype=np.int64),
        "step_end_indices": np.array([0, 3, 5], dtype=np.int64),
        "usable_capacity_ah": 2.153329048,
        "nominal_capacity_ah": 2.0,
        "parameter_set": "Ai2020",
        "soc_reference": "Full discharge to 3.0 V using reference usable capacity",
        "minimum_voltage_v": 3.0,
        "maximum_voltage_v": 4.2,
        "initial_soc": 0.5,
        "target_soc": 0.5,
        "sample_period_s": 1.0,
        "pre_rest_duration_s": 3600.0,
        "profile_name": "changing_load",
        "profile_steps": [
            {"current_a": 0.0, "duration_s": 1.0},
            {"current_a": 0.4, "duration_s": 60.0},
            {"current_a": 0.0, "duration_s": 60.0},
        ],
        "noise_settings": {
            "seed": np.int64(42),
            "voltage_noise_std_v": 0.005,
            "voltage_bias_v": 0.002,
            "current_noise_std_a": 0.020,
            "current_bias_a": 0.010,
            "temp_noise_std_c": 0.2,
            "temp_bias_c": 0.1,
        },
    }
    scenarios = []
    for name, initial_soc in (("nominal", 0.5), ("wrong_initial_soc", 0.4)):
        estimates = {
            # These deliberately produce None and zero convergence times and
            # a signed negative drift, which must not be confused during save.
            "Coulomb counting": reference - 0.10,
            "KF": reference + 0.002 - 0.000001 * time,
            "EKF": reference + 0.05 * np.exp(-time / 20),
        }
        scenarios.append({
            "profile_name": "changing_load", "scenario_name": name,
            "initial_soc": initial_soc, "data": data,
            "estimates": estimates,
            "metrics": {
                estimator: compare_estimators.calculate_soc_metrics(
                    time, reference, estimate, **settings
                ) for estimator, estimate in estimates.items()
            },
        })
    return {
        "seed": np.int64(42), "metric_settings": settings,
        "scenarios": scenarios,
        "estimator_settings": {
            "KF": dict(compare_estimators.KF_SETTINGS),
            "EKF": dict(compare_estimators.EKF_SETTINGS),
        },
        "linear_ocv_model": {
            "slope_v_per_soc": 0.63, "intercept_v": 3.50,
            "fitted_ocv_v": np.array([3.50, 3.815, 4.13]),
            "ocv_at_empty_v": 3.50, "ocv_at_full_v": 4.13,
        },
        "characterization": {
            "fit": {
                "r0_ohm": 0.0415531574, "r1_ohm": 0.00340375,
                "c1_f": 3576.18, "tau_s": 12.1724,
                "bounds_hit": False,
                "training": {
                    "time_s": time.copy(), "voltage_v": data["voltage_v"].copy(),
                    "residual_v": np.zeros(time.size),
                },
            },
            "validation": {"voltage_rmse_v": 0.0002321},
            "ocv_curve": {
                "soc_points": np.array([0.0, 0.5, 1.0]),
                "ocv_points_v": np.array([3.30, 3.80, 4.18]),
                "usable_capacity_ah": 2.153329048,
                "settled": np.array([True, True, False], dtype=bool),
            },
        },
    }


class NestedComparisonAssertions:
    def assert_nested_equal(self, expected, actual):
        """JSON may normalize tuples; arrays must retain their type and dtype."""
        if isinstance(expected, np.ndarray):
            self.assertIsInstance(actual, np.ndarray)
            self.assertEqual(expected.dtype, actual.dtype)
            np.testing.assert_array_equal(actual, expected)
        elif isinstance(expected, dict):
            self.assertIsInstance(actual, dict)
            self.assertEqual(set(actual), set(expected))
            for name, value in expected.items():
                with self.subTest(field=name):
                    self.assert_nested_equal(value, actual[name])
        elif isinstance(expected, (list, tuple)):
            self.assertIsInstance(actual, (list, tuple))
            self.assertEqual(len(actual), len(expected))
            for first, second in zip(expected, actual):
                self.assert_nested_equal(first, second)
        else:
            self.assertEqual(actual, expected)


class DatabasePersistenceTests(NestedComparisonAssertions, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "experiment.sqlite3"
        self.results = sample_comparison()

    def row_counts(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            return {
                name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("comparisons", "scenarios", "measurements", "metrics", "estimates")
            }

    def test_initialization_creates_versioned_schema_and_is_idempotent(self):
        initialize_database(self.path)
        self.assertTrue(self.path.is_file())
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue({"comparisons", "scenarios", "measurements", "metrics", "estimates"} <= tables)
        first = save_comparison(self.path, self.results)
        initialize_database(self.path)
        self.assertEqual([item["comparison_id"] for item in list_comparisons(self.path)], [first])

    def test_complete_round_trip_preserves_measurements_estimates_and_metadata(self):
        before = deepcopy(self.results)
        comparison_id = save_comparison(self.path, self.results)
        self.assertIsInstance(comparison_id, int)
        self.assertGreater(comparison_id, 0)
        loaded = load_comparison(self.path, comparison_id)
        self.assert_nested_equal(before, loaded)
        self.assert_nested_equal(before, self.results)
        self.assertEqual(
            [scenario["scenario_name"] for scenario in loaded["scenarios"]],
            ["nominal", "wrong_initial_soc"],
        )
        for scenario in loaded["scenarios"]:
            self.assertEqual(list(scenario["estimates"]), ["Coulomb counting", "KF", "EKF"])
            self.assertEqual(list(scenario["metrics"]), ["Coulomb counting", "KF", "EKF"])
            self.assertIsNone(scenario["metrics"]["Coulomb counting"]["convergence_time_s"])
            self.assertEqual(scenario["metrics"]["KF"]["convergence_time_s"], 0)
            self.assertLess(scenario["metrics"]["KF"]["drift_pp_per_hour"], 0)
        self.assertEqual(self.row_counts(), {
            "comparisons": 1, "scenarios": 2, "measurements": 12,
            "metrics": 6, "estimates": 36,
        })

    def test_duplicate_and_near_duplicate_times_have_separate_sample_rows(self):
        comparison_id = save_comparison(self.path, self.results)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            rows = connection.execute(
                "SELECT sample_index, time_s FROM measurements WHERE scenario_id = "
                "(SELECT MIN(scenario_id) FROM scenarios WHERE comparison_id = ?) "
                "ORDER BY sample_index", (comparison_id,),
            ).fetchall()
        self.assertEqual([row[0] for row in rows], list(range(6)))
        np.testing.assert_array_equal(
            [row[1] for row in rows], self.results["scenarios"][0]["data"]["time_s"]
        )
        self.assertEqual(rows[1][1], rows[2][1])
        self.assertGreater(rows[3][1], rows[2][1])

    def test_appending_another_comparison_keeps_previous_run_independent(self):
        first = save_comparison(self.path, self.results)
        changed = deepcopy(self.results)
        changed["seed"] = 17
        changed["scenarios"][0]["data"]["noise_settings"]["current_bias_a"] = 0.05
        changed["scenarios"][0]["data"]["measured_current_a"] += 0.04
        second = save_comparison(self.path, changed)
        self.assertNotEqual(first, second)
        entries = list_comparisons(self.path)
        self.assertEqual({row["comparison_id"] for row in entries}, {first, second})
        for row in entries:
            self.assertEqual(row["scenario_count"], 2)
            self.assertTrue(row["created_at"])
        self.assert_nested_equal(self.results, load_comparison(self.path, first))
        self.assert_nested_equal(changed, load_comparison(self.path, second))

    def test_absent_optional_phase_is_preserved_without_fabricating_labels(self):
        for scenario in self.results["scenarios"]:
            scenario["data"].pop("phase", None)
        comparison_id = save_comparison(self.path, self.results)
        self.assert_nested_equal(self.results, load_comparison(self.path, comparison_id))

    def test_missing_database_reads_do_not_create_a_file(self):
        for reader in (lambda: list_comparisons(self.path), lambda: load_comparison(self.path, 1)):
            with self.assertRaises(FileNotFoundError):
                reader()
            self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_unknown_comparison_raises_key_error(self):
        initialize_database(self.path)
        self.assertEqual(list_comparisons(self.path), [])
        with self.assertRaises(KeyError):
            load_comparison(self.path, 987654)

    def test_unsupported_schema_version_is_rejected_without_overwriting_it(self):
        initialize_database(self.path)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA user_version = 999")
        for operation in (
            lambda: initialize_database(self.path),
            lambda: save_comparison(self.path, self.results),
            lambda: list_comparisons(self.path),
            lambda: load_comparison(self.path, 1),
        ):
            with self.assertRaises(ValueError):
                operation()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 999)

    def test_invalid_inputs_leave_previously_saved_comparison_unchanged(self):
        first = save_comparison(self.path, self.results)
        before_counts = self.row_counts()
        invalid = []
        short_measurements = deepcopy(self.results)
        short_measurements["scenarios"][0]["data"]["measured_voltage_v"] = np.array([3.8])
        invalid.append(short_measurements)
        bad_measurement = deepcopy(self.results)
        bad_measurement["scenarios"][0]["data"]["measured_current_a"][1] = np.nan
        invalid.append(bad_measurement)
        bad_estimate = deepcopy(self.results)
        bad_estimate["scenarios"][0]["estimates"]["KF"][1] = np.inf
        invalid.append(bad_estimate)
        missing_metric = deepcopy(self.results)
        del missing_metric["scenarios"][0]["metrics"]["EKF"]
        invalid.append(missing_metric)
        short_estimate = deepcopy(self.results)
        short_estimate["scenarios"][0]["estimates"]["KF"] = np.array([0.5])
        invalid.append(short_estimate)
        decreasing_time = deepcopy(self.results)
        decreasing_time["scenarios"][0]["data"]["time_s"][1] = -1
        invalid.append(decreasing_time)
        bad_metric = deepcopy(self.results)
        bad_metric["scenarios"][0]["metrics"]["KF"]["rmse_pp"] = np.nan
        invalid.append(bad_metric)
        short_phase = deepcopy(self.results)
        short_phase["scenarios"][0]["data"]["phase"] = np.array(["rest"])
        invalid.append(short_phase)
        bad_capacity = deepcopy(self.results)
        bad_capacity["scenarios"][0]["data"]["usable_capacity_ah"] = 0
        invalid.append(bad_capacity)
        for index, result in enumerate(invalid):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    save_comparison(self.path, result)
                self.assertEqual(self.row_counts(), before_counts)
        self.assert_nested_equal(self.results, load_comparison(self.path, first))

    def test_sql_failure_halfway_through_save_rolls_back_every_table(self):
        first = save_comparison(self.path, self.results)
        before_counts = self.row_counts()
        # Fail after a comparison, scenario, measurements, and an estimator have
        # already been inserted. This exercises transaction rollback directly.
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER reject_test_estimate BEFORE INSERT ON estimates "
                "WHEN NEW.estimator_name = 'KF' "
                "BEGIN SELECT RAISE(ABORT, 'injected test failure'); END"
            )
        with self.assertRaises(sqlite3.Error):
            save_comparison(self.path, self.results)
        self.assertEqual(self.row_counts(), before_counts)
        self.assert_nested_equal(self.results, load_comparison(self.path, first))

    def test_schema_prevents_duplicate_samples_and_orphan_measurements(self):
        save_comparison(self.path, self.results)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            scenario_id = connection.execute("SELECT MIN(scenario_id) FROM scenarios").fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE measurements SET sample_index = 0 "
                    "WHERE scenario_id = ? AND sample_index = 1", (scenario_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE measurements SET scenario_id = -1 "
                    "WHERE scenario_id = ? AND sample_index = 0", (scenario_id,),
                )


class RealComparisonStorageTests(NestedComparisonAssertions, unittest.TestCase):
    def test_real_profile_reloads_with_identical_soc_metrics_and_characterization(self):
        original = compare_estimators.run_comparison(profile_names=("changing_load",), seed=42)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "real_comparison.sqlite3"
            comparison_id = save_comparison(path, original)
            loaded = load_comparison(path, comparison_id)
        self.assert_nested_equal(original, loaded)
        for scenario in loaded["scenarios"]:
            for name, estimate in scenario["estimates"].items():
                recomputed = compare_estimators.calculate_soc_metrics(
                    scenario["data"]["time_s"], scenario["data"]["true_soc"],
                    estimate, **loaded["metric_settings"],
                )
                self.assert_nested_equal(scenario["metrics"][name], recomputed)


class ComparisonStorageCLITests(unittest.TestCase):
    def test_new_cli_comparison_is_saved_by_default(self):
        results = sample_comparison()
        with (
            patch.object(sys, "argv", ["compare_estimators", "--no-show"]),
            patch.object(compare_estimators, "run_comparison", return_value=results) as run,
            patch.object(compare_estimators, "save_comparison", return_value=7) as save,
            patch.object(compare_estimators, "print_comparison_summary") as summary,
            patch.object(compare_estimators, "plot_estimator_comparison") as plot,
            patch("builtins.print"),
        ):
            compare_estimators.main()
        run.assert_called_once_with(None, seed=42)
        save.assert_called_once_with(Path("results/smart_bms.sqlite3"), results)
        summary.assert_called_once_with(results)
        plot.assert_not_called()

    def test_no_store_runs_requested_profile_without_saving(self):
        results = sample_comparison()
        with (
            patch.object(sys, "argv", [
                "compare_estimators", "--profiles", "changing_load", "--seed", "17",
                "--no-store", "--no-show",
            ]),
            patch.object(compare_estimators, "run_comparison", return_value=results) as run,
            patch.object(compare_estimators, "save_comparison") as save,
            patch.object(compare_estimators, "print_comparison_summary"),
            patch.object(compare_estimators, "plot_estimator_comparison") as plot,
        ):
            compare_estimators.main()
        run.assert_called_once_with(["changing_load"], seed=17)
        save.assert_not_called()
        plot.assert_not_called()

    def test_saved_comparison_can_be_replotted_without_simulation_or_another_save(self):
        results = sample_comparison()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparison.sqlite3"
            export = Path(directory) / "export"
            with (
                patch.object(sys, "argv", [
                    "compare_estimators", "--database", str(path), "--load-id", "7",
                    "--save-dir", str(export), "--no-show",
                ]),
                patch.object(compare_estimators, "load_comparison", return_value=results) as load,
                patch.object(compare_estimators, "run_comparison") as run,
                patch.object(compare_estimators, "save_comparison") as save,
                patch.object(compare_estimators, "print_comparison_summary"),
                patch.object(compare_estimators, "save_comparison_summary") as summary_export,
                patch.object(compare_estimators, "plot_estimator_comparison") as plot,
                patch("builtins.print"),
            ):
                compare_estimators.main()
            load.assert_called_once_with(path, 7)
            run.assert_not_called()
            save.assert_not_called()
            summary_export.assert_called_once_with(results, export)
            plot.assert_called_once_with(results, save_dir=export, show=False)


if __name__ == "__main__":
    unittest.main()
