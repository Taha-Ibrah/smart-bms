"""Plot completed estimator comparisons without rerunning any simulations.

The comparison runner supplies reference SOC, estimator outputs, and metrics.
SOC is stored as a fraction; plots display percent and errors in percentage
points so a change from 50% to 52% reads as a +2 pp error.
"""

from pathlib import Path
import re

import matplotlib.pyplot as plt
import numpy as np


def plot_estimator_comparison(results, save_dir=None, show=True):
    """Return one SOC/error figure per operating condition, optionally saving PNGs.

    Each column represents an evaluation scenario. Axes share time within a
    column, while each scenario gets its own vertical scale: a large initial
    SOC error should not hide the smaller errors in the nominal experiment.
    This function only visualizes the supplied arrays; it does not tune models,
    run estimators, or use reference SOC to change any estimate.
    """
    grouped = {}
    for scenario in results["scenarios"]:
        grouped.setdefault(scenario.get("condition_name", scenario["profile_name"]), []).append(scenario)

    tolerance = results["metric_settings"]["convergence_tolerance_pp"]
    colors = {"Coulomb counting": "tab:blue", "KF": "tab:orange", "EKF": "tab:green"}
    output_dir = Path(save_dir) if save_dir is not None else None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    figures = []
    for condition_name, scenarios in grouped.items():
        figure, axes = plt.subplots(
            2, len(scenarios), figsize=(6 * len(scenarios), 8),
            sharex="col", squeeze=False, constrained_layout=True,
        )
        first = scenarios[0]
        context = ""
        if "condition_name" in first:
            context = f" — start {first['target_soc'] * 100:g}% SOC, {first['ambient_temperature_c']:g}°C ambient"
        figure.suptitle(
            f"SOC estimator comparison: {first['profile_name'].replace('_', ' ')}{context}",
            fontsize=14,
        )

        for column, scenario in enumerate(scenarios):
            data = scenario["data"]
            time = np.asarray(data["time_s"], dtype=float)
            minutes = (time - time[0]) / 60
            reference = np.asarray(data["true_soc"], dtype=float)
            soc_axis, error_axis = axes[:, column]

            name = scenario["scenario_name"]
            if name == "wrong_initial_soc":
                offset_pp = 100 * (scenario["initial_soc"] - reference[0])
                title = f"Wrong initial SOC ({offset_pp:+.1f} pp)"
            elif name == "current_bias":
                title = "Current sensor bias"
            elif name == "nominal":
                title = "Nominal initial SOC"
            else:
                title = name.replace("_", " ").capitalize()
            soc_axis.set_title(title)

            soc_axis.plot(
                minutes, 100 * reference, color="black", linestyle="--",
                linewidth=2, label="Simulation reference", zorder=5,
            )
            for estimator_name, values in scenario["estimates"].items():
                estimate = np.asarray(values, dtype=float)
                color = colors.get(estimator_name)
                rmse = scenario["metrics"][estimator_name]["rmse_pp"]
                soc_axis.plot(
                    minutes, 100 * estimate, color=color, linewidth=1.4,
                    label=f"{estimator_name} (RMSE {rmse:.2f} pp)",
                )
                error_axis.plot(
                    minutes, 100 * (estimate - reference), color=color,
                    linewidth=1.4, label=estimator_name,
                )

            # These guides show the metric's error tolerance, not confidence
            # bounds. The required hold duration is evaluated by the runner.
            error_axis.axhline(0, color="0.5", linewidth=0.8)
            for level in (-tolerance, tolerance):
                error_axis.axhline(
                    level, color="tab:red", linestyle="--", linewidth=0.9,
                    label=f"±{tolerance:g} pp tolerance" if level > 0 else None,
                )
            soc_axis.set_ylabel("SOC (%)")
            error_axis.set_ylabel("Estimated − reference SOC (pp)")
            error_axis.set_xlabel("Elapsed time (min)")
            soc_axis.legend(loc="best", fontsize=8)
            error_axis.legend(loc="best", fontsize=8)
            for axis in (soc_axis, error_axis):
                axis.grid(True, alpha=0.25)
                axis.margins(x=0.01)

        if output_dir is not None:
            # Restrict filenames to a simple profile identifier even when a
            # caller supplies a descriptive title containing spaces or slashes.
            filename = re.sub(r"[^A-Za-z0-9_-]+", "_", condition_name).strip("_") or "profile"
            figure.savefig(output_dir / f"{filename}_comparison.png", dpi=160)
            if not show:
                plt.close(figure)
        figures.append(figure)

    if show and figures:
        plt.show()
    return figures
