"""Check estimator mathematics against independent, known battery responses.

These tests use charge integrals, scalar Bayesian updates, and the closed-form
response of an ideal one-RC battery. They do not use PyBaMM, the fitted project
parameters, or reference SOC as an estimator input. Passing them verifies the
calculation under the stated model; it does not establish hardware accuracy.

Run from the repository root with either:
    python -m unittest python.tests.test_estimators -v
    python python/tests/test_estimators.py -v
"""

from pathlib import Path
import sys
import unittest

import numpy as np

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python.estimation.coulomb_counter import estimate_soc_coulomb_counting
from python.estimation.ekf import estimate_soc_ekf
from python.estimation.kalman_filter import estimate_soc_kalman_filter


def nonlinear_ocv(soc):
    """An invented, monotonic curve with a known analytical derivative."""
    soc = np.asarray(soc)
    return 3.3 + 0.65 * soc + 0.2 * soc**2


def nonlinear_ocv_slope(soc):
    return 0.65 + 0.4 * np.asarray(soc)


def analytical_battery_response():
    """Build a rest/discharge/rest/charge trace without a state recurrence.

    Each rectangular pulse contributes its closed-form exponential response.
    Both sides of switches have the same timestamp, preserving instantaneous
    R0 voltage changes without inventing charge removal at the switch itself.
    """
    time_s = np.array([
        0, 0.3, 2, 7, 7, 7.25, 9, 15, 27, 67,
        67, 67.7, 72, 97, 167, 167, 169, 174, 187,
    ], dtype=float)
    current_a = np.array([0] * 4 + [0.4] * 6 + [0] * 5 + [-0.2] * 4)
    discharge_elapsed = np.clip(time_s - 7, 0, 60)
    charge_elapsed = np.clip(time_s - 167, 0, 20)
    soc = 0.65 - (0.4 * discharge_elapsed - 0.2 * charge_elapsed) / (3600 * 2)
    # The initial RC voltage relaxes independently; superposition then adds
    # the discharge and charge responses of R1 = 0.03 ohm, tau = 20 seconds.
    polarization_v = (
        0.012 * np.exp(-time_s / 20)
        + 0.4 * 0.03 * (1 - np.exp(-discharge_elapsed / 20))
        * np.exp(-np.maximum(time_s - 67, 0) / 20)
        - 0.2 * 0.03 * (1 - np.exp(-charge_elapsed / 20))
    )
    voltage_v = nonlinear_ocv(soc) - 0.04 * current_a - polarization_v
    return time_s, current_a, voltage_v, soc


def call_kf(**overrides):
    settings = {
        "time_s": [0, 10],
        "measured_current_a": [0, 0],
        "measured_voltage_v": [3.8, 3.8],
        "nominal_capacity_ah": 2,
        "initial_soc": 0.4,
        "ocv_at_empty_v": 3,
        "ocv_at_full_v": 5,
    }
    settings.update(overrides)
    return estimate_soc_kalman_filter(**settings)


def call_ekf(**overrides):
    settings = {
        "time_s": [0, 10],
        "measured_current_a": [0, 0],
        "measured_voltage_v": [nonlinear_ocv(0.4)] * 2,
        "nominal_capacity_ah": 2,
        "initial_soc": 0.4,
        "ocv_function": nonlinear_ocv,
        "ocv_derivative_function": nonlinear_ocv_slope,
        "r0_ohm": 0.04,
        "r1_ohm": 0.03,
        "c1_f": 20 / 0.03,
    }
    settings.update(overrides)
    return estimate_soc_ekf(**settings)


class CoulombCountingMathTests(unittest.TestCase):
    def test_constant_discharge_charge_and_rest_use_amp_hours(self):
        # One ampere for 30 minutes removes 0.5 Ah, or 25% of a 2 Ah cell.
        for current in (1.0, -1.0, 0.0):
            with self.subTest(current_a=current):
                actual = estimate_soc_coulomb_counting(
                    [100, 1000, 1900], [current] * 3, 2, 0.6,
                )
                expected = 0.6 - current * np.array([0, 0.125, 0.25])
                np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-15)

    def test_irregular_samples_integrate_a_linear_current_exactly(self):
        time_s = np.array([50, 53, 101, 280, 650], dtype=float)
        elapsed_s = time_s - time_s[0]
        current_a = 0.2 + 0.001 * elapsed_s
        # Integral of 0.2 + 0.001*t is 0.2*t + 0.0005*t^2 amp-seconds.
        expected = 0.8 - (0.2 * elapsed_s + 0.0005 * elapsed_s**2) / 7200
        actual = estimate_soc_coulomb_counting(time_s, current_a, 2, 0.8)
        np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-15)

    def test_duplicate_switch_timestamps_remove_no_charge(self):
        actual = estimate_soc_coulomb_counting(
            [0, 30, 30, 90, 90, 100, 100, 160],
            [0, 0, 2, 2, 0, 0, -1, -1], 2, 0.7,
        )
        # Discharge: 2 A * 60 s = 120 As; charge: 1 A * 60 s = 60 As.
        expected = 0.7 - np.array([0, 0, 0, 120, 120, 120, 120, 60]) / 7200
        np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-15)

    def test_ten_milliamp_offset_causes_three_percentage_points_in_six_hours(self):
        time_s = np.array([0, 3600, 7200, 21600], dtype=float)
        actual = estimate_soc_coulomb_counting(time_s, [0.01] * 4, 2, 0.8)
        np.testing.assert_allclose(actual, 0.8 - 0.01 * time_s / 7200, atol=1e-15)
        self.assertAlmostEqual(actual[-1], 0.77, places=14)

    def test_wrong_initial_soc_persists_without_voltage_correction(self):
        time_s = [0, 300, 900, 1800]
        current_a = [0.2] * 4
        correct = estimate_soc_coulomb_counting(time_s, current_a, 2, 0.8)
        wrong = estimate_soc_coulomb_counting(time_s, current_a, 2, 0.4)
        np.testing.assert_allclose(correct - wrong, 0.4, rtol=0, atol=1e-15)

    def test_saturation_and_reversing_current_stay_within_physical_range(self):
        actual = estimate_soc_coulomb_counting(
            [0, 3600, 3600, 7200], [3, 3, -3, -3], 1, 0.3,
        )
        np.testing.assert_array_equal(actual, [0.3, 0, 0, 1])

    def test_single_sample_and_out_of_range_initial_soc(self):
        for initial, expected in ((0.4, 0.4), (-0.2, 0), (1.2, 1)):
            with self.subTest(initial_soc=initial):
                actual = estimate_soc_coulomb_counting([30], [100], 2, initial)
                np.testing.assert_array_equal(actual, [expected])


class KalmanFilterMathTests(unittest.TestCase):
    def test_two_hand_calculated_updates_include_initial_measurement(self):
        # h(z,I)=3+2z-0.1I. Initial z=.4, P=.09, R=.04.
        # First: S=.4, K=.45, residual=.2 => z=.49, posterior P=.009.
        # After 72 s at 1 A / 2 Ah: z_pred=.48; Qdt=.009 => P_pred=.018.
        # Second: S=.112, K=9/28, residual=.1 => z=.48+9/280.
        actual = call_kf(
            time_s=[100, 172], measured_current_a=[1, 1],
            measured_voltage_v=[3.9, 3.96], internal_resistance_ohm=0.1,
            initial_soc_variance=0.09, measurement_variance_v2=0.04,
            process_variance_per_s=0.009 / 72,
        )
        np.testing.assert_allclose(actual, [0.49, 0.48 + 9 / 280], rtol=0, atol=1e-14)

    def test_exact_linear_battery_handles_discharge_and_charge_ir_drop(self):
        time_s = np.array([0, 30, 50, 200], dtype=float)
        for current in (0.8, -0.6):
            with self.subTest(current_a=current):
                expected = 0.6 - current * time_s / 7200
                voltage_v = 3 + 1.1 * expected - current * 0.04
                actual = call_kf(
                    time_s=time_s, measured_current_a=[current] * 4,
                    measured_voltage_v=voltage_v, initial_soc=0.6,
                    ocv_at_full_v=4.1, internal_resistance_ohm=0.04,
                )
                np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-14)

    def test_repeated_measurements_match_scalar_bayesian_batch_solution(self):
        # For z ~ N(.2,.04) and independent y=3+2z with variance .01,
        # posterior mean after n observations is information-weighted below.
        # Duplicate timestamps add observations but no elapsed-time process noise.
        voltage_v = np.array([3.8, 4.0, 3.9, 4.1, 3.95])
        n = np.arange(1, voltage_v.size + 1)
        expected = (0.2 / 0.04 + 2 * np.cumsum(voltage_v - 3) / 0.01) / (
            1 / 0.04 + n * 4 / 0.01
        )
        actual = call_kf(
            time_s=[30] * 5, measured_current_a=[0] * 5,
            measured_voltage_v=voltage_v, initial_soc=0.2,
            initial_soc_variance=0.04, measurement_variance_v2=0.01,
            process_variance_per_s=100,
        )
        np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-14)

    def test_elapsed_time_process_noise_can_restore_voltage_correction(self):
        # P0=0 prevents the first correction. Q*10=.1 and R=.1 give K=.5
        # for H=1 at the second sample, moving z=.4 halfway toward .8.
        actual = call_kf(
            measured_voltage_v=[3.8, 3.8], ocv_at_full_v=4,
            initial_soc_variance=0, process_variance_per_s=0.01,
            measurement_variance_v2=0.1,
        )
        np.testing.assert_allclose(actual, [0.4, 0.6], rtol=0, atol=1e-14)

    def test_zero_state_uncertainty_reduces_to_coulomb_counting(self):
        time_s = np.array([0, 30, 90, 150], dtype=float)
        actual = call_kf(
            time_s=time_s, measured_current_a=[0.5] * 4,
            measured_voltage_v=[4.2] * 4, initial_soc=0.7,
            initial_soc_variance=0, process_variance_per_s=0,
        )
        np.testing.assert_allclose(actual, 0.7 - 0.5 * time_s / 7200, atol=1e-15)

    def test_joseph_update_retains_uncertainty_at_extreme_scale(self):
        # P0/R=1e22 makes 1-KH round to zero. Joseph's K*R*K term must
        # still retain P=R/4 after the first observation (H=2). The second
        # equal-precision observation then averages the .8 and .4 measurements.
        actual = call_kf(
            time_s=[0, 0], measured_voltage_v=[4.6, 3.8], initial_soc=0.2,
            initial_soc_variance=1e12, measurement_variance_v2=1e-10,
            process_variance_per_s=0,
        )
        np.testing.assert_allclose(actual, [0.8, 0.6], rtol=0, atol=1e-14)

    def test_initial_and_corrected_soc_are_clipped(self):
        for initial, voltage, expected in ((-1, -10, 0), (2, 10, 1)):
            with self.subTest(initial_soc=initial):
                actual = call_kf(initial_soc=initial, measured_voltage_v=[voltage] * 2)
                np.testing.assert_array_equal(actual, [expected] * 2)


class ExtendedKalmanFilterMathTests(unittest.TestCase):
    def test_closed_form_nonlinear_battery_with_rest_discharge_and_charge(self):
        time_s, current_a, voltage_v, expected_soc = analytical_battery_response()
        self.assertTrue(np.any(np.diff(time_s) == 0))
        actual = call_ekf(
            time_s=time_s, measured_current_a=current_a,
            measured_voltage_v=voltage_v, initial_soc=0.65,
            initial_polarization_voltage_v=0.012,
        )
        np.testing.assert_allclose(actual, expected_soc, rtol=0, atol=2e-14)

    def test_initial_polarization_relaxes_without_spurious_soc_change(self):
        time_s = np.array([0, 0.2, 2, 20, 60, 200], dtype=float)
        voltage_v = nonlinear_ocv(0.5) - 0.025 * np.exp(-time_s / 20)
        actual = call_ekf(
            time_s=time_s, measured_current_a=np.zeros(time_s.size),
            measured_voltage_v=voltage_v, initial_soc=0.5,
            initial_polarization_voltage_v=0.025,
        )
        np.testing.assert_allclose(actual, 0.5, rtol=0, atol=1e-14)

    def test_two_hand_calculated_nonlinear_updates_preserve_cross_covariance(self):
        # h(z,vp)=3+z^2-vp, x0=(.5,.1), P0=diag(.04,.01), R=.01.
        # H0=(1,-1), S0=.06, K0=(2/3,-1/6), innovation=.06.
        # x0+ = (.54,.09), P0+ = [[1/75,1/150],[1/150,1/120]].
        # After 10 s with decay=.5 and Qdt=diag(1/400,1/800):
        # x1- = (.54,.045), P1- = [[19/1200,1/300],[1/300,1/300]].
        # H1=(1.08,-1), S1=18451/750000, K_soc=10325/18451.
        # The second innovation is .04, so z1+ = 518827/922550.
        actual = call_ekf(
            time_s=[0, 10], measured_voltage_v=[3.21, 3.2866],
            initial_soc=0.5, initial_polarization_voltage_v=0.1,
            ocv_function=lambda z: 3 + z**2,
            ocv_derivative_function=lambda z: 2 * z,
            r1_ohm=0.02, c1_f=(10 / np.log(2)) / 0.02,
            initial_soc_variance=0.04, initial_polarization_variance_v2=0.01,
            process_soc_variance_per_s=1 / 4000,
            process_polarization_variance_v2_per_s=1 / 8000,
            measurement_variance_v2=0.01,
        )
        np.testing.assert_allclose(actual, [0.54, 518827 / 922550], rtol=0, atol=1e-14)

    def test_numerical_derivative_agrees_with_analytic_at_interior_and_boundaries(self):
        def bounded_ocv(soc):
            if not 0 <= soc <= 1:
                raise AssertionError("OCV evaluated outside its physical domain.")
            return nonlinear_ocv(soc)

        for initial_soc, voltage_offset in ((0, 0.03), (0.4, 0.03), (1, -0.03)):
            with self.subTest(initial_soc=initial_soc):
                settings = {
                    "time_s": [0, 1, 10, 50], "measured_current_a": [0] * 4,
                    "measured_voltage_v": [nonlinear_ocv(initial_soc) + voltage_offset] * 4,
                    "initial_soc": initial_soc, "ocv_function": bounded_ocv,
                    "derivative_step": 1e-7,
                }
                analytic = call_ekf(**settings)
                numerical = call_ekf(**settings, ocv_derivative_function=None)
                np.testing.assert_allclose(numerical, analytic, rtol=0, atol=1e-8)

    def test_wrong_initial_soc_recovers_on_observable_known_model(self):
        time_s = np.arange(0, 602, 2, dtype=float)
        actual = call_ekf(
            time_s=time_s, measured_current_a=np.zeros(time_s.size),
            measured_voltage_v=np.full(time_s.size, nonlinear_ocv(0.72)),
            initial_soc=0.25, initial_soc_variance=0.1,
            initial_polarization_variance_v2=0.0001,
            measurement_variance_v2=0.005**2,
            process_polarization_variance_v2_per_s=1e-7,
        )
        self.assertLess(abs(actual[-1] - 0.72), 0.001)
        self.assertLess(abs(actual[-1] - 0.72), abs(actual[0] - 0.72) / 10)

    def test_flat_ocv_cannot_correct_soc_without_observability(self):
        actual = call_ekf(
            time_s=[0, 30, 60], measured_current_a=[0] * 3,
            measured_voltage_v=[3.9] * 3, initial_soc=0.2,
            ocv_function=lambda z: 3.7, ocv_derivative_function=lambda z: 0,
            initial_polarization_voltage_v=0.1,
        )
        np.testing.assert_array_equal(actual, [0.2] * 3)

    def test_zero_series_resistance_is_valid(self):
        time_s, current_a, voltage_v, expected_soc = analytical_battery_response()
        actual = call_ekf(
            time_s=time_s, measured_current_a=current_a,
            measured_voltage_v=voltage_v + 0.04 * current_a,
            initial_soc=0.65, initial_polarization_voltage_v=0.012,
            r0_ohm=0,
        )
        np.testing.assert_allclose(actual, expected_soc, rtol=0, atol=2e-14)

    def test_joseph_matrix_update_retains_tiny_posterior_variance(self):
        # With fixed zero polarization uncertainty and a linear OCV, the EKF
        # has the same two-observation Gaussian answer as the scalar example.
        actual = call_ekf(
            time_s=[0, 0], measured_voltage_v=[4.6, 3.8], initial_soc=0.2,
            ocv_function=lambda z: 3 + 2 * z,
            ocv_derivative_function=lambda z: 2,
            initial_soc_variance=1e12, initial_polarization_variance_v2=0,
            process_soc_variance_per_s=0,
            process_polarization_variance_v2_per_s=0,
            measurement_variance_v2=1e-10,
        )
        np.testing.assert_allclose(actual, [0.8, 0.6], rtol=0, atol=1e-14)

    def test_clipping_keeps_ocv_queries_within_domain_during_prediction(self):
        def bounded_ocv(soc):
            self.assertGreaterEqual(soc, 0)
            self.assertLessEqual(soc, 1)
            return nonlinear_ocv(soc)

        for initial, current, expected in ((-0.1, 2, 0), (1.1, -2, 1)):
            with self.subTest(initial_soc=initial):
                actual = call_ekf(
                    time_s=[0, 3600], measured_current_a=[current] * 2,
                    initial_soc=initial, ocv_function=bounded_ocv,
                    initial_soc_variance=0, process_soc_variance_per_s=0,
                )
                np.testing.assert_array_equal(actual, [expected] * 2)


class EstimatorInputValidationTests(unittest.TestCase):
    def estimators(self):
        return (
            ("Coulomb counting", estimate_soc_coulomb_counting, {
                "time_s": [0, 10], "measured_current_a": [0, 0],
                "nominal_capacity_ah": 2, "initial_soc": 0.4,
            }),
            ("KF", call_kf, {}),
            ("EKF", call_ekf, {}),
        )

    def test_all_estimators_reject_invalid_shared_inputs(self):
        cases = (
            {"time_s": [[0, 10]]}, {"measured_current_a": [[0, 0]]},
            {"time_s": []}, {"measured_current_a": [0]},
            {"time_s": [10, 0]}, {"time_s": [0, np.nan]},
            {"measured_current_a": [0, np.inf]},
            {"nominal_capacity_ah": 0}, {"nominal_capacity_ah": -2},
            {"nominal_capacity_ah": np.nan}, {"initial_soc": np.inf},
        )
        for name, function, base in self.estimators():
            for invalid in cases:
                with self.subTest(estimator=name, invalid=invalid):
                    with self.assertRaises(ValueError):
                        function(**(base | invalid))

    def test_voltage_arrays_are_validated_by_both_filters(self):
        for function in (call_kf, call_ekf):
            for voltage in ([], [3.7], [[3.7, 3.7]], [3.7, np.nan]):
                with self.subTest(function=function.__name__, voltage=voltage):
                    with self.assertRaises(ValueError):
                        function(measured_voltage_v=voltage)

    def test_kf_rejects_invalid_voltage_model_and_covariances(self):
        cases = (
            {"ocv_at_empty_v": np.nan}, {"ocv_at_full_v": np.inf},
            {"ocv_at_full_v": 3}, {"internal_resistance_ohm": -0.1},
            {"process_variance_per_s": -1}, {"initial_soc_variance": -1},
            {"measurement_variance_v2": 0}, {"measurement_variance_v2": np.nan},
        )
        for invalid in cases:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    call_kf(**invalid)

    def test_ekf_rejects_invalid_circuit_and_covariance_parameters(self):
        cases = (
            {"r0_ohm": -0.1}, {"r0_ohm": np.nan},
            {"r1_ohm": 0}, {"r1_ohm": -1}, {"c1_f": 0}, {"c1_f": np.inf},
            {"initial_polarization_voltage_v": np.nan},
            {"initial_soc_variance": -1}, {"initial_polarization_variance_v2": -1},
            {"process_soc_variance_per_s": -1},
            {"process_polarization_variance_v2_per_s": np.inf},
            {"measurement_variance_v2": 0}, {"measurement_variance_v2": np.nan},
            {"derivative_step": 0}, {"derivative_step": np.inf},
        )
        for invalid in cases:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    call_ekf(**invalid)

    def test_ekf_rejects_invalid_ocv_and_derivative_outputs(self):
        cases = (
            {"ocv_function": 3.7},
            {"ocv_function": lambda z: np.nan},
            {"ocv_function": lambda z: [3.7, 3.8]},
            {"ocv_derivative_function": 1},
            {"ocv_derivative_function": lambda z: np.inf},
            {"ocv_derivative_function": lambda z: [0.5, 0.6]},
        )
        for invalid in cases:
            with self.subTest(parameter=next(iter(invalid))):
                with self.assertRaises(ValueError):
                    call_ekf(**invalid)

    def test_no_estimator_mutates_measurement_arrays(self):
        for name, function, base in self.estimators():
            with self.subTest(estimator=name):
                time_s = np.array([0, 10.0])
                current_a = np.array([0.1, 0.2])
                voltage_v = np.array([3.7, 3.8])
                settings = base | {"time_s": time_s, "measured_current_a": current_a}
                if name != "Coulomb counting":
                    settings["measured_voltage_v"] = voltage_v
                function(**settings)
                np.testing.assert_array_equal(time_s, [0, 10])
                np.testing.assert_array_equal(current_a, [0.1, 0.2])
                np.testing.assert_array_equal(voltage_v, [3.7, 3.8])


if __name__ == "__main__":
    unittest.main()
