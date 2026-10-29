"""Check the offline KF voltage-model explanation against known identities."""

import json
from pathlib import Path
import sys
import unittest

import numpy as np

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python.evaluation.model_diagnostics import diagnose_kf_model


class KFModelDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.line = {
            "ocv_slope_v_per_soc": 1.0,
            "ocv_intercept_v": 3.0,
            "ocv_at_empty_v": 3.0,
            "ocv_at_full_v": 4.0,
        }
        self.resistance = 0.1
        # At 50% SOC this nonlinear OCV is 25 mV above the fitted line.
        self.ocv = lambda soc: 3 + soc + 0.1 * soc**2
        soc = np.full(4, 0.5)
        current = np.array([0, 0.5, -0.2, 0.0])
        clean_voltage = self.ocv(soc) - current * self.resistance - 0.010
        self.data = {
            "time_s": np.array([0, 1, 4, 10], dtype=float),
            "current_a": current,
            "voltage_v": clean_voltage,
            "true_soc": soc,
            "measured_current_a": current + 0.050,
            "measured_voltage_v": clean_voltage + 0.002,
        }

    def diagnose(self, data=None, line=None, ocv=None, resistance=None):
        return diagnose_kf_model(
            self.data if data is None else data,
            self.line if line is None else line,
            self.ocv if ocv is None else ocv,
            self.resistance if resistance is None else resistance,
        )

    def test_known_nonlinearity_polarization_and_sensor_offsets_add_exactly(self):
        result = self.diagnose()
        # 1 V/SOC converts 25,-10,2,5 mV into 2.5,-1,0.2,0.5 pp.
        expected = {
            "ocv_nonlinearity_mean_pp": 2.5,
            "unmodeled_dynamics_mean_pp": -1.0,
            "voltage_sensor_error_mean_pp": 0.2,
            "current_sensor_error_mean_pp": 0.5,
            "voltage_implied_soc_error_mean_pp": 2.2,
            "linear_ocv_local_rmse_mv": 25.0,
            "linear_ocv_local_max_error_mv": 25.0,
            "duration_s": 10,
            "reference_soc_min": 0.5,
            "reference_soc_max": 0.5,
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertAlmostEqual(result[key], value, places=11)
        self.assertLess(result["decomposition_residual_max_abs_pp"], 1e-12)
        # Output can be placed directly in run metadata, without ndarray tags.
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        self.assertTrue(all(type(value) is float for value in result.values()))

    def test_irregular_duplicate_timestamps_weight_elapsed_time_instead_of_samples(self):
        data = {key: value.copy() for key, value in self.data.items()}
        data["time_s"] = np.array([0, 1, 1, 10], dtype=float)
        # OCV-line residuals: 0, 0.010, 0.020, 0.030 V. The instantaneous
        # duplicate boundary has no duration. Trapezoidal signed mean is
        # (0.005*1 + 0.025*9)/10 = 0.023 V, or 2.3 pp at slope 1.
        residual = np.array([0, 0.010, 0.020, 0.030])
        data["true_soc"] = np.sqrt(residual / 0.1)
        supplied_ocv = self.ocv(data["true_soc"])
        data["voltage_v"] = supplied_ocv - data["current_a"] * self.resistance
        data["measured_voltage_v"] = data["voltage_v"].copy()
        data["measured_current_a"] = data["current_a"].copy()
        result = self.diagnose(data=data)
        self.assertAlmostEqual(result["ocv_nonlinearity_mean_pp"], 2.3)
        self.assertAlmostEqual(result["voltage_implied_soc_error_mean_pp"], 2.3)
        # Squared residual integral = (0+.0001)/2*1 +
        # (.0004+.0009)/2*9 = .0059 V^2*s.
        self.assertAlmostEqual(result["linear_ocv_local_rmse_mv"], np.sqrt(0.00059) * 1000)
        self.assertAlmostEqual(result["linear_ocv_local_max_error_mv"], 30)

    def test_known_rc_transient_is_separate_from_nonlinear_ocv_and_sensor_bias(self):
        time = np.array([0, 1, 2], dtype=float)
        soc = np.array([0.5, 0.49, 0.48])
        current = np.ones(3)
        # R1=0.02 ohm, I=1 A, tau=1/ln(2) seconds, initially rested:
        # Vp(t) = I*R1*(1-exp(-t/tau)) = [0, .010, .015] V.
        polarization = 0.02 * (1 - 2.0**(-time))
        voltage = self.ocv(soc) - self.resistance * current - polarization
        data = {
            "time_s": time, "true_soc": soc, "current_a": current,
            "voltage_v": voltage, "measured_current_a": current + 0.05,
            "measured_voltage_v": voltage + 0.002,
        }
        result = self.diagnose(data=data)
        # Trapezoidal means: OCV-line residual=.024015 V, Vp=.00875 V.
        self.assertAlmostEqual(result["ocv_nonlinearity_mean_pp"], 2.4015)
        self.assertAlmostEqual(result["unmodeled_dynamics_mean_pp"], -0.875)
        self.assertAlmostEqual(result["voltage_sensor_error_mean_pp"], 0.2)
        self.assertAlmostEqual(result["current_sensor_error_mean_pp"], 0.5)
        self.assertAlmostEqual(result["voltage_implied_soc_error_mean_pp"], 2.2265)

    def test_slope_scales_all_pp_contributions_and_total_is_not_clipped(self):
        line = {
            "ocv_intercept_v": 3.0, "ocv_slope_v_per_soc": 0.5,
            "ocv_at_empty_v": 3.0, "ocv_at_full_v": 3.5,
        }
        data = {key: value.copy() for key, value in self.data.items()}
        # A voltage-implied SOC above 100% stays above 100% in this diagnostic.
        data["measured_voltage_v"] = np.full(4, 4.0)
        data["measured_current_a"] = np.zeros(4)
        result = self.diagnose(data=data, line=line)
        self.assertAlmostEqual(result["voltage_implied_soc_error_mean_pp"], 150)
        self.assertAlmostEqual(result["ocv_nonlinearity_mean_pp"], 55)
        contribution_keys = (
            "ocv_nonlinearity_mean_pp", "unmodeled_dynamics_mean_pp",
            "voltage_sensor_error_mean_pp", "current_sensor_error_mean_pp",
        )
        self.assertAlmostEqual(sum(result[key] for key in contribution_keys), 150)

    def test_reference_and_measurement_arrays_are_not_modified(self):
        before = {key: value.copy() for key, value in self.data.items()}
        self.diagnose()
        for key, value in before.items():
            np.testing.assert_array_equal(self.data[key], value)

    def test_endpoint_roundoff_only_clips_ocv_query_and_preserves_reference(self):
        data = {key: value.copy() for key, value in self.data.items()}
        reference = np.array([-1e-9, 0.0, 1.0, 1 + 1e-9])
        data["true_soc"] = reference.copy()
        data["current_a"] = np.zeros(4)
        data["measured_current_a"] = np.zeros(4)
        data["voltage_v"] = self.ocv(np.clip(reference, 0, 1))
        data["measured_voltage_v"] = data["voltage_v"].copy()
        queries = []

        def strict_ocv(query):
            self.assertTrue(np.all((query >= 0) & (query <= 1)))
            queries.append(query.copy())
            return self.ocv(query)

        result = self.diagnose(data=data, ocv=strict_ocv)
        np.testing.assert_array_equal(queries[0], [0, 0, 1, 1])
        np.testing.assert_array_equal(data["true_soc"], reference)
        self.assertEqual(result["reference_soc_min"], -1e-9)
        self.assertEqual(result["reference_soc_max"], 1 + 1e-9)
        self.assertLess(result["decomposition_residual_max_abs_pp"], 1e-12)

    def test_invalid_data_arrays_are_rejected(self):
        changes = (
            {"time_s": []},
            {"time_s": [0]},
            {"time_s": [[0, 1, 4, 10]]},
            {"time_s": [0, 1, 0.5, 10]},
            {"time_s": [1, 1, 1, 1]},
            {"time_s": [0, 1, 4, np.inf]},
            {"current_a": [0, 1]},
            {"voltage_v": [3.5, np.nan, 3.5, 3.5]},
            {"true_soc": [0.5, -0.01, 0.5, 0.5]},
            {"true_soc": [0.5, 1.01, 0.5, 0.5]},
            {"measured_current_a": [[0, 0, 0, 0]]},
            {"measured_voltage_v": [3.5, 3.5, 3.5]},
        )
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    self.diagnose(data={**self.data, **change})
        missing = dict(self.data)
        del missing["true_soc"]
        with self.assertRaises(ValueError):
            self.diagnose(data=missing)

    def test_invalid_line_resistance_and_ocv_outputs_are_rejected(self):
        for key, value in (
            ("ocv_slope_v_per_soc", 0), ("ocv_slope_v_per_soc", -1),
            ("ocv_slope_v_per_soc", np.inf), ("ocv_intercept_v", np.nan),
            ("ocv_at_empty_v", 3.1), ("ocv_at_full_v", 4.1),
            ("ocv_at_full_v", np.inf),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    self.diagnose(line={**self.line, key: value})
        for value in (-0.1, np.nan, np.inf, "invalid"):
            with self.subTest(resistance=value):
                with self.assertRaises(ValueError):
                    self.diagnose(resistance=value)
        for function in (42, lambda soc: 3.5, lambda soc: [3.5, 3.5], lambda soc: soc * np.nan):
            with self.subTest(ocv_function=function):
                with self.assertRaises(ValueError):
                    self.diagnose(ocv=function)


if __name__ == "__main__":
    unittest.main()
