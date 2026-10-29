"""Explain the scalar KF's voltage-model error using an offline reference.

The scalar KF assumes V = b + m*SOC - I*R0. Inverting that equation gives
SOC_voltage = (V_measured + I_measured*R0 - b)/m. Its error can be split into
four voltage terms before dividing by m and converting to percentage points:

    OCV(SOC_reference) - (b + m*SOC_reference)       [OCV-line approximation]
  + V_clean - (OCV(SOC_reference) - I_clean*R0)     [unmodeled dynamics]
  + V_measured - V_clean                          [voltage sensor error]
  + R0*(I_measured - I_clean)                      [current sensor error]

This identity describes unclipped, voltage-implied SOC, rather than the KF's
actual output. The KF also combines prior SOC/current information and clips
SOC. Reference SOC is used here only after estimation, for diagnosis; none of
these quantities should be fed back into the filters or used to tune them on
the evaluation profiles. A positive contribution implies SOC overestimation.
"""

import numpy as np


def diagnose_kf_model(data, linear_ocv_model, ocv_function, r0_ohm):
    """Return time-weighted model/sensor contributions as JSON-native floats.

    SOC arrays contain fractions from 0 to 1. Endpoint roundoff up to 1e-8 is
    tolerated; only the OCV lookup query is clipped to its strict [0, 1] domain.
    The supplied reference arrays remain unchanged. All signed contribution means
    and total error use SOC percentage points (pp); local OCV-line RMSE and
    maximum absolute error use millivolts (mV). Trapezoidal time weighting
    permits irregular samples and duplicate timestamps at current switches.

    ``unmodeled_dynamics`` includes every clean-terminal-voltage discrepancy
    from OCV minus the supplied R0 drop, including polarization, hysteresis,
    temperature dependence, and R0/model mismatch when present. This function
    diagnoses supplied data without fitting parameters or running estimators.
    """
    keys = (
        "time_s", "current_a", "voltage_v", "true_soc",
        "measured_current_a", "measured_voltage_v",
    )
    try:
        arrays = {key: np.asarray(data[key], dtype=float) for key in keys}
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Diagnostic data must contain the six numeric measurement arrays.") from error
    time = arrays["time_s"]
    if any(value.ndim != 1 for value in arrays.values()):
        raise ValueError("Diagnostic arrays must be one-dimensional.")
    if time.size < 2 or any(value.shape != time.shape for value in arrays.values()):
        raise ValueError("Diagnostic arrays must be aligned with at least two samples.")
    if not all(np.isfinite(value).all() for value in arrays.values()):
        raise ValueError("Diagnostic arrays must be finite.")
    if np.any(np.diff(time) < 0) or time[-1] <= time[0]:
        raise ValueError("Diagnostic timestamps must be chronological and span positive time.")
    reference = arrays["true_soc"]
    if np.any((reference < -1e-8) | (reference > 1 + 1e-8)):
        raise ValueError("Reference SOC must contain fractions between 0 and 1.")

    try:
        slope = float(linear_ocv_model["ocv_slope_v_per_soc"])
        intercept = float(linear_ocv_model["ocv_intercept_v"])
        resistance = float(r0_ohm)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("Supply a numeric OCV slope/intercept and R0 resistance.") from error
    if not np.isfinite(slope) or slope <= 0 or not np.isfinite(intercept):
        raise ValueError("The OCV line must have a finite intercept and positive finite slope.")
    if not np.isfinite(resistance) or resistance < 0:
        raise ValueError("R0 must be finite and nonnegative.")
    # The filter is given fitted endpoints, while this identity uses b and m.
    # Reject inconsistent representations instead of explaining another model.
    for key, expected in (
        ("ocv_at_empty_v", intercept), ("ocv_at_full_v", intercept + slope),
    ):
        if key in linear_ocv_model:
            try:
                endpoint = float(linear_ocv_model[key])
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError("OCV line endpoints must be finite numeric values.") from error
            if not np.isfinite(endpoint) or not np.isclose(endpoint, expected, rtol=1e-10, atol=1e-12):
                raise ValueError("OCV line endpoints must match its slope and intercept.")
    if not callable(ocv_function):
        raise ValueError("ocv_function must be callable.")
    try:
        ocv = np.asarray(ocv_function(np.clip(reference, 0.0, 1.0)), dtype=float)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("ocv_function must evaluate the reference SOC array.") from error
    if ocv.shape != reference.shape or not np.isfinite(ocv).all():
        raise ValueError("ocv_function must return one finite voltage per reference sample.")

    line_voltage = intercept + slope * reference
    line_error_v = ocv - line_voltage
    contributions_v = {
        "ocv_nonlinearity_mean_pp": line_error_v,
        "unmodeled_dynamics_mean_pp": (
            arrays["voltage_v"] - (ocv - arrays["current_a"] * resistance)
        ),
        "voltage_sensor_error_mean_pp": arrays["measured_voltage_v"] - arrays["voltage_v"],
        "current_sensor_error_mean_pp": resistance * (
            arrays["measured_current_a"] - arrays["current_a"]
        ),
    }
    # Compute the total independently so residual closure verifies the identity.
    implied_soc = (
        arrays["measured_voltage_v"] + arrays["measured_current_a"] * resistance - intercept
    ) / slope
    total_error_pp = (implied_soc - reference) * 100
    contribution_sum_pp = sum(contributions_v.values()) * (100 / slope)
    duration = float(time[-1] - time[0])

    def time_mean(values):
        return float(np.trapezoid(values, time) / duration)

    return {
        **{key: time_mean(values) * 100 / slope for key, values in contributions_v.items()},
        "voltage_implied_soc_error_mean_pp": time_mean(total_error_pp),
        "linear_ocv_local_rmse_mv": float(np.sqrt(time_mean(line_error_v**2)) * 1000),
        "linear_ocv_local_max_error_mv": float(np.max(np.abs(line_error_v)) * 1000),
        "decomposition_residual_max_abs_pp": float(np.max(np.abs(total_error_pp - contribution_sum_pp))),
        "duration_s": duration,
        "reference_soc_min": float(np.min(reference)),
        "reference_soc_max": float(np.max(reference)),
    }
