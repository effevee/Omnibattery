# Predictive charging

Predictive charging is an **optional** feature that charges batteries from the grid when the expected energy balance for today is negative.

## Decision logic

```
If (Usable battery + Solar forecast) < Expected consumption:
    Charge from grid the exact deficit
Else:
    Do not charge (cost saving)
```

- **Usable battery**: energy currently stored above the configured min SOC.
- **Solar forecast**: preferably the production remaining today (Solcast/Forecast.Solar sensor). Whole-day sensors remain a legacy fallback during the transition.
- **Expected consumption**: 7-day rolling average. See [Daily consumption estimate](../../features/consumption-estimate.md).

---

## Charge target

When charging is triggered, the integration does not charge all the way to `max_soc` from the grid. Instead it calculates a **grid-only target SOC** — enough to cover only what solar will not be able to provide during the day:

```
solar_surplus = max(0, solar_forecast − estimated_consumption)
grid_charge   = max(0, gap_to_max − solar_surplus)
target_soc    = current_soc + grid_charge / capacity × 100
```

`gap_to_max` is the kWh distance from the current SOC to `max_soc`. Solar output in excess of household demand charges the battery the rest of the way during the day.

**Example**: the battery needs 5 kWh to reach max_soc. Solar forecast is 13 kWh, expected consumption is 10 kWh — a surplus of 3 kWh available for the battery. The integration charges only **2 kWh** from the grid; solar handles the remaining 3 kWh during the day.

### Grid charge margin

The grid-charge calculation trusts the solar forecast. When the forecast is optimistic — or the weather turns out worse than predicted — solar may not deliver the expected surplus and the battery ends the day below `max_soc`. The optional **Predictive Grid Charge Margin** (%) hedges this by topping up the grid amount:

```
grid_charge = max(0, gap_to_max − solar_surplus) × (1 + margin%)
```

Continuing the example above, a 2 kWh grid need with a **50 %** margin charges **3 kWh** from the grid instead. The result is capped at `gap_to_max`, so the margin can never charge past `max_soc`. Default is `0 %` (off); it also applies to the dynamic-pricing evening re-evaluation. Set it in the **setup wizard**, the options flow, or via the `number.*_predictive_grid_charge_margin_pct` slider on the dashboard **Control** tab.

### Multi-battery systems

In systems with multiple batteries at different SOC levels the grid charge is distributed **proportionally to each battery's individual gap to max_soc**. A battery further from full receives a larger share; a battery already close to full relies mostly on solar for its remainder. This prevents overcharging any single unit from the grid and minimises total grid import.

---

## Guaranteed minimum SOC floor

Predictive charging only grid-charges when the day nets to a deficit. On a sunny day the whole-day balance can be positive even though the battery is near empty at dawn — leaving the morning gap (before solar ramps up) covered from the grid at full price, or the battery drained.

The optional **Guaranteed Minimum SOC** slider (Control tab, `0` = off) forces an overnight grid charge to reach **at least** that SOC by the end of the charging window, regardless of the day's net balance. It sizes the deficit to the floor, so it flows through the per-battery target SOC and the dynamic-pricing slot sizing unchanged — the cheapest slots are still chosen to reach it.

It re-triggers with hysteresis: once SOC recovers to the configured floor, charging stops if the floor was the only reason to charge; it re-arms when SOC drops to `floor − 5 %`. Set it via the `number.*_predictive_min_soc_floor` slider, paired with the **Guaranteed Minimum SOC** switch.

---

## Consumption forecast source

The daily estimate is retained as a compatibility fallback, but mature
installations use the local 15-minute profile described in [Daily and hourly
consumption estimate](../../features/consumption-estimate.md). Dynamic Pricing
and its intraday re-evaluations request only the remaining local-time horizon.
Predictive charging windows are not subtracted from household demand. The decision attributes identify the
source as `profile` or `legacy_daily`, together with profile coverage and the
number of learned days.

## Available modes

| Mode | Description |
|---|---|
| [Time Slot](time-slot.md) | Charges during a fixed window (e.g. overnight off-peak tariff) |
| [Dynamic Pricing](dynamic-pricing.md) | Automatically selects the cheapest hours of the day |
| [Real-Time Price](real-time-price.md) | Activates/deactivates charging based on the current price |

![Predictive charging mode selector](../../assets/screenshots/configuration/predictive-charging/mode-selector.png){ width="600"  style="display: block; margin: 0 auto;"}

---

## Notifications

The integration sends Home Assistant notifications:

- **1 hour before** the slot starts: energy balance analysis and charging decision.
- **When the slot starts**: confirmation that charging has begun.
- In Dynamic Pricing mode, the plan is also checked **1 hour before each future slot**, once in the **late afternoon/evening**, and after a **30 percentage-point SOC drop**.

Use the **Override Predictive Charging** switch to cancel predictive charging at any time.

![Predictive charging notification](../../assets/screenshots/configuration/predictive-charging/notification-example.png){ width="500"  style="display: block; margin: 0 auto;"}
