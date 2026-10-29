"""Replay shared noisy PyBaMM profiles through Coulomb counting, KF, and EKF.

Run this file for SOC/error plots and a table of RMSE, maximum error,
convergence time, and final-window drift. Characterization is performed once;
the separate evaluation profiles never enter the parameter-fitting optimizer.
The command line saves complete results to SQLite by default and can reload
them without repeating simulations. No Arduino is required for this comparison.
"""

import argparse
import csv
from importlib.metadata import version
from itertools import product
import json
from pathlib import Path
import sys

import numpy as np

# Direct execution adds python/evaluation/ to sys.path. Add the repository
# root so package imports also work when this file is run directly.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python.estimation.coulomb_counter import estimate_soc_coulomb_counting
from python.estimation.kalman_filter import estimate_soc_kalman_filter
from python.estimation.ekf import estimate_soc_ekf
from python.database.database import save_comparison, load_comparison
from python.evaluation.model_diagnostics import diagnose_kf_model
from python.simulation.fit_ecm import run_ecm_fit
from python.simulation.ocv_soc import fit_linear_ocv_model, build_nonlinear_ocv_model
from python.simulation.profiles import PROFILE_STEPS, run_test_profile
from python.simulation.sensor_noise import add_sensor_noise
from python.visualization.plots import plot_estimator_comparison


# Keep tuning explicit and identical across scenarios. These are the existing
# filter defaults, not settings optimized against the evaluation reference SOC.
KF_SETTINGS = {
    "process_variance_per_s": 1e-8,
    "measurement_variance_v2": 0.05**2,
    "initial_soc_variance": 0.05**2,
}
EKF_SETTINGS = {
    "initial_polarization_voltage_v": 0.0,
    "process_soc_variance_per_s": 1e-8,
    "process_polarization_variance_v2_per_s": 1e-6,
    "measurement_variance_v2": 0.02**2,
    "initial_soc_variance": 0.05**2,
    "initial_polarization_variance_v2": 0.05**2,
}


def calculate_soc_metrics(
    time_s, reference_soc, estimated_soc, convergence_tolerance_pp=2.0,
    convergence_hold_s=60.0, drift_window_s=300.0,
):
    """Score SOC fractions, returning errors in SOC percentage points (pp).

    RMSE uses the trapezoidal time average of squared sampled error, so extra
    switch samples do not receive the weight of a full normal sample interval.
    Maximum error is the largest absolute sampled error.

    Convergence is the first sampled in-band time followed by an uninterrupted
    in-band period lasting at least convergence_hold_s. None means convergence
    was not demonstrated; a short in-band tail cannot count as convergence.

    Drift is a time-weighted linear trend in signed error over the last
    drift_window_s, in pp/hour. It is a final-window trend, not an assertion
    about long-term behavior. Initial convergence is excluded when it occurs
    before that window. The exact drift interval is returned with the metrics.
    """
    time, reference, estimate = (
        np.asarray(value, dtype=float) for value in (time_s, reference_soc, estimated_soc)
    )
    if any(value.ndim != 1 for value in (time, reference, estimate)):
        raise ValueError("Metric inputs must be one-dimensional arrays.")
    if time.size < 2 or not (time.shape == reference.shape == estimate.shape):
        raise ValueError("Metric inputs must be aligned and contain at least two samples.")
    if not all(np.isfinite(value).all() for value in (time, reference, estimate)):
        raise ValueError("Metric inputs must be finite.")
    if np.any(np.diff(time) < 0) or time[-1] <= time[0]:
        raise ValueError("Metric timestamps must be chronological and span positive time.")
    for name, value, allow_zero in (
        ("convergence_tolerance_pp", convergence_tolerance_pp, False),
        ("convergence_hold_s", convergence_hold_s, True),
        ("drift_window_s", drift_window_s, False),
    ):
        if not np.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
            raise ValueError(f"Invalid metric setting: {name}.")

    error_pp = (estimate - reference) * 100
    elapsed_s = time[-1] - time[0]
    rmse_pp = np.sqrt(np.trapezoid(error_pp**2, time) / elapsed_s)

    convergence = None
    run_start = None
    # SOC subtraction can turn an exact 2 pp error into 2.0000000000000018.
    # Preserve the inclusive threshold without accepting meaningful excess.
    inside_band = np.abs(error_pp) <= convergence_tolerance_pp + 1e-12
    for index, inside in enumerate(inside_band):
        if not inside:
            run_start = None
        else:
            if run_start is None:
                run_start = index
            if time[index] - time[run_start] >= convergence_hold_s:
                convergence = float(time[run_start] - time[0])
                break

    # Interpolate the start boundary so drift uses the requested time window,
    # even when it falls between samples. Near-duplicate switch times remain.
    window_start = max(float(time[0]), float(time[-1] - drift_window_s))
    after_start = time > window_start
    window_time = np.r_[window_start, time[after_start]]
    window_error = np.r_[np.interp(window_start, time, error_pp), error_pp[after_start]]
    x = window_time - window_start
    intervals = np.diff(window_time)
    weights = np.zeros(x.size)
    weights[:-1] += intervals / 2
    weights[1:] += intervals / 2
    if weights.sum() <= 0:
        raise ValueError("Drift window is too short for these timestamps.")
    x_mean = np.average(x, weights=weights)
    error_mean = np.average(window_error, weights=weights)
    denominator = np.sum(weights * (x - x_mean)**2)
    if denominator <= 0:
        raise ValueError("Drift requires a window with distinct timestamps.")
    drift = np.sum(weights * (x - x_mean) * (window_error - error_mean)) / denominator
    return {
        "rmse_pp": float(rmse_pp),
        "max_error_pp": float(np.max(np.abs(error_pp))),
        "convergence_time_s": convergence,
        "drift_pp_per_hour": float(drift * 3600),
        "initial_error_pp": float(error_pp[0]),
        "final_error_pp": float(error_pp[-1]),
        "drift_interval_s": (window_start, float(time[-1])),
    }


def _validated_grid(values, name, minimum, maximum, *, include_minimum=True):
    """Reject empty, repeated, or out-of-range evaluation conditions early."""
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a sequence of finite numbers.") from error
    if array.ndim != 1 or not array.size or not np.isfinite(array).all():
        raise ValueError(f"{name} must be a nonempty sequence of finite numbers.")
    below = array < minimum if include_minimum else array <= minimum
    if np.any(below) or np.any(array > maximum) or np.unique(array).size != array.size:
        raise ValueError(f"{name} contains repeated or out-of-range conditions.")
    return tuple(float(value) for value in array)


def run_comparison(
    profile_names=None, seed=42, convergence_tolerance_pp=2.0,
    convergence_hold_s=60.0, drift_window_s=300.0,
    *, initial_socs=(0.5,), ambient_temperatures_c=(25.0,),
):
    """Return shared measurements, SOC estimates, metrics, and run metadata.

    Each profile has nominal, wrong-initial-SOC (-10 pp), and increased-current-
    bias (+50 mA instead of +10 mA) scenarios. Nominal and wrong-initial-SOC
    scenarios share the same noisy data. Increased bias uses the same random
    draws, changing only the current offset. Initialization uses the requested
    starting SOC (or 10 pp lower, limited to zero), never the reference trajectory.

    Each profile runs at every requested starting-SOC/ambient-temperature pair.
    Calibration remains at 50% SOC and 25 C, including the 25 C capacity reference.
    No evaluation data is used to refit the model or retune the estimators.

    This function computes results; the CLI or save_comparison() stores them.
    """
    names = tuple(PROFILE_STEPS) if profile_names is None else tuple(profile_names)
    if not names or len(set(names)) != len(names) or any(name not in PROFILE_STEPS for name in names):
        raise ValueError("Choose one or more distinct, known evaluation profile names.")
    if not isinstance(seed, (int, np.integer)) or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be a nonnegative integer.")
    socs = _validated_grid(initial_socs, "initial_socs", 0, 1, include_minimum=False)
    temperatures = _validated_grid(ambient_temperatures_c, "ambient_temperatures_c", 0, 45)
    metric_settings = {
        "convergence_tolerance_pp": convergence_tolerance_pp,
        "convergence_hold_s": convergence_hold_s, "drift_window_s": drift_window_s,
    }
    # Validate metric settings before running the more expensive simulations.
    calculate_soc_metrics([0, 1], [0.5, 0.5], [0.5, 0.5], **metric_settings)

    characterization = run_ecm_fit()
    fit, curve = characterization["fit"], characterization["ocv_curve"]
    if fit["bounds_hit"]:
        raise RuntimeError("Inspect the bound-limited circuit fit before comparison.")
    linear = fit_linear_ocv_model(curve["soc_points"], curve["ocv_points_v"])
    ocv, derivative = build_nonlinear_ocv_model(curve["soc_points"], curve["ocv_points_v"])
    scenarios = []
    for condition_index, (temperature, target_soc, name) in enumerate(product(temperatures, socs, names)):
        clean = run_test_profile(
            name, target_soc=target_soc, ambient_temperature_c=temperature, reference_data=curve,
        )
        if not np.isclose(clean["usable_capacity_ah"], curve["usable_capacity_ah"], rtol=1e-9, atol=1e-10):
            raise ValueError("Evaluation and characterization must use the same SOC reference.")
        nominal = add_sensor_noise(clean, seed=int(seed) + condition_index)
        biased = add_sensor_noise(clean, seed=int(seed) + condition_index, current_bias_a=0.050)
        for scenario_name, data, initial_soc in (
            ("nominal", nominal, target_soc),
            ("wrong_initial_soc", nominal, max(0.0, target_soc - 0.1)),
            ("current_bias", biased, target_soc),
        ):
            common = {
                "time_s": data["time_s"], "measured_current_a": data["measured_current_a"],
                "nominal_capacity_ah": data["usable_capacity_ah"], "initial_soc": initial_soc,
            }
            # Reference SOC is deliberately absent from all estimator calls.
            estimates = {
                "Coulomb counting": estimate_soc_coulomb_counting(**common),
                "KF": estimate_soc_kalman_filter(
                    **common, measured_voltage_v=data["measured_voltage_v"],
                    ocv_at_empty_v=linear["ocv_at_empty_v"],
                    ocv_at_full_v=linear["ocv_at_full_v"],
                    internal_resistance_ohm=fit["r0_ohm"], **KF_SETTINGS,
                ),
                "EKF": estimate_soc_ekf(
                    **common, measured_voltage_v=data["measured_voltage_v"],
                    ocv_function=ocv, ocv_derivative_function=derivative,
                    r0_ohm=fit["r0_ohm"], r1_ohm=fit["r1_ohm"], c1_f=fit["c1_f"],
                    **EKF_SETTINGS,
                ),
            }
            metrics = {
                estimator: calculate_soc_metrics(data["time_s"], data["true_soc"], values, **metric_settings)
                for estimator, values in estimates.items()
            }
            scenarios.append({
                "profile_name": name, "scenario_name": scenario_name,
                # repr keeps distinct floating-point conditions distinct; :g
                # would merge very close SOC/temperature values in plots.
                "condition_name": f"{name}_soc_{target_soc!r}_temp_{temperature!r}C",
                "target_soc": target_soc, "ambient_temperature_c": temperature,
                "initial_soc": initial_soc, "data": data,
                "estimates": estimates, "metrics": metrics,
                # This reference-based voltage analysis happens after estimation.
                # Its contributions describe implied SOC, not exact KF outputs.
                "kf_model_diagnostics": diagnose_kf_model(data, linear, ocv, fit["r0_ohm"]),
            })
    return {
        "scenarios": scenarios, "metric_settings": metric_settings,
        "characterization": characterization,
        "estimator_settings": {"KF": dict(KF_SETTINGS), "EKF": dict(EKF_SETTINGS)},
        "linear_ocv_model": linear, "seed": int(seed),
        "evaluation_settings": {
            "initial_socs": list(socs), "ambient_temperatures_c": list(temperatures),
            "profile_names": list(names), "calibration_soc": 0.5,
            "calibration_temperature_c": 25.0, "capacity_reference_temperature_c": 25.0,
            "calibration_is_fixed": True,
        },
        "software_versions": {
            "python": sys.version.split()[0],
            **{name: version(name) for name in ("pybamm", "numpy", "scipy", "matplotlib")},
        },
    }


def print_comparison_summary(results):
    """Print consistent units and distinguish non-convergence from zero time."""
    settings = results["metric_settings"]
    print("\nAi2020 surrogate comparison; fixed circuit fit near 50% SOC at 25 C")
    print(
        f"Convergence: within {settings['convergence_tolerance_pp']:g} pp for "
        f"{settings['convergence_hold_s']:g} s; drift: final {settings['drift_window_s']:g} s trend."
    )
    print("RMSE is time-weighted. Drift is signed pp/hour. '--' means no sustained convergence.")
    print(f"{'Profile / scenario':43} {'Start %':>7} {'Temp C':>7} {'Estimator':17} {'RMSE pp':>9} {'Max pp':>9} {'Conv s':>9} {'Drift pp/h':>12}")
    for scenario in results["scenarios"]:
        label = f"{scenario['profile_name']} / {scenario['scenario_name']}"
        start = scenario.get("target_soc", scenario["data"].get("target_soc", 0.5)) * 100
        temperature = scenario.get("ambient_temperature_c", scenario["data"].get("ambient_temperature_c", 25.0))
        for name, metrics in scenario["metrics"].items():
            convergence = metrics["convergence_time_s"]
            convergence_text = "--" if convergence is None else f"{convergence:.1f}"
            print(
                f"{label:43} {start:7.1f} {temperature:7.1f} {name:17} {metrics['rmse_pp']:9.3f} {metrics['max_error_pp']:9.3f} "
                f"{convergence_text:>9} {metrics['drift_pp_per_hour']:12.3f}"
            )
    diagnosed = [item for item in results["scenarios"] if item["scenario_name"] == "nominal" and "kf_model_diagnostics" in item]
    if diagnosed:
        print("\nKF voltage-implied SOC mean-error contributions (pp, before filtering/clipping):")
        print(f"{'Condition':45} {'OCV line':>9} {'Dynamics':>9} {'V sensor':>9} {'I sensor':>9} {'Total':>9}")
        for item in diagnosed:
            values = item["kf_model_diagnostics"]
            print(
                f"{item.get('condition_name', item['profile_name']):45} "
                + " ".join(f"{values[key]:9.3f}" for key in (
                    "ocv_nonlinearity_mean_pp", "unmodeled_dynamics_mean_pp",
                    "voltage_sensor_error_mean_pp", "current_sensor_error_mean_pp",
                    "voltage_implied_soc_error_mean_pp",
                ))
            )


def save_comparison_summary(results, directory):
    """Export compact CSV/JSON summaries in addition to full SQLite storage."""
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    rows = []
    for scenario in results["scenarios"]:
        for name, metrics in scenario["metrics"].items():
            rows.append({
                "profile": scenario["profile_name"], "scenario": scenario["scenario_name"],
                "target_soc": scenario.get("target_soc", scenario["data"].get("target_soc", 0.5)),
                "ambient_temperature_c": scenario.get("ambient_temperature_c", scenario["data"].get("ambient_temperature_c", 25.0)),
                "estimator": name,
                **{key: metrics[key] for key in ("rmse_pp", "max_error_pp", "convergence_time_s", "drift_pp_per_hour")},
            })
    with (destination / "metrics.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    diagnostic_rows = [{
        "profile": item["profile_name"], "scenario": item["scenario_name"],
        "target_soc": item.get("target_soc", item["data"].get("target_soc", 0.5)),
        "ambient_temperature_c": item.get("ambient_temperature_c", item["data"].get("ambient_temperature_c", 25.0)),
        **item["kf_model_diagnostics"],
    } for item in results["scenarios"] if "kf_model_diagnostics" in item]
    if diagnostic_rows:
        with (destination / "kf_diagnostics.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(diagnostic_rows[0]))
            writer.writeheader()
            writer.writerows(diagnostic_rows)
    fit = results["characterization"]["fit"]
    metadata = {
        "seed": results["seed"], "metric_settings": results["metric_settings"],
        "software_versions": results.get("software_versions", {}),
        "evaluation_settings": results.get("evaluation_settings", {}),
        "estimator_settings": results["estimator_settings"],
        "circuit_parameters": {key: fit[key] for key in ("r0_ohm", "r1_ohm", "c1_f", "tau_s")},
        "linear_ocv_model": results["linear_ocv_model"],
        "scenarios": [{
            "profile_name": item["profile_name"], "scenario_name": item["scenario_name"],
            "target_soc": item.get("target_soc", item["data"].get("target_soc", 0.5)),
            "ambient_temperature_c": item.get("ambient_temperature_c", item["data"].get("ambient_temperature_c", 25.0)),
            "kf_model_diagnostics": item.get("kf_model_diagnostics"),
            "initial_soc": item["initial_soc"], "noise_settings": item["data"]["noise_settings"],
            "profile_steps": item["data"]["profile_steps"],
            "capacity_ah": item["data"]["usable_capacity_ah"],
            "soc_reference": item["data"]["soc_reference"],
            "sample_period_s": item["data"]["sample_period_s"],
        } for item in results["scenarios"]],
    }
    (destination / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", nargs="+", choices=tuple(PROFILE_STEPS), default=None)
    parser.add_argument("--seed", type=int, help="Noise seed for a new comparison (default: 42).")
    parser.add_argument("--soc-starts", nargs="+", type=float, help="Starting SOC fractions, e.g. 0.1 0.5 0.9 (default: 0.5).")
    parser.add_argument("--temperatures", nargs="+", type=float, help="Ambient/initial temperatures in C, e.g. 15 25 35 (default: 25).")
    parser.add_argument(
        "--database", type=Path, default=Path("results/smart_bms.sqlite3"),
        help="SQLite file for saving/loading comparisons (default: results/smart_bms.sqlite3).",
    )
    parser.add_argument("--no-store", action="store_true", help="Compute without saving a comparison to SQLite.")
    parser.add_argument("--load-id", type=int, help="Load a saved comparison instead of running simulations.")
    parser.add_argument("--save-dir", type=Path, help="Save plots, metrics.csv, and metadata.json.")
    parser.add_argument("--no-show", action="store_true", help="Compute without opening plot windows.")
    args = parser.parse_args()
    if args.load_id is not None:
        if args.load_id <= 0:
            parser.error("--load-id must be a positive comparison ID.")
        if any(value is not None for value in (args.profiles, args.seed, args.soc_starts, args.temperatures)):
            parser.error("--load-id uses stored settings; omit --profiles, --seed, --soc-starts, and --temperatures.")
        results = load_comparison(args.database, args.load_id)
        storage_message = f"Loaded comparison {args.load_id} from {args.database}"
    else:
        grid = {}
        if args.soc_starts is not None:
            grid["initial_socs"] = args.soc_starts
        if args.temperatures is not None:
            grid["ambient_temperatures_c"] = args.temperatures
        results = run_comparison(args.profiles, seed=42 if args.seed is None else args.seed, **grid)
        storage_message = None
        if not args.no_store:
            comparison_id = save_comparison(args.database, results)
            storage_message = f"Saved comparison {comparison_id} to {args.database}"
    print_comparison_summary(results)
    if storage_message is not None:
        print(storage_message)
    if args.save_dir is not None:
        save_comparison_summary(results, args.save_dir)
    if not args.no_show or args.save_dir is not None:
        plot_estimator_comparison(results, save_dir=args.save_dir, show=not args.no_show)


if __name__ == "__main__":
    main()
