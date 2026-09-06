"""
Why an EKF is better suited to battery SOC estimation:
- A standard Kalman filter assumes that both the state and measurement models
  are linear. A lithium-ion battery's open-circuit voltage does not change
  linearly with SOC, especially near very low and very high SOC.
- Terminal voltage also includes the immediate voltage drop across internal
  resistance and the slower polarization response inside the battery.
- An Extended Kalman Filter can use these nonlinear battery equations. At every
  sample, it calculates Jacobians that locally approximate the nonlinear model
  around the current state estimate.
- This gives voltage corrections a more realistic relationship to SOC and should
  improve convergence and reduce drift when the battery model is well calibrated.
- An EKF is not automatically accurate: its OCV curve, equivalent-circuit
  parameters, sensor-noise assumptions, and covariance tuning must be valid.

Quick notes:
- The planned state vector is [SOC, polarization voltage].
- Measured current is the control input used during the prediction.
- Measured terminal voltage is the observation used during correction.
- SOC prediction still begins with Coulomb counting.
- A first-order Thevenin equivalent-circuit model uses R0 for the immediate
  voltage drop and an R1-C1 branch for the slower polarization response.
- Predicted terminal voltage is OCV(SOC) - current * R0 - polarization voltage.
- The measurement Jacobian contains the slope of the nonlinear OCV-SOC curve.
- Q represents uncertainty in the battery state model, R represents voltage
  measurement uncertainty, and P represents state-estimation uncertainty.
- Positive current means discharge in this project, so it decreases SOC.
- True SOC is reserved for evaluation and must not be given to the EKF.
- The returned SOC estimate should remain between 0.0 and 1.0.
"""

import numpy as np


def _evaluate_ocv(ocv_function, soc):
    """Evaluate the supplied OCV function and require one finite voltage value."""
    ocv_v = np.asarray(ocv_function(soc), dtype=float)
    if ocv_v.size != 1 or not np.isfinite(ocv_v).all():
        raise ValueError("The OCV function must return one finite voltage value.")
    return float(ocv_v.item())


def _calculate_ocv_slope(
    ocv_function,
    soc,
    ocv_derivative_function,
    derivative_step,
):
    """Return dOCV/dSOC from a supplied derivative or a numerical approximation."""
    if ocv_derivative_function is not None:
        slope = np.asarray(ocv_derivative_function(soc), dtype=float)
        if slope.size != 1 or not np.isfinite(slope).all():
            raise ValueError(
                "The OCV derivative function must return one finite value."
            )
        return float(slope.item())

    # Use central differences inside the SOC range and one-sided differences at
    # the boundaries. This keeps the OCV function from receiving an invalid SOC.
    lower_soc = max(0.0, soc - derivative_step)
    upper_soc = min(1.0, soc + derivative_step)
    lower_ocv_v = _evaluate_ocv(ocv_function, lower_soc)
    upper_ocv_v = _evaluate_ocv(ocv_function, upper_soc)
    return (upper_ocv_v - lower_ocv_v) / (upper_soc - lower_soc)


def estimate_soc_ekf(
    time_s,
    measured_current_a,
    measured_voltage_v,
    nominal_capacity_ah,
    initial_soc,
    ocv_function,
    r0_ohm,
    r1_ohm,
    c1_f,
    initial_polarization_voltage_v=0.0,
    ocv_derivative_function=None,
    process_soc_variance_per_s=1e-8,
    process_polarization_variance_v2_per_s=1e-6,
    measurement_variance_v2=0.02**2,
    initial_soc_variance=0.05**2,
    initial_polarization_variance_v2=0.05**2,
    derivative_step=1e-5,
):
    """Estimate SOC using a two-state Extended Kalman Filter.

    The filter state is ``[SOC, polarization voltage]``. Measured current drives
    the state prediction, while measured terminal voltage corrects both states.

    Parameters:
        time_s: Sensor timestamps in seconds.
        measured_current_a: Noisy battery-current measurements in amperes.
        measured_voltage_v: Noisy terminal-voltage measurements in volts.
        nominal_capacity_ah: Rated battery capacity in amp-hours.
        initial_soc: Starting SOC estimate from 0.0 to 1.0.
        ocv_function: Callable that maps SOC to open-circuit voltage in volts.
        r0_ohm: Series resistance responsible for the immediate voltage drop.
        r1_ohm: Polarization resistance in the first-order RC branch.
        c1_f: Polarization capacitance in farads.
        initial_polarization_voltage_v: Starting RC-branch voltage estimate.
        ocv_derivative_function: Optional callable that returns dOCV/dSOC.
        process_soc_variance_per_s: SOC-model uncertainty added per second.
        process_polarization_variance_v2_per_s: RC-model uncertainty per second.
        measurement_variance_v2: Terminal-voltage uncertainty in volts squared.
        initial_soc_variance: Initial uncertainty in the SOC state.
        initial_polarization_variance_v2: Initial uncertainty in the RC state.
        derivative_step: SOC step used for a numerical OCV derivative.

    Returns:
        A NumPy array of SOC estimates aligned with the input timestamps.
    """
    # CONVERT THE SENSOR INPUTS INTO CONSISTENT FLOATING-POINT ARRAYS.
    time_s = np.asarray(time_s, dtype=float)
    measured_current_a = np.asarray(measured_current_a, dtype=float)
    measured_voltage_v = np.asarray(measured_voltage_v, dtype=float)

    # VERIFY THAT EVERY SENSOR SAMPLE IS VALID AND TIME-ALIGNED.
    if (
        time_s.ndim != 1
        or measured_current_a.ndim != 1
        or measured_voltage_v.ndim != 1
    ):
        raise ValueError("Time, current, and voltage must be one-dimensional arrays.")
    if time_s.size == 0:
        raise ValueError("Sensor inputs cannot be empty.")
    if not (
        time_s.shape == measured_current_a.shape == measured_voltage_v.shape
    ):
        raise ValueError("Time, current, and voltage must have the same length.")
    if not (
        np.isfinite(time_s).all()
        and np.isfinite(measured_current_a).all()
        and np.isfinite(measured_voltage_v).all()
    ):
        raise ValueError("Sensor inputs must contain finite values.")
    if np.any(np.diff(time_s) < 0):
        raise ValueError("Timestamps must be in chronological order.")

    # VERIFY THE BATTERY PARAMETERS USED BY THE THEVENIN EQUIVALENT CIRCUIT.
    if not np.isfinite(nominal_capacity_ah) or nominal_capacity_ah <= 0:
        raise ValueError("Nominal capacity must be greater than zero.")
    if not np.isfinite(initial_soc):
        raise ValueError("Initial SOC must be finite.")
    if not np.isfinite(initial_polarization_voltage_v):
        raise ValueError("Initial polarization voltage must be finite.")
    if not np.isfinite(r0_ohm) or r0_ohm < 0:
        raise ValueError("R0 cannot be negative.")
    if not np.isfinite(r1_ohm) or r1_ohm <= 0:
        raise ValueError("R1 must be greater than zero.")
    if not np.isfinite(c1_f) or c1_f <= 0:
        raise ValueError("C1 must be greater than zero.")
    if not callable(ocv_function):
        raise ValueError("ocv_function must be callable.")
    if ocv_derivative_function is not None and not callable(
        ocv_derivative_function
    ):
        raise ValueError("ocv_derivative_function must be callable when supplied.")
    if not np.isfinite(derivative_step) or derivative_step <= 0:
        raise ValueError("Derivative step must be greater than zero.")

    # VERIFY THAT ALL COVARIANCES REPRESENT NONNEGATIVE UNCERTAINTY.
    variances = (
        process_soc_variance_per_s,
        process_polarization_variance_v2_per_s,
        initial_soc_variance,
        initial_polarization_variance_v2,
    )
    if not all(np.isfinite(value) and value >= 0 for value in variances):
        raise ValueError("Process and initial variances cannot be negative.")
    if not np.isfinite(measurement_variance_v2) or measurement_variance_v2 <= 0:
        raise ValueError("Measurement variance must be greater than zero.")

    # CHECK THE OCV MODEL BEFORE ENTERING THE FILTER LOOP.
    # Evaluating both endpoints catches many invalid lookup tables or functions
    # before the EKF has partially processed a dataset.
    _evaluate_ocv(ocv_function, 0.0)
    _evaluate_ocv(ocv_function, 1.0)

    # CREATE THE ARRAYS AND MATRICES USED TO STORE THE EKF STATE.
    estimated_soc = np.empty(time_s.size, dtype=float)
    identity_matrix = np.eye(2, dtype=float)

    # x contains the two current state estimates:
    # x[0] = SOC, a unitless fraction from 0.0 to 1.0.
    # x[1] = polarization voltage across the R1-C1 branch in volts.
    state = np.array(
        [
            np.clip(initial_soc, 0.0, 1.0),
            initial_polarization_voltage_v,
        ],
        dtype=float,
    )

    # P is the covariance matrix. Its diagonal entries contain the uncertainty
    # of each state. Zero off-diagonal values initially assume no correlation.
    covariance = np.diag(
        [initial_soc_variance, initial_polarization_variance_v2]
    ).astype(float)

    # PROCESS EVERY SAMPLE THROUGH AN EKF PREDICTION AND CORRECTION.
    for index in range(time_s.size):
        if index == 0:
            # USE THE INITIAL STATE AS THE FIRST PREDICTION.
            # No current integration is possible until a second timestamp exists.
            predicted_state = state.copy()
            predicted_covariance = covariance.copy()
        else:
            # CALCULATE THE SAMPLE INTERVAL AND CURRENT APPLIED DURING IT.
            dt_s = time_s[index] - time_s[index - 1]
            average_current_a = (
                measured_current_a[index - 1] + measured_current_a[index]
            ) / 2.0

            # DISCRETIZE THE R1-C1 POLARIZATION RESPONSE FOR THIS TIME STEP.
            # The exponential term describes how much previous polarization
            # remains after dt seconds. A value near one means slow relaxation.
            polarization_decay = np.exp(-dt_s / (r1_ohm * c1_f))

            # PREDICT SOC BY INTEGRATING THE MEASURED CURRENT.
            # Positive current means discharge, so removed charge reduces SOC.
            charge_removed_ah = average_current_a * dt_s / 3600.0
            predicted_soc = state[0] - charge_removed_ah / nominal_capacity_ah

            # PREDICT THE RC-BRANCH POLARIZATION VOLTAGE.
            # Under steady positive current, this state approaches I * R1. When
            # current becomes zero, it exponentially relaxes back toward zero.
            predicted_polarization_voltage_v = (
                polarization_decay * state[1]
                + r1_ohm * (1.0 - polarization_decay) * average_current_a
            )

            predicted_state = np.array(
                [
                    np.clip(predicted_soc, 0.0, 1.0),
                    predicted_polarization_voltage_v,
                ],
                dtype=float,
            )

            # BUILD THE STATE-TRANSITION JACOBIAN F.
            # With fixed R1 and C1, SOC keeps its previous sensitivity of one,
            # while polarization retains the exponential decay sensitivity.
            state_transition_jacobian = np.array(
                [
                    [1.0, 0.0],
                    [0.0, polarization_decay],
                ],
                dtype=float,
            )

            # BUILD Q, THE PROCESS-NOISE COVARIANCE FOR THIS SAMPLE INTERVAL.
            # The two diagonal values represent uncertainty in current-based SOC
            # prediction and in the simplified R1-C1 polarization model.
            process_covariance = np.diag(
                [
                    process_soc_variance_per_s * dt_s,
                    process_polarization_variance_v2_per_s * dt_s,
                ]
            )

            # PROPAGATE STATE UNCERTAINTY THROUGH THE PREDICTION MODEL.
            predicted_covariance = (
                state_transition_jacobian
                @ covariance
                @ state_transition_jacobian.T
                + process_covariance
            )

        # EVALUATE THE NONLINEAR OCV MODEL AT THE PREDICTED SOC.
        predicted_soc = predicted_state[0]
        predicted_ocv_v = _evaluate_ocv(ocv_function, predicted_soc)

        # PREDICT THE TERMINAL VOLTAGE USING THE THEVENIN MODEL.
        # R0 produces the immediate I*R voltage drop, while the second state
        # represents the slower polarization voltage drop.
        predicted_terminal_voltage_v = (
            predicted_ocv_v
            - measured_current_a[index] * r0_ohm
            - predicted_state[1]
        )

        # CALCULATE THE LOCAL SLOPE OF THE NONLINEAR OCV-SOC CURVE.
        # This derivative is what turns the nonlinear measurement equation into
        # a local linear approximation for the current EKF update.
        ocv_slope_v_per_soc = _calculate_ocv_slope(
            ocv_function,
            predicted_soc,
            ocv_derivative_function,
            derivative_step,
        )

        # BUILD THE MEASUREMENT JACOBIAN H = [dOCV/dSOC, -1].
        # The first element describes voltage sensitivity to SOC. The second is
        # -1 because greater polarization reduces measured terminal voltage.
        measurement_jacobian = np.array(
            [ocv_slope_v_per_soc, -1.0],
            dtype=float,
        )

        # CALCULATE THE VOLTAGE RESIDUAL BETWEEN MEASUREMENT AND MODEL.
        voltage_residual_v = (
            measured_voltage_v[index] - predicted_terminal_voltage_v
        )

        # CALCULATE S, THE EXPECTED VARIANCE OF THE VOLTAGE RESIDUAL.
        # S combines projected state uncertainty with voltage-measurement and
        # unmodeled-voltage uncertainty represented by R.
        residual_variance_v2 = float(
            measurement_jacobian
            @ predicted_covariance
            @ measurement_jacobian.T
            + measurement_variance_v2
        )
        if not np.isfinite(residual_variance_v2) or residual_variance_v2 <= 0:
            raise ValueError("Calculated voltage-residual variance is invalid.")

        # CALCULATE THE TWO-ELEMENT KALMAN GAIN.
        # One gain corrects SOC and the other corrects polarization voltage using
        # the same terminal-voltage residual.
        kalman_gain = (
            predicted_covariance @ measurement_jacobian.T
        ) / residual_variance_v2

        # CORRECT BOTH STATE ESTIMATES USING THE MEASURED TERMINAL VOLTAGE.
        state = predicted_state + kalman_gain * voltage_residual_v
        state[0] = np.clip(state[0], 0.0, 1.0)
        estimated_soc[index] = state[0]

        # UPDATE P AFTER THE VOLTAGE MEASUREMENT HAS REDUCED UNCERTAINTY.
        # Joseph form is used because it better preserves a valid covariance
        # matrix when floating-point calculations introduce small rounding errors.
        correction_matrix = identity_matrix - np.outer(
            kalman_gain,
            measurement_jacobian,
        )
        covariance = (
            correction_matrix
            @ predicted_covariance
            @ correction_matrix.T
            + np.outer(kalman_gain, kalman_gain) * measurement_variance_v2
        )

        # Force exact symmetry because covariance should be symmetric by definition.
        covariance = 0.5 * (covariance + covariance.T)

    # RETURN ONE SOC ESTIMATE FOR EACH INPUT SENSOR SAMPLE.
    return estimated_soc
