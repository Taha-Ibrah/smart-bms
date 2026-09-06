# Purpose of this python code:
# add random noise
# add constant bias
# return simulation sensor readings

import numpy as np

# Support both package imports and running this file directly.
if __package__:
    from .battery_simulation import run_battery_simulation
else:
    from battery_simulation import run_battery_simulation


def add_sensor_noise(simulation_data, seed=42):
    """Add repeatable noise and bias to clean PyBaMM measurements."""
    # These arrays are the ideal sensor values produced by PyBaMM.
    true_voltage_v = simulation_data["voltage_v"]
    true_current_a = simulation_data["current_a"]
    true_temp_c = simulation_data["temperature_c"]

    # A fixed seed produces the same random measurements for repeatable tests.
    rng = np.random.default_rng(seed)

    # Initial sensor assumptions; replace these with hardware specifications later.
    voltage_noise_std_v = 0.005
    voltage_bias_v = 0.002

    current_noise_std_a = 0.020
    current_bias_a = 0.010

    temp_noise_std_c = 0.2
    temp_bias_c = 0.1

    # A sensor reading equals the true value plus bias and random noise.
    measured_voltage_v = (
        true_voltage_v
        + voltage_bias_v
        + rng.normal(loc=0.0, scale=voltage_noise_std_v, size=true_voltage_v.shape)
    )

    measured_current_a = (
        true_current_a
        + current_bias_a
        + rng.normal(loc=0.0, scale=current_noise_std_a, size=true_current_a.shape)
    )

    measured_temp_c = (
        true_temp_c
        + temp_bias_c
        + rng.normal(loc=0.0, scale=temp_noise_std_c, size=true_temp_c.shape)
    )

    # Keep the clean simulation values and add the sensor measurements.
    sensor_data = simulation_data.copy()
    sensor_data.update(
        {
            "measured_voltage_v": measured_voltage_v,
            "measured_current_a": measured_current_a,
            "measured_temp_c": measured_temp_c,
        }
    )
    return sensor_data


def print_sensor_error_summary(sensor_data):
    """Print a short check of the configured sensor errors."""
    # Subtract truth from each measurement to inspect the simulated sensor error.
    voltage_error_v = sensor_data["measured_voltage_v"] - sensor_data["voltage_v"]
    current_error_a = sensor_data["measured_current_a"] - sensor_data["current_a"]
    temp_error_c = sensor_data["measured_temp_c"] - sensor_data["temperature_c"]

    print("\nVoltage sensor error")
    print("Configured bias: 0.0020 V")
    print(f"Measured mean error: {voltage_error_v.mean():.4f} V")
    print(f"Measured error standard deviation: {voltage_error_v.std():.4f} V")

    print("\nCurrent sensor error")
    print("Configured bias: 0.0100 A")
    print(f"Measured mean error: {current_error_a.mean():.4f} A")
    print(f"Measured error standard deviation: {current_error_a.std():.4f} A")

    print("\nTemperature sensor error")
    print("Configured bias: 0.1000 °C")
    print(f"Measured mean error: {temp_error_c.mean():.4f} °C")
    print(f"Measured error standard deviation: {temp_error_c.std():.4f} °C")


# Only run and print a simulation when this file is executed directly.
if __name__ == "__main__":
    simulation_data = run_battery_simulation()
    sensor_data = add_sensor_noise(simulation_data)
    print_sensor_error_summary(sensor_data)
