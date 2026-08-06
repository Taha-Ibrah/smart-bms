"""PRACTICE RUN"""


import pybamm as pyb
import matplotlib.pyplot as mpb

# 1. SPM is a fast lithium-ion model that is useful for learning.
model = pyb.lithium_ion.SPM()

# 2. This experiment discharges the battery at 1C until it reaches 3.0 V.
experiment = pyb.Experiment(["Discharge at 1C until 3.0 V"])

# 3. Combine the model and experiment, then calculate the battery response.
simulation = pyb.Simulation(model, experiment=experiment)
solution = simulation.solve()

# 4. A solution stores each simulated quantity under a descriptive variable name.
time = solution["Time [h]"].entries
voltage = solution["Terminal voltage [V]"].entries
discharge_capacity = solution["Discharge capacity [A.h]"].entries

# 5. Estimate SOC from discharged capacity, assuming the cell starts at 100%.
nominal_capacity = simulation.parameter_values["Nominal cell capacity [A.h]"]
soc = 100 * (1 - discharge_capacity / nominal_capacity)
soc = soc.clip(0, 100)

# Keep the 1C result, then vary only C-rate so the comparison is meaningful.
results = {1.0: (time, voltage, soc)}

# 7. Run the same model at one slower and one faster discharge rate.
for c_rate in (0.5, 2.0):
    comparison_experiment = pyb.Experiment(
        [f"Discharge at {c_rate}C until 3.0 V"]
    )
    comparison_simulation = pyb.Simulation(
        pyb.lithium_ion.SPM(), experiment=comparison_experiment
    )
    comparison_solution = comparison_simulation.solve()

    comparison_time = comparison_solution["Time [h]"].entries
    comparison_voltage = comparison_solution["Terminal voltage [V]"].entries
    comparison_capacity = comparison_solution["Discharge capacity [A.h]"].entries
    comparison_nominal_capacity = comparison_simulation.parameter_values[
        "Nominal cell capacity [A.h]"
    ]
    comparison_soc = 100 * (
        1 - comparison_capacity / comparison_nominal_capacity
    )
    results[c_rate] = (
        comparison_time,
        comparison_voltage,
        comparison_soc.clip(0, 100),
    )

# 6 and 8. Plot voltage and SOC while changing only the C-rate.
figure, (voltage_axis, soc_axis) = mpb.subplots(2, 1, figsize=(8, 8))

for c_rate in sorted(results):
    result_time, result_voltage, result_soc = results[c_rate]
    voltage_axis.plot(result_time, result_voltage, label=f"{c_rate:g}C")
    soc_axis.plot(result_time, result_soc, label=f"{c_rate:g}C")

voltage_axis.set(title="Voltage comparison", ylabel="Voltage [V]")
soc_axis.set(title="SOC comparison", xlabel="Time [h]", ylabel="SOC [%]")

for axis in (voltage_axis, soc_axis):
    axis.grid(True)
    axis.legend()

figure.tight_layout()
mpb.show()
