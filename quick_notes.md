# Battery capacity and the 2.153 Ah SOC reference

- **Ah (amp-hours)** measures electrical charge. The simulation delivers about **2.153 Ah** before reaching the **3.0 V cutoff** at **0.4 A** and **25°C ambient temperature**.
- The code treats this capacity as a full "fuel tank" to calculate SOC: removing half gives 50% remaining. It changes the calculated percentages, not the physical battery.
- The real battery is rated **2.0 Ah**. The 2.153 Ah value comes from our approximate model; hardware testing is needed to establish the real battery's capacity.
