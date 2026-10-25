"""
Purpose: Approximate the EEMB LP103454 (3.7 V, 2000 mAh) battery.
1.1: Select a battery and define its capacity and operating limits
1.2: Use PyBaMM to simulate current, voltage, temperature and true SOC

Ai2020 supplies LCO/graphite electrochemistry from an Enertech cell. It is a
surrogate, not a measured parameter set for the EEMB cell. Electrode area is
scaled to match nominal capacity; OCV, resistance, kinetics, and thermal
properties still need validation against the purchased battery.

Specification source (EEMB LP103454, 2019-04-29):
https://datasheet.octopart.com/LP103454-EEMB-datasheet-182240587.pdf
"""
import pybamm
import matplotlib.pyplot as plt
import numpy as np


def run_battery_simulation():
    """Run the battery model and return its measurements and metadata."""
    #SPM = Single Particle Model
    #Simplified Lithium-ion battery that runs quickly, captures important battery behavior, commonly used
    model = pybamm.lithium_ion.SPM(
        options={ #including a simple thermal model
            "thermal" : "lumped"
        }
    )

    battery_name = "EEMB LP103454"
    parameter_set = "Ai2020"
    parameter_values = pybamm.ParameterValues(parameter_set)

    # Published EEMB specs. Replace rated capacity with a measured value later.
    nominal_capacity_ah = 2.0
    minimum_capacity_ah = 1.9
    nominal_voltage_v = 3.7
    datasheet_minimum_voltage_v = 2.75
    maximum_charge_current_a = 2.0
    maximum_discharge_current_a = 4.0
    charge_temp_range_c = (0.0, 45.0)
    discharge_temp_range_c = (-20.0, 60.0)
    cell_mass_kg = 0.040
    # Approximate outer envelope, not the internal electrode dimensions.
    length_m, width_m, thickness_m = 0.056, 0.0345, 0.0103

    # Changing only the nominal-capacity label would leave the original
    # 2.28 Ah electrode inventory in place. Scale its area by 2.0 / 2.28 too.
    capacity_scale = nominal_capacity_ah / parameter_values["Nominal cell capacity [A.h]"]
    scaled_electrode_width_m = parameter_values["Electrode width [m]"] * capacity_scale

    #Define opeating limits used by this project.
    #PROJECT-LEVEL LIMITS, separate from PyBaMM's internal parameters

    minimum_voltage_v = 3.0
    maximum_voltage_v = 4.2

    minimum_temp_c = 0.0
    maximum_temp_c = 45.0

    # Use the EEMB outer envelope for an approximate lumped thermal model.
    # Specific heat, density, and heat transfer remain Ai2020 assumptions;
    # the temperature trace is not a validated prediction for the real cell.
    parameter_values.update({
        "Nominal cell capacity [A.h]": nominal_capacity_ah,
        "Electrode width [m]": scaled_electrode_width_m,
        "Lower voltage cut-off [V]": minimum_voltage_v,
        "Upper voltage cut-off [V]": maximum_voltage_v,
        "Cell volume [m3]": length_m * width_m * thickness_m,
        "Cell cooling surface area [m2]": 2.0 * (
            length_m * width_m + length_m * thickness_m + width_m * thickness_m
        ),
        "Initial temperature [K]": 298.15,
        "Ambient temperature [K]": 298.15,
    })

    #Starting SOC for the simulation
    initial_soc = 1.0 #1.0 = 100%


    # The published capacity is specified at 0.2C: 2.0 Ah * 0.2 = 0.4 A.
    # This is a simulated load, not the breadboard voltage-divider circuit.
    discharge_c_rate = 0.2
    discharge_current_a = nominal_capacity_ah * discharge_c_rate
    # The borrowed electrochemistry can deliver a different usable capacity.
    # Stop after removing the nominal 2 Ah as well as checking the voltage,
    # so the nominal-capacity SOC reference never silently runs below zero.
    discharge_duration_h = nominal_capacity_ah / discharge_current_a

    #Define what is happening in the battery.
    experiment = pybamm.Experiment(
        [
            "Rest for 10 minutes",
            f"Discharge at {discharge_current_a} A for {discharge_duration_h} hours "
            f"or until {minimum_voltage_v} V"
        ],
        period ="10 seconds"
    )

    #Combine the model, parameter set, and experiment
    simulation =  pybamm.Simulation(
        model = model,
        parameter_values = parameter_values,
        experiment = experiment
    )

    #Run the simulation
    solution = simulation.solve(initial_soc=initial_soc)

    #Each .entries property is NumPy array containing the value
    # of that variable at every simulation time point.
    time_s = solution["Time [s]"].entries
    time_h = solution["Time [h]"].entries

    current_a = solution["Current [A]"].entries
    voltage_v = solution["Terminal voltage [V]"].entries
    temperature_k = solution["Volume-averaged cell temperature [K]"].entries
    # Discharge capacity is the charge removed from the battery, not charge remaining.
    discharge_capacity_ah = solution["Discharge capacity [A.h]"].entries

    #convert temperature from Kelvin to Celsius
    temperature_c = temperature_k - 273.15

    #Calculate true SOC from the simulated discharged capacity

    #Starting SOC is 100%, so:
    #SOC = 1 - discharged charge / nominal capacity
    true_soc = initial_soc - (discharge_capacity_ah / nominal_capacity_ah)
    #Keep numerical results within the valid SOC range
    true_soc = np.clip(true_soc, 0.0,1.0)
    true_soc_percent = true_soc * 100.0

    # Later pipeline stages can consume these values without opening plots.
    return {
        "battery_name": battery_name,
        "model_name": model.name,
        "parameter_set": parameter_set,
        "model_is_approximation": True,
        "capacity_scale": capacity_scale,
        "nominal_capacity_ah": nominal_capacity_ah,
        "minimum_capacity_ah": minimum_capacity_ah,
        "nominal_voltage_v": nominal_voltage_v,
        "datasheet_minimum_voltage_v": datasheet_minimum_voltage_v,
        "maximum_charge_current_a": maximum_charge_current_a,
        "maximum_discharge_current_a": maximum_discharge_current_a,
        "charge_temp_range_c": charge_temp_range_c,
        "discharge_temp_range_c": discharge_temp_range_c,
        "cell_mass_kg": cell_mass_kg,
        "cell_dimensions_m": (length_m, width_m, thickness_m),
        "discharge_c_rate": discharge_c_rate,
        "discharge_current_a": discharge_current_a,
        "discharge_duration_h": discharge_duration_h,
        "minimum_voltage_v": minimum_voltage_v,
        "maximum_voltage_v": maximum_voltage_v,
        "minimum_temp_c": minimum_temp_c,
        "maximum_temp_c": maximum_temp_c,
        "initial_soc": initial_soc,
        "time_s": time_s,
        "time_h": time_h,
        "current_a": current_a,
        "voltage_v": voltage_v,
        "temperature_c": temperature_c,
        "true_soc": true_soc,
        "true_soc_percent": true_soc_percent,
    }


def show_simulation_results(results):
    """Print and plot a completed simulation run."""
    model_name = results["model_name"]
    parameter_set = results["parameter_set"]
    nominal_capacity_ah = results["nominal_capacity_ah"]
    minimum_voltage_v = results["minimum_voltage_v"]
    maximum_voltage_v = results["maximum_voltage_v"]
    minimum_temp_c = results["minimum_temp_c"]
    maximum_temp_c = results["maximum_temp_c"]
    time_h = results["time_h"]
    current_a = results["current_a"]
    voltage_v = results["voltage_v"]
    temperature_c = results["temperature_c"]
    true_soc_percent = results["true_soc_percent"]

    # ============================================
    # PRINT BATTERY & SIMULATION INFORMATION
    # ============================================
    print("\nBattery configuration")
    print("---------------------")
    print(f"Target battery: {results['battery_name']}")
    print(f"Model: {model_name}")
    print(f"Parameter set: {parameter_set} (scaled surrogate; not hardware-calibrated)")
    print(f"Nominal voltage: {results['nominal_voltage_v']:.1f} V")
    print(f"Nominal capacity: {nominal_capacity_ah:.2f} Ah")
    print(
        f"Simulated discharge: {results['discharge_c_rate']:g}C "
        f"({results['discharge_current_a']:.2f} A)"
    )
    print(f"Datasheet discharge cutoff: {results['datasheet_minimum_voltage_v']:.2f} V")
    print(f"Maximum discharge duration: {results['discharge_duration_h']:g} hours")
    print(
        f"Project voltage limits: "
        f"{minimum_voltage_v:.2f} V to {maximum_voltage_v:.2f} V"
    )
    print(
        f"Project temperature references (not enforced): "
        f"{minimum_temp_c:.1f} °C to "
        f"{maximum_temp_c:.1f} °C"
    )

    print("\nSimulation results")
    print("------------------")
    print(f"Simulation duration: {time_h[-1]:.2f} hours")
    print(f"Starting voltage: {voltage_v[0]:.3f} V")
    print(f"Ending voltage: {voltage_v[-1]:.3f} V")
    print(f"Starting SOC: {true_soc_percent[0]:.2f}%")
    print(f"Ending SOC: {true_soc_percent[-1]:.2f}%")
    print(f"Starting temperature: {temperature_c[0]:.2f} °C")
    print(f"Maximum temperature: {temperature_c.max():.2f} °C")


    # =========================================================
    # PLOT THE RESULTS
    # =========================================================

    # plt.subplots(...) creates one Figure and an array of Axes objects. The tuple on
    # the left unpacks those two return values into the names ``figure`` and ``axes``.
    figure, axes = plt.subplots(
        nrows=2,          # Keyword argument: arrange two rows of plots.
        ncols=2,          # Keyword argument: arrange two columns of plots.
        figsize=(11, 8),  # A tuple sets the figure width and height in inches.
        sharex=True,      # A Boolean makes every plot use the same x-axis scale.
    )

    # Two pairs of square brackets select an Axes object by [row][column]. Python
    # starts counting at zero, so [0][0] is the upper-left plot.
    voltage_axis = axes[0][0]
    current_axis = axes[0][1]
    temperature_axis = axes[1][0]
    soc_axis = axes[1][1]

    # suptitle() adds one heading for the complete figure, above the subplot titles.
    figure.suptitle(
        f"{results['battery_name']} Approximation: 10 Minute Rest, "
        f"{results['discharge_c_rate']:g}C Discharge",
        fontsize=16,
        fontweight="bold",
    )

    # .plot(x, y) calls the plotting method on one Axes object. Here, time is the
    # x-coordinate array and the simulated measurement is the y-coordinate array.
    # The other keyword arguments control the line's color, width, markers, and label.
    voltage_axis.plot(
        time_h,
        voltage_v,
        color="tab:blue",
        linewidth=2,
        marker="o",
        markevery=[0, -1],
        label="Terminal voltage",
    )
    voltage_axis.axhline(
        minimum_voltage_v,
        color="tab:red",
        linestyle="--",
        label=f"Minimum limit ({minimum_voltage_v:.1f} V)",
    )
    voltage_axis.axhline(
        maximum_voltage_v,
        color="tab:green",
        linestyle="--",
        label=f"Maximum limit ({maximum_voltage_v:.1f} V)",
    )
    # Dot notation calls methods that title this subplot and label both axes.
    voltage_axis.set_title("Terminal Voltage vs. Time")
    voltage_axis.set_xlabel("Time [h]")
    voltage_axis.set_ylabel("Voltage [V]")

    current_axis.plot(
        time_h,
        current_a,
        color="tab:orange",
        linewidth=2,
        marker="o",
        markevery=[0, -1],
        label="Applied current",
    )
    current_axis.axhline(
        0,
        color="black",
        linewidth=1,
        linestyle="--",
        label="Zero-current reference",
    )
    current_axis.set_title("Current vs. Time")
    current_axis.set_xlabel("Time [h]")
    current_axis.set_ylabel("Current [A]")

    temperature_axis.plot(
        time_h,
        temperature_c,
        color="tab:red",
        linewidth=2,
        marker="o",
        markevery=[0, -1],
        label="Cell temperature",
    )
    temperature_axis.axhline(
        minimum_temp_c,
        color="tab:blue",
        linestyle="--",
        label=f"Minimum limit ({minimum_temp_c:.0f} °C)",
    )
    temperature_axis.axhline(
        maximum_temp_c,
        color="tab:red",
        linestyle="--",
        label=f"Maximum limit ({maximum_temp_c:.0f} °C)",
    )
    temperature_axis.set_title("Cell Temperature vs. Time")
    temperature_axis.set_xlabel("Time [h]")
    temperature_axis.set_ylabel("Temperature [°C]")

    soc_axis.plot(
        time_h,
        true_soc_percent,
        color="tab:green",
        linewidth=2,
        marker="o",
        markevery=[0, -1],
        label="Simulated SOC reference",
    )
    soc_axis.axhline(
        20,
        color="tab:red",
        linestyle="--",
        label="Low-SOC reference (20%)",
    )
    soc_axis.set_title("State of Charge vs. Time")
    soc_axis.set_xlabel("Time [h]")
    soc_axis.set_ylabel("SOC [%]")
    soc_axis.set_ylim(0, 105)

    # .flat turns the 2D Axes array into one sequence for the loop to visit.
    for axis in axes.flat:
        axis.minorticks_on()
        axis.grid(True, which="major", linestyle="-", alpha=0.4)
        axis.grid(True, which="minor", linestyle=":", alpha=0.2)
        axis.legend(fontsize=8)

    # tight_layout() adjusts spacing so labels do not overlap. show() displays the
    # completed Figure in a window (or sends it to the active Matplotlib backend).
    figure.tight_layout(rect=(0, 0, 1, 0.95))
    plt.show()


if __name__ == "__main__":
    simulation_results = run_battery_simulation()
    show_simulation_results(simulation_results)

# =========================================================
# GRAPH EXPLANATIONS
# =========================================================

# QUICK REMINDER: 0.2C for the 2 Ah target cell is 0.4 A. Ideally, discharging
# the full rated capacity at that current would take 5 hours, excluding rest.

# TERMINAL VOLTAGE VS. TIME
# What happens: The voltage stays nearly constant during the 10-minute rest,
# drops when the 0.2C discharge begins, and then decreases more gradually
# until it reaches the 3.0 V minimum limit or the 5-hour discharge ends.
# Why it happens: No load is applied during rest. Applying the discharge current
# produces an immediate voltage drop from internal resistance and electrochemical
# polarization. The later decline comes from lithium depletion and changing
# electrode concentrations. Reaching 3.0 V or removing the rated 2 Ah ends the
# experiment. The 4.2 V line shows the project maximum, but it is not a stop
# condition during this discharge step.

# CURRENT VS. TIME
# What happens: Current begins at 0 A during the rest period and then steps up to
# 0.4 A, where it remains constant for the rest of the simulation.
# Why it happens: The experiment explicitly requests a rest followed by a constant
# 0.2C discharge. The 2 Ah target capacity gives 2 * 0.2 = 0.4 A.
# PyBaMM uses positive current for discharge, which is why the line is above zero.

# CELL TEMPERATURE VS. TIME
# What happens: Cell temperature stays near its 25 °C starting value during rest,
# then changes during discharge. Its magnitude is an uncalibrated approximation.
# Why it happens: Current flow creates irreversible resistive heat and reversible
# electrochemical heat inside the cell. The lumped thermal model treats the cell
# as one uniform temperature and includes heat loss to the surroundings, which
# causes the rate of temperature increase to slow. The 0 °C and 45 °C lines are
# visual project references; this experiment does not stop at those temperatures.

# STATE OF CHARGE VS. TIME
# What happens: SOC remains at 100% during rest and then decreases almost linearly,
# until the voltage cutoff or nominal-capacity limit is met.
# Why it happens: Rest removes no charge, while constant current removes charge at
# an almost constant rate. Discharged capacity therefore rises almost linearly,
# causing the calculated SOC to fall almost linearly. The voltage cutoff can
# stop the experiment before nominal-capacity SOC reaches 0%. The 20% line is a
# warning reference only and does not stop this simulation.
