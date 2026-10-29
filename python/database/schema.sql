-- Each comparison is one complete invocation of the evaluation pipeline.
-- JSON keeps configuration and characterization details alongside the samples;
-- measurements, estimates, and metrics remain ordinary queryable SQL columns.
CREATE TABLE comparisons (
    comparison_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metadata_json TEXT NOT NULL
);

CREATE TABLE scenarios (
    scenario_id INTEGER PRIMARY KEY,
    comparison_id INTEGER NOT NULL REFERENCES comparisons(comparison_id) ON DELETE CASCADE,
    scenario_index INTEGER NOT NULL CHECK (scenario_index >= 0),
    profile_name TEXT NOT NULL,
    scenario_name TEXT NOT NULL,
    initial_soc REAL NOT NULL CHECK (initial_soc BETWEEN 0 AND 1),
    metadata_json TEXT NOT NULL,
    data_metadata_json TEXT NOT NULL,
    UNIQUE (comparison_id, scenario_index)
);

-- Current switches can have duplicate timestamps. Sample order, rather than
-- time, identifies a measurement and preserves both sides of a voltage jump.
CREATE TABLE measurements (
    scenario_id INTEGER NOT NULL REFERENCES scenarios(scenario_id) ON DELETE CASCADE,
    sample_index INTEGER NOT NULL CHECK (sample_index >= 0),
    time_s REAL NOT NULL,
    current_a REAL NOT NULL,
    voltage_v REAL NOT NULL,
    temperature_c REAL NOT NULL,
    discharge_capacity_ah REAL NOT NULL,
    true_soc REAL NOT NULL CHECK (true_soc BETWEEN -0.00000001 AND 1.00000001),
    measured_current_a REAL NOT NULL,
    measured_voltage_v REAL NOT NULL,
    measured_temp_c REAL NOT NULL,
    phase TEXT,
    PRIMARY KEY (scenario_id, sample_index)
);

-- One metrics row also identifies each estimator in a scenario. Its index
-- preserves display order without relying on alphabetical SQL ordering.
CREATE TABLE metrics (
    scenario_id INTEGER NOT NULL REFERENCES scenarios(scenario_id) ON DELETE CASCADE,
    estimator_name TEXT NOT NULL,
    estimator_index INTEGER NOT NULL CHECK (estimator_index >= 0),
    rmse_pp REAL NOT NULL CHECK (rmse_pp >= 0),
    max_error_pp REAL NOT NULL CHECK (max_error_pp >= 0),
    convergence_time_s REAL CHECK (convergence_time_s >= 0),
    drift_pp_per_hour REAL NOT NULL,
    initial_error_pp REAL NOT NULL,
    final_error_pp REAL NOT NULL,
    drift_start_s REAL NOT NULL,
    drift_end_s REAL NOT NULL CHECK (drift_end_s > drift_start_s),
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (scenario_id, estimator_name),
    UNIQUE (scenario_id, estimator_index)
);

CREATE TABLE estimates (
    scenario_id INTEGER NOT NULL,
    estimator_name TEXT NOT NULL,
    sample_index INTEGER NOT NULL,
    soc REAL NOT NULL,
    PRIMARY KEY (scenario_id, estimator_name, sample_index),
    FOREIGN KEY (scenario_id, estimator_name)
        REFERENCES metrics(scenario_id, estimator_name) ON DELETE CASCADE,
    FOREIGN KEY (scenario_id, sample_index)
        REFERENCES measurements(scenario_id, sample_index) ON DELETE CASCADE
);
