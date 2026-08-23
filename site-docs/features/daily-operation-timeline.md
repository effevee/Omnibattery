# Daily operation timeline

The Overview tab includes a 24-hour operation timeline with 96 fixed
quarter-hour cells. Cells describe battery actions; the overlaid curves show
solar and household energy in `kWh/15 min`.

## Reading the card

- Closed intervals are observed data. The current interval contains the real
  accumulated value and a projected remainder until the quarter closes.
- Future intervals are an informational projection from the active consumption
  and solar profiles, the current battery state and the already selected
  Dynamic Pricing or Time Slot plan.
- Real-Time Price intentionally has no future calendar. It records only its
  actual activations in the past and current interval.
- Solid curves are measured; dashed curves are forecast. Solar and consumption
  use the left `kWh/15 min` axis; total SOC uses the right `0–100%` axis. The
  tooltip shows the interval's observed or projected SOC and names the learned
  solar shape or active sinusoidal fallback.

Action colors describe flows, not permissions: green means solar energy that
has charged the battery or is allocated to it by the future plan, purple means
a grid-charge decision, blue means observed or projected battery discharge,
and grey means an explicit `grid_charge_not_needed` decision. A soft yellow
fill indicates a window with available solar surplus in which the battery could
charge, without presenting that opportunity as an effective charge. In the
current quarter, a projected solar charge remains yellow until energy entering
the battery is observed. A cell can contain up to three actions; diagonal
patterns and the accessible text preserve the distinction in light and dark
themes. Actions are combined visually only when they were simultaneous; if the
battery changed direction within the quarter-hour, the action observed for the
longest time is shown. The current interval never presents an action projected
for its remaining minutes as observed. “Charging to setpoint” is context, not
another color. Charge Delay uses
a clock marker and an estimated unlock time. A charging interval's tooltip also
shows the energy that actually entered the battery or, for a future interval,
the total energy projected from solar and grid.

## Entity contract

The diagnostic entity is
`sensor.omnibattery_daily_operation_timeline`. Its state is the local snapshot
date. Attributes include `schema_version`, timezone, freshness, profile
sources, 96-value energy series, observed and projected total SOC, operation masks, grid decisions and delay
metadata. Arrays are bounded to the current local day and are excluded from
Recorder; the entity is safe to use from a dashboard without causing control
reevaluations.

The backend keeps completed quarter-hours immutable. A plan reevaluation may
replace only the open current interval and the future. Store restoration is
accepted only for the same local date and temporal fingerprint; corrupt data
degrades to an empty timeline and never blocks battery control.

## Mobile and missing data

On narrow screens the 96 cells keep a readable minimum width and scroll
horizontally by hour. Keyboard arrows, touch and mouse tooltips expose the same
interval details. Missing telemetry remains `null`; it is not silently turned
into zero. When a forecast is unavailable or stale, the card keeps the observed
past and marks only unjustified future values as unavailable.

See [consumption estimate](consumption-estimate.md), [solar charge delay](solar-charge-delay.md),
[Dynamic Pricing](../configuration/predictive-charging/dynamic-pricing.md),
[Time Slot](../configuration/predictive-charging/time-slot.md) and
[Real-Time Price](../configuration/predictive-charging/real-time-price.md) for
the sources used by the projection.
