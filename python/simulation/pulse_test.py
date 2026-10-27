"""Generate a simulated current pulse and the battery's voltage response.

Run this file to plot a rest, discharge pulse, and recovery rest. It reuses the
Ai2020/EEMB configuration and capacity reference from battery_simulation.py.
The returned data will support later R0, R1, and C1 fitting; this file does not
fit those parameters or run the SOC estimators yet.
"""

import matplotlib.pyplot as plt
import numpy as np
import pybamm

# Support importing the module and running it directly from the terminal.
if __package__:
    from .battery_simulation import create_battery_configuration, run_battery_simulation
else:
    from battery_simulation import create_battery_configuration, run_battery_simulation


def run_pulse_test(
    target_soc=0.5,
    pulse_current_a=0.4,
    pulse_duration_s=60.0,
    pre_rest_duration_s=3600.0,
    recovery_duration_s=1800.0,
    sample_period_s=1.0,
):
    """Return clean time-aligned measurements for one rest-pulse-recovery test.

    Defaults: condition to 50% SOC, rest one hour, discharge at 0.4 A for one
    minute, then rest for 30 minutes. Positive current means discharge.
    target_soc uses the same usable-capacity convention as ocv_soc.py. Start
    from full and remove the corresponding Ah instead of treating PyBaMM's
    internal initial_soc setting as interchangeable with our SOC definition.

    Output timestamps start at the beginning of the pre-pulse rest. Conditioning
    data is excluded, but cumulative discharge_capacity_ah still includes it.
    phase identifies each sample as pre_rest, pulse, or recovery. Switch indices
    and times retain both sides of each current change for later fitting.
    A voltage-cutoff-truncated or skipped discharge raises RuntimeError rather
    than returning it as a complete pulse. This is simulated data, not a test
    of the physical battery or a globally valid equivalent-circuit model.
    """
    if not np.isfinite(target_soc) or not 0 < target_soc <= 1:
        raise ValueError("target_soc must be greater than 0 and at most 1.")
    for name, value in (
        ("pulse_current_a", pulse_current_a),
        ("pulse_duration_s", pulse_duration_s),
        ("pre_rest_duration_s", pre_rest_duration_s),
        ("recovery_duration_s", recovery_duration_s),
        ("sample_period_s", sample_period_s),
    ):
        if not np.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and greater than zero.")
    if sample_period_s > min(pre_rest_duration_s, pulse_duration_s, recovery_duration_s):
        raise ValueError("sample_period_s cannot exceed the shortest test phase.")

    model, parameters, specs = create_battery_configuration()
    if pulse_current_a > specs["maximum_discharge_current_a"]:
        raise ValueError("Pulse current exceeds the configured maximum discharge current.")

    # Measure the reference capacity using the existing continuous discharge.
    # The advertised 2 Ah remains metadata; our SOC scale uses this test result.
    reference = run_battery_simulation()
    capacity_ah = reference["usable_capacity_ah"]
    pulse_charge_ah = pulse_current_a * pulse_duration_s / 3600
    if pulse_charge_ah >= target_soc * capacity_ah:
        raise ValueError("The pulse would exhaust the remaining reference capacity.")

    minimum_voltage_v = specs["minimum_voltage_v"]
    prepared_solution = None
    if target_soc < 1:
        # Match the reference test's initial history, stopping at the amount of
        # charge corresponding to the requested SOC. This preserves the model's
        # lithium concentrations and temperature for the following pulse test.
        conditioning_current_a = reference["discharge_current_a"]
        conditioning_duration_s = (1 - target_soc) * capacity_ah / conditioning_current_a * 3600
        conditioning = pybamm.Simulation(
            model=model,
            parameter_values=parameters,
            experiment=pybamm.Experiment([(
                "Rest for 10 minutes",
                f"Discharge at {conditioning_current_a} A for {conditioning_duration_s} seconds "
                f"or until {minimum_voltage_v} V",
            )], period="10 seconds"),
        )
        prepared_solution = conditioning.solve(initial_soc=1.0)
        if prepared_solution.cycles[-1].steps[-1].termination != "final time":
            raise RuntimeError("Conditioning reached a voltage limit before the target SOC.")

    experiment = pybamm.Experiment([(
        f"Rest for {pre_rest_duration_s} seconds",
        f"Discharge at {pulse_current_a} A for {pulse_duration_s} seconds "
        f"or until {minimum_voltage_v} V",
        f"Rest for {recovery_duration_s} seconds",
    )], period=f"{sample_period_s} seconds")
    simulation = pybamm.Simulation(model, parameter_values=parameters, experiment=experiment)
    if prepared_solution is None:
        solution = simulation.solve(initial_soc=1.0)
    else:
        solution = simulation.solve(starting_solution=prepared_solution)

    steps = solution.cycles[-1].steps
    if len(steps) != 3 or any(isinstance(step, pybamm.EmptySolution) for step in steps):
        raise RuntimeError("A pulse-test phase was skipped; choose a higher SOC or smaller pulse.")
    pre_rest, pulse, recovery = steps
    # Inspect the discharge itself: a later rest can finish normally even when
    # the preceding pulse stopped early at the voltage cutoff.
    if pulse.termination != "final time":
        raise RuntimeError("The pulse reached a voltage limit before its requested duration.")

    variables = {
        "time_s": "Time [s]",
        "current_a": "Current [A]",
        "voltage_v": "Terminal voltage [V]",
        "temperature_c": "Volume-averaged cell temperature [K]",
        "discharge_capacity_ah": "Discharge capacity [A.h]",
    }
    # Keep each step's final and initial samples. PyBaMM places them very close
    # in time at a switch; rounding or resampling could erase the voltage jump.
    data = {
        key: np.concatenate([step[variable].entries for step in steps])
        for key, variable in variables.items()
    }
    data["time_s"] -= data["time_s"][0]
    data["temperature_c"] -= 273.15
    data["true_soc"] = 1 - data["discharge_capacity_ah"] / capacity_ah
    lengths = [len(step["Time [s]"].entries) for step in steps]
    pulse_start_index = lengths[0]
    recovery_start_index = lengths[0] + lengths[1]
    phase = np.repeat(["pre_rest", "pulse", "recovery"], lengths)

    return {
        **specs,
        **data,
        "usable_capacity_ah": capacity_ah,
        "soc_reference": reference["soc_reference"],
        "target_soc": target_soc,
        "initial_soc": float(data["true_soc"][0]),
        "pulse_current_a": pulse_current_a,
        "pulse_duration_s": pulse_duration_s,
        "pre_rest_duration_s": pre_rest_duration_s,
        "recovery_duration_s": recovery_duration_s,
        "sample_period_s": sample_period_s,
        "phase": phase,
        "pulse_start_index": pulse_start_index,
        "recovery_start_index": recovery_start_index,
        "pulse_start_s": float(data["time_s"][pulse_start_index]),
        "pulse_end_s": float(data["time_s"][recovery_start_index - 1]),
    }


def show_pulse_test(results):
    """Plot the full recovery and a closer view of the current-switch response."""
    start = results["pulse_start_index"]
    recovery = results["recovery_start_index"]
    # Reference the plot to load-on so the initial conditioning/rest does not
    # hide the short pulse. Returned data still contains the full pre-pulse rest.
    relative_time_s = results["time_s"] - results["pulse_start_s"]
    pulse_end_s = results["pulse_end_s"] - results["pulse_start_s"]

    print(f"\nSimulated pulse test: {results['parameter_set']} surrogate for {results['battery_name']}")
    print(f"SOC reference: {results['usable_capacity_ah']:.4f} Ah; {results['soc_reference']}")
    print(f"SOC before/after pulse: {results['true_soc'][start] * 100:.3f}% / {results['true_soc'][recovery] * 100:.3f}%")
    print(f"Pulse: {results['pulse_current_a']:g} A for {results['pulse_duration_s']:g} seconds")
    print(f"Pre-pulse rest: {results['pre_rest_duration_s'] / 60:g} min; recovery: {results['recovery_duration_s'] / 60:g} min")
    print(f"Voltage before load: {results['voltage_v'][start - 1]:.6f} V")
    print(f"Voltage just after load-on: {results['voltage_v'][start]:.6f} V")
    print(f"Voltage just before load-off: {results['voltage_v'][recovery - 1]:.6f} V")
    print(f"Voltage just after load-off: {results['voltage_v'][recovery]:.6f} V")
    print(f"Voltage after recovery: {results['voltage_v'][-1]:.6f} V")
    print("Current/voltage traces are simulation output; R0, R1, and C1 have not been fitted.")

    figure, axes = plt.subplots(2, 2, figsize=(12, 7), sharex="col")
    for column, time_scale in enumerate((60.0, 1.0)):
        time = relative_time_s / time_scale
        axes[0, column].plot(time, results["current_a"], color="tab:orange")
        axes[1, column].plot(time, results["voltage_v"], color="tab:blue")
        for axis in axes[:, column]:
            axis.axvspan(0, pulse_end_s / time_scale, color="tab:orange", alpha=0.12)
            axis.axvline(0, color="gray", linestyle="--", linewidth=1)
            axis.axvline(pulse_end_s / time_scale, color="gray", linestyle="--", linewidth=1)
            axis.grid(True, alpha=0.3)

    axes[0, 0].set_title("Pulse and full recovery")
    axes[0, 1].set_title("Pulse detail (shading = load applied)")
    axes[0, 0].set_ylabel("Discharge current [A]")
    axes[1, 0].set_ylabel("Terminal voltage [V]")
    axes[1, 0].set_xlabel("Time since load-on [min]")
    axes[1, 1].set_xlabel("Time since load-on [s]")
    axes[0, 0].set_xlim(-min(60, results["pre_rest_duration_s"]) / 60, relative_time_s[-1] / 60)
    axes[0, 1].set_xlim(-min(5, results["pre_rest_duration_s"]), pulse_end_s + min(60, results["recovery_duration_s"]))
    figure.suptitle(
        f"{results['parameter_set']} surrogate: {results['pulse_current_a']:g} A pulse "
        f"near {results['initial_soc'] * 100:.1f}% SOC\n"
        "Immediate voltage jumps and slower relaxation; not a hardware measurement"
    )
    figure.tight_layout()
    plt.show()


if __name__ == "__main__":
    show_pulse_test(run_pulse_test())
