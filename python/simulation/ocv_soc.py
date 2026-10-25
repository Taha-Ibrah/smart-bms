"""Generate and plot rested voltage versus usable-capacity SOC.

The experiment follows one battery from full charge through repeated discharge
and rest steps. It reuses the EEMB/Ai2020 configuration in battery_simulation.py.
First measure capacity by continuously discharging the model to the project
cutoff. That test defines the SOC axis used here. This is an explicit reference
convention; using the nominal rating would be a different convention, not an
invalid calculation. Neither convention calibrates the model to the EEMB cell.
The result is a simulated OCV approximation from the borrowed Ai2020 chemistry.
R0, R1, and C1 identification will be a separate step after this curve exists.
"""

import warnings

import matplotlib.pyplot as plt
import numpy as np
import pybamm

# Support both package imports and running this file directly from the terminal.
if __package__:
    from .battery_simulation import create_battery_configuration, run_battery_simulation
else:
    from battery_simulation import create_battery_configuration, run_battery_simulation


def generate_ocv_soc_curve(
    soc_step=0.02,
    rest_duration_s=3600.0,
    settling_window_s=600.0,
    settling_tolerance_v=0.001,
):
    """Return rested-voltage samples and a settling check for every point.

    soc_step is the reference-capacity fraction removed between rests: 0.02
    means 2%. It must divide 1 evenly. Defaults give 51 points from 100% to 0%,
    unless voltage stops the experiment early. The discharge duration per step
    comes from a separate continuous-discharge capacity test.
    The finer grid resolves Ai2020's steep low-SOC knee instead of joining 0%
    directly to 10% with one long line segment. It does not alter the physics.

    The reference test uses the project cutoff (currently 3.0 V), at 0.4 A and
    25 degrees C ambient. This capacity is protocol-dependent. Resting lets the
    voltage recover, so the rested 0% point need not equal the loaded cutoff.

    Each rest lasts rest_duration_s. A point is marked settled if the sampled
    voltage range over the final settling_window_s is <= settling_tolerance_v.
    This practical test does not prove complete electrochemical equilibrium.

    SOC and voltage arrays are returned in increasing SOC order for later use
    in lookup tables. Other point arrays follow the same order.
    """
    # Reject settings that would create an invalid experiment or settling test.
    for name, value in (
        ("soc_step", soc_step),
        ("rest_duration_s", rest_duration_s),
        ("settling_window_s", settling_window_s),
        ("settling_tolerance_v", settling_tolerance_v),
    ):
        if not np.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and greater than zero.")
    if soc_step > 1 or not np.isclose(
        round(1 / soc_step) * soc_step, 1.0, rtol=0, atol=1e-12
    ):
        raise ValueError("soc_step must divide 1 evenly, for example 0.1 or 0.05.")
    if settling_window_s > rest_duration_s:
        raise ValueError("The settling window cannot be longer than the rest.")

    # Run the existing discharge script without plotting. It measures how much
    # charge this particular model supplies before reaching the project cutoff.
    # This sets a reference scale; it does not fit or validate EEMB chemistry.
    capacity_test = run_battery_simulation()
    usable_capacity_ah = capacity_test["usable_capacity_ah"]

    # Start a fresh, full battery using the same configuration for the OCV test.
    model, parameter_values, battery_specs = create_battery_configuration()
    minimum_voltage_v = battery_specs["minimum_voltage_v"]
    initial_soc = 1.0
    discharge_current_a = capacity_test["discharge_current_a"]

    # Q_removed = I * time_hours. Rearranging gives the required step duration.
    number_of_steps = round(1 / soc_step)
    discharge_duration_s = usable_capacity_ah * soc_step / discharge_current_a * 3600
    # Use at least two samples in the settling window, even for a short test.
    period_s = min(10.0, settling_window_s / 2)
    rest_step = f"Rest for {rest_duration_s} seconds"

    soc_points = []
    removed_capacity_ah = []
    ocv_points_v = []
    rest_drift_v = []
    settled_points = []

    def record_rest_point(rest_solution):
        """Read one point from the end of a zero-current rest step."""
        time_s = rest_solution["Time [s]"].entries
        voltage_v = rest_solution["Terminal voltage [V]"].entries
        removed_ah = rest_solution["Discharge capacity [A.h]"].entries[-1]

        # Use actual cumulative charge removed, not the intended SOC label.
        # Resting removes no external charge, so it does not advance this SOC.
        # Here 0% means the amount removed in the reference cutoff test. This
        # is a chosen SOC definition, not a way to force a desired voltage.
        soc = initial_soc - removed_ah / usable_capacity_ah
        soc_points.append(float(np.clip(soc, 0.0, 1.0)))
        removed_capacity_ah.append(float(removed_ah))
        ocv_points_v.append(float(voltage_v[-1]))

        # Measure the range of voltage over the final part of the rest. Include
        # an interpolated window boundary when it lies between output samples.
        window_start_s = time_s[-1] - settling_window_s
        window_voltage_v = np.concatenate((
            [np.interp(window_start_s, time_s, voltage_v)],
            voltage_v[time_s >= window_start_s],
        ))
        drift_v = float(np.ptp(window_voltage_v))
        rest_drift_v.append(drift_v)
        settled_points.append(drift_v <= settling_tolerance_v)

    # First record the full-charge point after resting at zero current.
    rest_simulation = pybamm.Simulation(
        model=model,
        parameter_values=parameter_values,
        experiment=pybamm.Experiment([rest_step], period=f"{period_s} seconds"),
    )
    solution = rest_simulation.solve(initial_soc=initial_soc)
    record_rest_point(solution.cycles[-1].steps[-1])

    # A cycle is one discharge followed by one rest. Grouping the two steps lets
    # us inspect the discharge stopping condition before starting another cycle.
    cycle_experiment = pybamm.Experiment(
        [(
            f"Discharge at {discharge_current_a} A for {discharge_duration_s} seconds "
            f"or until {minimum_voltage_v} V",
            rest_step,
        )],
        period=f"{period_s} seconds",
    )
    cycle_simulation = pybamm.Simulation(
        model=model,
        parameter_values=parameter_values,
        experiment=cycle_experiment,
    )

    stopped_at_voltage_limit = False
    for _ in range(number_of_steps):
        # Continue from the previous state, preserving lithium concentrations,
        # temperature, and cumulative discharged capacity throughout the test.
        solution = cycle_simulation.solve(starting_solution=solution)
        discharge_solution, rest_solution = solution.cycles[-1].steps
        record_rest_point(rest_solution)

        # An event-ended discharge reached a limit before its requested duration.
        # Record the final rested point, then stop instead of discharging again.
        if discharge_solution.termination != "final time":
            stopped_at_voltage_limit = True
            break

    # Reverse the chronological discharge order into increasing SOC order.
    # Keep settling diagnostics aligned with their corresponding voltage points.
    order = np.argsort(soc_points)
    soc_points = np.asarray(soc_points)[order]
    removed_capacity_ah = np.asarray(removed_capacity_ah)[order]
    ocv_points_v = np.asarray(ocv_points_v)[order]
    rest_drift_v = np.asarray(rest_drift_v)[order]
    settled_points = np.asarray(settled_points, dtype=bool)[order]

    if not settled_points.all():
        warnings.warn(
            "Some rested-voltage points exceed the settling tolerance. "
            "Try a longer rest before using them as an OCV lookup table.",
            stacklevel=2,
        )
    if stopped_at_voltage_limit:
        warnings.warn(
            "The voltage cutoff ended the experiment before all reference SOC "
            "steps completed. Use only the returned SOC range; do not extrapolate.",
            stacklevel=2,
        )

    return {
        **battery_specs,
        "usable_capacity_ah": usable_capacity_ah,
        "soc_reference": capacity_test["soc_reference"],
        "soc_points": soc_points,
        "removed_capacity_ah": removed_capacity_ah,
        "ocv_points_v": ocv_points_v,
        "rest_drift_v": rest_drift_v,
        "settled_points": settled_points,
        "rest_duration_s": rest_duration_s,
        "settling_window_s": settling_window_s,
        "settling_tolerance_v": settling_tolerance_v,
        "discharge_current_a": discharge_current_a,
        "discharge_duration_s": discharge_duration_s,
        "stopped_at_voltage_limit": stopped_at_voltage_limit,
    }


def show_ocv_soc_curve(results):
    """Print the points and display the rested-voltage curve."""
    soc_percent = results["soc_points"] * 100
    voltage_v = results["ocv_points_v"]
    settled = results["settled_points"]

    print(f"\n{results['parameter_set']} surrogate for {results['battery_name']}: rested voltage")
    print(f"Advertised nominal capacity: {results['nominal_capacity_ah']:.4f} Ah")
    print(f"Reference-test capacity / SOC scale: {results['usable_capacity_ah']:.4f} Ah")
    print(f"Reference test: {results['soc_reference']}")
    print("Rested voltage can recover above the discharge cutoff, including at 0% SOC.")
    print(f"Rest per point: {results['rest_duration_s'] / 60:g} minutes")
    print(
        f"Settling check: <= {results['settling_tolerance_v'] * 1000:g} mV "
        f"range over the final {results['settling_window_s'] / 60:g} minutes"
    )
    print("SOC (%)   Rested voltage (V)   Drift (mV)   Settled")
    for soc, voltage, drift, is_settled in zip(
        soc_percent, voltage_v, results["rest_drift_v"] * 1000, settled
    ):
        print(f"{soc:7.1f}   {voltage:18.4f}   {drift:10.4f}   {is_settled}")

    # Connecting sample points helps visualize the curve; it is not a fitted
    # KF line or an EKF interpolation function yet. Those are later steps.
    figure, axis = plt.subplots(figsize=(9, 5))
    axis.plot(soc_percent, voltage_v, color="gray", linewidth=1)
    axis.scatter(soc_percent[settled], voltage_v[settled], label="Settling check passed")
    if not settled.all():
        axis.scatter(
            soc_percent[~settled], voltage_v[~settled],
            color="red", marker="x", label="Longer rest needed",
        )
    axis.set_title(
        f"{results['parameter_set']} surrogate: rested-voltage curve\n"
        f"Target: {results['battery_name']}; SOC reference: "
        f"{results['usable_capacity_ah']:.3f} Ah at "
        f"{results['discharge_current_a']:g} A to {results['minimum_voltage_v']:g} V"
    )
    axis.set_xlabel("SOC relative to the defined capacity test [%]")
    axis.set_ylabel("Rested terminal voltage [V]")
    axis.set_xlim(0, 100)
    axis.grid(True, alpha=0.4)
    axis.legend()
    figure.tight_layout()
    plt.show()


# Importing this module only defines functions; running it starts the experiment.
if __name__ == "__main__":
    ocv_results = generate_ocv_soc_curve()
    show_ocv_soc_curve(ocv_results)
