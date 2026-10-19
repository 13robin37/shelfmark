# Konzept: Merkliste mit automatischem Download („Wanted“)

Status: umgesetzt auf Branch `feat/wanted-list` (Nutzer-Doku: `docs/wanted-list.md`).
Abweichungen der Umsetzung vom ursprünglichen Konzept, aus den Gegenproben:

- Bricht ein Nutzer einen Merklisten-Download ab, wird dieses Release verworfen und das nächste
  probiert; der Eintrag wird nicht pausiert (Pausieren bleibt Admins vorbehalten). „Retry failed
  releases“ gibt fehlgeschlagene und verworfene Releases wieder frei.
- Entscheidungen nach der Abschlussprüfung:
  - Nur in „Preferred Formats“ aktivierte Formate werden geladen; PDF, DJVU, CBZ und CBR sind
    standardmäßig aus.
  - Ein Treffer der Bibliotheksprüfung gilt nur für Vormerker mit Ordner-Ziel; E-Mail- und
    BookLore-Empfänger bekommen das Buch trotzdem zugestellt.
  - Will niemand mehr ein Buch, wird sein laufender Download abgebrochen.
  - Darf keiner der Vormerker aus einer verfügbaren Quelle laden, ruht der Eintrag (keine
    Suchen), bis sich Quellen, Rechte, Rollen oder Vormerker ändern.
  - Abschalten der Merkliste stoppt neue Suchen; laufende Downloads laufen zu Ende und werden
    verbucht.
  - Merklisten-Quellen und -Formate sowie Rechte und Rollen pro Nutzer lösen wie die globalen
    Quellen-Einstellungen eine sofortige Neusuche aus.
- Gesperrte Releases (zwei Fehlschläge in getrennten Durchgängen) werden nach 30 Tagen wieder
  zugelassen, damit ein Ausfall einer Quelle nicht dauerhaft alles sperrt.
- Fehler auf unserer Seite (Ziel nicht beschreibbar, E-Mail/BookLore, Worker-Ausnahmen) sind
  kein Strike; der Admin wird benachrichtigt, der Eintrag wartet mit normalem Backoff.
  Ein Release ohne verwertbare Buchdatei zählt dagegen als Fehlschlag des Releases.
- Weitere Zustellziele gelten erst als beliefert, wenn ihr eigener Zustell-Task fertig ist;
  ein Nutzer mit `{User}` im Benennungs-Template ist immer ein eigenes Ziel.
- `wanted_id` und `staged_path` sind Parameter von `queue_release`, nie Teil der Release-Daten
  (sonst könnte ein Client eine beliebige Serverdatei zustellen lassen).

## 1. Ziel

Nutzer merken sich ein E-Book aus der Metadaten-Suche vor. Shelfmark sucht es danach
selbstständig in allen erlaubten Quellen, lädt das beste passende Release herunter und probiert
bei einem Fehlschlag das nächste Release bzw. die nächste Quelle. Ist gerade nichts verfügbar,
bleibt das Buch dauerhaft vorgemerkt: Es wird regelmäßig neu gesucht, und neue Quellen oder
neu aufgetauchte Dateien werden automatisch probiert. Merken mehrere Nutzer dasselbe Buch vor,
wird es nur einmal gesucht und geladen und allen zugestellt.

Vorbild ist Readarrs „Wanted / Missing“ mit „Automatic Search“ und „Blocklist“. Bewusst **nicht**
Teil dieses Konzepts: Hörbücher, Autoren- oder Serien-Monitoring, RSS-Feeds,
Qualitäts-Upgrades (ein schon geladenes Buch später durch ein besseres Format ersetzen).

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
| Geladene Datei ohne erneuten Download weiterverarbeiten | ja: Task mit `staged_path` überspringt den Download | `orchestrator.py:791` |
| Prüfen, ob das Buch schon in der Bibliothek ist | ja, Calibre, E-Books | `core/library_index.py:143` |
| Rechte pro Nutzer/Quelle | ja: `resolve_policy_mode()` | `core/request_policy.py:331` |
| Reagieren, wenn Einstellungen gespeichert werden | ja: `register_on_save` | `core/settings_registry.py:313` |
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
3. **Zustellung eines Downloads an mehrere Ziele.** Ausgabemodus und Zielordner sind pro Nutzer
   einstellbar (`BOOKS_OUTPUT_MODE`, `DESTINATION`, beide `user_overridable`). Heute geht jeder
   Download an genau ein Ziel.
4. **Datenmodell, API, Oberfläche** für die Merkliste.

**Wichtig vorab – Projekt-Scope:** Das Upstream-README (`readme.md:255–265`) nennt
„Monitor authors, series, or new releases – there is no background automation“ und
„Queue future downloads“ ausdrücklich als **Nicht-Ziele**. Eine Übernahme ins Original-Projekt
ist damit unwahrscheinlich. Für diesen Fork heißt das: Funktion als eigenständiges,
standardmäßig ausgeschaltetes Modul bauen und die Berührungspunkte mit bestehendem Code klein
halten, damit spätere Merges von Upstream günstig bleiben (siehe Abschnitt 10).

## 3. Ablauf

```
Nutzer merkt Buch vor (oder tritt einem bestehenden Eintrag bei)
        │
        ▼
 ┌─────────────┐  fällig (next_search_at)  ┌─────────────┐
 │   wartend   │ ─────────────────────────▶│   suchend   │
 └─────────────┘                           └──────┬──────┘
        ▲                                         │ alle erlaubten Quellen durchsuchen,
        │ keine (weiteren) Kandidaten:            │ Kandidaten filtern + ranken,
        │ nächste Suche nach Backoff,             │ gesperrte Releases auslassen
        │ sofort bei neuer Quelle                 ▼
        │                                  ┌─────────────┐
        └──────────────────────────────────│   ladend    │◀──┐
                                           └──────┬──────┘   │ Fehler: Strike für das Release,
                                                  │          │ nächster Kandidat
                                      COMPLETE    │ ERROR ───┘
                                                  ▼
                                           ┌─────────────┐
                                           │  erledigt   │ → an alle Abonnenten zustellen
                                           └─────────────┘
```

Im Detail:

1. **Vormerken.** Gibt es das Buch schon auf der Merkliste, wird der Nutzer nur als weiterer
   Abonnent eingetragen. Sonst entsteht ein neuer Eintrag mit `next_search_at = jetzt`, die
   erste Suche startet also sofort (wie Readarrs „Search on add“).
2. **Bibliotheks-Check.** Vor jeder Suche: Ist das Buch laut `library_index.is_in_library()`
   schon vorhanden, wird der Eintrag als *erledigt (bereits in der Bibliothek)* abgeschlossen.
3. **Suchen.** Nacheinander alle Quellen, die (a) E-Books liefern (`supported_content_types`),
   (b) in den Merklisten-Einstellungen freigegeben sind und (c) für mindestens einen
   Abonnenten im Modus *Download* stehen. Ein gemeinsames Suchbudget für den ganzen Durchgang,
   wie `/api/releases` es macht (`main.py:3229`).
4. **Kandidaten bilden.** Alle Treffer aller Quellen in eine Liste, filtern und ranken
   (Abschnitt 5). Gesperrte Releases fliegen raus (Abschnitt 4).
5. **Einreihen.** Der beste Kandidat wird mit `queue_release()` eingereiht, verknüpft über
   ein neues `_wanted_id` (analog zu `_request_id`). Es läuft immer nur **ein** Download pro
   Eintrag, unabhängig von der Zahl der Abonnenten.
6. **Ergebnis abwarten.** Der verkettete Terminal-Hook meldet:
   - `COMPLETE` → Eintrag *erledigt*, Zustellung an alle Abonnenten (Abschnitt 7),
     Benachrichtigung „Vorgemerktes Buch geladen“.
   - `ERROR` → das Release bekommt einen Strike, sofort der nächste Kandidat aus Schritt 4.
     Ist die Kandidatenliste älter als ca. 30 Minuten, wird stattdessen neu gesucht
     (Prowlarr-Links verfallen nach einer Stunde, siehe Abschnitt 6).
   - `CANCELLED` durch einen Nutzer → Eintrag pausieren, nicht weiterprobieren.
7. **Nichts (mehr) gefunden.** Der Eintrag bleibt dauerhaft vorgemerkt und geht zurück auf
   *wartend*. Es gibt kein Aufgeben. Neue Versuche kommen auf zwei Wegen:
   - **Neue Dateien:** Jede Suche liefert die aktuellen Releases aller Quellen. Ein Release,
     das seit der letzten Suche neu aufgetaucht ist, hat eine neue Release-ID, steht also
     nicht auf der Sperrliste und wird probiert. Gesucht wird mit wachsenden Abständen
     (z. B. 1 h → 6 h → 24 h), danach dauerhaft im längsten Abstand.
   - **Neue Quellen:** Wird eine Quelle aktiviert oder neu konfiguriert (Mirror, Indexer,
     Prowlarr/Newznab), werden alle wartenden Einträge sofort fällig (Abschnitt 6).

Damit werden pro Suchdurchgang **alle** erlaubten Quellen abgefragt und ihre Releases in einer
gemeinsamen Rangliste der Reihe nach probiert. Erst wenn die Liste leer ist, wartet der
Eintrag auf den nächsten Durchgang.

## 4. Datenmodell

Drei neue Tabellen in `users.db`, angelegt in `_CREATE_TABLES_SQL` (`user_db.py:27`).

**`wanted_items`** – ein Eintrag pro vorgemerktem Buch, gemeinsam für alle Nutzer

| Spalte | Zweck |
|---|---|
| `id` | |
| `provider`, `provider_id` | Identität; `UNIQUE(provider, provider_id)` |
| `isbn_13`, `isbn_10` | zweiter Schlüssel, damit dasselbe Buch über verschiedene Metadaten-Anbieter (z. B. Hardcover und Open Library) nicht doppelt angelegt wird |
| `book_data` (JSON) | Anzeige + Rückfall, falls der Metadaten-Anbieter später ausfällt: Titel, Autoren-Liste, ISBNs, Jahr, Cover, Serie, Sprache. Mehr als beim Request (`requestPayload.ts:29` schickt keine ISBNs) |
| `state` | `waiting`, `searching`, `downloading`, `obtained`, `paused` |
| `current_task_id` | laufender Download |
| `search_count`, `last_search_at`, `next_search_at` | Backoff-Steuerung |
| `last_result` | z. B. „3 Quellen durchsucht, 0 passende Releases“ |
| `obtained_at`, `obtained_reason` | `downloaded` / `in_library` |
| `created_at`, `updated_at` | |

**`wanted_subscribers`** – wer das Buch haben will

| Spalte | Zweck |
|---|---|
| `wanted_id` (FK, `ON DELETE CASCADE`), `user_id` (FK `users`, `ON DELETE CASCADE`) | `PRIMARY KEY(wanted_id, user_id)` |
| `languages` (JSON, optional) | Sprachwahl dieses Nutzers; gesucht wird mit der Vereinigung aller Abonnenten |
| `delivered_at`, `delivery_task_id` | Zustellung an diesen Nutzer |
| `created_at` | |

Entfernt der letzte Abonnent seine Vormerkung, wird der Eintrag gelöscht.

**`wanted_attempts`** – Verlauf und Sperrliste

| Spalte | Zweck |
|---|---|
| `id`, `wanted_id` (FK, `ON DELETE CASCADE`) | |
| `source`, `source_id` (= Task-ID) | Release-Identität, Basis der Sperrliste |
| `release_title`, `format`, `size`, `indexer` | Anzeige und Abgleich |
| `status` | `queued`, `complete`, `error`, `cancelled` |
| `error_message`, `created_at`, `finished_at` | |

**Sperrliste mit zwei Strikes:** Weil Einträge nie aufgeben, darf ein einmaliger Netzwerkfehler
ein Release nicht für immer ausschließen. Darum:
- Erster Fehlschlag eines Releases: Es wird für den **laufenden** Durchgang übersprungen,
  beim nächsten Durchgang aber wieder probiert.
- Zweiter Fehlschlag in einem späteren Durchgang: Das Release ist für diesen Eintrag dauerhaft
  gesperrt.
- Die Sperrliste lässt sich pro Eintrag leeren (Admin).

Gespeichert wird nur die Identität eines Releases, **nicht** die Download-URL:
Prowlarr-Links verfallen, später wird immer neu gesucht.

`download_history` bleibt unverändert die globale Historie. Dort kommt nur eine neue Herkunft
`wanted` dazu (`VALID_ORIGINS`, `download_history_service.py:25`).

## 5. Automatische Release-Auswahl

Neues Modul, z. B. `shelfmark/core/release_picker.py`, rein funktional und gut testbar.

**Filter** (wie das Frontend heute):
- Format in `SUPPORTED_FORMATS`. Anders als im Frontend: Releases **ohne** Format-Angabe nur
  nehmen, wenn sonst nichts übrig ist.
- Sprache passt zu mindestens einer Sprachwahl der Abonnenten (`releaseLanguageMatchesFilter`;
  Releases ohne Sprache zählen als passend).
- Titel muss passen: Mindest-Score, damit kein falsches Buch geladen wird. Bausteine:
  `core/text_match.py` (`title_tokens_match`, `significant_tokens`, `isbn_variants`).
- Autor darf nicht widersprechen: `author_affinity() != AUTHOR_MISMATCH` (`core/author_match.py:40`).
- Nicht gesperrt (Abschnitt 4).

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
6. Plausible Größe (z. B. nicht über 200 MB).

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
   Obergrenze an Suchen pro Stunde (Einstellung). Das verteilt auch den Schwall, wenn nach
   einer neuen Quelle alle Einträge gleichzeitig fällig werden.
4. Quellen überspringen, deren Host gerade in der 429-Sperre steckt
   (`network.host_cooldown_remaining`). Eine Quelle, die bei der Suche einen Fehler meldet
   (statt „keine Treffer“), zählt nicht als durchsucht: Der Eintrag wird dann früher erneut
   fällig als nach dem normalen Backoff.

**Neue Quellen erkennen:** Über `register_on_save` hängt sich die Merkliste an das Speichern
der Quellen-Einstellungen (Download-Quellen, Mirrors, Libgen, Prowlarr, Newznab). Ändert sich
dort die Menge der aktiven Quellen, Mirrors oder Indexer, wird `next_search_at` aller wartenden
Einträge auf „jetzt“ gesetzt und die Backoff-Stufe zurückgesetzt. Zusätzlich merkt sich jeder
Eintrag, welche Quellen er beim letzten Durchgang abgefragt hat; eine Quelle, die damals nicht
verfügbar war und jetzt wieder ist, macht ihn ebenfalls sofort fällig.

Quellen-spezifische Stolpersteine:

| Quelle | Problem | Umgang |
|---|---|---|
| Anna's Archive / Direct | Jede Suche kann einen Bypass-Durchlauf von 60–120 s kosten | strenge Taktung, Nutzer hat Vorrang |
| Prowlarr / Newznab | Release-Links verfallen nach 1 h (In-Memory-Cache `prowlarr/cache.py:15`) | Suchen und Einreihen im selben Durchgang; alte Kandidatenlisten verwerfen |
| Prowlarr | Ohne GUID ist die Task-ID nicht stabil über Neustarts (`prowlarr/utils.py:44`, gesalzenes `hash()`) | Sperrliste zusätzlich über Titel + Indexer + Größe abgleichen |
| IRC | Suchergebnisse werden 30 Tage gecacht; höchstens 3 identische Anfragen pro 24 h | standardmäßig **nicht** für die Merkliste freigeben |
| Metadaten-Anbieter | OpenLibrary/Moly haben Rate-Limiter | `get_book` nur einmal pro Durchgang, Ergebnis wird ohnehin gecacht |

**Neustart:** Die Download-Queue lebt nur im Speicher. `startup_reconcile.py` setzt Einträge
in `searching`/`downloading` auf `waiting` mit kurzer Verzögerung zurück, analog zu den
Requests (`REOPEN_WINDOW`).

## 7. Rechte, gemeinsame Einträge und Zustellung

**Rechte:**
- Der Merk-Button erscheint nur für Nutzer, die für mindestens eine freigegebene Quelle im
  Modus *Download* sind. Alle anderen sehen ihn nicht.
- Der Job braucht dafür eine sitzungsfreie Variante der Prüfung aus `main.py:451–489`
  (Nutzer und Admin-Status aus der DB statt aus der Session).
- Ein Release wird nur an Abonnenten zugestellt, die für seine Quelle Download-Recht haben.
  Wer es nicht hat, bleibt Abonnent und wartet auf ein Release aus einer für ihn erlaubten
  Quelle. In der Praxis sind die Rechte meist global gleich, dann tritt der Fall nicht auf.
- Admins sehen alle Einträge mit ihren Abonnenten, Nutzer nur ihre eigenen.

**Gemeinsamer Download, getrennte Zustellung:** Gesucht und geladen wird pro Buch genau einmal.
Die Abonnenten werden nach ihrem tatsächlichen Ziel gruppiert (Ausgabemodus + Zielordner bzw.
E-Mail-Empfänger bzw. BookLore-Bibliothek, jeweils mit Nutzer-Überschreibungen):

1. Der Download läuft unter dem Abonnenten, der das Buch zuerst vorgemerkt hat
   (`queue_release(user_id=…, username=…)`), damit Ziel und Dateiname für ihn stimmen.
2. Teilen alle Abonnenten dasselbe Ziel (typisch: eine gemeinsame Calibre-Bibliothek), ist
   damit alles erledigt. Alle bekommen die Benachrichtigung.
3. Für jedes **weitere** Ziel wird die fertig geladene Datei kopiert, bevor die
   Nachverarbeitung sie verschiebt, und ein eigener Task für diesen Nutzer eingereiht, dessen
   `staged_path` auf die Kopie zeigt. Der Orchestrator überspringt dann den Download und
   führt nur die Nachverarbeitung für diesen Nutzer aus (bestehender Pfad,
   `orchestrator.py:791`). Das ist die einzige Stelle, an der der Download-Ablauf selbst
   erweitert werden muss.

## 8. API, Einstellungen, Benachrichtigungen

**Endpunkte** (neues Blueprint-Modul, z. B. `core/wanted_routes.py`):

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/api/wanted` | Liste (eigene; Admins alle, mit Abonnenten) |
| `POST` | `/api/wanted` | vormerken bzw. bestehendem Eintrag beitreten (`book_data`, optional `languages`) |
| `DELETE` | `/api/wanted/<id>` | eigene Vormerkung entfernen; Admin: Eintrag ganz löschen |
| `POST` | `/api/wanted/<id>/search` | jetzt suchen (setzt `next_search_at = jetzt`) |
| `POST` | `/api/wanted/<id>/pause` · `/resume` | pausieren / fortsetzen (Admin) |
| `DELETE` | `/api/wanted/<id>/blocklist` | Sperrliste leeren (Admin) |
| `GET` | `/api/wanted/<id>/attempts` | Versuchsverlauf |

Zusätzlich liefert die Metadaten-Suche pro Buch ein Feld `wanted` (analog zu `library`), damit
Suchergebnisse den Merk-Zustand anzeigen können.

**Einstellungs-Tab „Wanted“** (`@register_settings`, gespeichert in `plugins/wanted.json`):

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `WANTED_ENABLED` | `false` | Hauptschalter |
| `WANTED_SOURCES` | AA, Libgen, Prowlarr, Newznab | erlaubte Quellen in Reihenfolge (`OrderableListField`) |
| `WANTED_PREFERRED_FORMATS` | epub, azw3, mobi, … | Format-Reihenfolge für die Auswahl |
| `WANTED_SEARCH_BACKOFF_HOURS` | `1, 6, 24` | Abstände zwischen Suchen; der letzte Wert gilt danach dauerhaft |
| `WANTED_MAX_SEARCHES_PER_HOUR` | `6` | Lastschutz |

**Benachrichtigungen:** ein neues Event `WANTED_OBTAINED` an alle Abonnenten. Dafür werden das
Enum, `_resolve_notify_type`, `_render_message` (`notifications.py:66`, `:321`, `:347`),
`_ADMIN_EVENT_OPTIONS` (`config/notifications_settings.py:24`) und die feste Liste im Frontend
(`UserNotificationOverridesSection.tsx`) erweitert. Einzelne gescheiterte Versuche lösen keine
Benachrichtigung aus: Das normale `DOWNLOAD_FAILED` wird für Merklisten-Downloads unterdrückt,
weil der Job direkt das nächste Release probiert.

**WebSocket:** Event `wanted_update` an `user_{id}` jedes Abonnenten und an `admins` über
`emit_ws_event`; das Frontend lädt daraufhin die Liste neu (gleiches Muster wie `request_update`).

## 9. Oberfläche

Das Frontend hat keine Seiten-Routen, nur eine Suchseite mit Modals und der Aktivitäts-Seitenleiste.

- **Merk-Button** dort, wo heute schon der Hardcover-Listen-Dropdown `BookTargetDropdown` sitzt:
  Karten- (`CardView.tsx:147`), Kompakt- (`CompactView.tsx:149`) und Listenansicht
  (`ListView.tsx:305`), Detail-Dialog (`DetailsModal.tsx:405`) und Kopf des Release-Dialogs
  (`ReleaseModal.tsx:1749`). Zustand an/aus aus dem neuen `wanted`-Feld; ausgeblendet für
  Nutzer ohne Download-Recht und für Hörbücher.
  Achtung: `BookTargetDropdown` nutzt bereits ein Lesezeichen-Symbol – für die Merkliste ein
  anderes wählen (z. B. Auge oder Glocke), sonst sind beide verwechselbar.
- **Neuer Tab „Wanted“ in der Aktivitäts-Seitenleiste** (`ActivitySidebar.tsx:68`, neuer
  `ActivityTabKey` und `ActivityKind`). Pro Eintrag: Cover, Titel, Zustand, letzte und nächste
  Suche, Zahl der Versuche; für Admins zusätzlich die Abonnenten. Aktionen: *Jetzt suchen*,
  *Vormerkung entfernen*, für Admins *Pausieren* und *Sperrliste leeren*. Eine eigene
  Verwaltungsansicht ist nicht nötig.
- Daten über einen neuen Hook `useWanted`, der auf `wanted_update` hört; Fallback Polling.
- Tests nach dem bestehenden Muster: Logik in `utils/*.ts`, Vitest-Unit-Tests ohne Rendering.

## 10. Berührungspunkte mit bestehendem Code

So wenig wie möglich, damit Upstream-Merges günstig bleiben. Neu und eigenständig:
`core/wanted_service.py`, `core/wanted_routes.py`, `core/release_picker.py`,
`download/wanted_scheduler.py`, `config/wanted_settings.py`, Frontend-Hook, -Tab und -Button.

Geänderte bestehende Stellen:

| Datei | Änderung |
|---|---|
| `core/user_db.py` | drei Tabellen in `_CREATE_TABLES_SQL` |
| `core/models.py` | `DownloadTask.wanted_id` |
| `download/orchestrator.py` | `_wanted_id` in `queue_release` übernehmen, `wanted_id` in `_task_to_dict`; vor der Nachverarbeitung eine Kopie für weitere Zustellziele abzweigen |
| `main.py` | Terminal-Hook verketten (`_record_download_terminal_snapshot`), Scheduler starten, Blueprint registrieren, `wanted` in Metadaten-Antwort |
| `core/download_history_service.py` | Herkunft `wanted` |
| `core/startup_reconcile.py` | hängende Einträge zurücksetzen |
| `core/notifications.py` (+ Settings, Frontend-Liste) | ein Event |
| `download/warmup.py` | Zeitstempel der letzten Nutzersuche |
| Quellen-Einstellungen | `register_on_save`-Handler für „neue Quelle“ |
| Frontend: Ergebnis-Ansichten, `DetailsModal`, `ReleaseModal`, `ActivitySidebar`, `types` | Button und Tab |

**Zusammenspiel mit dem Auto-Retry** (Branch `feat/auto-retry-failed-downloads`): Beide greifen
am Fehlerpfad an. Der Auto-Retry wiederholt **dasselbe** Release mit Wartezeit, die Merkliste
wechselt zum **nächsten** Release und gibt einem gescheiterten Release über die zwei Strikes
ohnehin eine zweite Chance im nächsten Durchgang. Empfehlung: Merklisten-Tasks vom Auto-Retry
ausnehmen (`_is_auto_retryable` prüft zusätzlich `task.wanted_id is None`), sonst verzögert er
das Weiterschalten.

## 11. Entscheidungen

| Frage | Entscheidung | Folge im Konzept |
|---|---|---|
| Nutzer ohne Download-Recht | Button ausblenden | Abschnitt 7, 9 |
| Hörbücher | nein, nur E-Books | keine Hörbuch-Formate, -Quellen oder -Größen; Bibliotheks-Check über Calibre deckt alles ab |
| Aufgeben | nie; dauerhaft vorgemerkt, bei neuen Quellen/Dateien erneut versuchen | kein Zustand *aufgegeben*; Backoff mit Dauer-Abstand; „neue Quelle“-Trigger; Sperrliste mit zwei Strikes |
| Admin-Übersicht | Tab mit allen Einträgen reicht | keine eigene Verwaltungsansicht |
| Mehrere Nutzer, gleiches Buch | gemeinsam | ein Eintrag mit Abonnenten, ein Download, Zustellung je Ziel |

## 12. Umsetzung in Phasen

| Phase | Inhalt | Ergebnis |
|---|---|---|
| 1 | Datenmodell, `WantedService`, API, Release-Picker mit Tests | Merkliste per API pflegbar, Auswahl testbar |
| 2 | Scheduler, Hook-Verkettung, Sperrliste, „neue Quelle“-Trigger, Neustart-Abgleich, Lastschutz | Automatik läuft für einen Abonnenten pro Eintrag, noch ohne UI |
| 3 | Gemeinsame Einträge: Zustellung an weitere Ziele über Kopie + eigenen Task | mehrere Abonnenten mit unterschiedlichen Zielen |
| 4 | Einstellungs-Tab, Benachrichtigung, WebSocket-Event | konfigurierbar, meldet Erfolge |
| 5 | Frontend: Button, Wanted-Tab, `useWanted` | vollständig bedienbar |

Phase 1 und 2 sind der größte Teil (Picker und Scheduler mit ihren Randfällen). Jede Phase ist
für sich lauffähig und standardmäßig ausgeschaltet. Phase 3 kann entfallen, solange alle Nutzer
dasselbe Ziel haben.

## 13. Risiken

- **Falsches Buch geladen** (gleicher Titel, anderer Autor, Sammelband). Gegenmittel: strenger
  Mindest-Score, Autor-Abgleich, ISBN bevorzugen; im Zweifel lieber nicht laden.
- **Last auf Quellen** und Sperren der eigenen IP – durch das Nie-Aufgeben auf Dauer, nicht nur
  kurzzeitig. Gegenmittel: Taktung, Obergrenze pro Stunde, Nutzer-Vorrang, 429-Sperren
  respektieren, IRC standardmäßig aus, langer Dauer-Abstand.
- **Wachsende Merkliste:** Viele dauerhaft offene Einträge erzeugen dauerhaft Suchen. Die
  Obergrenze pro Stunde begrenzt die Last, verlängert dann aber die Abstände pro Eintrag.
  Die Liste zeigt deshalb „nächste Suche“ ehrlich an.
- **Unterschiedliche Rechte unter Abonnenten** können dazu führen, dass ein Eintrag für einen
  Nutzer erledigt ist und für einen anderen nicht (Abschnitt 7).
- **Wartungsaufwand im Fork**, weil Upstream die Funktion nicht will. Gegenmittel: eigene
  Module, wenige gezielte Eingriffe (Abschnitt 10).

## 14. Readarr-Erweiterungen (Branch `feat/wanted-list-readarr`)

Abgleich mit Readarr (Vormerken und Herunterladen) und die gewählten Erweiterungen:

| # | Funktion | Entscheidung |
|---|----------|--------------|
| 1 | Erscheinungsdatum | Datum aus Hardcover, OpenLibrary und Google Books; gesucht wird ab einem Tag vor Erscheinen, die Liste zeigt „Releases …“. |
| 2 | Verlauf und Sperrliste | Aufklappbarer Verlauf pro Eintrag; Admins geben einzelne Releases wieder frei. |
| 3 | Manuelle Auswahl | „Für Merkliste laden“ im Release-Dialog zählt für den Eintrag und alle Vormerker; pro Release ein Hinweis, warum die Automatik ihn übergehen würde. |
| 4 | Wunschformat abwarten | Einstellbar: Format, ab dem ein Release reicht, und Tage, die darauf gewartet wird. Standard 0 Tage (aus). |
| 5 | Sprache pro Vormerkung | Vormerken aus dem Release-Dialog übernimmt dessen Sprachfilter; im Wanted-Tab änderbar, eine Änderung sucht sofort neu. |
| 6 | RSS-Abgleich | Neueste Releases von Prowlarr/Newznab regelmäßig gegen alle Vormerkungen; Intervall einstellbar, Standard 15 Minuten, 0 schaltet ab. |
| 7 | Serien und Autoren | „Alle fehlenden Bände vormerken“ und dauerhaftes Beobachten von Serien und Autoren. |
| 8 | Hardcover-Listen | Eine Hardcover-Liste pro Shelfmark-Nutzer füllt die Merkliste (alle 6 Stunden); Entfernen in Hardcover entfernt nichts. |
| 9 | Wanted-Tab | Status-Filter, Sortierung, „Erhaltene entfernen“. Keine Mehrfachauswahl, kein „Alle suchen“. |
| 10 | Benachrichtigungen | Neu: „Release gefunden, wird geladen“, einmal pro Suchdurchgang. |
| + | Anfragen | Beim Genehmigen einer Anfrage kann der Admin sie auf die Merkliste setzen; die Vormerkung gilt für den anfragenden Nutzer. |

Bewusst nicht übernommen: Qualitäts-Upgrades bereits zugestellter Bücher, Wortlisten und Scoring,
Kalenderseite, Umbenennen/Import (übernehmen Shelfmarks Ausgabemodi), Hörbücher.

Umsetzung in vier Phasen mit Gegenprobe nach jeder Phase: (a) 2, 4, 5, 9, 10; (b) 1, 3; (c) 6;
(d) 7, 8 und Anfragen.
