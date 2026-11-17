# PDF Compression API

API asincrona per recuperare un PDF da uno storage HTTP(S) autorizzato, comprimerlo con Ghostscript e restituire il risultato.

## Avvio

```bash
cp .env.example .env
# modifica .env: host dello storage, IP client e API_KEY
docker compose up --build
```

## Flusso

1. `POST /v1/compressions` crea un job e restituisce `id`.
2. `GET /v1/compressions/{id}` mostra `queued`, `downloading`, `compressing`, `completed` o `failed`.
3. Quando è `completed`, `GET /v1/compressions/{id}/download` scarica il PDF.

Esempio:

```bash
curl -X POST http://localhost:8000/v1/compressions \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: change-me' \
  -d '{"source_url":"http://host.docker.internal:8080/files/document.pdf","compression":"balanced"}'
```

Profili disponibili: `screen` (massima riduzione), `ebook`/`balanced`, `printer`, `prepress` (qualità maggiore).

## Note di sicurezza

- `ALLOWED_STORAGE_HOSTS` è obbligatorio: l'API non accetta URL arbitrari.
- Anche le porte sono allowlistate; il controllo riduce il rischio SSRF verso servizi interni.
- L'autenticazione può usare IP/CIDR e `X-API-Key`.
- Sono disabilitati i redirect automatici impliciti del client standard e i file hanno un limite dimensionale.
- Per produzione sostituire il dizionario in memoria con Redis/PostgreSQL e `BackgroundTasks` con una coda (Celery/RQ/Arq).
