# Konzept: Merkliste mit automatischem Download („Wanted“)

Status: Konzept, noch kein Code · Branch `docs/wanted-list-concept` (Basis `main` @ 9321096)

## 1. Ziel

Nutzer merken sich ein Buch aus der Metadaten-Suche vor. Shelfmark sucht es danach selbstständig
in allen erlaubten Quellen, lädt das beste passende Release herunter und probiert bei einem
Fehlschlag das nächste Release bzw. die nächste Quelle. Ist gerade nichts verfügbar, wird
in wachsenden Abständen erneut gesucht, bis das Buch da ist oder der Nutzer es entfernt.

Vorbild ist Readarrs „Wanted / Missing“ mit „Automatic Search“ und „Blocklist“. Bewusst **nicht**
Teil dieses Konzepts: Autoren- oder Serien-Monitoring, RSS-Feeds, Qualitäts-Upgrades
(ein schon geladenes Buch später durch ein besseres Format ersetzen).

## 2. Ergebnis der Machbarkeitsprüfung

**Machbar, ohne bestehende Abläufe umzubauen.** Die harten Teile gibt es schon:

| Baustein | Vorhanden | Fundstelle |
|---|---|---|
| Buch eindeutig identifizieren und später neu laden | ja: `(provider, provider_id)` + `provider.get_book(id)` | `metadata_providers/__init__.py:186`, `:366`; `/api/releases` macht das schon (`main.py:3186`) |
| Eine Quelle ohne HTTP-Route durchsuchen | ja: `search_source_releases()`, laut Docstring genau für Hintergrund-Automatisierung herausgelöst | `core/release_search.py:28` |
| Suchbudget / Abbruch | ja: `search_deadline` (ContextVar, muss im Job-Thread selbst betreten werden) | `core/search_deadline.py:95` |
| Release einreihen | ja: `orchestrator.queue_release()` | `download/orchestrator.py:236` |
| Rückmeldung, wenn ein Download endet | ja: Terminal-Status-Hook (ein Slot, muss verkettet werden) | `main.py:1744`, `main.py:1575` |
| Vorbild „Task gehört zu X“ | ja: `_request_id` → `DownloadTask.request_id` | `orchestrator.py:251`, `core/models.py:162` |
| Prüfen, ob das Buch schon in der Bibliothek ist | ja, aber nur Calibre und nur E-Books | `core/library_index.py:143` |
| Rechte pro Nutzer/Quelle | ja: `resolve_policy_mode()` | `core/request_policy.py:331` |
| Benachrichtigungen, WebSocket-Events | ja | `core/notifications.py:619`, `core/request_helpers.py:50` |
| DB-Tabellen + Migrationen | ja, Muster `CREATE TABLE IF NOT EXISTS` + `_migrate_*` | `core/user_db.py:27`, `:202` |
| Einstellungs-Tab | ja: `@register_settings` | `core/settings_registry.py:281` |

**Was fehlt und neu gebaut werden muss:**

1. **Automatische Release-Auswahl.** Es gibt keine quellenübergreifende „bestes Release“-Logik.
   Die Sortierung, die Nutzer im Release-Dialog sehen, passiert im Frontend
   (`src/frontend/src/utils/releaseScoring.ts`, Filter in `ReleaseModal.tsx:1066`) und muss
   ins Backend portiert werden.
2. **Ein Hintergrund-Job.** Shelfmark hat bisher keinen allgemeinen Scheduler. Muster für die
   Schleife ist der Download-Koordinator (`orchestrator.py:1126`), Muster für „dem Nutzer den
   Vortritt lassen“ ist der Such-Warm-up (`download/warmup.py:41`).
3. **Datenmodell, API, Oberfläche** für die Merkliste.

**Wichtig vorab – Projekt-Scope:** Das Upstream-README (`readme.md:255–265`) nennt
„Monitor authors, series, or new releases – there is no background automation“ und
„Queue future downloads“ ausdrücklich als **Nicht-Ziele**. Eine Übernahme ins Original-Projekt
ist damit unwahrscheinlich. Für diesen Fork heißt das: Funktion als eigenständiges,
standardmäßig ausgeschaltetes Modul bauen und die Berührungspunkte mit bestehendem Code klein
halten, damit spätere Merges von Upstream günstig bleiben (siehe Abschnitt 10).

## 3. Ablauf

```
Nutzer merkt Buch vor
        │
        ▼
 ┌─────────────┐  fällig (next_search_at)  ┌─────────────┐
 │   wartend   │ ─────────────────────────▶│   suchend   │
 └─────────────┘                           └──────┬──────┘
        ▲                                         │ alle erlaubten Quellen durchsuchen,
        │ keine (weiteren) Kandidaten:            │ Kandidaten filtern + ranken,
        │ nächste Suche mit Backoff               │ gesperrte Releases auslassen
        │                                         ▼
        │                                  ┌─────────────┐
        └──────────────────────────────────│ ladend      │◀──┐
                                           └──────┬──────┘   │ Fehler: Release sperren,
                                                  │          │ nächster Kandidat
                                      COMPLETE    │ ERROR ───┘
                                                  ▼
                                           ┌─────────────┐
                                           │  erledigt   │
                                           └─────────────┘
```

Im Detail:

1. **Vormerken.** Der Eintrag wird mit `next_search_at = jetzt` angelegt, die erste Suche
   startet also sofort (wie Readarrs „Search on add“).
2. **Bibliotheks-Check.** Vor jeder Suche: Ist das Buch laut `library_index.is_in_library()`
   schon vorhanden, wird der Eintrag als *erledigt (bereits in der Bibliothek)* abgeschlossen.
3. **Suchen.** Nacheinander alle Quellen, die (a) für den Inhaltstyp passen
   (`supported_content_types`), (b) in den Merklisten-Einstellungen freigegeben sind und
   (c) für den Nutzer im Modus *Download* stehen. Ein gemeinsames Suchbudget für den ganzen
   Durchgang, wie `/api/releases` es macht (`main.py:3229`).
4. **Kandidaten bilden.** Alle Treffer aller Quellen in eine Liste, filtern und ranken
   (Abschnitt 5). Releases, die für diesen Eintrag schon gescheitert sind, fliegen raus
   (Sperrliste, Abschnitt 4).
5. **Einreihen.** Der beste Kandidat wird mit `queue_release()` eingereiht, verknüpft über
   ein neues `_wanted_id` (analog zu `_request_id`). Es läuft immer nur **ein** Download pro
   Eintrag.
6. **Ergebnis abwarten.** Der verkettete Terminal-Hook meldet:
   - `COMPLETE` → Eintrag *erledigt*, Benachrichtigung „Vorgemerktes Buch geladen“.
   - `ERROR` → Release auf die Sperrliste, sofort der nächste Kandidat aus Schritt 4.
     Ist die Kandidatenliste älter als ca. 30 Minuten, wird stattdessen neu gesucht
     (Prowlarr-Links verfallen nach einer Stunde, siehe Abschnitt 6).
   - `CANCELLED` durch den Nutzer → Eintrag pausieren, nicht weiterprobieren.
7. **Nichts (mehr) gefunden.** Eintrag geht zurück auf *wartend*, `next_search_at` nach
   Backoff-Stufe, z. B. 1 h → 6 h → 24 h → danach täglich. Optional gibt der Job nach einer
   einstellbaren Zahl von Tagen auf (*aufgegeben*), Standard: nie.

Damit ist die Frage „alle Anbieter nacheinander, bis das Buch da ist“ beantwortet: Pro
Suchdurchgang werden **alle** erlaubten Quellen abgefragt und ihre Releases in einer
gemeinsamen Rangliste der Reihe nach probiert. Erst wenn die Liste leer ist, wartet der
Eintrag auf den nächsten Durchgang.

## 4. Datenmodell

Zwei neue Tabellen in `users.db`, angelegt in `_CREATE_TABLES_SQL` (`user_db.py:27`).

**`wanted_items`** – ein Eintrag pro vorgemerktem Buch und Nutzer

| Spalte | Zweck |
|---|---|
| `id`, `user_id` (FK `users`, `ON DELETE CASCADE`) | |
| `provider`, `provider_id`, `content_type` | Identität; `UNIQUE(user_id, provider, provider_id, content_type)` |
| `book_data` (JSON) | Anzeige + Rückfall, falls der Metadaten-Anbieter später ausfällt: Titel, Autoren-Liste, ISBN-10/13, Jahr, Cover, Serie, Sprache. Mehr als beim Request (`requestPayload.ts:29` schickt keine ISBNs) |
| `languages` (JSON, optional) | eigene Sprachwahl, sonst Nutzer-Standard `BOOK_LANGUAGE` |
| `state` | `waiting`, `searching`, `downloading`, `obtained`, `paused`, `given_up` |
| `current_task_id` | laufender Download |
| `search_count`, `last_search_at`, `next_search_at` | Backoff-Steuerung |
| `last_result` | z. B. „3 Quellen durchsucht, 0 passende Releases“ |
| `obtained_at`, `obtained_reason` | `downloaded` / `in_library` |
| `created_at`, `updated_at` | |

**`wanted_attempts`** – Verlauf und Sperrliste

| Spalte | Zweck |
|---|---|
| `id`, `wanted_id` (FK, `ON DELETE CASCADE`) | |
| `source`, `source_id` (= Task-ID) | Release-Identität, Basis der Sperrliste |
| `release_title`, `format`, `size`, `indexer` | Anzeige |
| `status` | `queued`, `complete`, `error`, `cancelled` |
| `error_message`, `created_at`, `finished_at` | |

Ein Release gilt für einen Eintrag als gesperrt, wenn es dort mit `error` endete. Gespeichert
wird nur die Identität, **nicht** die Download-URL: Prowlarr-Links verfallen, später wird immer
neu gesucht. Die Sperrliste lässt sich pro Eintrag leeren.

`download_history` bleibt unverändert die globale Historie. Dort kommt nur eine neue Herkunft
`wanted` dazu (`VALID_ORIGINS`, `download_history_service.py:25`).

## 5. Automatische Release-Auswahl

Neues Modul, z. B. `shelfmark/core/release_picker.py`, rein funktional und gut testbar.

**Filter** (wie das Frontend heute):
- Format in `SUPPORTED_FORMATS` bzw. `SUPPORTED_AUDIOBOOK_FORMATS`. Anders als im Frontend:
  Releases **ohne** Format-Angabe nur nehmen, wenn sonst nichts übrig ist.
- Sprache passt zum Filter (`releaseLanguageMatchesFilter`; Releases ohne Sprache zählen als passend).
- Titel muss passen: Mindest-Score, damit kein falsches Buch geladen wird. Bausteine:
  `core/text_match.py` (`title_tokens_match`, `significant_tokens`, `isbn_variants`).
- Autor darf nicht widersprechen: `author_affinity() != AUTHOR_MISMATCH` (`core/author_match.py:40`).
- Nicht auf der Sperrliste.

**Ranking** (absteigend wichtig):
1. ISBN-Treffer (wenn die Quelle ISBNs liefert).
2. Titel-Score aus `releaseScoring.ts` (exakt 10000, Präfix 6000, Teilstring 3000,
   Token-Überlappung bis 2500) plus Autor-Treffer (+1500). Ins Backend portieren und mit
   denselben Testfällen absichern wie das Frontend.
3. Bevorzugte Format-Reihenfolge – **neue Einstellung** (`OrderableListField`), z. B.
   epub > azw3 > mobi > pdf. Heute gibt es so eine Reihenfolge nur hart codiert für IRC
   (`EBOOK_FORMAT_PRIORITY`).
4. Quellen-Reihenfolge laut Merklisten-Einstellung (z. B. AA vor Libgen vor Prowlarr).
5. Torrents: Seeder > 0 Pflicht, mehr ist besser.
6. Plausible Größe (E-Book nicht über z. B. 200 MB, Hörbuch nicht unter 5 MB).

Der Picker liefert die ganze geordnete Liste; der Job arbeitet sie der Reihe nach ab.

## 6. Hintergrund-Job und Lastschutz

Ein einzelner Thread `WantedScheduler`, gestartet in `main.py` neben `backend.start()`
(`main.py:265`). Shelfmark läuft mit genau einem gunicorn-gevent-Worker (`entrypoint.sh:499`),
ein Prozess-interner Thread reicht also; mehrere Worker würden doppelt suchen.

Schleife (Takt ca. 60 s, nie sterbend wie der Koordinator):
1. Ist die Funktion aus (`WANTED_ENABLED`) → schlafen.
2. Läuft gerade eine Nutzersuche oder hat in den letzten Minuten eine stattgefunden → warten.
   Grund: Der Cloudflare/DDoS-Guard-Bypasser arbeitet immer nur einen Browser-Durchlauf zur
   Zeit ab (`internal_bypasser.py:1102`), eine Hintergrundsuche würde die Suche blockieren,
   auf die ein Nutzer gerade wartet. Signal: `warmup.note_user_search()` um einen Zeitstempel
   erweitern.
3. Höchstens **ein** fälliger Eintrag pro Takt, ältestes `next_search_at` zuerst, und eine
   Obergrenze an Suchen pro Stunde (Einstellung).
4. Quellen überspringen, deren Host gerade in der 429-Sperre steckt
   (`network.host_cooldown_remaining`).

Quellen-spezifische Stolpersteine:

| Quelle | Problem | Umgang |
|---|---|---|
| Anna's Archive / Direct | Jede Suche kann einen Bypass-Durchlauf von 60–120 s kosten | strenge Taktung, Nutzer hat Vorrang |
| Prowlarr / Newznab | Release-Links verfallen nach 1 h (In-Memory-Cache `prowlarr/cache.py:15`) | Suchen und Einreihen im selben Durchgang; alte Kandidatenlisten verwerfen |
| Prowlarr | Ohne GUID ist die Task-ID nicht stabil über Neustarts (`prowlarr/utils.py:44`, salziges `hash()`) | Sperrliste zusätzlich über Titel + Indexer + Größe abgleichen |
| IRC | Suchergebnisse werden 30 Tage gecacht; höchstens 3 identische Anfragen pro 24 h | standardmäßig **nicht** für die Merkliste freigeben |
| Metadaten-Anbieter | OpenLibrary/Moly haben Rate-Limiter | `get_book` nur einmal pro Durchgang, Ergebnis wird ohnehin gecacht |

**Neustart:** Die Download-Queue lebt nur im Speicher. `startup_reconcile.py` setzt Einträge
in `searching`/`downloading` auf `waiting` mit kurzer Verzögerung zurück, analog zu den
Requests (`REOPEN_WINDOW`).

## 7. Rechte und Mehrbenutzer

- Vormerken ist nur sinnvoll für Quellen, bei denen der Nutzer im Modus *Download* ist. Der Job
  braucht dafür eine sitzungsfreie Variante der Prüfung aus `main.py:451–489`
  (Nutzer und Admin-Status aus der DB statt aus der Session).
- Hat ein Nutzer für **keine** Quelle Download-Rechte, ist der Button ausgeblendet. Ob der Job
  stattdessen einen Request anlegen soll, ist eine offene Entscheidung (Abschnitt 11).
- Admins sehen die Einträge aller Nutzer, Nutzer nur ihre eigenen.
- Downloads laufen unter der Identität des Nutzers (`queue_release(user_id=…, username=…)`),
  damit Ausgabeziel, E-Mail-Empfänger und Benachrichtigungen pro Nutzer stimmen.

## 8. API, Einstellungen, Benachrichtigungen

**Endpunkte** (neues Blueprint-Modul, z. B. `core/wanted_routes.py`):

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/api/wanted` | Liste (eigene; Admin optional alle) |
| `POST` | `/api/wanted` | vormerken (`book_data`, `content_type`, optional `languages`) |
| `DELETE` | `/api/wanted/<id>` | entfernen |
| `POST` | `/api/wanted/<id>/search` | jetzt suchen (setzt `next_search_at = jetzt`) |
| `POST` | `/api/wanted/<id>/pause` · `/resume` | pausieren / fortsetzen |
| `DELETE` | `/api/wanted/<id>/blocklist` | Sperrliste leeren |
| `GET` | `/api/wanted/<id>/attempts` | Versuchsverlauf |

Zusätzlich liefert die Metadaten-Suche pro Buch ein Feld `wanted` (analog zu `library`), damit
Suchergebnisse den Merk-Zustand anzeigen können.

**Einstellungs-Tab „Wanted“** (`@register_settings`, gespeichert in `plugins/wanted.json`):

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `WANTED_ENABLED` | `false` | Hauptschalter |
| `WANTED_SOURCES` | AA, Libgen, Prowlarr, Newznab | erlaubte Quellen in Reihenfolge (`OrderableListField`) |
| `WANTED_PREFERRED_FORMATS` | epub, azw3, mobi, … | Format-Reihenfolge für die Auswahl |
| `WANTED_SEARCH_BACKOFF_HOURS` | `1, 6, 24` | Abstände zwischen Suchen, danach letzter Wert |
| `WANTED_MAX_SEARCHES_PER_HOUR` | `6` | Lastschutz |
| `WANTED_GIVE_UP_AFTER_DAYS` | `0` (nie) | danach Zustand *aufgegeben* |

**Benachrichtigungen:** neue Events `WANTED_OBTAINED` und optional `WANTED_GAVE_UP`. Dafür
werden das Enum, `_resolve_notify_type`, `_render_message` (`notifications.py:66`, `:321`, `:347`),
`_ADMIN_EVENT_OPTIONS` (`config/notifications_settings.py:24`) und die feste Liste im Frontend
(`UserNotificationOverridesSection.tsx`) erweitert. Einzelne gescheiterte Versuche lösen keine
Benachrichtigung aus: Das normale `DOWNLOAD_FAILED` wird für Merklisten-Downloads unterdrückt,
weil der Job ja direkt das nächste Release probiert.

**WebSocket:** Event `wanted_update` an `user_{id}` und `admins` über `emit_ws_event`; das
Frontend lädt daraufhin die Liste neu (gleiches Muster wie `request_update`).

## 9. Oberfläche

Das Frontend hat keine Seiten-Routen, nur eine Suchseite mit Modals und der Aktivitäts-Seitenleiste.

- **Merk-Button** (Lesezeichen-Symbol) dort, wo heute schon der Hardcover-Listen-Dropdown
  `BookTargetDropdown` sitzt: Karten- (`CardView.tsx:147`), Kompakt- (`CompactView.tsx:149`)
  und Listenansicht (`ListView.tsx:305`), Detail-Dialog (`DetailsModal.tsx:405`) und Kopf des
  Release-Dialogs (`ReleaseModal.tsx:1749`). Zustand an/aus aus dem neuen `wanted`-Feld.
  Achtung: `BookTargetDropdown` nutzt bereits ein Lesezeichen-Symbol – für die Merkliste ein
  anderes wählen (z. B. Auge oder Glocke), sonst sind beide verwechselbar.
- **Neuer Tab „Wanted“ in der Aktivitäts-Seitenleiste** (`ActivitySidebar.tsx:68`, neuer
  `ActivityTabKey` und `ActivityKind`). Pro Eintrag: Cover, Titel, Zustand, letzte und nächste
  Suche, Zahl der Versuche; Aktionen *Jetzt suchen*, *Pausieren*, *Entfernen*, *Sperrliste
  leeren*. Eine eigene Seite unter `/wanted` wäre übersichtlicher, wäre aber die erste echte
  Route der App und deutlich mehr Umbau – für den Anfang reicht der Tab.
- Daten über einen neuen Hook `useWanted`, der auf `wanted_update` hört; Fallback Polling.
- Tests nach dem bestehenden Muster: Logik in `utils/*.ts`, Vitest-Unit-Tests ohne Rendering.

## 10. Berührungspunkte mit bestehendem Code

So wenig wie möglich, damit Upstream-Merges günstig bleiben. Neu und eigenständig:
`core/wanted_service.py`, `core/wanted_routes.py`, `core/release_picker.py`,
`download/wanted_scheduler.py`, `config/wanted_settings.py`, Frontend-Hook, -Tab und -Button.

Geänderte bestehende Stellen:

| Datei | Änderung |
|---|---|
| `core/user_db.py` | zwei Tabellen in `_CREATE_TABLES_SQL` |
| `core/models.py` | `DownloadTask.wanted_id` |
| `download/orchestrator.py` | `_wanted_id` in `queue_release` übernehmen, `wanted_id` in `_task_to_dict` |
| `main.py` | Terminal-Hook verketten (`_record_download_terminal_snapshot`), Scheduler starten, Blueprint registrieren, `wanted` in Metadaten-Antwort |
| `core/download_history_service.py` | Herkunft `wanted` |
| `core/startup_reconcile.py` | hängende Einträge zurücksetzen |
| `core/notifications.py` (+ Settings, Frontend-Liste) | zwei Events |
| `download/warmup.py` | Zeitstempel der letzten Nutzersuche |
| Frontend: Ergebnis-Ansichten, `DetailsModal`, `ReleaseModal`, `ActivitySidebar`, `types` | Button und Tab |

**Zusammenspiel mit dem Auto-Retry** (Branch `feat/auto-retry-failed-downloads`): Beide greifen
am Fehlerpfad an. Der Auto-Retry wiederholt **dasselbe** Release mit Wartezeit, die Merkliste
wechselt zum **nächsten** Release. Hängen beide an einem Task, verzögert der Auto-Retry das
Weiterschalten um bis zu `AUTO_RETRY_MAX_ATTEMPTS` Wartezeiten. Empfehlung: Merklisten-Tasks
vom Auto-Retry ausnehmen (`_is_auto_retryable` prüft zusätzlich `task.wanted_id is None`) –
die Merkliste hat ihre eigene, bessere Strategie.

## 11. Offene Entscheidungen

1. **Nutzer ohne Download-Recht:** Button ausblenden (Vorschlag) oder vom Job automatisch einen
   Request anlegen lassen, sobald ein Release gefunden wurde?
2. **Hörbücher:** mit aufnehmen? Es gibt keinen Bibliotheks-Check für Hörbücher
   (nur Calibre, nur E-Books), Torrents können Stunden laufen.
3. **Aufgeben:** nie (Vorschlag) oder nach X Tagen?
4. **Admin-Übersicht:** reicht es, wenn Admins im Tab alle Einträge sehen, oder braucht es
   eine eigene Verwaltungsansicht?
5. **Mehrere Nutzer, gleiches Buch:** getrennte Einträge (Vorschlag, wegen pro Nutzer
   verschiedener Ziele) oder ein gemeinsamer Download?

## 12. Umsetzung in Phasen

| Phase | Inhalt | Ergebnis |
|---|---|---|
| 1 | Datenmodell, `WantedService`, API, Release-Picker mit Tests | Merkliste per API pflegbar, Auswahl testbar |
| 2 | Scheduler, Hook-Verkettung, Sperrliste, Neustart-Abgleich, Lastschutz | Automatik läuft, noch ohne UI |
| 3 | Einstellungs-Tab, Benachrichtigungen, WebSocket-Event | konfigurierbar, meldet Erfolge |
| 4 | Frontend: Button, Wanted-Tab, `useWanted` | vollständig bedienbar |

Phase 1 und 2 sind der größte Teil (Picker und Scheduler mit ihren Randfällen). Jede Phase ist
für sich lauffähig und standardmäßig ausgeschaltet.

## 13. Risiken

- **Falsches Buch geladen** (gleicher Titel, anderer Autor, Sammelband). Gegenmittel: strenger
  Mindest-Score, Autor-Abgleich, ISBN bevorzugen; im Zweifel lieber nicht laden.
- **Last auf Quellen** und Sperren der eigenen IP. Gegenmittel: Taktung, Nutzer-Vorrang,
  429-Sperren respektieren, IRC standardmäßig aus.
- **Wartungsaufwand im Fork**, weil Upstream die Funktion nicht will. Gegenmittel: eigene
  Module, wenige gezielte Eingriffe (Abschnitt 10).
