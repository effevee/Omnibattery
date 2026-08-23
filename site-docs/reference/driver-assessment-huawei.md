# Battery driver assessment: Huawei SUN2000 + LUNA2000

Completed against [driver-requirements-template.md](driver-requirements-template.md).

Every value below was read from hardware, not from a datasheet. Where a figure
was measured, the measurement is stated. Where something is unknown, it says so.

## Assessment outcome

**SUITABLE WITH LIMITATIONS.** Every blocking requirement is covered. The
limitations are not gaps in the data but two architectural mismatches with the
existing driver model, both described in [§13](#13-what-a-dc-coupled-hybrid-breaks).

## 1. Device and documentation evidence

| Field | Value |
|---|---|
| Manufacturer | Huawei |
| Commercial model | SUN2000-8K-MAP0 with LUNA2000 |
| Device-reported model | `SUN2000-8K-MAP0` (register 30000) |
| Verified firmware range | Inverter `V200R024C00SPC110`, storage `V200R025C00SPC103` |
| Region/hardware variant | EU, three-phase, EMMA-A02 energy manager, SmartGuard-63A-T0 |
| Rated capacity and power | 13.8 kWh; BMS 7000 W charge and discharge; inverter 8000 W rated, 8800 W maximum |
| Coupling type | **DC** — battery and PV strings share one inverter |
| Official document | Solar Inverter Modbus Interface Definitions v05; SmartHEMS V100R024C00 Modbus Interface Definitions |
| Interface used | The `huawei_solar` integration by @wlcrs (register map + control services) |
| Hardware used for validation | The installation above, one unit |
| Test date | 2026-08-22 / 2026-08-23 |

- [x] The interface is published by the manufacturer (PDFs above).
- [x] Applicable models and firmware are known for the tested unit.
- [ ] **Only one installation tested.** Everything below is single-sample.
- [x] Every field used documents type, unit, scale and sign; each was verified
      against the same value surfaced by `huawei_solar`.
- [x] Read rate and concurrency measured (see transport).
- [x] Restart and disconnect behaviour known (watchdog, see control).

### Firmware compatibility matrix

| Model | Firmware | Transport | Read | Write | Known differences | Status |
|---|---|---|---|---|---|---|
| SUN2000-8K-MAP0 | V200R024C00SPC110 | Modbus TCP via proxy | yes | via `huawei_solar` | Per-string current reads 0 below ~100 W; pack 1 firmware string empty; registers 37026/37036 answer with a Modbus exception | tested |
| Other SUN2000 | — | — | — | — | String count read from 30071; up to four published | untested |

### Transport and access worksheet

| Aspect | Value |
|---|---|
| Scope | local |
| Protocol | Modbus TCP, FC03 only |
| Address | The inverter, reached through a `modbus-proxy` add-on |
| Unit id | **4** = inverter. Also on the bus: 0 = EMMA (`SmartHEMS`), 2 = SmartGuard, 9 = charger. Not discoverable — the user must supply it |
| Discovery | manual |
| Authentication | none |
| Maximum simultaneous connections | The inverter tolerates one client; a Modbus proxy fans it out. Verified: this driver read continuously while `huawei_solar` held its own connection |
| Read latency | **median 3.6 ms, 6 ms at p95** over 800 consecutive reads with nothing else on the bus |
| Post-connect pause | **1500 ms required.** The first request after the TCP handshake is dropped without it |
| Request pacing | Single in-flight request; the inverter does not tolerate pipelining. 800 reads each at 10 ms and 0 ms pacing produced zero failures, so the reference library's 50 ms is conservative for Modbus TCP; the driver uses 20 ms |
| Telemetry freshness | Battery and PV values refresh every **2–3 s** at the register |
| Volatile vs persistent | Forcible-charge registers are volatile and carry a duration. SOC cutoffs (47081/47082) are **persistent** |
| Behaviour without network | Forcible commands expire on their own; see the watchdog below |

> The 30 s figure often quoted for Huawei is the `huawei_solar` coordinator
> interval, not the hardware. Reading the registers directly is ~4 ms and the
> data is 2–3 s old at worst.

## 2. Admission gate for automatic control

- [x] Programmable transport with controlled connect, reconnect and close.
- [x] Real, fresh SOC as a percentage — register 37004, verified against the
      `huawei_solar` sensor.
- [x] Real battery power — register 37001, `+charge / −discharge`, already the
      Omnibattery convention.
- [x] Power-limited charging — `huawei_solar.forcible_charge`.
- [x] Power-limited discharging — `huawei_solar.forcible_discharge`.
- [x] A safe idle. **Not the obvious one** — see §13.1.
- [x] Safe per-unit maxima — registers 37046/37048 report 7000 W each.
- [x] BMS protections remain active; the inverter enforces its own cutoffs.
- [x] Write cadence is safe **only with a driver-side throttle** — see §13.2.
- [x] Stale communications are detectable; a failed block omits its keys.

## 3. Declared capabilities

| `DriverCapabilities` | Value | Evidence |
|---|---:|---|
| `hardware_soc_cutoff` | `False` | 47081/47082 exist and are writable, but accept only 90–100 % and 0–20 % — narrower than the window a user may configure. Claiming hardware enforcement would leave an out-of-range limit unenforced anywhere |
| `has_force_mode` | `True` | Forcible charge/discharge/stop |
| `push_telemetry` | `False` | Polled |
| `max_charge_power_w` | 7000 | Register 37046 |
| `max_discharge_power_w` | 7000 | Register 37048 |
| `has_mppt_pv` | `False` | Deliberate. See §13.3 |
| `has_alarm_registers` | `False` | 37014 exists but was not validated |
| `has_rs485_control` | `False` | No external-control gate needed |
| `has_energy_counters` | `True` | 37066/37068 cumulative, 37015/37017 daily |
| `setpoint_confirm_reliable` | `False` | The command registers echo instantly while the battery is still ramping |
| `actuator_latency_s` | 25.0 | **Measured 19.7 s** to 90 % of a +1000 W charge from idle, 11.3 s to reverse to −1000 W, sampled at 1 Hz with exclusive access |
| `readback_latency_s` | 25.0 | Telemetry is milliseconds away; the delay is the physical ramp |
| `engage_grace_s` | 25.0 | Same ramp |

## 4. Telemetry mapping worksheet

All registers FC03 holding, unit id 4, verified against the corresponding
`huawei_solar` sensor on the same installation.

| Omnibattery key | B/R/O | Register | Type | Scale → unit | Cadence | Src | Tested |
|---|---|---|---|---|---|---|---|
| `battery_soc` | B | 37004 | u16 | ÷10 → % | high | N | [x] |
| `battery_power` | B | 37001 | i32 | → W, +charge/−discharge | high | N | [x] |
| `battery_voltage` | O | 37003 | u16 | ÷10 → V | high | N | [x] |
| `inverter_state` | O | 37000 | u16 | enum, refined by direction (§13.4) | high | D | [x] |
| `battery_total_energy` | R | 37758 | u32 | **×0.001 → kWh** | very_low | N | [x] |
| `total_charging_energy` | O | 37066 | u32 | ÷100 → kWh | low | N | [x] |
| `total_discharging_energy` | O | 37068 | u32 | ÷100 → kWh | low | N | [x] |
| `total_daily_charging_energy` | O | 37015 | u32 | ÷100 → kWh | low | N | [x] |
| `total_daily_discharging_energy` | O | 37017 | u32 | ÷100 → kWh | low | N | [x] |
| `internal_temperature` | O | 37022 | i16 | ÷10 → °C | low | N | [x] |
| `max_charge_power` | R | 37046 | u32 | → W | low | N | [x] |
| `max_discharge_power` | R | 37048 | u32 | → W | low | N | [x] |
| `charging_cutoff_capacity` | O | 47081 | u16 | ÷10 → % | low | N | [x] |
| `discharging_cutoff_capacity` | O | 47082 | u16 | ÷10 → % | low | N | [x] |
| `user_work_mode` | O | 47086 | u16 | enum | low | N | [x] |
| `solar_power` | O | 32064 | i32 | → W (DC total) | high | N | [x] |
| `mppt1..4_power` | O | 32016+2n | i16 ×2 | V × I → W | high | D | [x] |
| `inverter_ac_power` | O | 32080 | i32 | → W | high | N | [x] |
| `inverter_max_power` | R | 30075 | u32 | → W | very_low | N | [x] |
| `off_grid_state` | O | 32003 | u32 | bitfield | medium | N | [x] |
| `ac_offgrid_power` | O | — | — | derived (§13.5) | medium | D | [x] |
| `device_name` | O | 30000 | str(15) | — | very_low | N | [x] |
| `storage_product_model` | O | 47000 | u16 | enum | very_low | N | — |
| `power_module_serial_number` | O | 37052 | str(10) | — | very_low | N | [x] |
| `power_module_firmware_version` | O | 37814 | str(15) | — | very_low | N | [x] |
| `inverter_serial_number` | O | 30015 | str(10) | — | very_low | N | [x] |
| `inverter_software_version` | O | 30050 | str(15) | — | very_low | N | [x] |
| `pack1..3_firmware_version` | O | 38210/38252/38294 | str(15) | — | very_low | N | [x] |
| `pack1..3_serial_number` | O | 38200/38242/38284 | str(10) | — | very_low | N | [x] |
| `max_cell_voltage` / `min_cell_voltage` | O | — | — | **X** | — | X | — |

**Not available.** A LUNA2000 reports per *pack* values, not per cell. There is
nothing honest to put in the cell fields, so balance monitoring and the 100 %
voltage taper are disabled for this brand.

**Registers that did not answer** on the tested unit: 37026 (DCDC version) and
37036 (BMS version) return a Modbus exception; the whole pack 1 run (38200)
returns padding, because that slot holds no pack — the tested unit has packs 2
and 3 only. All three are optional and omitted rather than shipped as
permanently-missing entities.

## 5. Control mapping worksheet

Set-points take either of two paths, selected per battery.

By default they go through the `huawei_solar` services. Optionally the driver
writes the same four-register sequence itself via FC16 — same registers, same
order — which removes the dependency for control. Verified on hardware: a no-op
FC16 write was acknowledged through the Modbus proxy while another client held
its own connection, and a full charge/reverse/release cycle drove the battery as
commanded.

Writing directly means reimplementing two things the services provided: the
power is clamped to the register maximum (`huawei_solar` refuses an over-range
value outright, aborting the control cycle), and the mode register is written
last so a sequence failing earlier leaves the inverter untouched.

| Operation | Call | Range | Persistence | Latency | Safe failure | Tested |
|---|---|---|---|---|---|---|
| Charge at W | `forcible_charge`, or FC16 47247/47083/47246/47100 | 0…37046, duration 1–1440 min | volatile | 19.7 s | expires | [x] |
| Discharge at W | `forcible_discharge`, or FC16 47249/47083/47246/47100 | 0…37048 | volatile | 11.3 s from charge | expires | [x] |
| Idle | `huawei_solar.stop_forcible_charge` | — | volatile | — | already released | [x] |
| Shutdown | same as idle, from `standby()` | — | — | — | — | [x] |
| Max/min SOC | `number.set_value` on the cutoff entities | 90–100 % / 0–20 % | **persistent** | — | skipped when out of range | [x] |

**The service validates power against the register maximum** and raises
`ValueError: Power cannot be more than 7000W`. Configuring a limit above the
BMS figure produces failing writes, not clamped ones.

**One "battery" is three kinds of hardware.** A LUNA2000 installation is an
inverter, a power module, and one to three battery packs, and each carries its
own serial and firmware in its own registers — 30015/30050 for the inverter,
37052/37814 for the power module, 38200+ per pack. Publishing any one of them as
*the* serial mislabels the other two, so the driver names each part. The device
registry entry stands for the storage, so it takes the power module's serial —
and its model from 47000 (`2` = LUNA2000) rather than from 30000, which is the
inverter's. Calling the device a SUN2000 would read as though the packs belonged
to the inverter. That enum is telemetry-only: it resolves to a label and is
dropped, so no entity carries a bare `2`.

The Modbus endpoint can be a fourth device again: on the reference installation
the address answers as `SmartHEMS` (an EMMA-A02, serial NS24A1211290) on slave 0,
with the inverter behind it on slave 4. That is the gateway, not the battery, and
it appears nowhere in the telemetry.

**The setup names the battery twice.** On the service path a Modbus address
identifies the inverter and a registry device identifies the battery, and
nothing forces those to be the same unit — Huawei inverters cascade, so a
two-inverter bus can readily have telemetry coming from one while the commands
land on the other. Register 30015 carries the inverter serial, which is also
what `huawei_solar` builds its device identifiers from, so the config flow
compares the two and refuses a pairing that contradicts itself. A serial that
is simply absent — older `huawei_solar` releases leave `serial_number` unset —
never blocks the setup; only a contradiction does.

**The cutoff entities live on two different devices.** `huawei_solar` puts the
charge cutoff on the inverter and the discharge cutoff on the battery, so
resolving against the configured battery device alone finds one and misses the
other. The driver searches the whole config entry.

**Watchdog.** Every command carries a duration (10 minutes as issued). If Home
Assistant dies without unloading, the inverter drops the command by itself and
returns to its own regulation. This is why a duration is sent rather than an
open-ended command.

## 6. Feature degradation matrix

| Feature | Status |
|---|---|
| PD charge/discharge | Supported, with the throttle of §13.2 |
| Multi-battery | Supported; capacity is native |
| Min/max SOC | Software-enforced; registers written as a backstop when representable |
| Predictive / pricing charge | Supported |
| Energy, cycles, efficiency | Native counters |
| 100 % taper, balance monitoring | **Disabled** — no cell voltages |
| Thermal limit | Supported (37022) |
| Backup exclusion | Derived, see §13.5 |
| MPPT / DC production | Reported, with the caveat of §13.6 |
| Alarms | Omitted — 37014 not validated |

## 7. Minimum acceptance tests

Covered by `tests/test_huawei_driver.py` (107 tests). Beyond the template's
list, these encode failures found on hardware:

- Capacity is published in kWh, not the register's Wh.
- A throttled set-point reports the standing command, not the request.
- A reversal skips the deadband but not the ramp.
- The dynamic discharge limit ignores the battery's own contribution.
- No read group may hold a single key.
- The inverter's AC total is not published as the battery's AC port.

## 13. What a DC-coupled hybrid breaks

This is the part worth reading. Every previous brand is an AC battery with its
own inverter, and several of the driver model's assumptions quietly depend on
that. Each item below was found by breaking a live installation.

### 13.1 Idle cannot mean "hold at zero"

`stop_forcible_charge` does not idle the battery — it returns control to the
inverter's working mode, which resumes self-consumption. The obvious
alternative, a forcible charge at 0 W, does hold it, and was the driver's first
implementation.

It was wrong twice over. A pinned battery cannot absorb its own PV, so the
inverter derates the strings instead — observed dropping from 4757 W to 55 W
within one 30 s sample and staying there for over half an hour of daylight. And
the control layer means something else by idle: manual mode idles once on
turn-on and then deliberately leaves the device alone, so switching a battery to
manual left 13.8 kWh frozen with nothing to clear the command.

**A zero set-point must release this battery.** That does hand it back to a
second regulator on the same meter, which is what the pin was meant to avoid.
Measured against a derated array and a frozen battery, it is the cheaper
side-effect.

### 13.2 The write path needs its own throttle

A set-point costs four serialised Modbus writes inside `huawei_solar`, and the
battery needs ~15 s to reach any target. A 2 s control loop that sees no
response yet keeps revising its request.

Without a throttle, the battery received a new forced command every 10–20 s
swinging between 0 and 4190 W, and the inverter answered by derating PV. A
throttle that lets direction changes bypass it is no throttle at all: the loop
flip-flops between a held zero and a discharge, so nearly every cycle counts as
a reversal. The rule that works is *a reversal skips the deadband but not the
ramp*.

The throttle must also report the **standing** command when it suppresses a
write. Reporting the request tells the control layer the battery was commanded
to a value it never received; it then measures the older power and flags a
battery that accepts commands without delivering.

### 13.3 The static power envelope is not reachable

Battery and PV share one inverter, so the discharge power actually available is
whatever the AC rating has left after PV. At 7 kW of PV on an 8.8 kW inverter
the battery can contribute 1.8 kW whatever its 7 kW BMS allows — and load
sharing, allocating proportionally to nameplate limits, hands it 74 % of the
deficit and starves the batteries that could have delivered.

`BatteryDriver.dynamic_discharge_limit_w(data)` was added for this, defaulting
to None so every AC brand keeps its static envelope. The subtraction must
exclude the battery's own contribution, or the limit chases its own output.

### 13.4 The status register knows no direction

Register 37000 reports Offline / Standby / Running / Fault / Sleep. The panel
prints that verbatim, so this brand sat on the word "Running" all day. Running
is refined by measured battery power into Charge / Discharge / Standby, using
the Marstek register map's wording so the header reads the same across brands.

The direction deadband is not cosmetic: this inverter idles around +50 W, so
comparing against zero labels a standing battery "Charge".

### 13.5 Backup output is not metered

There is no register for off-grid power. Register 32003 bit 0 says on-grid or
off-grid, and while the grid is disconnected the inverter feeds nothing but the
backup circuit — so its AC power *is* the backup output. On-grid the value is
zero, deliberately: printing the house supply in that tile would be wrong.

### 13.6 `ac_power` means something else here

The system aggregates derive household load as
`home = grid + sum(ac_power) + external_solar`, reading `ac_power` as the
battery's own AC port with DC-coupled PV already netted into it.

Register 32080 is the whole inverter's AC output, PV included. Publishing it
under that key counted the roof array twice — once inside `ac_power`, once in
the external solar sensor — and showed 8.42 kW of house load against 8.87 kW of
PV when the real figure was near 0.7 kW.

The inverter total is published as `inverter_ac_power` instead. With no
`ac_power` key the aggregates take their documented fallback, `-battery_power`,
which is what a battery with no AC port of its own actually contributes.

### 13.7 A read group must never hold one optional key

Not brand-specific, but it bites here. The coordinator counts a group that
returns nothing as a failed read, and a cycle in which every attempted group
fails marks the whole battery unavailable and stops the control loop writing to
it.

A block holding a single optional value therefore takes the battery offline on
every poll of that block. The tested inverter answers register 38210 with
padding, so the pack 1 firmware string decodes to nothing, and the battery
flapped in and out of the pool every three seconds all day. Groups are now one
per cadence rather than one per block.

## 11. Decision report

```text
Manufacturer/model: Huawei SUN2000-8K-MAP0 + LUNA2000 13.8 kWh
Firmware tested:    inverter V200R024C00SPC110, storage V200R025C00SPC103
Documentation:      Solar Inverter Modbus Interface Definitions v05

Verdict: SUITABLE WITH LIMITATIONS

Blocking items:
- Real SOC:        N — register 37004, verified
- Real power:      N — register 37001, sign already matches
- Adjustable charge/discharge: yes — 0..7000 W via huawei_solar services
- Safe idle:       yes, as a release rather than a hold (§13.1)
- Safe limits:     registers 37046/37048
- Freshness:       2-3 s at the register; failed blocks omit their keys

Omnibattery adaptations:
- Split transport: native Modbus for telemetry, services or direct registers for control
- Dynamic discharge limit from the inverter's AC headroom
- Per-string power derived from separate voltage and current registers
- Inverter status refined by measured direction

Disabled features:
- Cell voltages, balance monitoring, 100% voltage taper (per-pack data only)
- Alarms (37014 not validated)

Open risks:
- One installation, one firmware pair
- Direct Modbus writes verified on one installation only; off by default
- Two regulators share the meter whenever the battery is released

Pending hardware tests:
- A second installation, ideally with more than two strings
- Off-grid behaviour (the tested unit has its off-grid switch disabled)
- Whether per-string current is unpopulated below ~100 W on other models
```
