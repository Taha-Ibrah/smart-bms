"""
Purpose: Define the lithium-ion battery being simulated
1.1: Select a battery and define its capacity and operating limilts
1.2: Use PyBaMM to simulate current, voltage, temperature and true SOC
"""
import pybamm
import matplotlib.pyplot as plt
import numpy as np

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




# ============================================
# PRINT BATTERY & SIMULATION INFORMATION
# ============================================
print("\nBattery configuration")
print("---------------------")
print(f"Model: {model.name}")
print("Parameter set: Chen2020")
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

# .plot(x, y) calls the plotting method on one Axes object. Here, time is the
# x-coordinate array and the simulated measurement is the y-coordinate array.
voltage_axis.plot(time_h, voltage_v)
# Dot notation calls methods that title this subplot and label both axes.
voltage_axis.set_title("Terminal Voltage vs. Time")
voltage_axis.set_xlabel("Time [h]")
voltage_axis.set_ylabel("Voltage [V]")

current_axis.plot(time_h, current_a)
current_axis.set_title("Current vs. Time")
current_axis.set_xlabel("Time [h]")
current_axis.set_ylabel("Current [A]")

temperature_axis.plot(time_h, temperature_c)
temperature_axis.set_title("Cell Temperature vs. Time")
temperature_axis.set_xlabel("Time [h]")
temperature_axis.set_ylabel("Temperature [°C]")

soc_axis.plot(time_h, true_soc_percent)
soc_axis.set_title("State of Charge vs. Time")
soc_axis.set_xlabel("Time [h]")
soc_axis.set_ylabel("SOC [%]")

# .flat turns the 2D Axes array into one sequence for the loop to visit.
for axis in axes.flat:
    axis.grid(True)

# tight_layout() adjusts spacing so labels do not overlap. show() displays the
# completed Figure in a window (or sends it to the active Matplotlib backend).
figure.tight_layout()
plt.show()
