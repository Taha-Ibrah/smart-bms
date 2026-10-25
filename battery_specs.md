# Target battery: EEMB LP103454

Selected listing: [Amazon B08214DJLJ](https://www.amazon.com/dp/B08214DJLJ),
the **103454 (2000mAh) 1-Pack** version.

| Published specification | Value |
| --- | --- |
| Type | Rechargeable lithium-ion polymer pouch cell, one cell |
| Nominal / minimum rated capacity | 2.0 Ah / 1.9 Ah, specified at 0.2C |
| Nominal voltage | 3.7 V |
| Charge voltage | 4.2 V |
| Discharge cutoff | 2.75 V |
| Maximum charge / discharge current | 2 A / 4 A |
| Charge temperature | 0–45 °C; charge limited to 0.3C at 0–20 °C |
| Discharge temperature | −20–60 °C |
| Approximate dimensions / mass | 56 × 34.5 × 10.3 mm / 40 g |

Source: [EEMB LP103454 specification, 2019-04-29, page 3](https://datasheet.octopart.com/LP103454-EEMB-datasheet-182240587.pdf).
These are published model-family specifications. Confirm the supplied pack's
revision and limits before configuring hardware charging or discharge tests.
The maximum currents are ratings, not the project's chosen test currents.
The 1 kHz AC impedance specification is not a measured DC resistance for an EKF.

## Simulation settings

`battery_simulation.py` uses PyBaMM's SPM with lumped temperature and **Ai2020**.
Ai2020 describes an Enertech LCO/graphite cell. An
[EEMB safety document for LP103454-PCM-NTC-LD](https://www.chipcad.hu/Image/GetDocument/en/2458/lp103454%20msds%20non%20dg-by%20sea.pdf)
identifies LCO/carbon chemistry, supporting this starting approximation. It does
not establish that every Amazon variant has identical electrochemistry.

The nominal capacity and electrode area are both scaled from 2.28 Ah to 2.0 Ah.
The external dimensions provide an approximate rectangular thermal envelope.
Electrode composition, OCV functions, transport properties, density, specific
heat, and cooling assumptions otherwise come from Ai2020. The listed 40 g mass
is metadata; it is not a fitted thermal mass. Voltage and temperature predictions
need validation against the purchased cell.

The experiment rests for 10 minutes, then discharges at **0.2C = 0.4 A** for at
most **5 hours**, or until **3.0 V**, whichever comes first. The time limit caps
removed charge at the rated 2 Ah: the borrowed electrochemistry can otherwise
deliver more than the nominal capacity and make the nominal-capacity SOC
reference negative. The final voltage need not equal 3.0 V, and reaching 0%
nominal-capacity SOC does not establish that the real battery is exhausted.
The project cutoff is above the published 2.75 V limit. The model's lower
and upper voltage cutoffs are explicitly set to 3.0 V and 4.2 V. The 0–45 °C
lines on the temperature plot are project references, not enforced stop events.
Initial SOC is assumed to be 100% for the simulation, not for a delivered battery.

The simulated 0.4 A load is separate from the breadboard's two-resistor voltage
measurement circuit. This software does not control or protect the hardware.

Next, measure usable capacity, rested OCV versus SOC, pulse response, and thermal
behavior. Use separate measurements to validate the model and retain the
distinction between simulated SOC and a hardware Coulomb-count reference.
