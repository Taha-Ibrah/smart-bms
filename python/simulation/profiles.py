"""Generate repeatable evaluation data, separate from the ECM fitting pulses.

Positive current discharges the battery; negative current charges it. Profiles
default to 50% SOC and 25 C, where our present R0/R1/C1 fit was characterized.
Other initial SOC and ambient temperatures test how that fixed fit generalizes.
Conditioning and the one-hour settling rest are excluded from returned data.
These are local software evaluation profiles, not hardware test instructions.
"""

import matplotlib.pyplot as plt
import numpy as np
import pybamm

if __package__:
    from .battery_simulation import create_battery_configuration, run_battery_simulation
else:
    from battery_simulation import create_battery_configuration, run_battery_simulation


# Each pair means (current in amperes, duration in seconds). These durations and
# currents differ from both the fitting pulse and its separate validation pulse.
PROFILE_STEPS = {
    "constant_discharge": ((0, 60), (0.3, 900), (0, 300)),
    "changing_load": (
        (0, 60), (0.2, 180), (0, 60), (0.6, 120),
        (0.1, 240), (0, 120), (0.4, 180), (0, 180),
    ),
    "charge_discharge": (
        (0, 60), (0.3, 240), (0, 120), (-0.2, 180),
        (0, 120), (0.5, 180), (0, 180),
    ),
}


def run_test_profile(
    profile_name="changing_load",
    target_soc=0.5,
    sample_period_s=1.0,
    pre_rest_duration_s=3600.0,
    reference_data=None,
    *,
    ambient_temperature_c=25.0,
):
    """Return clean V/I/T/reference-SOC arrays for one evaluation profile.

    reference_data may be the capacity-test result or generated OCV-curve data;
    supplying it avoids repeating the capacity test for each profile. Its usable
    capacity defines SOC exactly as in battery_simulation.py and ocv_soc.py.
    Changing ambient temperature changes the simulated cell, while the supplied
    reference capacity and its SOC scale remain fixed. With no supplied reference,
    the capacity test still runs at the default characterization temperature, 25 C.

    The battery is conditioned from full, then rested for at least one hour.
    Evaluation starts at t=0 after that rest. Both sides of every current switch
    are retained, so actual time differences, rather than a fixed sample period,
    must be used when integrating current or replaying the estimators.
    """
    if profile_name not in PROFILE_STEPS:
        raise ValueError(f"Unknown profile {profile_name!r}; choose from {tuple(PROFILE_STEPS)}.")
    if not np.isfinite(target_soc) or not 0 < target_soc <= 1:
        raise ValueError("target_soc must be finite, greater than zero, and at most one.")
    if not np.isfinite(sample_period_s) or sample_period_s <= 0:
        raise ValueError("sample_period_s must be finite and greater than zero.")
    if not np.isfinite(pre_rest_duration_s) or pre_rest_duration_s < 3600:
        raise ValueError("pre_rest_duration_s must be finite and at least 3600 seconds.")
    steps = PROFILE_STEPS[profile_name]
    if sample_period_s > min(duration for _, duration in steps):
        raise ValueError("sample_period_s cannot exceed the shortest profile step.")

    model, parameters, specs = create_battery_configuration(
        ambient_temperature_c=ambient_temperature_c,
    )
    for current, duration in steps:
        if not np.isfinite(current) or not np.isfinite(duration) or duration <= 0:
            raise ValueError("Profile currents must be finite and durations positive and finite.")
        if current > specs["maximum_discharge_current_a"]:
            raise ValueError("Profile exceeds the maximum discharge current.")
        if -current > specs["maximum_charge_current_a"]:
            raise ValueError("Profile exceeds the maximum charge current.")

    reference = run_battery_simulation() if reference_data is None else reference_data
    capacity_ah = float(reference["usable_capacity_ah"])
    if not np.isfinite(capacity_ah) or capacity_ah <= 0:
        raise ValueError("Reference usable_capacity_ah must be positive and finite.")
    # Check every step boundary; a later charge step cannot undo an earlier
    # exhausted reference capacity. Do not conceal an invalid trajectory by clipping.
    expected_soc = target_soc
    for current, duration in steps:
        expected_soc -= current * duration / (3600 * capacity_ah)
        if not 0 < expected_soc <= 1:
            raise ValueError("Profile would exhaust the remaining reference capacity or overcharge it.")

    minimum_v, maximum_v = specs["minimum_voltage_v"], specs["maximum_voltage_v"]
    conditioning_current_a = float(reference.get("discharge_current_a", 0.2 * specs["nominal_capacity_ah"]))
    if not np.isfinite(conditioning_current_a) or conditioning_current_a <= 0:
        raise ValueError("Reference conditioning current must be positive and finite.")
    preparation_steps = []
    if target_soc < 1:
        conditioning_s = (1 - target_soc) * capacity_ah / conditioning_current_a * 3600
        preparation_steps.extend([
            "Rest for 10 minutes",
            f"Discharge at {conditioning_current_a} A for {conditioning_s} seconds or until {minimum_v} V",
        ])
    preparation_steps.append(f"Rest for {pre_rest_duration_s} seconds")
    preparation = pybamm.Simulation(
        model, parameter_values=parameters,
        experiment=pybamm.Experiment([tuple(preparation_steps)], period="10 seconds"),
    ).solve(initial_soc=1.0)
    prepared_steps = preparation.cycles[-1].steps
    if (
        len(prepared_steps) != len(preparation_steps)
        or any(isinstance(step, pybamm.EmptySolution) for step in prepared_steps)
        or any(step.termination != "final time" for step in prepared_steps)
    ):
        raise RuntimeError("Conditioning reached a voltage limit or skipped a step before the target SOC.")

    experiment_steps = []
    for current, duration in steps:
        if current == 0:
            experiment_steps.append(f"Rest for {duration} seconds")
        elif current > 0:
            experiment_steps.append(f"Discharge at {current} A for {duration} seconds or until {minimum_v} V")
        else:
            experiment_steps.append(f"Charge at {-current} A for {duration} seconds or until {maximum_v} V")
    solution = pybamm.Simulation(
        model, parameter_values=parameters,
        experiment=pybamm.Experiment([tuple(experiment_steps)], period=f"{sample_period_s} seconds"),
    ).solve(starting_solution=preparation)
    evaluation_steps = solution.cycles[-1].steps
    if (
        len(evaluation_steps) != len(steps)
        or any(isinstance(step, pybamm.EmptySolution) for step in evaluation_steps)
        or any(step.termination != "final time" for step in evaluation_steps)
    ):
        raise RuntimeError("Evaluation profile reached a voltage limit or skipped a requested step.")

    variable_names = {
        "time_s": "Time [s]", "current_a": "Current [A]",
        "voltage_v": "Terminal voltage [V]",
        "temperature_c": "Volume-averaged cell temperature [K]",
        "discharge_capacity_ah": "Discharge capacity [A.h]",
    }
    # Keep the final sample before and first sample after every current change.
    # Their nearly equal timestamps preserve instantaneous voltage jumps.
    data = {
        key: np.concatenate([np.asarray(step[variable].entries).reshape(-1) for step in evaluation_steps])
        for key, variable in variable_names.items()
    }
    data["time_s"] -= data["time_s"][0]
    data["temperature_c"] -= 273.15
    data["true_soc"] = 1 - data["discharge_capacity_ah"] / capacity_ah
    if np.any(data["true_soc"] < -1e-8) or np.any(data["true_soc"] > 1 + 1e-8):
        raise RuntimeError("Evaluation SOC exceeded the configured reference range.")
    if not np.all(np.diff(data["time_s"]) > 0):
        raise RuntimeError("Evaluation timestamps must increase strictly.")
    lengths = np.array([len(step["Time [s]"].entries) for step in evaluation_steps])
    end_indices = np.cumsum(lengths) - 1
    start_indices = np.r_[0, end_indices[:-1] + 1]

    return {
        **specs, **data,
        "usable_capacity_ah": capacity_ah,
        "reference_capacity_temperature_c": float(reference.get("ambient_temperature_c", 25.0)),
        "soc_reference": reference["soc_reference"],
        "profile_name": profile_name,
        "target_soc": float(target_soc),
        "initial_soc": float(data["true_soc"][0]),
        "sample_period_s": float(sample_period_s),
        "pre_rest_duration_s": float(pre_rest_duration_s),
        "profile_steps": [{"current_a": float(current), "duration_s": float(duration)} for current, duration in steps],
        "step_start_indices": start_indices,
        "step_end_indices": end_indices,
        "phase": np.repeat(["rest" if current == 0 else "discharge" if current > 0 else "charge" for current, _ in steps], lengths),
    }


def show_test_profile(results):
    """Display clean evaluation inputs and reference SOC, without estimators."""
    time_min = results["time_s"] / 60
    figure, axes = plt.subplots(4, 1, figsize=(10, 8), sharex=True)
    for axis, key, label in zip(
        axes, ("current_a", "voltage_v", "temperature_c", "true_soc"),
        ("Current [A]", "Voltage [V]", "Temperature [°C]", "Reference SOC [%]"),
    ):
        values = results[key] * (100 if key == "true_soc" else 1)
        axis.plot(time_min, values)
        axis.set_ylabel(label)
        axis.grid(True, alpha=0.3)
    axes[-1].set_xlabel("Evaluation time [min]")
    figure.suptitle(f"{results['profile_name']}: {results['parameter_set']} surrogate evaluation profile")
    figure.tight_layout()
    plt.show()


if __name__ == "__main__":
    show_test_profile(run_test_profile())
