# smart_bms

A battery simulation and measurement project for learning how to estimate
**State of Charge (SOC)** using Coulomb counting, a Kalman Filter (KF), and an
Extended Kalman Filter (EKF).

The goal is to compare the estimators using simulated data, then evaluate them
offline in Python using voltage, current, and temperature logged by an Arduino.
The current implementation is a research prototype; hardware acquisition and
protection control have not been implemented.

## Current focus

Clarify the model and build the estimator comparison one step at a time.
The OCV–SOC experiment now supplies a fitted linear model for the KF and a
nonlinear lookup with its derivative for the EKF. A simulated pulse test now
generates current-switch and voltage-recovery data. **The immediate next task is
to fit R0, R1, and C1 from that response.** OCV means open-circuit voltage: the
battery voltage after resting without a load.

The three estimator implementations exist, but there is no complete comparison
pipeline or hardware validation yet.

## Selected battery and simulation

The intended hardware battery is the **EEMB LP103454**, a rechargeable
**3.7 V, 2000 mAh lithium-ion polymer pouch cell**:
[Amazon listing](https://www.amazon.com/dp/B08214DJLJ),
**103454 (2000mAh) 1-Pack** variant.

Full specifications, sources, and assumptions are in [battery_specs.md](battery_specs.md).

| Simulation setting | Current value |
| --- | --- |
| Battery model | PyBaMM Single Particle Model (SPM) |
| Parameter set | Ai2020, an Enertech LCO/graphite starting point |
| Target nominal capacity | 2.0 Ah; electrode area also scaled from Ai2020's 2.28 Ah |
| Temperature model | Lumped: one average temperature for the cell |
| Initial cell and ambient temperature | 25 °C |
| Initial SOC | 100%, assumed for simulation |
| Experiment | Rest for 10 minutes, then discharge at 0.2C = 0.4 A |
| Capacity-test stopping condition | Reach 3.0 V; reject the test if the 10-hour time guard ends it first |
| Project voltage limits | 3.0–4.2 V |
| Temperature plot references | 0–45 °C; these do not trigger a shutdown |
| Output interval | 10 seconds, with additional step-boundary points possible |

This replaces the earlier Chen2020 configuration for an LG M50 5 Ah cell.
Ai2020 supplies a chemistry approximation; it is not a measured EEMB parameter
set. The OCV curve, resistance, transport properties, and thermal behavior still
need validation against the purchased battery. Its outer dimensions are used
for an approximate thermal envelope; other thermal properties remain borrowed.

The simulation first measures usable capacity by discharging the model at
0.4 A to 3.0 V. It calculates SOC as `1 - discharged_Ah / usable_capacity_Ah`.
The current surrogate delivers approximately **2.153 Ah** in that reference
test. Its advertised target rating remains **2.0 Ah**; these are separate values.
Both the discharge simulation and the OCV experiment use the same capacity
reference. When replaying simulated data through the estimators, pass
`usable_capacity_ah` as their capacity argument. This reference-test measurement
defines an SOC scale; it does not calibrate the battery chemistry or establish
the purchased battery's capacity. Using `1 - discharged_Ah / 2.0` would instead
express nominal-capacity SOC. That is another valid convention, with a different
meaning for its 0% point.

## What exists today

| Component | Files | Status |
| --- | --- | --- |
| Battery simulation and plots | [battery_simulation.py](python/simulation/battery_simulation.py) | Implemented; EEMB approximation needs calibration |
| Repeatable sensor noise and bias | [sensor_noise.py](python/simulation/sensor_noise.py) | Implemented; noise settings are initial assumptions |
| Rested OCV–SOC experiment | [ocv_soc.py](python/simulation/ocv_soc.py) | Implemented; settling checks, KF least-squares line, and EKF lookup/derivative |
| Simulated pulse response | [pulse_test.py](python/simulation/pulse_test.py) | Implemented; rest/pulse/recovery data and plots, parameter fitting pending |
| Coulomb counting | [coulomb_counter.py](python/estimation/coulomb_counter.py) | Implemented |
| Linear, one-state KF | [kalman_filter.py](python/estimation/kalman_filter.py) | Implemented; fitted OCV line available, resistance identification pending |
| Two-state EKF: SOC and polarization voltage | [ekf.py](python/estimation/ekf.py) | Implemented; OCV lookup/derivative available, R0/R1/C1 identification pending |
| Estimation notes | [soc_estimation.pdf](python/estimation/soc_estimation.pdf) | Reference document |
| Arduino sensors, calibration, and serial protocol | [arduino_firmware/](arduino_firmware/) | Empty placeholders |
| Shared comparison plots and dashboard | [python/visualization/](python/visualization/) | Empty placeholders; simulation plots already work |
| Automated tests | [python/tests/](python/tests/) | Simulation/OCV regression checks implemented; estimator and database test files remain placeholders |
| Database and Docker setup | [python/database/](python/database/), [docker/](docker/) | Empty placeholders; optional supporting work |

The filters do not receive the simulated SOC reference as an input. It is
reserved for evaluating their estimates. Positive current means discharge,
timestamps are in seconds, capacity is in Ah, and SOC is a fraction from 0 to 1.

## Run the existing simulation

The current simulation has been run with **Python 3.13 and PyBaMM 26.7.1.0**.
From the repository root, create an environment and install the dependencies:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install "pybamm==26.7.1.0" numpy matplotlib scipy
```

Run the battery simulation and its voltage, current, temperature, and SOC plots:

```bash
python python/simulation/battery_simulation.py
```

Run the simulation with repeatable sensor noise and print the measurement errors:

```bash
python python/simulation/sensor_noise.py
```

Generate a rested-voltage table and plot its OCV–SOC approximation:

```bash
python python/simulation/ocv_soc.py
```

This experiment first runs the continuous-discharge capacity test. By default
it then removes 2% of that reference capacity at 0.4 A between one-hour rests,
producing 51 points unless the voltage cutoff is reached first. For the current
model, each discharge step is approximately 6.46 minutes. The finer sampling
resolves Ai2020's low-SOC knee; it does not change the model's voltage equations.
Pass `soc_step=0.1` to reproduce the earlier 10% grid for comparison.
It checks voltage drift over the final 10 minutes of each rest and flags points
that exceed 1 mV. `generate_ocv_soc_curve()` returns `soc_points` and
`ocv_points_v` in increasing SOC order, together with settling diagnostics.
The SOC axis uses the same usable-capacity reference as the discharge script.
Its 0% point represents capacity removed to cutoff in the reference test;
the subsequently rested voltage can recover above 3.0 V. Usable capacity
depends on current, cutoff, temperature, and rest history, so this curve does
not establish the real battery's SOC mapping.
The existing discharge simulation and this experiment share
`create_battery_configuration()` in `battery_simulation.py`.

The plot overlays the KF's least-squares line and the EKF's nonlinear lookup on
the rested samples, and the terminal reports the line's voltage RMSE and maximum
error. The straight line can have substantial error near the low-SOC knee.
These are OCV fit errors, not SOC estimator accuracy results.

Build the inputs for the existing filters in Python:

```python
from python.simulation.ocv_soc import (
    generate_ocv_soc_curve,
    fit_linear_ocv_model,
    build_nonlinear_ocv_model,
)

curve = generate_ocv_soc_curve()
if not curve["settled_points"].all():
    raise ValueError("Increase the rest duration before building filter models.")
linear = fit_linear_ocv_model(curve["soc_points"], curve["ocv_points_v"])
ocv_function, ocv_derivative_function = build_nonlinear_ocv_model(
    curve["soc_points"], curve["ocv_points_v"]
)
```

Pass `linear["ocv_at_empty_v"]` and `linear["ocv_at_full_v"]` to the KF's
arguments of the same names. These values are the fitted line at SOC 0 and 1,
so they can differ from the original rested endpoint voltages. The returned
dictionary also includes `ocv_intercept_v`, `ocv_slope_v_per_soc`, `rmse_v`, and
`max_error_v`. Pass the two nonlinear callables to the EKF's `ocv_function` and
`ocv_derivative_function` arguments.

The nonlinear model uses
[SciPy PCHIP interpolation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.interpolate.PchipInterpolator.html)
to follow the sampled curve and obtains its derivative from the same interpolant.
Both helpers require finite, ordered samples covering SOC 0 to 1. They cannot
build a full-range filter model from an experiment stopped at a partial SOC.
Lookup queries must also be fractions from 0 to 1; the derivative is in volts
per unit SOC, not per percentage point. No extrapolation is performed.
The caller must check settling before fitting; the plot shows only raw samples
if settling or full-range coverage fails.

Use `curve["usable_capacity_ah"]` for both filters' `nominal_capacity_ah` argument
to keep the SOC reference consistent. The KF still needs series resistance and
the EKF needs R0, R1, and C1 before a meaningful loaded-cell comparison.

Generate a simulated current pulse and voltage-recovery plot:

```bash
python python/simulation/pulse_test.py
```

`run_pulse_test()` conditions the battery from full to **50% reference SOC**,
rests for **one hour**, discharges at **0.4 A for 60 seconds**, and rests for
**30 minutes**. It reuses the existing model and usable-capacity reference.
The pulse removes about 0.00667 Ah, bringing SOC to approximately 49.690%.
The first discharge is preparation; the returned arrays start at the pre-pulse
rest, while cumulative discharged Ah still includes that preparation.

The left plots show current and voltage through the full recovery, displaying
only the last minute of the initial rest. The right plots show a closer view of
the pulse. Plot time zero is load-on; returned `time_s` starts at the beginning
of the full pre-pulse rest. Shading marks the load interval. Voltage jumps at
current switching and slower relaxation provide data for later effective
R0/R1/C1 fitting. The final rested voltage can differ from the initial voltage
because the pulse removed charge.

The function returns clean `time_s`, `current_a`, `voltage_v`, `temperature_c`,
`discharge_capacity_ah`, and `true_soc` arrays, together with the capacity
reference, per-sample `phase`, and switch indices/times. Both sides of each
switch are preserved; do not round or resample them away before identifying
the immediate voltage jump. Incomplete conditioning or a cutoff-truncated
pulse raises an error. The module generates data only; R0/R1/C1 fitting and
the estimator comparison remain to be implemented. Future fitted values will
describe this surrogate under the selected conditions and still need hardware
validation.

Run the focused simulation, OCV-model, and pulse regression tests:

```bash
python -m unittest python.tests.test_simulation -v
```

These commands run simulations only. They do not connect to an Arduino, charge
a battery, or control a physical load.

## Roadmap

1. **Prepare the filter model.** The rested OCV–SOC curve and filter OCV inputs
   are implemented. Next fit a 1-RC equivalent circuit using pulse-response data.
   Obtain R0 for the immediate voltage drop and R1/C1 for voltage relaxation.
   Keep identification data separate from evaluation data.
2. **Compare all three estimators.** Replay the same noisy measurements through
   Coulomb counting, KF, and EKF. Plot SOC estimates and errors; report RMSE and
   maximum absolute error in SOC percentage points. Include changing loads,
   current bias, and a wrong-initial-SOC test. Add focused automated checks.
3. **Build Arduino acquisition.** Begin with voltage logging, then add current
   and temperature. Use the planned ADS1115, INA226, and NTC thermistor. Power
   the Uno through USB and use a separate controlled battery load. Log timestamps,
   voltage, current, and temperature over serial; run estimation in Python.
4. **Calibrate and characterize the real cell.** Calibrate against a multimeter,
   especially current offset. Measure usable capacity, rested OCV–SOC, and pulse
   response within documented operating limits. Fit the hardware model and check
   thermal behavior using measured cell temperature.
5. **Validate on hardware logs.** Use separate discharge records to evaluate the
   fitted model and estimators. Report reference uncertainty and model limitations.
   Hardware reference SOC is a Coulomb-count estimate, not independent ground truth.
6. **Optional stretch goals.** Port the two-state EKF to Arduino C++ and compare
   its output with Python. Add storage or a dashboard after the core comparison
   and hardware pipeline work.

The core project is complete when it provides a reproducible simulation
comparison, calibrated Arduino logs, and a documented hardware evaluation of
all three SOC estimators.
