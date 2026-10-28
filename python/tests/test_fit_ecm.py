"""Check the one-RC model and parameter fit against known, independent data.

The synthetic battery below uses the closed-form response to a rectangular
current pulse. Its known R0, R1, and C1 let us check the fitting code without
treating PyBaMM as an exact one-RC battery. A separate integration check runs
the actual PyBaMM characterization and evaluates a different validation pulse.

Run from the repository root with either:
    python -m unittest python.tests.test_fit_ecm -v
    python python/tests/test_fit_ecm.py -v
"""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python.simulation import fit_ecm
from python.simulation.ocv_soc import build_nonlinear_ocv_model


R0_OHM = 0.04
R1_OHM = 0.015
TAU_S = 20.0
C1_F = TAU_S / R1_OHM


def synthetic_ocv(soc):
    """A nonlinear test voltage curve, independent of the project lookup."""
    soc = np.asarray(soc)
    return 3.3 + 0.65 * soc + 0.2 * soc ** 2


def analytical_pulse(current_a=0.4, duration_s=60.0, start_s=10.0):
    """Build exact samples, retaining both sides of each current switch.

    The RC voltage has a closed-form exponential during a constant-current
    pulse and another exponential during recovery. No numerical recurrence
    from the implementation is used to generate the expected measurements.
    """
    pre_rest_time = start_s * np.array([0.0, 0.3, 0.8, 1.0])
    pulse_time = start_s + duration_s * np.array([0.0, 0.004, 0.03, 0.1, 0.25, 0.5, 0.8, 1.0])
    recovery_time = start_s + duration_s + np.array([0.0, 0.25, 2.0, 7.0, 25.0, 60.0, 140.0])
    time_s = np.concatenate((pre_rest_time, pulse_time, recovery_time))
    current = np.concatenate((
        np.zeros(pre_rest_time.size),
        np.full(pulse_time.size, current_a),
        np.zeros(recovery_time.size),
    ))
    pulse_elapsed = np.clip(time_s - start_s, 0, duration_s)
    recovery_elapsed = np.maximum(time_s - start_s - duration_s, 0)
    polarization_v = (
        current_a * R1_OHM * (1 - np.exp(-pulse_elapsed / TAU_S))
        * np.exp(-recovery_elapsed / TAU_S)
    )
    # Charge removal changes OCV during the pulse. Ignoring that change would
    # incorrectly attribute some of the voltage decrease to the RC branch.
    soc = 0.6 - current_a * pulse_elapsed / (3600 * 2.153)
    return {
        "time_s": time_s,
        "current_a": current,
        "voltage_v": synthetic_ocv(soc) - current * R0_OHM - polarization_v,
        "true_soc": soc,
        "pulse_start_index": pre_rest_time.size,
        "recovery_start_index": pre_rest_time.size + pulse_time.size,
    }


def copy_pulse(data):
    """Keep input mutations confined to one invalid-input example."""
    return {key: value.copy() if isinstance(value, np.ndarray) else value for key, value in data.items()}


class EquivalentCircuitTests(unittest.TestCase):
    def setUp(self):
        self.data = analytical_pulse()

    def predict(self, data=None, **parameters):
        data = self.data if data is None else data
        settings = {"r0_ohm": R0_OHM, "r1_ohm": R1_OHM, "c1_f": C1_F}
        settings.update(parameters)
        return fit_ecm.predict_ecm_voltage(
            data["time_s"], data["current_a"], data["true_soc"], synthetic_ocv,
            **settings,
        )

    def test_prediction_matches_closed_form_at_irregular_and_duplicate_times(self):
        self.assertTrue(np.any(np.diff(self.data["time_s"]) == 0))
        np.testing.assert_allclose(self.predict(), self.data["voltage_v"], rtol=0, atol=1e-12)

    def test_initial_polarization_decays_analytically(self):
        initial_vp = 0.012
        expected = self.data["voltage_v"] - initial_vp * np.exp(-self.data["time_s"] / TAU_S)
        np.testing.assert_allclose(
            self.predict(initial_polarization_voltage_v=initial_vp), expected,
            rtol=0, atol=1e-12,
        )

    def test_zero_series_resistance_is_allowed(self):
        expected = self.data["voltage_v"] + R0_OHM * self.data["current_a"]
        np.testing.assert_allclose(self.predict(r0_ohm=0.0), expected, rtol=0, atol=1e-12)

    def test_fit_recovers_known_parameters(self):
        result = fit_ecm.fit_ecm_parameters(self.data, synthetic_ocv)
        self.assertTrue(result["optimizer_success"])
        self.assertFalse(result["bounds_hit"])
        self.assertAlmostEqual(result["r0_on_ohm"], R0_OHM, delta=1e-9)
        self.assertAlmostEqual(result["r0_off_ohm"], R0_OHM, delta=1e-9)
        self.assertAlmostEqual(result["r0_ohm"], R0_OHM, delta=1e-9)
        self.assertAlmostEqual(result["r1_ohm"], R1_OHM, delta=1e-6)
        self.assertAlmostEqual(result["tau_s"], TAU_S, delta=0.005)
        self.assertAlmostEqual(result["c1_f"], C1_F, delta=0.5)
        self.assertLess(result["training"]["voltage_rmse_v"], 1e-7)

    def test_fit_predicts_a_separate_pulse_without_refitting(self):
        parameters = fit_ecm.fit_ecm_parameters(self.data, synthetic_ocv)
        before = tuple(parameters[key] for key in ("r0_ohm", "r1_ohm", "c1_f"))
        validation = analytical_pulse(current_a=0.7, duration_s=90.0, start_s=18.0)
        result = fit_ecm.evaluate_ecm(validation, synthetic_ocv, parameters)
        self.assertEqual(before, tuple(parameters[key] for key in ("r0_ohm", "r1_ohm", "c1_f")))
        for name in ("voltage_rmse_v", "voltage_max_error_v", "pulse_rmse_v", "recovery_rmse_v"):
            self.assertLess(result[name], 1e-7, name)
        np.testing.assert_allclose(result["ocv_v"], synthetic_ocv(result["true_soc"]), atol=1e-12)
        np.testing.assert_allclose(
            np.abs(result["residual_v"]),
            np.abs(result["predicted_voltage_v"] - result["voltage_v"]),
            atol=1e-12,
        )

    def test_invalid_model_parameters_are_rejected(self):
        for invalid in (
            {"r0_ohm": -0.01}, {"r0_ohm": np.inf},
            {"r1_ohm": 0}, {"r1_ohm": -0.01}, {"r1_ohm": np.nan},
            {"c1_f": 0}, {"c1_f": -100}, {"c1_f": np.inf},
            {"initial_polarization_voltage_v": np.nan},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.predict(**invalid)

    def test_invalid_measurement_arrays_are_rejected(self):
        cases = []
        decreasing = copy_pulse(self.data)
        decreasing["time_s"][2] = -1
        cases.append(decreasing)
        nonfinite = copy_pulse(self.data)
        nonfinite["current_a"][0] = np.nan
        cases.append(nonfinite)
        unequal = copy_pulse(self.data)
        unequal["true_soc"] = unequal["true_soc"][:-1]
        cases.append(unequal)
        for index, data in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    self.predict(data)

    def test_missing_switch_indices_are_rejected(self):
        for name in ("pulse_start_index", "recovery_start_index"):
            data = copy_pulse(self.data)
            del data[name]
            with self.subTest(missing=name):
                with self.assertRaises(ValueError):
                    fit_ecm.fit_ecm_parameters(data, synthetic_ocv)

    def test_unexcited_and_nonrectangular_currents_are_rejected(self):
        for kind in ("no_pulse", "ramp"):
            data = copy_pulse(self.data)
            if kind == "no_pulse":
                data["current_a"][:] = 0
            else:
                data["current_a"][data["pulse_start_index"] + 2] *= 0.5
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    fit_ecm.fit_ecm_parameters(data, synthetic_ocv)

    def test_missing_switch_side_samples_are_rejected(self):
        for name in ("pulse_start_index", "recovery_start_index"):
            data = copy_pulse(self.data)
            # Shift both sides around the boundary apart without reordering.
            index = data[name]
            data["time_s"][index - 1] -= 0.01
            with self.subTest(switch=name):
                with self.assertRaises(ValueError):
                    fit_ecm.fit_ecm_parameters(data, synthetic_ocv)

    def test_unsettled_ocv_baseline_is_rejected(self):
        data = copy_pulse(self.data)
        data["voltage_v"][:data["pulse_start_index"]] -= 0.002
        with self.assertRaises(ValueError):
            fit_ecm.fit_ecm_parameters(data, synthetic_ocv, baseline_tolerance_v=0.001)


class PyBaMMFitIntegrationTests(unittest.TestCase):
    def test_real_fit_predicts_an_unfitted_validation_pulse(self):
        # Only training may fit parameters. Validation must reuse them, even
        # though its pulse current/duration differ from the training protocol.
        with patch.object(fit_ecm, "fit_ecm_parameters", wraps=fit_ecm.fit_ecm_parameters) as fitter:
            experiment = fit_ecm.run_ecm_fit()
        self.assertEqual(fitter.call_count, 1)
        result = experiment["fit"]
        validation = experiment["validation"]
        self.assertTrue(result["optimizer_success"])
        self.assertFalse(result["bounds_hit"])
        for name in ("r0_ohm", "r1_ohm", "c1_f", "tau_s"):
            self.assertTrue(np.isfinite(result[name]), name)
            self.assertGreater(result[name], 0, name)
        self.assertLess(result["training"]["voltage_rmse_v"], 0.001)
        self.assertLess(validation["voltage_rmse_v"], 0.002)
        self.assertIn("soc_points", experiment["ocv_curve"])
        # Reconstruct the validation voltage using the original fitted values.
        # This also catches accidentally returning a second, refitted model.
        ocv, _ = build_nonlinear_ocv_model(
            experiment["ocv_curve"]["soc_points"],
            experiment["ocv_curve"]["ocv_points_v"],
        )
        predicted = fit_ecm.predict_ecm_voltage(
            validation["time_s"], validation["current_a"], validation["true_soc"], ocv,
            r0_ohm=result["r0_ohm"], r1_ohm=result["r1_ohm"], c1_f=result["c1_f"],
        )
        np.testing.assert_allclose(validation["predicted_voltage_v"], predicted, rtol=0, atol=1e-12)


if __name__ == "__main__":
    unittest.main()
