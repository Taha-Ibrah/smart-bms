"""Fit a one-RC equivalent circuit to the simulated battery pulse response.

R0 comes from the immediate voltage jumps. SciPy fits R1 and the time constant
tau = R1 * C1 to the slower response, after accounting for changing OCV/SOC.
Running this file fits one pulse, predicts a different pulse without refitting,
and plots voltage predictions and errors. These are local surrogate-model
parameters, not measured EEMB specifications or SOC estimator accuracy results.
"""

import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import least_squares

if __package__:
    from .ocv_soc import generate_ocv_soc_curve, build_nonlinear_ocv_model
    from .pulse_test import run_pulse_test
else:
    from ocv_soc import generate_ocv_soc_curve, build_nonlinear_ocv_model
    from pulse_test import run_pulse_test


def _validate_series(time_s, current_a, soc):
    """Require finite, aligned arrays; allow two sides of a switch at one time."""
    time, current, state = (np.asarray(value, dtype=float) for value in (time_s, current_a, soc))
    if any(value.ndim != 1 for value in (time, current, state)):
        raise ValueError("Time, current, and SOC must be one-dimensional arrays.")
    if time.size == 0 or not (time.shape == current.shape == state.shape):
        raise ValueError("Time, current, and SOC must be nonempty and aligned.")
    if not all(np.isfinite(value).all() for value in (time, current, state)):
        raise ValueError("Time, current, and SOC must contain finite values.")
    if np.any(np.diff(time) < 0):
        raise ValueError("Timestamps must be in chronological order.")
    if np.any((state < -1e-10) | (state > 1 + 1e-10)):
        raise ValueError("SOC must be a fraction between 0 and 1.")
    return time, current, np.clip(state, 0, 1)


def _ocv_values(ocv_function, soc):
    if not callable(ocv_function):
        raise ValueError("ocv_function must be callable.")
    voltage = np.asarray(ocv_function(soc), dtype=float)
    # A constant-OCV function is also valid for synthetic circuit tests.
    if voltage.ndim == 0:
        voltage = np.full(soc.shape, float(voltage))
    if voltage.shape != soc.shape or not np.isfinite(voltage).all():
        raise ValueError("The OCV function must return finite, aligned voltages.")
    return voltage


def _polarization_voltage(time, current, r1_ohm, tau_s, initial_voltage_v=0.0):
    """Advance the RC voltage exactly for current held between sample times."""
    dt = np.diff(time)
    decay = np.exp(-dt / tau_s)
    # expm1 retains precision when dt is tiny at a current switch.
    drive = r1_ohm * (-np.expm1(-dt / tau_s)) * current[:-1]
    polarization = np.empty(time.size)
    polarization[0] = initial_voltage_v
    for index in range(1, time.size):
        polarization[index] = decay[index - 1] * polarization[index - 1] + drive[index - 1]
    return polarization


def predict_ecm_voltage(
    time_s, current_a, soc, ocv_function, r0_ohm, r1_ohm, c1_f,
    initial_polarization_voltage_v=0.0,
):
    """Return terminal voltage from supplied SOC/current and circuit parameters.

    Voltage is OCV(SOC) minus the R0 current drop and RC polarization voltage.
    Current at sample k is held until sample k+1; duplicate switch timestamps
    preserve the instantaneous R0 jump while polarization remains continuous.
    This predicts voltage from a supplied SOC trajectory; it does not estimate
    SOC. Reference SOC is used here only for offline model identification/tests.
    """
    time, current, state = _validate_series(time_s, current_a, soc)
    for name, value, allow_zero in (
        ("R0", r0_ohm, True), ("R1", r1_ohm, False), ("C1", c1_f, False),
    ):
        if not np.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
            raise ValueError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}.")
    tau_s = r1_ohm * c1_f
    if not np.isfinite(tau_s) or tau_s <= 0 or not np.isfinite(initial_polarization_voltage_v):
        raise ValueError("Time constant and initial polarization voltage must be finite and valid.")
    ocv = _ocv_values(ocv_function, state)
    polarization = _polarization_voltage(time, current, r1_ohm, tau_s, initial_polarization_voltage_v)
    return ocv - r0_ohm * current - polarization


def _pulse_window(pulse_data):
    """Validate a clean 0/I/0 pulse and retain its final pre-rest sample onward."""
    try:
        time, current, soc = _validate_series(
            pulse_data["time_s"], pulse_data["current_a"], pulse_data["true_soc"]
        )
        voltage = np.asarray(pulse_data["voltage_v"], dtype=float)
        start = pulse_data["pulse_start_index"]
        recovery = pulse_data["recovery_start_index"]
    except (KeyError, TypeError) as error:
        raise ValueError("Pulse data must include measurements, reference SOC, and switch indices.") from error
    if voltage.shape != time.shape or not np.isfinite(voltage).all():
        raise ValueError("Pulse voltages must be finite and aligned with timestamps.")
    if not all(isinstance(index, (int, np.integer)) and not isinstance(index, bool) for index in (start, recovery)):
        raise ValueError("Switch indices must be integers.")
    if not (1 <= start < recovery - 1 < time.size - 2):
        raise ValueError("Pulse data needs pre-rest, pulse, and recovery samples.")
    if not (time[recovery - 1] > time[start] and time[-1] > time[recovery]):
        raise ValueError("Pulse and recovery must each span positive time.")
    if any(time[index] - time[index - 1] > 1e-6 for index in (start, recovery)):
        raise ValueError("Preserve both sides of current switches; do not resample the jumps away.")
    pulse_current = current[start]
    if pulse_current <= 0 or not (
        np.allclose(current[:start], 0, rtol=0, atol=1e-12)
        and np.allclose(current[start:recovery], pulse_current, rtol=1e-9, atol=1e-12)
        and np.allclose(current[recovery:], 0, rtol=0, atol=1e-12)
    ):
        raise ValueError("Identification requires a clean zero-current/discharge/zero-current pulse.")

    # Early pre-rest contains relaxation from conditioning. Starting at its
    # final sample lets the circuit use zero initial polarization after resting.
    window = slice(start - 1, None)
    return {
        "time_s": time[window], "current_a": current[window],
        "true_soc": soc[window], "voltage_v": voltage[window],
        "pulse_start_index": 1,
        "recovery_start_index": recovery - start + 1,
    }


def evaluate_ecm(pulse_data, ocv_function, parameters):
    """Predict a pulse using fixed parameters and report unweighted voltage errors.

    Evaluate from the final pre-rest sample onward, assuming sufficient rest
    for zero initial RC voltage. Baseline mismatch is reported, not rejected.
    No parameter is adjusted here. Use a separate pulse for held-out validation.
    All error values are in volts; they measure circuit voltage fit, not SOC.
    """
    data = _pulse_window(pulse_data)
    try:
        circuit = {name: parameters[name] for name in ("r0_ohm", "r1_ohm", "c1_f")}
    except (KeyError, TypeError) as error:
        raise ValueError("Supply r0_ohm, r1_ohm, and c1_f.") from error
    predicted = predict_ecm_voltage(
        data["time_s"], data["current_a"], data["true_soc"], ocv_function, **circuit
    )
    residual = predicted - data["voltage_v"]
    recovery = data["recovery_start_index"]
    return {
        **data, "parameters": circuit,
        "ocv_v": _ocv_values(ocv_function, data["true_soc"]),
        "predicted_voltage_v": predicted, "residual_v": residual,
        "baseline_error_v": float(residual[0]),
        "voltage_rmse_v": float(np.sqrt(np.mean(residual**2))),
        "voltage_max_error_v": float(np.max(np.abs(residual))),
        "pulse_rmse_v": float(np.sqrt(np.mean(residual[1:recovery]**2))),
        "recovery_rmse_v": float(np.sqrt(np.mean(residual[recovery:]**2))),
    }


def fit_ecm_parameters(pulse_data, ocv_function, baseline_tolerance_v=0.001):
    """Estimate R0 from switches, then fit positive R1/tau and derive C1.

    Use a settled, clean rectangular pulse and an OCV function with the same SOC
    reference. Reference SOC is allowed for this offline identification step.
    A free voltage offset is not fitted: it could hide an incorrect OCV baseline.

    Pulse and recovery each receive equal influence (their mean squared errors
    are summed), so the long recovery cannot overwhelm the short pulse. Fit
    log(R1) and log(tau) within broad numerical bounds, try four initial time
    constants, and choose the converged solution with the smallest objective.
    Reported RMSE values are ordinary, unweighted voltage errors.
    """
    if not np.isfinite(baseline_tolerance_v) or baseline_tolerance_v <= 0:
        raise ValueError("baseline_tolerance_v must be finite and positive.")
    data = _pulse_window(pulse_data)
    time, current = data["time_s"], data["current_a"]
    ocv = _ocv_values(ocv_function, data["true_soc"])
    loss = ocv - data["voltage_v"]
    if abs(loss[0]) > baseline_tolerance_v:
        raise ValueError("Pre-pulse voltage differs from OCV: check rest settling, OCV resolution, and SOC reference.")

    recovery = data["recovery_start_index"]
    switches = np.array([1, recovery])
    # At a switch, the RC state is continuous. The change in OCV-minus-voltage
    # divided by the current change isolates R0 at load-on and load-off.
    jumps = (loss[switches] - loss[switches - 1]) / (current[switches] - current[switches - 1])
    if np.any(jumps < -1e-10):
        raise ValueError("Current switches must imply a nonnegative series resistance.")
    r0_ohm = max(0.0, float(np.mean(jumps)))
    transient = loss[1:recovery] - r0_ohm * current[1:recovery]
    if np.max(transient) <= 1e-10:
        raise ValueError("No positive RC response is available to identify R1 and C1.")

    # These are numerical search limits, not measured EEMB specifications.
    lower = np.array([1e-6, 0.01])  # R1 in ohms, tau in seconds
    upper = np.array([1.0, 1e5])
    r1_guess = np.clip(np.max(transient) / current[1], lower[0] * 10, upper[0] / 10)
    weights = np.zeros(time.size)
    weights[1:recovery] = 1 / np.sqrt(recovery - 1)
    weights[recovery:] = 1 / np.sqrt(time.size - recovery)

    def residuals(log_parameters):
        r1, tau = np.exp(log_parameters)
        polarization = _polarization_voltage(time, current, r1, tau)
        error_v = ocv - r0_ohm * current - polarization - data["voltage_v"]
        # Scale to mV so optimizer stopping tolerances suit these small errors.
        return 1000 * weights * error_v

    solutions = [
        least_squares(
            residuals, np.log([r1_guess, tau_guess]),
            bounds=(np.log(lower), np.log(upper)),
            ftol=1e-10, xtol=1e-10, gtol=1e-10, max_nfev=300,
        )
        for tau_guess in (1.0, 10.0, 100.0, 1000.0)
    ]
    converged = [result for result in solutions if result.success and np.isfinite(result.cost)]
    if not converged:
        raise RuntimeError("Circuit fitting did not converge; inspect the pulse and OCV model.")
    best = min(converged, key=lambda result: result.cost)
    # This screens for a grossly unidentifiable fit; full rank does not prove
    # unique physical cell parameters or adequacy of the one-RC approximation.
    if np.linalg.matrix_rank(best.jac) < 2:
        raise RuntimeError("This pulse does not independently identify R1 and its time constant.")
    r1_ohm, tau_s = np.exp(best.x)
    parameters = {"r0_ohm": r0_ohm, "r1_ohm": float(r1_ohm), "c1_f": float(tau_s / r1_ohm)}
    return {
        **parameters, "tau_s": float(tau_s),
        "r0_on_ohm": float(jumps[0]), "r0_off_ohm": float(jumps[1]),
        "optimizer_success": bool(best.success), "optimizer_message": best.message,
        "bounds_hit": bool(np.any(best.active_mask)),
        "r1_bounds_ohm": (float(lower[0]), float(upper[0])),
        "tau_bounds_s": (float(lower[1]), float(upper[1])),
        "training": evaluate_ecm(pulse_data, ocv_function, parameters),
    }


def run_ecm_fit():
    """Fit a 0.4 A/60 s pulse, then validate a 0.8 A/120 s pulse at the same SOC."""
    curve = generate_ocv_soc_curve()
    if not curve["settled_points"].all():
        raise ValueError("The OCV curve needs longer rests before parameter identification.")
    ocv_function, _ = build_nonlinear_ocv_model(curve["soc_points"], curve["ocv_points_v"])
    training = run_pulse_test()
    validation = run_pulse_test(pulse_current_a=0.8, pulse_duration_s=120.0)
    for pulse in (training, validation):
        if not np.isclose(pulse["usable_capacity_ah"], curve["usable_capacity_ah"], rtol=1e-9, atol=1e-10):
            raise ValueError("Pulse and OCV characterization must use the same capacity reference.")
    fit = fit_ecm_parameters(training, ocv_function)
    # Validation data never enters the optimizer. Carry the calibration values
    # unchanged into a prediction at a different current and pulse duration.
    return {
        "fit": fit, "validation": evaluate_ecm(validation, ocv_function, fit),
        "ocv_curve": curve,
    }


def show_ecm_fit(results):
    """Print effective parameters and plot calibration/validation voltage errors."""
    fit = results["fit"]
    print("\nOne-RC fit to the Ai2020 surrogate near 50% SOC")
    print(f"R0: {fit['r0_ohm']:.8f} ohm (on/off jumps: {fit['r0_on_ohm']:.8f} / {fit['r0_off_ohm']:.8f})")
    print(f"R1: {fit['r1_ohm']:.8f} ohm; C1: {fit['c1_f']:.3f} F; tau: {fit['tau_s']:.3f} s")
    if fit["bounds_hit"]:
        print("A fitted parameter reached a search bound; inspect identifiability before using it.")
    print("Objective: equal pulse/recovery influence; no fitted voltage offset.")
    print("Errors below are voltage prediction errors, not SOC estimation errors.")

    figure, axes = plt.subplots(2, 2, figsize=(12, 7), sharex="col")
    for column, (name, data) in enumerate((("Calibration", fit["training"]), ("Validation", results["validation"]))):
        start, recovery = data["pulse_start_index"], data["recovery_start_index"]
        relative_minutes = (data["time_s"] - data["time_s"][start]) / 60
        duration_s = data["time_s"][recovery - 1] - data["time_s"][start]
        print(
            f"{name}: RMSE {data['voltage_rmse_v'] * 1000:.4f} mV; "
            f"max {data['voltage_max_error_v'] * 1000:.4f} mV; "
            f"pulse/recovery RMSE {data['pulse_rmse_v'] * 1000:.4f} / "
            f"{data['recovery_rmse_v'] * 1000:.4f} mV; "
            f"pre-pulse baseline error {data['baseline_error_v'] * 1000:.6f} mV"
        )
        axes[0, column].plot(relative_minutes, data["voltage_v"], color="tab:blue", label="PyBaMM")
        axes[0, column].plot(relative_minutes, data["predicted_voltage_v"], "--", color="tab:orange", label="One-RC prediction")
        axes[1, column].plot(relative_minutes, data["residual_v"] * 1000, color="tab:red")
        axes[1, column].axhline(0, color="gray", linewidth=1)
        axes[0, column].set_title(f"{name}: {data['current_a'][start]:g} A for {duration_s:g} s")
        axes[0, column].legend()
        axes[1, column].set_xlabel("Time since load-on [min]")
        for axis in axes[:, column]:
            axis.axvspan(0, duration_s / 60, color="tab:orange", alpha=0.12)
            axis.grid(True, alpha=0.3)
    axes[0, 0].set_ylabel("Terminal voltage [V]")
    axes[1, 0].set_ylabel("Prediction minus PyBaMM [mV]")
    figure.suptitle("One-RC voltage model: fitted once, then validated on a separate pulse")
    figure.tight_layout()
    plt.show()


if __name__ == "__main__":
    show_ecm_fit(run_ecm_fit())
