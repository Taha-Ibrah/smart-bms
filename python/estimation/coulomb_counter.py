"""
Coulomb counting: SOC estimation method

Quick notes:
- Estimates Sate of Charge (SOC) by integrating measured current over elapsed time.
- Positive current means discharge in this project, so it decreases SOC.
- Requires an initial SOC value and the battery's nominal capacity in Ah.
- Uses: SOC[k] = SOC[k-1] - current * dt / (3600 * capacity).
- Current bias and capacity error accumulate over time and cause SOC drift.
- It has no voltage-based correction, so an incorrect initial SOC persists.
- The final estimate should be limited to the valid range from 0.0 to 1.0.
"""

import numpy as np


def estimate_soc_coulomb_counting(
    time_s,
    measured_current_a,
    nominal_capacity_ah,
    initial_soc,
):
    """Estimate battery SOC by integrating measured current over time."""
    # Convert the inputs to floating-point arrays for consistent calculations.
    time_s = np.asarray(time_s, dtype=float)
    measured_current_a = np.asarray(measured_current_a, dtype=float)

    # Reject invalid inputs before beginning the SOC calculation.
    if time_s.ndim != 1 or measured_current_a.ndim != 1:
        raise ValueError("Time and current inputs must be one-dimensional arrays.")
    if time_s.size == 0:
        raise ValueError("Time and current inputs cannot be empty.")
    if time_s.shape != measured_current_a.shape:
        raise ValueError("Time and current inputs must have the same length.")
    if not np.isfinite(time_s).all() or not np.isfinite(measured_current_a).all():
        raise ValueError("Time and current inputs must contain finite values.")
    if not np.isfinite(nominal_capacity_ah) or nominal_capacity_ah <= 0:
        raise ValueError("Nominal capacity must be greater than zero.")
    if not np.isfinite(initial_soc):
        raise ValueError("Initial SOC must be a finite value.")
    if np.any(np.diff(time_s) < 0):
        raise ValueError("Timestamps must be in chronological order.")

    # Each timestamp receives one SOC estimate, starting from the supplied value.
    estimated_soc = np.empty(time_s.size, dtype=float)
    estimated_soc[0] = np.clip(initial_soc, 0.0, 1.0) #forces intial_soc value within 0 and 1

    # Process every measurement interval after the initial sample.
    for index in range(1, time_s.size):
        dt_s = time_s[index] - time_s[index - 1]

        # Average the current at both ends of the interval.
        average_current_a = (
            measured_current_a[index - 1] + measured_current_a[index]
        ) / 2.0

        # Dividing by 3600 converts amp-seconds into amp-hours.
        charge_removed_ah = average_current_a * dt_s / 3600.0

        # Positive current represents discharge, so it lowers the estimated SOC.
        estimated_soc[index] = estimated_soc[index - 1] - (
            charge_removed_ah / nominal_capacity_ah
        )

        # Prevent numerical estimates outside the physical SOC range.
        estimated_soc[index] = np.clip(estimated_soc[index], 0.0, 1.0)

    # The returned array aligns element-for-element with the input timestamps.
    return estimated_soc
