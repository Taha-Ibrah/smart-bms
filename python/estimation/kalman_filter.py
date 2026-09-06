"""
Kalman filter: linear SOC estimation method

Quick notes:
- A Kalman filter estimates a state that cannot be measured directly, such as SOC.
- The prediction step uses measured current to advance SOC by Coulomb counting.
- The correction step compares measured voltage with predicted battery voltage.
- The Kalman gain decides how much to trust the prediction versus the measurement.
- P represents uncertainty in the SOC estimate, Q represents process uncertainty,
  and R represents voltage-measurement and model uncertainty.
- A standard Kalman filter requires a linear model. This implementation approximates
  open-circuit voltage as a straight line between empty and full battery voltage.
- Real battery voltage is nonlinear with SOC, so this is a baseline estimator. The
  later EKF will use a nonlinear OCV-SOC relationship and an equivalent-circuit model.
"""

import numpy as np


def estimate_soc_kalman_filter(
    time_s, #sensor data
    measured_current_a, #sensor data
    measured_voltage_v, #sensor data
    nominal_capacity_ah, #battery specs
    initial_soc,
    ocv_at_empty_v, #battery specs
    ocv_at_full_v, #battery specs
    internal_resistance_ohm=0.0, #battery specs
    #initial tuning values: The following values have not been calibrated yet
    #variance is statistical measurement that tells you how spread out the numbers are in a data set from their mean value.
    process_variance_per_s=1e-8,
    measurement_variance_v2=0.05**2,
    initial_soc_variance=0.05**2,
):
    """Estimate SOC with a one-state linear Kalman filter.

    SOC is the filter state, measured current is the control input, and terminal
    voltage is the measurement used to correct the predicted SOC.

    Parameters:
        time_s: Sensor timestamps in seconds.
        measured_current_a: Noisy current measurements in amperes.
        measured_voltage_v: Noisy terminal-voltage measurements in volts.
        nominal_capacity_ah: Rated battery capacity in amp-hours.
        initial_soc: Starting SOC estimate expressed from 0.0 to 1.0.
        ocv_at_empty_v: Approximate open-circuit voltage at 0% SOC.
        ocv_at_full_v: Approximate open-circuit voltage at 100% SOC.
        internal_resistance_ohm: Fixed resistance used to estimate the load drop.
        process_variance_per_s: Rate at which prediction uncertainty grows.
        measurement_variance_v2: Uncertainty assigned to the voltage model.
        initial_soc_variance: Uncertainty assigned to the initial SOC estimate.

    Returns:
        A NumPy array of SOC estimates aligned with the input timestamps.
    """
    # Convert sensor inputs to floating-point arrays before validating them.
    time_s = np.asarray(time_s, dtype=float)
    measured_current_a = np.asarray(measured_current_a, dtype=float)
    measured_voltage_v = np.asarray(measured_voltage_v, dtype=float)

    # All three inputs describe the same sequence of sensor samples.
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

    # A zero or negative capacity would make the SOC update undefined.
    if not np.isfinite(nominal_capacity_ah) or nominal_capacity_ah <= 0:
        raise ValueError("Nominal capacity must be greater than zero.")

    # SOC may be outside 0.0 to 1.0 initially, but it must still be a real number.
    # The value is clipped into the physical range before filtering begins.
    if not np.isfinite(initial_soc):
        raise ValueError("Initial SOC must be a finite value.")

    # These endpoints define the straight-line approximation of OCV versus SOC.
    if not np.isfinite(ocv_at_empty_v) or not np.isfinite(ocv_at_full_v):
        raise ValueError("Empty and full open-circuit voltages must be finite.")
    if ocv_at_full_v <= ocv_at_empty_v:
        raise ValueError("Full open-circuit voltage must exceed empty voltage.")
    if not np.isfinite(internal_resistance_ohm) or internal_resistance_ohm < 0:
        raise ValueError("Internal resistance cannot be negative.")
    # Variances describe uncertainty, so negative values are not meaningful.
    if not np.isfinite(process_variance_per_s) or process_variance_per_s < 0:
        raise ValueError("Process variance cannot be negative.")
    if not np.isfinite(measurement_variance_v2) or measurement_variance_v2 <= 0:
        raise ValueError("Measurement variance must be greater than zero.")
    if not np.isfinite(initial_soc_variance) or initial_soc_variance < 0:
        raise ValueError("Initial SOC variance cannot be negative.")

    # CREATE THE OUTPUT ARRAY THAT WILL STORE ONE SOC ESTIMATE PER SAMPLE.
    # Reserve one output position for every sensor sample.
    estimated_soc = np.empty(time_s.size, dtype=float)

    # BUILD THE LINEAR OPEN-CIRCUIT-VOLTAGE MODEL USED BY THE FILTER.
    # The linear measurement model is OCV = intercept + slope * SOC.
    # At SOC = 0, the equation returns ocv_at_empty_v. At SOC = 1, it
    # returns ocv_at_full_v.
    ocv_intercept_v = ocv_at_empty_v
    ocv_slope_v_per_soc = ocv_at_full_v - ocv_at_empty_v

    # INITIALIZE THE SOC STATE AND ITS STARTING UNCERTAINTY.
    # Begin with the supplied SOC estimate and our uncertainty about that value.
    soc_estimate = np.clip(initial_soc, 0.0, 1.0)
    soc_variance = initial_soc_variance

    # PROCESS EACH SENSOR SAMPLE THROUGH THE PREDICTION AND CORRECTION STEPS.
    # Each loop iteration performs one prediction followed by one correction.
    for index in range(time_s.size):
        if index == 0:
            # USE THE INITIAL STATE AS THE FIRST PREDICTION.
            # There is no earlier sample, so the first prediction is the initial SOC.
            predicted_soc = soc_estimate
            predicted_variance = soc_variance
        else:
            # CALCULATE THE TIME ELAPSED SINCE THE PREVIOUS SENSOR SAMPLE.
            dt_s = time_s[index] - time_s[index - 1]

            # CALCULATE THE AVERAGE CURRENT FLOWING DURING THIS TIME INTERVAL.
            # Average current improves integration across each sample interval.
            average_current_a = (
                measured_current_a[index - 1] + measured_current_a[index]
            ) / 2.0

            # CONVERT CURRENT OVER TIME INTO AMP-HOURS OF CHARGE REMOVED.
            # Current multiplied by time gives charge. Dividing by 3600 changes
            # amp-seconds into amp-hours so it matches nominal_capacity_ah.
            charge_removed_ah = average_current_a * dt_s / 3600.0

            # PREDICT THE NEW SOC USING COULOMB COUNTING.
            # Prediction: advance SOC using the same sign convention as PyBaMM.
            predicted_soc = soc_estimate - (
                charge_removed_ah / nominal_capacity_ah
            )

            # INCREASE THE PREDICTION UNCERTAINTY ACCORDING TO ELAPSED TIME.
            # Q represents current-integration and model uncertainty. Scaling it
            # by dt means a longer interval creates more prediction uncertainty.
            predicted_variance = (
                soc_variance + process_variance_per_s * dt_s
            )

        # PREDICT TERMINAL VOLTAGE FROM THE PREDICTED SOC AND MEASURED CURRENT.
        # Measurement model:
        # terminal voltage = estimated OCV - current * internal resistance.
        # Positive discharge current lowers terminal voltage by the I * R drop.
        predicted_voltage_v = (
            ocv_intercept_v
            + ocv_slope_v_per_soc * predicted_soc
            - measured_current_a[index] * internal_resistance_ohm
        )

        # CALCULATE THE VOLTAGE RESIDUAL AND ITS TOTAL UNCERTAINTY.
        # Innovation, also called the residual, shows how far the measured voltage
        # is from the voltage predicted using the current SOC estimate.
        voltage_residual_v = measured_voltage_v[index] - predicted_voltage_v
        residual_variance_v2 = (
            ocv_slope_v_per_soc**2 * predicted_variance
            + measurement_variance_v2
        )

        # CALCULATE HOW STRONGLY THE VOLTAGE MEASUREMENT SHOULD CORRECT SOC.
        # The denominator combines prediction uncertainty and measurement
        # uncertainty. A larger Kalman gain gives voltage more influence.
        kalman_gain = (
            predicted_variance
            * ocv_slope_v_per_soc
            / residual_variance_v2
        )

        # CORRECT THE SOC PREDICTION AND STORE THE RESULT.
        # Correction: move the predicted SOC in the direction indicated by the
        # voltage residual. Clipping prevents an impossible reported SOC.
        soc_estimate = predicted_soc + kalman_gain * voltage_residual_v
        soc_estimate = np.clip(soc_estimate, 0.0, 1.0)
        estimated_soc[index] = soc_estimate

        # UPDATE THE SOC UNCERTAINTY AFTER USING THE VOLTAGE MEASUREMENT.
        # After using a measurement, uncertainty normally decreases. This
        # Joseph-form scalar update helps keep the variance nonnegative despite
        # floating-point rounding.
        correction_factor = 1.0 - kalman_gain * ocv_slope_v_per_soc
        soc_variance = (
            correction_factor**2 * predicted_variance
            + kalman_gain**2 * measurement_variance_v2
        )

    # RETURN THE COMPLETE TIME-ALIGNED SOC ESTIMATE.
    # Each returned SOC value corresponds to the input sample at the same index.
    return estimated_soc
