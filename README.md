# smart_bms

A battery simulation and measurement project for learning how to estimate
**State of Charge (SOC)** using Coulomb counting, a Kalman Filter (KF), and an
Extended Kalman Filter (EKF).

The goal is to compare the estimators using simulated data, then evaluate them
offline in Python using voltage, current, and temperature logged by an Arduino.
The current implementation is a research prototype; hardware acquisition and
protection control have not been implemented.

## Current focus

The Python pipeline now characterizes the model, generates shared evaluation
profiles, adds sensor noise, and compares all three SOC estimators with plots
and accuracy metrics. SQLite now retains the measurements, estimates, metrics,
and metadata so a comparison can be reopened without rerunning PyBaMM or the
filters. Direct mathematical estimator tests and a broader SOC/temperature
comparison are now implemented; see [validation.md](validation.md) for results
and limitations. **Next: build Arduino acquisition and a Python serial logger,
keeping estimation offline in Python.** Hardware validation has not started.

OCV means open-circuit voltage: the battery voltage after resting without a
load. Its fitted straight line supplies the KF, while a nonlinear lookup and
fitted R0/R1/C1 supply the EKF. See [architecture.md](architecture.md) for file
responsibilities and the current data flow.

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
| Initial cell and ambient temperature | 25 °C by default; evaluation profiles can vary both together |
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
| Simulated pulse response | [pulse_test.py](python/simulation/pulse_test.py) | Implemented; rest/pulse/recovery data and plots |
| One-RC parameter identification | [fit_ecm.py](python/simulation/fit_ecm.py) | Implemented; R0/R1/C1 fitting and separate-pulse voltage validation |
| Coulomb counting | [coulomb_counter.py](python/estimation/coulomb_counter.py) | Implemented |
| Linear, one-state KF | [kalman_filter.py](python/estimation/kalman_filter.py) | Implemented; evaluated with fitted OCV line and local R0 |
| Two-state EKF: SOC and polarization voltage | [ekf.py](python/estimation/ekf.py) | Implemented; evaluated with nonlinear OCV and local R0/R1/C1 |
| Shared evaluation profiles | [profiles.py](python/simulation/profiles.py) | Implemented; constant discharge, changing load, charge/discharge, and rest periods |
| Estimator comparison | [compare_estimators.py](python/evaluation/compare_estimators.py) | Implemented; shared noisy inputs, selectable SOC/temperature grid, three scenarios per condition, four metrics, optional CSV/JSON exports |
| KF model diagnosis | [model_diagnostics.py](python/evaluation/model_diagnostics.py) | Implemented; offline decomposition of voltage-implied SOC error into OCV-line mismatch, unmodeled dynamics, and sensor errors |
| Arduino sensors, calibration, and serial protocol | [arduino_firmware/](arduino_firmware/) | Empty placeholders |
| Shared comparison plots | [plots.py](python/visualization/plots.py) | Implemented; actual estimated SOC and signed error against the simulation reference |
| Automated tests | [python/tests/](python/tests/) | Direct estimator mathematics plus simulation, circuit-fit, profile, metric, noise, comparison, diagnostic, and database checks implemented |
| SQLite storage | [python/database/](python/database/) | Implemented; append-only comparisons with measurements, estimates, metrics, and metadata; save/load integrated into the runner |
| Docker setup | [docker/](docker/) | Empty placeholder; optional supporting work |

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
to keep the SOC reference consistent. Obtain the KF's series resistance and
the EKF's R0/R1/C1 from the pulse-response fitter below.

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
pulse raises an error. The module generates characterization data; `fit_ecm.py`
identifies the circuit parameters from it. Fitted values still need hardware
validation.

Fit a one-RC voltage model and predict a separate pulse:

```bash
python python/simulation/fit_ecm.py
```

The script builds the nonlinear OCV lookup and fits a **0.4 A, 60 s** pulse
near **50% reference SOC**. It averages the load-on/load-off jump estimates for
R0, then uses
[SciPy least squares](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.least_squares.html)
to fit positive R1 and the RC time constant. C1 is derived from that time
constant and R1. Pulse and recovery receive equal influence in the objective;
reported voltage RMSE values are unweighted. No voltage offset is fitted.

Identification starts at the final pre-pulse rest sample, assuming zero initial
polarization. It uses reference SOC to account for changing OCV; this is offline
model identification, not an input given to the SOC estimators. The helper
rejects a pre-pulse OCV mismatch above its default 1 mV tolerance and requires
both sides of each current switch. Numerical search bounds and bound-hit status
are available in the returned fit.

The same parameters then predict a separately generated **0.8 A, 120 s** pulse
without refitting. The plot compares predicted voltage with PyBaMM voltage and
shows the residuals. These are voltage-model errors, not SOC estimator results.
Parameters describe a local surrogate approximation. The broader comparison
below tests how that unchanged fit generalizes; it does not establish validity
across an entire discharge.

For reuse, `run_ecm_fit()` returns `fit`, `validation`, and `ocv_curve` entries.
The fit contains `r0_ohm`, `r1_ohm`, `c1_f`, `tau_s`, jump estimates, optimizer
status, and `training` predictions/errors. Use its R0 as the KF's
`internal_resistance_ohm`, and pass R0/R1/C1 to the EKF's corresponding arguments.
`predict_ecm_voltage()` predicts voltage from supplied current and SOC;
`evaluate_ecm()` reports errors using fixed parameters.

Compare all three estimators on the shared evaluation profiles:

```bash
python -m python.evaluation.compare_estimators
```

By default this runs constant discharge, changing load, and charge/discharge profiles.
Each begins near 50% reference SOC after a one-hour settling rest. Preparation
is excluded from evaluation. Each profile is replayed with an initial estimate
of 50%, a wrong estimate of 40%, and an increased current bias (+50 mA instead
of +10 mA). The first two cases reuse identical readings; the bias case changes
only the current offset, preserving the random noise draws. Noise settings and
each condition's seed are recorded in the returned results.

Evaluate the three profiles at starting SOCs of 10%, 50%, and 90% and ambient
temperatures of 15, 25, and 35 °C:

```bash
python -m python.evaluation.compare_estimators --soc-starts 0.1 0.5 0.9 --temperatures 15 25 35 --database results/validation.sqlite3 --save-dir results/validation --no-show
```

This creates 27 profile/SOC/temperature conditions, each with three scenarios:
81 scenarios and 243 estimator traces. Nominal initialization uses the requested
starting SOC; wrong initialization is 10 percentage points lower, floored at
zero. These remain short windows starting at three SOC levels, rather than
full-discharge tests. The completed grid is saved as comparison 1 in
`results/validation.sqlite3`; the report is [validation.md](validation.md).

The capacity reference and OCV lookup stay fixed at 25 °C, and the circuit fit
stays fixed near 50% SOC at 25 °C. The grid changes the simulated cell's initial
and ambient temperatures together, leaving filter parameters and tuning
unchanged. Temperature is logged but is not an estimator input or a compensation
mechanism. This tests generalization of the existing model rather than fitting
a new model at every evaluation condition.

Each figure contains actual estimated SOC above signed SOC error, with one
column per scenario. The terminal reports time-weighted RMSE and maximum error
in SOC percentage points, convergence time in seconds, and signed drift in
percentage points/hour. Convergence requires remaining within ±2 points for
60 seconds; `--` means no qualifying interval was found. Drift is a time-weighted
linear trend over the final five minutes, not a long-term prediction.

Run one profile and save plots, metrics, and metadata without opening windows:

```bash
python -m python.evaluation.compare_estimators --profiles changing_load --save-dir results/comparison --no-show
```

The output folder contains one PNG per profile/SOC/temperature condition,
`metrics.csv`, `kf_diagnostics.csv`, and `metadata.json`. SOC and temperature
identify each condition in the exports. A completed comparison is also saved by default to
`results/smart_bms.sqlite3`, including the full measurement and estimate arrays.
The terminal reports the comparison ID needed to reopen it. `results/` contains
generated output and is excluded from Git tracking.

Use `--database PATH` to choose another SQLite file, or `--no-store` to compute
without saving to SQLite. With `--no-show` alone, the runner prints metrics and
saves the comparison without creating figures. Use `--seed` to choose a
repeatable noise realization; each condition uses that seed plus its position
in temperature, starting-SOC, then profile order.

Reopen a saved comparison, using the ID printed when it was stored:

```bash
python -m python.evaluation.compare_estimators --load-id 1
```

Add `--database PATH` if it was stored elsewhere. Loading prints the stored
metrics and plots the stored estimates; it does not rerun simulation or
estimation and does not insert another comparison. `--save-dir` and `--no-show`
also work when reopening results. Older comparisons without grid or diagnostic
metadata remain loadable and plottable.

The database helpers can also be called from Python:

```python
from python.database.database import list_comparisons, load_comparison, save_comparison
from python.evaluation.compare_estimators import run_comparison

database_path = "results/smart_bms.sqlite3"
results = run_comparison(profile_names=("changing_load",))
comparison_id = save_comparison(database_path, results)
print(list_comparisons(database_path))
restored_results = load_comparison(database_path, comparison_id)
```

SQLite uses Python's standard-library `sqlite3`; no database server or extra
package is needed. Each save appends a complete comparison in one transaction.
Clean/reference readings and noisy sensor readings occupy separate fields, and
sample indices preserve current-switch samples even when timestamps repeat.
Stored metadata includes noise settings, capacity/SOC reference, model inputs,
estimator settings, initialization, metric definitions, evaluation grid,
condition labels, offline KF diagnostics, and software versions.
Restored results can be passed to the existing summary and plotting functions.

Run module commands from the repository root. Direct execution is also supported
with `python python/evaluation/compare_estimators.py`. Python source files live
inside folders for simulation, estimation, evaluation, visualization, database
storage, and tests; the repository root contains documentation.

Filter settings are fixed across scenarios and have not been tuned against the
evaluation reference. With current defaults, short tests can favor correctly
initialized Coulomb counting, while the EKF can correct a wrong initial SOC.
The KF's full-range straight OCV approximation produces a larger bias here.
The offline diagnostic separates its voltage-implied SOC error into four
contributions: the straight-line OCV mismatch, unmodeled battery dynamics,
voltage-sensor error, and current-sensor error. The OCV-line mismatch is the main
contribution in the tested grid and worsens at low SOC. This decomposition
explains the model's voltage-implied SOC, not the KF's actual filtered output.
Reference SOC supports this diagnosis after estimation; the diagnosis never
feeds back into the filters or retunes them. See [validation.md](validation.md).

Each profile spans only a few SOC percentage points, and charge profiles use
parameters identified from discharge pulses. The broader grid does not establish
full-range or physical-cell accuracy.

Run all automated checks:

```bash
python -m unittest discover -s python/tests -v
```

The full suite currently passes **130 tests**. The focused
[estimator tests](python/tests/test_estimators.py) compare charge
integrals, hand-calculated KF/EKF updates, and known nonlinear one-RC trajectories
without PyBaMM. They also check initialization, offset drift, covariance
stability, derivatives, clipping, and input validation.

These commands run simulations only. They do not connect to an Arduino, charge
a battery, or control a physical load.

## Roadmap

1. **Prepare the filter model.** OCV inputs and a local 1-RC fit are implemented,
   with separate-pulse validation. Extend characterization across operating
   conditions as needed, keeping identification data separate from evaluation.
2. **Validate Phase 1.** Shared profiles, estimator replay,
   SOC/error plots, RMSE/maximum error/convergence/drift metrics, and SQLite
   storage are implemented. Direct estimator mathematics and short profiles
   starting at 10%, 50%, and 90% SOC at 15, 25, and 35 °C have been checked.
   See [validation.md](validation.md); full-cycle and hardware accuracy remain
   separate validation work.
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
   its output with Python. Add a dashboard and optional environment tooling.

The core project is complete when it provides a reproducible simulation
comparison, calibrated Arduino logs, and a documented hardware evaluation of
all three SOC estimators.
