"""
Purpose: Define the lithium-ion battery being simulated
1.1: Select a battery and define its capacity and operating limilts
1.2: Use PyBaMM to simulate current, voltage, temperature and true SOC
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

    #Loads a complete set of physical battery parameters
    #Chen2020 represents a commerical lithium-ion cell.
    parameter_values = pybamm.ParameterValues("Chen2020")

    #Read the battery capacity from the selected parameter set.
    nominal_capacity_ah = parameter_values["Nominal cell capacity [A.h]"]

    #Define opeating limits used by this project.
    #PROJECT-LEVEL LIMITS, separate from PyBaMM's internal parameters

    minimum_voltage_v = 3.0
    maximum_voltage_v = 4.2

    minimum_temp_c = 0.0
    maximum_temp_c = 45.0

    #Starting SOC for the simulation
    initial_soc = 1.0 #1.0 = 100%


    #Define what is happening in the battery.
    experiment = pybamm.Experiment(
        [
            "Rest for 10 minutes",
            f"Discharge at 1C until {minimum_voltage_v} V"
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
        "model_name": model.name,
        "parameter_set": "Chen2020",
        "nominal_capacity_ah": nominal_capacity_ah,
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
    print(f"Model: {model_name}")
    print(f"Parameter set: {parameter_set}")
    print(f"Nominal capacity: {nominal_capacity_ah:.2f} Ah")
    print(
        f"Voltage limits: "
        f"{minimum_voltage_v:.2f} V to {maximum_voltage_v:.2f} V"
    )
    print(
        f"Temperature limits: "
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
        "PyBaMM Battery Simulation: 10 Minute Rest Followed by 1C Discharge",
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
        label="Calculated SOC",
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

# TERMINAL VOLTAGE VS. TIME
# What happens: The voltage stays nearly constant during the 10-minute rest,
# drops quickly when the 1C discharge begins, and then decreases more gradually
# until it reaches the 3.0 V minimum limit.
# Why it happens: No load is applied during rest. Applying the discharge current
# produces an immediate voltage drop from internal resistance and electrochemical
# polarization. The later decline comes from lithium depletion and changing
# electrode concentrations. Reaching 3.0 V ends the experiment to avoid excessive
# discharge. The 4.2 V line shows the project maximum, but it is not a stop
# condition during this discharge step.

# CURRENT VS. TIME
# What happens: Current begins at 0 A during the rest period and then steps up to
# approximately 5 A, where it remains constant for the rest of the simulation.
# Why it happens: The experiment explicitly requests a rest followed by a constant
# 1C discharge. Chen2020 describes a 5 Ah cell, so a 1C rate corresponds to 5 A.
# PyBaMM uses positive current for discharge, which is why the line is above zero.

# CELL TEMPERATURE VS. TIME
# What happens: Cell temperature stays near its 25 °C starting value during rest,
# then rises during discharge while remaining below the 45 °C project limit.
# Why it happens: Current flow creates irreversible resistive heat and reversible
# electrochemical heat inside the cell. The lumped thermal model treats the cell
# as one uniform temperature and includes heat loss to the surroundings, which
# causes the rate of temperature increase to slow. The 0 °C and 45 °C lines are
# visual project references; this experiment does not stop at those temperatures.

# STATE OF CHARGE VS. TIME
# What happens: SOC remains at 100% during rest and then decreases almost linearly,
# crosses the 20% reference, and finishes near 5% when the voltage cutoff is met.
# Why it happens: Rest removes no charge, while constant current removes charge at
# an almost constant rate. Discharged capacity therefore rises almost linearly,
# causing the calculated SOC to fall almost linearly. SOC does not need to reach
# 0% because terminal voltage reaches the protective 3.0 V cutoff first. The 20%
# line is a warning reference only and does not stop this simulation.
