# Mail-Archiv: Hostinger-IMAP → Google Drive

Holt alle E-Mails aus einem Hostinger-Postfach per IMAP ab und legt sie als
`.eml`-Dateien in Google Drive ab, sortiert nach Jahr und Monat. Laeuft als
Cronjob auf GitHub Actions, alle 6 Stunden.

```
<Drive-Root-Ordner>/
├── mail_archive_state.json          ← Zustand (zuletzt gesicherte UID je IMAP-Ordner)
└── 2026/
    └── 2026-08/
        ├── 2026-08-04_0912_absender@example.com_Betreff der Mail.eml
        └── Anhaenge/
            └── 2026-08-04_0912_absender@example.com_Betreff der Mail__rechnung.pdf
```

## Eigenschaften

- **Inkrementell:** je IMAP-Ordner wird die zuletzt gesicherte UID zusammen mit der
  `UIDVALIDITY` gemerkt. Aendert der Server die `UIDVALIDITY` (Postfach neu angelegt,
  Migration), wird dieser Ordner automatisch komplett neu durchlaufen.
- **Zustand liegt in Drive**, nicht auf dem Runner — GitHub-Actions-Runner sind bei
  jedem Lauf frisch, ein lokaler Zustand waere sofort wieder weg.
- **Idempotent:** vor jedem Upload wird geprueft, ob der Dateiname im Zielordner schon
  existiert. Die Dateiliste je Ordner wird einmal geholt und gecacht.
- **Gedrosselt:** maximal `MAX_PER_RUN` Mails pro Lauf (Standard 300), Zwischenstand
  alle 25 Mails. Der Erstlauf eines grossen Postfachs verteilt sich so ueber mehrere
  Cron-Durchgaenge, ohne dass ein Timeout Fortschritt kostet.
- **Fehlertolerant:** eine einzelne unlesbare Mail wird geloggt und uebersprungen,
  ihre UID gilt trotzdem als erledigt. Abmelden und Zustand speichern passieren in
  einem `finally`.
- **Liest nicht-invasiv:** Postfaecher werden schreibgeschuetzt geoeffnet und Mails
  mit `BODY.PEEK` geholt — der Gelesen-Status bleibt unangetastet.

## Setup

### 1. Drive-Ordner anlegen und ID holen

1. In Google Drive einen Ordner anlegen, z.B. `Mail-Archiv`.
2. Den Ordner oeffnen. Die ID steht in der URL nach `/folders/`:

   ```
   https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz123456
                                          └──────────── das ist DRIVE_ROOT_ID ────┘
   ```

### 2. Google-Cloud-Projekt, Drive API und OAuth-Client

1. [Google Cloud Console](https://console.cloud.google.com/) → Projekt anlegen
   (oder ein vorhandenes waehlen).
2. **APIs & Dienste → Bibliothek** → „Google Drive API" suchen → **Aktivieren**.
3. **APIs & Dienste → OAuth-Zustimmungsbildschirm**:
   - Nutzertyp **Extern** (bei Workspace-Konten geht auch **Intern**).
   - App-Name und Support-E-Mail eintragen, Rest kann leer bleiben.
   - Scope `https://www.googleapis.com/auth/drive` hinzufuegen.
   - Bei Nutzertyp „Extern": die eigene Google-Adresse unter **Testnutzer**
     eintragen. Solange die App im Status „Test" ist, laeuft das Refresh-Token
     nach 7 Tagen ab — fuer den Dauerbetrieb die App auf **In Produktion**
     veroeffentlichen (bei nur eigenen Daten ist dafuer keine Google-Pruefung noetig).
4. **APIs & Dienste → Anmeldedaten → Anmeldedaten erstellen → OAuth-Client-ID**:
   - Anwendungstyp **Desktop-App**.
   - JSON herunterladen und als `client_secret.json` neben `mail_archive.py` legen.

> **Warum kein Service-Account?** Service-Accounts haben keine eigene
> Drive-Speicherquota. Uploads in einen normalen Nutzerordner scheitern deshalb mit
> `storageQuotaExceeded`. Darum OAuth mit Refresh-Token.

### 3. Refresh-Token holen (einmalig, lokal)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python mail_archive.py --auth
```

Es oeffnet sich der Browser, du meldest dich mit dem Google-Konto an, dem der
Drive-Ordner gehoert, und bestaetigst den Zugriff. Danach gibt das Script die drei
Werte aus:

```
GOOGLE_CLIENT_ID     = 1234567890-abc....apps.googleusercontent.com
GOOGLE_CLIENT_SECRET = GOCSPX-....
GOOGLE_REFRESH_TOKEN = 1//09....
```

`client_secret.json` und diese Werte gehoeren **nicht** ins Repository.

### 4. Secrets im Repository hinterlegen

**Settings → Secrets and variables → Actions → Secrets → New repository secret:**

| Secret | Beispiel / Beschreibung |
| --- | --- |
| `IMAP_USER` | volle E-Mail-Adresse, z.B. `kontakt@snoel.de` |
| `IMAP_PASS` | Postfach-Passwort |
| `DRIVE_ROOT_ID` | Ordner-ID aus Schritt 1 |
| `GOOGLE_CLIENT_ID` | aus Schritt 3 |
| `GOOGLE_CLIENT_SECRET` | aus Schritt 3 |
| `GOOGLE_REFRESH_TOKEN` | aus Schritt 3 |
| `IMAP_HOST` | optional, Standard `imap.hostinger.com` |
| `IMAP_PORT` | optional, Standard `993` |
| `IMAP_FOLDERS` | optional, z.B. `INBOX,INBOX.Sent,INBOX.Archive` |

**Unter „Variables"** (nicht geheim) koennen zusaetzlich gesetzt werden:

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `SAVE_ATTACHMENTS` | `true` | Anhaenge zusaetzlich einzeln in `Anhaenge/` ablegen |
| `MAX_PER_RUN` | `300` | Obergrenze an Mails pro Lauf |

### 5. Ersten Lauf manuell starten

**Actions → „Mail-Archiv" → Run workflow.** Im Log siehst du, welche Ordner
verarbeitet werden und wie viele Mails ankommen. Danach laeuft der Job automatisch
alle 6 Stunden.

Bei einem grossen Postfach greift die Drossel: der erste Lauf sichert 300 Mails,
der naechste die naechsten 300 usw. Wenn es schneller gehen soll, `MAX_PER_RUN`
voruebergehend hochsetzen (auf das 30-Minuten-Timeout achten) und den Workflow
mehrfach manuell starten.

## Lokal laufen lassen

```bash
export IMAP_USER="kontakt@snoel.de"
export IMAP_PASS="..."
export DRIVE_ROOT_ID="..."
export GOOGLE_CLIENT_ID="..."
export GOOGLE_CLIENT_SECRET="..."
export GOOGLE_REFRESH_TOKEN="..."
python mail_archive.py
```

`LOG_LEVEL=DEBUG` liefert ausfuehrlichere Ausgaben.

## IMAP-Ordnernamen bei Hostinger

Hostinger nutzt `.` als Trenner. Ueblich sind:

```
INBOX, INBOX.Sent, INBOX.Drafts, INBOX.Archive, INBOX.Junk, INBOX.Trash
```

Ordner, die es nicht gibt, werden mit einer Warnung uebersprungen — der Lauf
bricht deswegen nicht ab. Die genauen Namen zeigt:

```bash
python - <<'PY'
import imaplib, os
m = imaplib.IMAP4_SSL("imap.hostinger.com", 993)
m.login(os.environ["IMAP_USER"], os.environ["IMAP_PASS"])
for zeile in m.list()[1]:
    print(zeile.decode())
m.logout()
PY
```

## Neu anfangen

Die Datei `mail_archive_state.json` im Drive-Root loeschen — der naechste Lauf
beginnt dann wieder bei UID 0. Bereits vorhandene `.eml`-Dateien werden dabei
nicht doppelt angelegt, weil vor dem Upload auf den Dateinamen geprueft wird.

## Bekannte Stolpersteine

- **`invalid_grant` beim Start:** Refresh-Token abgelaufen oder widerrufen. Das
  passiert bei OAuth-Apps im Status „Test" nach 7 Tagen. App veroeffentlichen und
  `--auth` erneut ausfuehren.
- **`storageQuotaExceeded`:** es wurde doch ein Service-Account verwendet, oder die
  Drive-Quota des Kontos ist voll.
- **IMAP-Anmeldung schlaegt fehl:** `IMAP_USER` muss die volle E-Mail-Adresse sein,
  nicht nur der lokale Teil.
- **Zwei Laeufe gleichzeitig:** verhindert der `concurrency`-Block im Workflow —
  sonst koennten sich parallele Laeufe beim Schreiben der Zustandsdatei ueberholen.
