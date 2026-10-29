# Python validation results

The software pipeline is ready to support an Arduino **measurement logger**,
with SOC estimation remaining in Python. This validation checks the estimator
calculations and exposes the simulated model's limitations. It does not
establish accuracy for the purchased battery or implement hardware protection.

## Direct mathematical checks

All **130 automated tests pass** with Python 3.13.1 and PyBaMM 26.7.1.0.
The 29 tests in [test_estimators.py](python/tests/test_estimators.py) do not
depend on PyBaMM or the fitted project parameters. They check:

- Coulomb counting against exact charge integrals, including charging, irregular
  sample intervals, duplicate switching times, sensor-offset drift, and clipping.
- The scalar KF against hand-calculated updates and a Gaussian posterior,
  including current/resistance compensation and covariance propagation.
- The two-state EKF against closed-form nonlinear one-RC responses and a
  hand-calculated update with cross-covariance. Checks include polarization
  recovery, derivative boundaries, incorrect initialization, and flat OCV.

No changes to the three estimator algorithms were needed. These checks support
correctness under the specified mathematical models; they are not a proof of
accuracy under arbitrary battery conditions.

Run the checks from the repository root:

```bash
python -m unittest discover -s python/tests -v
```

## Broader simulated evaluation

The recorded evaluation uses three profiles (constant discharge, changing load,
and charge/discharge), starting at 10%, 50%, and 90% SOC, at 15°C, 25°C, and
35°C ambient/initial temperature. Each condition has nominal initialization,
an initial estimate 10 percentage points too low, and increased current bias.
That produces **27 conditions, 81 scenarios, and 243 estimator traces**.
All requested experiment steps completed without reaching a voltage cutoff.
The evaluated reference SOC spans approximately 6.5–90% in separate short
windows; it is not one continuous full-discharge validation.

Calibration is fixed across the entire experiment: the OCV curve and capacity
reference come from 25°C experiments, and R0/R1/C1 come from the 50% SOC pulse.
Usable reference capacity remains 2.153329 Ah, with approximately R0 = 0.04155 Ω,
R1 = 0.003404 Ω, and C1 = 3576 F. Evaluation data never enters the fitter or
changes the filter tuning. Temperature changes the PyBaMM cell physics; the
estimators do not yet compensate their parameters for measured temperature.

Reproduce the evaluation and save its results:

```bash
python -m python.evaluation.compare_estimators \
  --soc-starts 0.1 0.5 0.9 --temperatures 15 25 35 \
  --database results/validation.sqlite3 --save-dir results/validation --no-show
```

Each invocation appends a comparison; use the printed ID to reopen it. The
initial recorded evaluation is comparison 1 in `results/validation.sqlite3`.
The output directory contains 27 PNGs, `metrics.csv`, `kf_diagnostics.csv`, and
`metadata.json`. These generated files are ignored by Git.

The table reports the **range of SOC RMSE across the three profiles**, using
nominal initialization and the original +10 mA current bias. Units are SOC
percentage points; the ranges describe these runs, not confidence intervals.

| Starting SOC | Ambient | Coulomb counting | KF | EKF |
| --- | --- | --- | --- | --- |
| 10% | 15°C | 0.077–0.086 | 11.244–12.276 | 0.552–0.887 |
| 10% | 25°C | 0.087–0.093 | 10.507–11.472 | 0.587–0.782 |
| 10% | 35°C | 0.082–0.091 | 9.784–10.642 | 1.139–1.177 |
| 50% | 15°C | 0.087–0.092 | 3.195–3.352 | 1.103–1.239 |
| 50% | 25°C | 0.081–0.092 | 3.504–3.626 | 0.539–0.668 |
| 50% | 35°C | 0.079–0.086 | 3.814–3.984 | 0.116–0.154 |
| 90% | 15°C | 0.084–0.091 | 1.740–2.267 | 0.150–0.216 |
| 90% | 25°C | 0.081–0.089 | 1.618–2.132 | 0.091–0.139 |
| 90% | 35°C | 0.079–0.102 | 1.533–2.054 | 0.101–0.178 |

Correctly initialized Coulomb counting benefits from the known reference
capacity and short duration. Its results do not demonstrate small long-term
drift. With the wrong initial SOC, the EKF demonstrates a qualifying ±2-point,
60-second convergence interval in all 27 conditions, starting after 7–305
seconds. This metric does not guarantee the estimate remains inside the band
forever. The largest EKF error across all scenarios is about 9.43 points,
including the initial transient; all-scenario EKF RMSE ranges from 0.091 to
1.970 points.

At the 10% starting condition, the wrong initial estimate is exactly zero.
Coulomb-counter clipping then affects its error trend: a shrinking gap as the
reference discharges is not evidence that the counter recovered its initial SOC.
Likewise, lower errors at a different temperature or with a larger sensor bias
can result from cancellation of other errors, rather than a better model.

## Why the KF has a larger error

[model_diagnostics.py](python/evaluation/model_diagnostics.py) examines the
voltage-implied SOC from the scalar KF's fixed straight OCV line. It separates
four contributions: nonlinear OCV versus that line, unmodeled terminal-voltage
dynamics, voltage-sensor error, and current-sensor error through R0. The
identity is checked independently in eight diagnostic tests.

Near 50% SOC at 25°C, the straight-line OCV contribution averages approximately
**−3.27 to −3.85 SOC points**, depending on the profile. The unmodeled-dynamics
contribution is approximately −0.09 to −0.16 points. At a 10% start, the OCV-line
contribution instead becomes approximately **+11.22 to +11.95 points**. These
results support the straight-line approximation as the main source of the
large KF bias in these experiments. Correct KF arithmetic cannot remove a
systematic error in its voltage model.

These contributions describe **unclipped SOC implied by voltage**, before the
filter combines voltage with its prior and current integration. They are not
an exact decomposition of the filtered KF's RMSE. The diagnostic uses reference
SOC only after estimation, and never feeds corrections back into the filters.
Its unmodeled-dynamics term also includes temperature and resistance mismatch
when those differ from the calibration assumptions.

The KF remains a useful linear baseline. It has not been refitted separately
on each evaluation window to make its results appear better. A future local
linear model would need an explicit calibration range and separate validation.

## Next milestone

Implement timestamped Arduino voltage/current/temperature acquisition, a
documented serial record, and a Python serial logger. Calibrate the sensors and
characterize the real cell before applying the borrowed simulation parameters
to hardware measurements. Keep the SOC estimators in Python; an Arduino EKF
remains a later extension. Hardware reference SOC will itself be an uncertain
Coulomb-count reference.
