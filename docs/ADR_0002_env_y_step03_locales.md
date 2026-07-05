# ADR 0002 - Configuracion Y Step03 Locales En AbiesZipZap

## Estado

Aceptado.

## Contexto

AbiesZipZap debe evolucionar hacia un flujo completo: descargar ZIPs desde AbiesWeb y crear bibliotecas en Abies+ con esos ZIPs.

El proyecto padre contiene el script original `step03_abiesplus/pullup2Ab+.py` y un `.env` con credenciales y parametros Abies+.

La regla operativa es no tocar el proyecto padre.

## Decision

- Se copia `step03_abiesplus/pullup2Ab+.py` a `v2/pullup2Ab+.py`.
- Se copia `.env` a `v2/.env`.
- Se añade `v2/.gitignore` para excluir `v2/.env` y artefactos runtime.
- `v2/docker-compose.yml` monta `./.env` como `/app/.env`.

## Consecuencias

- AbiesZipZap puede evolucionar de forma independiente.
- El script original del proyecto padre queda intacto.
- Las credenciales quedan duplicadas temporalmente en `v2/.env`.
- En una fase futura se podran mover parametros no secretos a SQLite y dejar en `.env` solo credenciales.

## Alternativas Consideradas

### Seguir Montando `../.env`

Rechazado por la regla de independencia de V2 respecto al proyecto padre.

### Mover Todo A SQLite Ya

Rechazado por ahora. Conviene copiar primero el flujo operativo y luego separar secretos/configuracion con menor riesgo.
