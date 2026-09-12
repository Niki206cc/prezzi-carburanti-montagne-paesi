# Prezzi carburanti — Montagne & Paesi

Applicazione Docker che scarica ogni giorno gli open data ufficiali MIMIT, seleziona i distributori più economici nelle province di Bergamo e Brescia e pubblica un articolo WordPress con collegamenti a Google Maps.

## Funzioni

- pannello responsive sulla porta `8087`;
- anteprima reale dell'articolo;
- titolo dinamico con i prezzi minimi di benzina e diesel;
- sezioni grafiche separate per le province di Bergamo e Brescia;
- orario e ID categoria modificabili dal pannello senza redeploy;
- classifiche per Bergamo e Brescia;
- benzina e diesel self-service, GPL e metano;
- numero di risultati configurabile;
- esclusione automatica dei prezzi troppo vecchi;
- Google Maps tramite coordinate MIMIT, senza API key;
- stato bozza/pubblicato configurabile;
- pubblicazione manuale e automatica con blocco duplicati;
- immagine base facoltativa con data e prezzo minimo automatici;
- volume persistente, log e healthcheck.

## Portainer

1. **Stacks → Add stack → Repository**.
2. URL: `https://github.com/Niki206cc/prezzi-carburanti-montagne-paesi`.
3. Reference: `refs/heads/main`.
4. Compose path: `docker-compose.yml`.
5. Carica `.env.example` con **Load variables from .env file**, dopo aver inserito utente e password applicazione WordPress.
6. Deploy e apri `http://IP-DEL-RASPBERRY:8087`.

Per il primo test lascia `WP_POST_STATUS=draft` e l'automazione disattivata. Dal pannello apri l'anteprima, scegli ora e ID categoria, quindi usa **Pubblica ora**.

## Fonte

Dati del Ministero delle Imprese e del Made in Italy, licenza IODL 2.0. I file vengono letti direttamente dagli endpoint ufficiali e non sono inclusi nel repository.

## Versione

1.1.0
