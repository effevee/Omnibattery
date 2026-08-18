# Carga predictiva

La carga predictiva es una función **opcional** que carga las baterías desde la red cuando el balance energético previsto para lo que queda del día es negativo.

## Lógica de decisión

```
Si (Batería utilizable + Previsión solar) < Consumo esperado:
    Cargar desde la red la diferencia exacta
Si no:
    No cargar (ahorro económico)
```

- **Batería utilizable**: energía actual por encima del SOC mínimo configurado.
- **Previsión solar**: preferiblemente la producción restante de hoy (sensor Solcast/Forecast.Solar). El sensor del día completo se mantiene como fallback legado durante la transición.
- **Consumo esperado**: media móvil de 7 días. Ver [Estimación del consumo diario](../../features/consumption-estimate.md).

---

## Objetivo de carga

Cuando se activa la carga predictiva, la batería no se carga hasta `max_soc` desde la red. En su lugar, la integración calcula un **SOC objetivo de red** — el mínimo necesario para cubrir únicamente lo que la solar no podrá aportar durante el día:

```
excedente_solar = max(0, previsión_solar − consumo_estimado)
carga_red       = max(0, hueco_hasta_max − excedente_solar)
soc_objetivo    = soc_actual + carga_red / capacidad × 100
```

`hueco_hasta_max` es la distancia en kWh desde el SOC actual hasta `max_soc`. La producción solar en exceso sobre el consumo del hogar carga la batería el resto del camino durante el día.

**Ejemplo**: la batería necesita 5 kWh para llegar a max_soc. La previsión solar es de 13 kWh y el consumo estimado es de 10 kWh — un excedente de 3 kWh disponible para la batería. La integración carga solo **2 kWh** desde la red; la solar gestiona los 3 kWh restantes durante el día.

### Margen de carga de red

El cálculo de la carga de red confía en la previsión solar. Cuando la previsión es optimista — o el tiempo resulta peor de lo previsto — la solar puede no aportar el excedente esperado y la batería termina el día por debajo de `max_soc`. El **Margen de Carga de Red Predictiva** (%) opcional cubre este riesgo aumentando la cantidad de red:

```
carga_red = max(0, hueco_hasta_max − excedente_solar) × (1 + margen%)
```

Siguiendo el ejemplo anterior, una necesidad de 2 kWh de red con un margen del **50 %** carga **3 kWh** desde la red en su lugar. El resultado se limita a `hueco_hasta_max`, por lo que el margen nunca puede cargar por encima de `max_soc`. El valor por defecto es `0 %` (desactivado); también se aplica a la reevaluación de la tarde en precio dinámico. Configúralo en el **asistente de configuración**, en el flujo de opciones, o con el slider `number.*_predictive_grid_charge_margin_pct` en la pestaña **Control** del panel.

### Sistemas multibatería

En sistemas con varias baterías a distintos niveles de SOC, la carga de red se distribuye **proporcionalmente al hueco individual de cada batería hasta max_soc**. Una batería más lejos del máximo recibe una mayor parte; una batería ya próxima al máximo se apoya principalmente en la solar. Esto evita sobrecargar una única unidad desde la red y minimiza la importación total.

---

## SOC mínimo garantizado

La carga predictiva solo carga desde la red cuando el día arroja un déficit. En un día soleado el balance del día completo puede ser positivo aunque la batería esté casi vacía al amanecer — dejando el hueco de la mañana (antes de que arranque la solar) cubierto desde la red a precio completo, o la batería agotada.

El slider **SOC Mínimo Garantizado** opcional (pestaña Control, `0` = desactivado) reserva energía suficiente para mantener cada batería en ese suelo hasta que comience la producción solar efectiva, sin importar el balance neto del día. Precio Dinámico elige los slots elegibles más baratos que pueden entregar la reserva antes de ese plazo. El techo máximo de precio explícito y los bloqueos físicos siguen siendo autoritativos: una garantía imposible se muestra como *shortfall* en vez de asignarse a una franja posterior.

Se reactiva con histéresis: una vez que el SOC recupera el suelo configurado, la carga se detiene si el suelo era la única razón para cargar; se rearma cuando el SOC baja a `suelo − 5 %`. Configúralo con el slider `number.*_predictive_min_soc_floor`, junto al switch **SOC Mínimo Garantizado**.

---

## Origen de la previsión de consumo

La estimación diaria se conserva como fallback de compatibilidad, pero las
instalaciones maduras usan el perfil local de 15 minutos descrito en
[Estimación diaria y horaria del consumo](../../features/consumption-estimate.es.md).
Precio Dinámico y sus reevaluaciones intradía solicitan únicamente el horizonte
local restante. Las franjas de carga predictiva no se restan de la demanda del hogar. Los atributos de la
decisión identifican el origen como `profile` o `legacy_daily`, junto con la
cobertura y el número de días aprendidos.

## Modos disponibles

| Modo | Descripción |
|---|---|
| [Franja Horaria](time-slot.md) | Carga durante una ventana fija (p. ej. tarifa nocturna) |
| [Precio Dinámico](dynamic-pricing.md) | Selecciona automáticamente las horas más baratas del día |
| [Precio en Tiempo Real](real-time-price.md) | Activa/desactiva la carga en función del precio actual |

![Selector de modo de carga predictiva](../../assets/screenshots/configuration/predictive-charging/mode-selector.png){ width="600"  style="display: block; margin: 0 auto;"}

---

## Notificaciones

La integración envía notificaciones de Home Assistant:

- **1 hora antes** del inicio del slot: análisis del balance energético y decisión de carga.
- **Al inicio del slot**: confirmación de que la carga ha comenzado.
- En modo Precio Dinámico, el plan también se comprueba **1 hora antes de cada franja futura**, una vez a **última hora de la tarde/noche** y después de una **caída de 30 puntos porcentuales de SOC**.

Usa el switch **Override Predictive Charging** para cancelar la carga predictiva en cualquier momento.

## Timeline solar y modo de despliegue

El total de la previsión y su forma temporal son contratos separados. El total
procede del sensor de previsión configurado; la telemetría FV directa solo se
usa para aprender cuándo suele llegar esa energía. La prioridad del timeline es:

1. Periodos fechados válidos proporcionados explícitamente por el proveedor.
2. Perfil local maduro aprendido de potencia FV directa y MPPT de baterías.
3. La curva sinusoidal de luz ya existente.
4. Timeline cero cuando no existe una ventana solar segura.

El ajuste `solar_profile_mode` tiene por defecto `shadow`. Este modo calcula
los candidatos y publica origen, madurez, cobertura y motivo de fallback, pero
mantiene la curva sinusoidal para el control. `active` aplica la prioridad
anterior y `off` detiene captura y comparación. Las entradas sin el ajuste
permanecen en `shadow`.

El perfil se normaliza para sumar uno antes de aplicar el presupuesto de la
previsión. No predice kWh, no corrige una previsión meteorológica errónea, no
controla el inversor ni reconstruye energía perdida por curtailment. El margen
de seguridad se resta una sola vez del presupuesto restante antes de darle forma.

Atributos útiles de la decisión son `solar_timeline_source`,
`solar_remaining_raw_kwh`, `solar_remaining_effective_kwh`,
`solar_timeline_fallback_reason`, `solar_profile_mature` y
`solar_profile_coverage_ratio`.

![Notificación de carga predictiva en HA](../../assets/screenshots/configuration/predictive-charging/notification-example.png){ width="500"  style="display: block; margin: 0 auto;"}
