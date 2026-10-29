"""Store and reload complete estimator comparisons with SQLite.

Each save appends one comparison, including every scenario, clean/noisy sample,
estimated SOC, metric, and characterization input. Reference SOC is stored for
offline scoring, not supplied to the estimators. This module has no simulation
imports: loading a saved experiment never reruns PyBaMM or the filters.
"""

import json
from numbers import Real
from pathlib import Path
import sqlite3

import numpy as np


SCHEMA_VERSION = 1
MEASUREMENT_COLUMNS = (
    "time_s", "current_a", "voltage_v", "temperature_c",
    "discharge_capacity_ah", "true_soc", "measured_current_a",
    "measured_voltage_v", "measured_temp_c",
)
METRIC_COLUMNS = (
    "rmse_pp", "max_error_pp", "convergence_time_s", "drift_pp_per_hour",
    "initial_error_pp", "final_error_pp",
)
METRIC_KEYS = (*METRIC_COLUMNS, "drift_interval_s")
_ARRAY_TAG = "__smart_bms_ndarray__"


class _MetadataEncoder(json.JSONEncoder):
    """Keep arrays as arrays after loading, including integer switch indices."""

    def default(self, value):
        if isinstance(value, np.ndarray):
            if value.dtype.kind not in "biufU":
                raise ValueError("Metadata arrays must contain numbers, booleans, or text.")
            return {_ARRAY_TAG: True, "dtype": value.dtype.str,
                    "shape": list(value.shape), "values": value.tolist()}
        if isinstance(value, np.generic):
            return value.item()
        return super().default(value)


def _metadata_object(value):
    if value.get(_ARRAY_TAG) is True:
        return np.asarray(value["values"], dtype=value["dtype"]).reshape(value["shape"])
    return value


def _dump_metadata(value):
    try:
        # Reject NaN/Infinity here as well as in SQL sample/metric columns.
        return json.dumps(value, cls=_MetadataEncoder, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"Metadata must be finite JSON-compatible values: {error}") from error


def _load_metadata(value):
    return json.loads(value, object_hook=_metadata_object)


def _number(value, name, *, minimum=None, maximum=None):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number.")
    value = float(value)
    if not np.isfinite(value) or (minimum is not None and value < minimum) or (
        maximum is not None and value > maximum
    ):
        raise ValueError(f"{name} is outside its valid finite range.")
    return value


def _series(value, name, size=None):
    try:
        array = np.asarray(value, dtype=float)
    except (ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite numeric array.") from error
    if array.ndim != 1 or array.size < 2 or (size is not None and array.size != size):
        raise ValueError(f"{name} must be one-dimensional and aligned with at least two samples.")
    if not np.isfinite(array).all():
        raise ValueError(f"{name} must contain only finite samples.")
    return array


def _connect(database_path, *, read_only=False):
    path = Path(database_path).expanduser()
    if read_only:
        if not path.is_file():
            raise FileNotFoundError(f"Database does not exist: {path}")
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _check_version(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported database schema version {version}; expected {SCHEMA_VERSION}."
        )


def initialize_database(database_path):
    """Create a version-1 database; leave an existing compatible database intact."""
    connection = _connect(database_path)
    try:
        # Take the write lock before inspecting the version so simultaneous
        # first saves cannot both attempt to create the same schema.
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if tables:
                raise ValueError("Refusing to initialize a nonempty, unversioned database.")
            # complete_statement keeps each CREATE TABLE intact. executescript
            # would implicitly commit first and break this initialization transaction.
            statement = ""
            for line in Path(__file__).with_name("schema.sql").read_text().splitlines(True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise ValueError("Incomplete SQL in schema.sql.")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        else:
            _check_version(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _validated_scenario(scenario):
    """Normalize one scenario before SQL insertion and reject incomplete data."""
    if not isinstance(scenario, dict):
        raise ValueError("Each scenario must be a dictionary.")
    try:
        for name in ("profile_name", "scenario_name"):
            if not isinstance(scenario[name], str) or not scenario[name].strip():
                raise ValueError(f"{name} must be a nonempty string.")
        initial_soc = _number(scenario["initial_soc"], "initial_soc", minimum=0, maximum=1)
        data, estimates, metrics = (scenario[key] for key in ("data", "estimates", "metrics"))
        if not all(isinstance(item, dict) for item in (data, estimates, metrics)):
            raise ValueError("Scenario data, estimates, and metrics must be dictionaries.")
        time = _series(data["time_s"], "time_s")
        if np.any(np.diff(time) < 0) or time[-1] <= time[0]:
            raise ValueError("Timestamps must be chronological and span positive time.")
        arrays = {name: _series(data[name], name, time.size) for name in MEASUREMENT_COLUMNS}
        if np.any((arrays["true_soc"] < -1e-8) | (arrays["true_soc"] > 1 + 1e-8)):
            raise ValueError("Reference SOC must lie between zero and one.")
        _number(data["usable_capacity_ah"], "usable_capacity_ah", minimum=np.finfo(float).tiny)
        if "nominal_capacity_ah" in data:
            _number(data["nominal_capacity_ah"], "nominal_capacity_ah", minimum=np.finfo(float).tiny)
        if "initial_soc" in data:
            _number(data["initial_soc"], "data.initial_soc", minimum=-1e-8, maximum=1 + 1e-8)
        phase = None
        if "phase" in data:
            phase = np.asarray(data["phase"])
            if phase.shape != time.shape or phase.dtype.kind != "U":
                raise ValueError("phase must contain one text label per measurement.")
        if not estimates or estimates.keys() != metrics.keys():
            raise ValueError("Estimates and metrics must have the same nonempty estimator names.")
        validated_estimators = []
        for name, values in estimates.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Estimator names must be nonempty strings.")
            estimate = _series(values, f"{name} estimates", time.size)
            score = metrics[name]
            if not isinstance(score, dict):
                raise ValueError("Each estimator's metrics must be a dictionary.")
            numeric = {}
            for key in METRIC_COLUMNS:
                value = score[key]
                numeric[key] = None if key == "convergence_time_s" and value is None else _number(value, key)
            if numeric["rmse_pp"] < 0 or numeric["max_error_pp"] < 0:
                raise ValueError("RMSE and maximum error cannot be negative.")
            convergence = numeric["convergence_time_s"]
            if convergence is not None and not 0 <= convergence <= time[-1] - time[0]:
                raise ValueError("Convergence must be absent or within the experiment duration.")
            interval = np.asarray(score["drift_interval_s"], dtype=float)
            if interval.shape != (2,) or not np.isfinite(interval).all() or not (
                time[0] <= interval[0] < interval[1] <= time[-1]
            ):
                raise ValueError("The drift interval must be finite and within the measurement times.")
            score_metadata = _dump_metadata({key: value for key, value in score.items() if key not in METRIC_KEYS})
            validated_estimators.append((name, estimate, numeric, interval, score_metadata))
        scenario_metadata = _dump_metadata({
            key: value for key, value in scenario.items()
            if key not in ("profile_name", "scenario_name", "initial_soc", "data", "estimates", "metrics")
        })
        data_metadata = _dump_metadata({
            key: value for key, value in data.items() if key not in (*MEASUREMENT_COLUMNS, "phase")
        })
    except KeyError as error:
        raise ValueError(f"Missing required comparison field: {error.args[0]}") from error
    return initial_soc, arrays, phase, validated_estimators, scenario_metadata, data_metadata


def save_comparison(database_path, results):
    """Append a complete comparison atomically and return its comparison_id.

    SQL keys use scenario/sample indices so duplicate switch timestamps survive.
    An invalid later scenario rolls back earlier inserts in the same comparison.
    Existing comparisons are never replaced.
    """
    if not isinstance(results, dict) or not isinstance(results.get("scenarios"), (list, tuple)) or not results["scenarios"]:
        raise ValueError("A comparison must contain a nonempty scenarios list.")
    settings = results.get("metric_settings", {})
    if not isinstance(settings, dict):
        raise ValueError("metric_settings must be a dictionary.")
    for key, value in settings.items():
        if key in ("convergence_tolerance_pp", "convergence_hold_s", "drift_window_s"):
            value = _number(value, key, minimum=0)
            if key != "convergence_hold_s" and value == 0:
                raise ValueError(f"{key} must be positive.")
    metadata = _dump_metadata({key: value for key, value in results.items() if key != "scenarios"})
    initialize_database(database_path)
    connection = _connect(database_path)
    try:
        with connection:
            _check_version(connection)
            comparison_id = connection.execute(
                "INSERT INTO comparisons (metadata_json) VALUES (?)", (metadata,)
            ).lastrowid
            for scenario_index, scenario in enumerate(results["scenarios"]):
                initial_soc, arrays, phase, estimators, scenario_metadata, data_metadata = _validated_scenario(scenario)
                scenario_id = connection.execute(
                    "INSERT INTO scenarios (comparison_id, scenario_index, profile_name, scenario_name, initial_soc, metadata_json, data_metadata_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (comparison_id, scenario_index, scenario["profile_name"], scenario["scenario_name"],
                     initial_soc, scenario_metadata, data_metadata),
                ).lastrowid
                measurement_sql = (
                    "INSERT INTO measurements (scenario_id, sample_index, "
                    + ", ".join(MEASUREMENT_COLUMNS) + ", phase) VALUES ("
                    + ", ".join("?" for _ in range(len(MEASUREMENT_COLUMNS) + 3)) + ")"
                )
                connection.executemany(measurement_sql, (
                    (scenario_id, index, *(float(arrays[key][index]) for key in MEASUREMENT_COLUMNS),
                     None if phase is None else str(phase[index]))
                    for index in range(arrays["time_s"].size)
                ))
                for estimator_index, (name, estimate, score, interval, score_metadata) in enumerate(estimators):
                    connection.execute(
                        "INSERT INTO metrics (scenario_id, estimator_name, estimator_index, "
                        + ", ".join(METRIC_COLUMNS) + ", drift_start_s, drift_end_s, metadata_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (scenario_id, name, estimator_index, *(score[key] for key in METRIC_COLUMNS),
                         float(interval[0]), float(interval[1]), score_metadata),
                    )
                    connection.executemany(
                        "INSERT INTO estimates (scenario_id, estimator_name, sample_index, soc) VALUES (?, ?, ?, ?)",
                        ((scenario_id, name, index, float(value)) for index, value in enumerate(estimate)),
                    )
        return int(comparison_id)
    finally:
        connection.close()


def load_comparison(database_path, comparison_id):
    """Reload a saved result dictionary without simulating or estimating again."""
    if isinstance(comparison_id, (bool, np.bool_)) or not isinstance(comparison_id, (int, np.integer)) or comparison_id < 1:
        raise ValueError("comparison_id must be a positive integer.")
    connection = _connect(database_path, read_only=True)
    try:
        _check_version(connection)
        row = connection.execute(
            "SELECT metadata_json FROM comparisons WHERE comparison_id = ?", (int(comparison_id),)
        ).fetchone()
        if row is None:
            raise KeyError(f"Comparison {comparison_id} does not exist.")
        results = _load_metadata(row["metadata_json"])
        results["scenarios"] = []
        for scenario in connection.execute(
            "SELECT * FROM scenarios WHERE comparison_id = ? ORDER BY scenario_index", (int(comparison_id),)
        ):
            scenario_id = scenario["scenario_id"]
            item = _load_metadata(scenario["metadata_json"])
            data = _load_metadata(scenario["data_metadata_json"])
            samples = connection.execute(
                "SELECT * FROM measurements WHERE scenario_id = ? ORDER BY sample_index", (scenario_id,)
            ).fetchall()
            data.update({name: np.asarray([sample[name] for sample in samples], dtype=float) for name in MEASUREMENT_COLUMNS})
            if any(sample["phase"] is not None for sample in samples):
                data["phase"] = np.asarray([sample["phase"] for sample in samples], dtype=str)
            estimates, metrics = {}, {}
            for score in connection.execute(
                "SELECT * FROM metrics WHERE scenario_id = ? ORDER BY estimator_index", (scenario_id,)
            ):
                name = score["estimator_name"]
                estimates[name] = np.asarray([
                    row["soc"] for row in connection.execute(
                        "SELECT soc FROM estimates WHERE scenario_id = ? AND estimator_name = ? ORDER BY sample_index",
                        (scenario_id, name),
                    )
                ], dtype=float)
                metrics[name] = {**_load_metadata(score["metadata_json"]), **{key: score[key] for key in METRIC_COLUMNS},
                                 "drift_interval_s": (score["drift_start_s"], score["drift_end_s"])}
            item.update(profile_name=scenario["profile_name"], scenario_name=scenario["scenario_name"],
                        initial_soc=scenario["initial_soc"], data=data, estimates=estimates, metrics=metrics)
            results["scenarios"].append(item)
        return results
    finally:
        connection.close()


def list_comparisons(database_path):
    """List saved comparison IDs and UTC creation times in insertion order."""
    connection = _connect(database_path, read_only=True)
    try:
        _check_version(connection)
        return [dict(row) for row in connection.execute(
            "SELECT c.comparison_id, c.created_at, COUNT(s.scenario_id) AS scenario_count "
            "FROM comparisons c LEFT JOIN scenarios s USING (comparison_id) "
            "GROUP BY c.comparison_id ORDER BY c.comparison_id"
        )]
    finally:
        connection.close()
