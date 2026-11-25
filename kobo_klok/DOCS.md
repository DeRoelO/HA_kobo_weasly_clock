# Kobo Familieklok — Documentatie

Deze Home Assistant App rendert een moderne vierkante familieklok (`1072×1448` kleuren-PNG) op basis van je live `person.*` of `sensor.*` entiteiten, inclusief profielfoto's op de wijzers.

## Configuratie

- `refresh_seconds`: Verversingsinterval in seconden voor de Kobo-webpagina (standaard `300` = 5 minuten).
- `eink_boost`: Subtiele kleur- en contrastversterking voor Kaleido 3 kleuren e-ink schermen (standaard `true`).
- `screen_width` / `screen_height`: Schermresolutie (standaard `1072×1448` voor Kobo Clara Colour).
- `persons`: Lijst met gezinsleden (`name`, `entity`, `person_entity` voor profielfoto, `hand`: `iron` of `brass`).
- `sections`: Lijst met vakken op de klok (`name`, `key`: icoonnaam, `states`: komma-gescheiden lijst met HA-states die naar dit vak wijzen).

## Endpoints

- `http://<homeassistant-ip>:8099/` — Ultralichte Kobo-webpagina met automatische refresh (tikken ververst direct).
- `http://<homeassistant-ip>:8099/klok.png` — Directe PNG-afbeelding (ook geschikt voor ESPHome `online_image` of TRMNL).
- `http://<homeassistant-ip>:8099/health` — Healthcheck.

## Kobo Clara Colour via NickelMenu

1. Installeer [NickelMenu](https://pgaskin.net/NickelMenu/) (`KoboRoot.tgz` in `.kobo/` zetten en uitwerpen).
2. Zet een bestand `.adds/nm/familieklok` (zonder extensie, Unix LF) op je Kobo:
   ```text
   menu_item :main :Familieklok :nickel_browser :modal:http://homeassistant.local:8099/
   menu_item :main :WiFi aan (klok) :nickel_setting :toggle :force_wifi
     chain_success :nickel_wifi :autoconnect
   ```
3. Open op je Kobo **NickelMenu → Familieklok**.
