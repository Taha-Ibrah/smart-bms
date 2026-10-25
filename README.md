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
**The immediate next task is to obtain an OCV–SOC curve and fit the resistance
and RC parameters needed by the filters.** OCV means open-circuit voltage: the
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
| Discharge stopping condition | Five hours of discharge or 3.0 V, whichever comes first |
| Project voltage limits | 3.0–4.2 V |
| Temperature plot references | 0–45 °C; these do not trigger a shutdown |
| Output interval | 10 seconds, with additional step-boundary points possible |

This replaces the earlier Chen2020 configuration for an LG M50 5 Ah cell.
Ai2020 supplies a chemistry approximation; it is not a measured EEMB parameter
set. The OCV curve, resistance, transport properties, and thermal behavior still
need validation against the purchased battery. Its outer dimensions are used
for an approximate thermal envelope; other thermal properties remain borrowed.

The simulation calculates its SOC reference from discharged charge divided by
the nominal capacity. The five-hour limit prevents removal of more than 2 Ah in
this experiment. Reaching 0% on that reference does not establish that the real
battery is exhausted, and the run can end above 3.0 V.

## What exists today

| Component | Files | Status |
| --- | --- | --- |
| Battery simulation and plots | [battery_simulation.py](python/simulation/battery_simulation.py) | Implemented; EEMB approximation needs calibration |
| Repeatable sensor noise and bias | [sensor_noise.py](python/simulation/sensor_noise.py) | Implemented; noise settings are initial assumptions |
| Coulomb counting | [coulomb_counter.py](python/estimation/coulomb_counter.py) | Implemented |
| Linear, one-state KF | [kalman_filter.py](python/estimation/kalman_filter.py) | Implemented; requires a linear OCV approximation and resistance |
| Two-state EKF: SOC and polarization voltage | [ekf.py](python/estimation/ekf.py) | Implemented; requires nonlinear OCV and R0, R1, C1 |
| Estimation notes | [soc_estimation.pdf](python/estimation/soc_estimation.pdf) | Reference document |
| Arduino sensors, calibration, and serial protocol | [arduino_firmware/](arduino_firmware/) | Empty placeholders |
| Shared comparison plots and dashboard | [python/visualization/](python/visualization/) | Empty placeholders; simulation plots already work |
| Automated tests | [python/tests/](python/tests/) | Empty placeholders |
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

These commands run simulations only. They do not connect to an Arduino, charge
a battery, or control a physical load.

## Roadmap

1. **Prepare the filter model.** Generate a rested OCV–SOC curve from the
   simulation and fit a 1-RC equivalent circuit using pulse-response data.
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
