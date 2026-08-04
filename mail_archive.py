#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mail_archive.py — Sichert alle E-Mails eines IMAP-Postfachs als .eml-Dateien
nach Google Drive, sortiert nach <Jahr>/<Jahr>-<Monat>/.

Laeuft als Cronjob auf GitHub Actions: der Runner ist bei jedem Lauf frisch,
darum liegt der Zustand (zuletzt gesicherte UID je IMAP-Ordner) als JSON-Datei
im Drive-Root-Ordner und nicht auf der Platte.

Aufrufe:
    python mail_archive.py           # normaler Archivlauf
    python mail_archive.py --auth    # einmalig lokal: Refresh-Token holen
"""

from __future__ import annotations

import email
import imaplib
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

# Voller Drive-Scope: der Root-Ordner wurde vom Menschen in der Weboberflaeche
# angelegt, nicht von diesem Programm — drive.file wuerde dafuer nicht reichen.
SCOPES = ["https://www.googleapis.com/auth/drive"]

TOKEN_URI = "https://oauth2.googleapis.com/token"
STATE_DATEINAME = "mail_archive_state.json"
ANHANG_ORDNER = "Anhaenge"
ORDNER_MIME = "application/vnd.google-apps.folder"

# Nach so vielen gesicherten Mails wird der Zwischenstand nach Drive geschrieben,
# damit ein Timeout des Runners nicht den ganzen Fortschritt kostet.
STATE_INTERVALL = 25

# Maximale Laenge des Dateinamens inklusive ".eml".
MAX_DATEINAME = 110

log = logging.getLogger("mail-archiv")


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------


class Konfig:
    """Alle Einstellungen aus Umgebungsvariablen."""

    def __init__(self) -> None:
        self.imap_host = os.environ.get("IMAP_HOST", "imap.hostinger.com").strip()
        self.imap_port = int(os.environ.get("IMAP_PORT", "993"))
        self.imap_user = os.environ.get("IMAP_USER", "").strip()
        self.imap_pass = os.environ.get("IMAP_PASS", "")
        self.imap_folders = ordnerliste_lesen(
            os.environ.get("IMAP_FOLDERS", "INBOX")
        )

        self.drive_root_id = os.environ.get("DRIVE_ROOT_ID", "").strip()
        self.client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        self.refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "").strip()

        self.save_attachments = _bool_env("SAVE_ATTACHMENTS", True)
        self.max_per_run = int(os.environ.get("MAX_PER_RUN", "300"))

    def pruefen(self) -> None:
        """Bricht mit klarer Meldung ab, wenn Pflichtvariablen fehlen."""
        pflicht = {
            "IMAP_USER": self.imap_user,
            "IMAP_PASS": self.imap_pass,
            "DRIVE_ROOT_ID": self.drive_root_id,
            "GOOGLE_CLIENT_ID": self.client_id,
            "GOOGLE_CLIENT_SECRET": self.client_secret,
            "GOOGLE_REFRESH_TOKEN": self.refresh_token,
        }
        fehlend = [name for name, wert in pflicht.items() if not wert]
        if fehlend:
            raise SystemExit(
                "Abbruch: folgende Umgebungsvariablen fehlen oder sind leer: "
                + ", ".join(fehlend)
                + "\nSiehe README.md, Abschnitt 'Secrets im Repository hinterlegen'."
            )
        if self.max_per_run < 1:
            raise SystemExit("Abbruch: MAX_PER_RUN muss mindestens 1 sein.")


# Zuordnung IMAP-Ordner -> eigener Unterordner in Drive. None bedeutet:
# direkt im Wurzelverzeichnis, das bleibt dem Posteingang vorbehalten.
# Die Schluessel sind kleingeschrieben, verglichen wird ohne Gross-/Kleinschreibung.
STANDARD_UNTERORDNER: dict[str, str | None] = {
    "inbox": None,
    "inbox.sent": "Gesendet",
    "sent": "Gesendet",
    "sent messages": "Gesendet",
    "inbox.sent messages": "Gesendet",
    "inbox.trash": "Geloescht",
    "trash": "Geloescht",
    "inbox.deleted messages": "Geloescht",
    "inbox.archive": "Archiv",
    "archive": "Archiv",
    "inbox.drafts": "Entwuerfe",
    "drafts": "Entwuerfe",
    "inbox.junk": "Spam",
    "junk": "Spam",
    "inbox.spam": "Spam",
}


def unterordner_fuer(imap_ordner: str) -> str | None:
    """
    Liefert den Drive-Unterordner, unter dem dieser IMAP-Ordner abgelegt wird.

    Der Posteingang landet direkt im Wurzelverzeichnis, alles andere bekommt
    einen eigenen Zweig — sonst lagen eingegangene und gesendete Mails im selben
    Monatsordner und waeren hinterher nicht mehr auseinanderzuhalten.
    """
    schluessel = imap_ordner.strip().lower()
    if schluessel in STANDARD_UNTERORDNER:
        return STANDARD_UNTERORDNER[schluessel]
    # Unbekannter Ordner: den Namen selbst verwenden, ohne INBOX-Praefix.
    name = imap_ordner.strip()
    for praefix in ("INBOX.", "INBOX/"):
        if name.upper().startswith(praefix.upper()):
            name = name[len(praefix):]
            break
    return saeubern(name.replace(".", " ").replace("/", " "), 50)


def ordnerliste_lesen(rohwert: str) -> list[tuple[str, str | None]]:
    """
    Zerlegt IMAP_FOLDERS in Paare (IMAP-Ordner, Drive-Unterordner).

    Kommagetrennt. Optional laesst sich der Zielordner je Eintrag mit einem
    Doppelpunkt erzwingen, z.B. 'INBOX.Sent:Gesendet'. Ohne Angabe greift die
    Zuordnung aus STANDARD_UNTERORDNER.
    """
    ergebnis: list[tuple[str, str | None]] = []
    gesehen: set[str] = set()
    for eintrag in rohwert.split(","):
        eintrag = eintrag.strip()
        if not eintrag:
            continue
        if ":" in eintrag:
            ordner, _, label = eintrag.partition(":")
            ordner, label = ordner.strip(), label.strip()
            unterordner = saeubern(label, 50) if label else None
        else:
            ordner = eintrag
            unterordner = unterordner_fuer(ordner)
        if not ordner or ordner.lower() in gesehen:
            continue
        gesehen.add(ordner.lower())
        ergebnis.append((ordner, unterordner))
    return ergebnis or [("INBOX", None)]


def _bool_env(name: str, standard: bool) -> bool:
    """Liest eine Ja/Nein-Umgebungsvariable tolerant ein."""
    wert = os.environ.get(name)
    if wert is None or wert.strip() == "":
        return standard
    return wert.strip().lower() in ("1", "true", "yes", "ja", "on")


# ---------------------------------------------------------------------------
# Hilfsfunktionen: Header dekodieren, Dateinamen bauen
# ---------------------------------------------------------------------------


def header_dekodieren(rohwert: str | None) -> str:
    """Wandelt MIME-encoded-words (=?UTF-8?B?...?=) in lesbaren Text um."""
    if not rohwert:
        return ""
    try:
        text = str(make_header(decode_header(rohwert)))
    except Exception:
        # Kaputte Kodierung: lieber der Rohwert als ein Absturz.
        text = str(rohwert)
    # Zeilenumbrueche aus gefalteten Headern glaetten.
    return re.sub(r"\s+", " ", text).strip()


def absender_ermitteln(msg: Message) -> str:
    """Liefert die reine Absenderadresse, notfalls den Anzeigenamen."""
    roh = msg.get("From") or msg.get("Sender") or ""
    name, adresse = parseaddr(header_dekodieren(roh))
    if adresse:
        return adresse
    return name or "unbekannt"


def empfaenger_ermitteln(msg: Message) -> str:
    """
    Liefert die erste Empfaengeradresse. Wird fuer gesendete Mails verwendet,
    wo der Absender immer man selbst ist und nichts zur Unterscheidung beitraegt.
    """
    roh = msg.get("To") or msg.get("Cc") or ""
    for teil in header_dekodieren(roh).split(","):
        name, adresse = parseaddr(teil)
        if adresse:
            return adresse
        if name.strip():
            return name.strip()
    return "unbekannt"


def datum_ermitteln(msg: Message) -> datetime:
    """
    Datum aus dem Date-Header. Fehlt der Header oder ist er kaputt, wird die
    aktuelle Zeit genommen.

    Der Zeitstempel wird so uebernommen, wie er im Header steht — bewusst ohne
    Umrechnung in die Zeitzone des Runners. Sonst haenge die Einsortierung an
    der Zeitzone der Maschine (auf GitHub Actions UTC), und eine Mail vom
    1. August, 00:30 Uhr deutscher Zeit landete im Juli-Ordner.
    """
    roh = msg.get("Date")
    if roh:
        try:
            dt = parsedate_to_datetime(roh)
            if dt is not None:
                return dt.replace(tzinfo=None)
        except (TypeError, ValueError, OverflowError):
            pass
    log.warning("Date-Header fehlt oder ist unlesbar — verwende die aktuelle Zeit.")
    return datetime.now()


def saeubern(text: str, maxlen: int = 60) -> str:
    """Ersetzt fuer Dateisysteme unzulaessige Zeichen und kuerzt."""
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)          # Steuerzeichen
    text = re.sub(r'[<>:"/\\|?*]', "_", text)             # verbotene Zeichen
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" .")                               # keine Endpunkte/-leerzeichen
    if len(text) > maxlen:
        text = text[:maxlen].rstrip(" ._-")
    return text or "ohne"


def dateiname_bauen(dt: datetime, absender: str, betreff: str) -> str:
    """Baut 'YYYY-MM-DD_HHMM_absender_betreff.eml', gekuerzt auf MAX_DATEINAME."""
    praefix = dt.strftime("%Y-%m-%d_%H%M")
    abs_teil = saeubern(absender, 40)
    # Restbudget fuer den Betreff ausrechnen: Praefix + 2 Unterstriche + ".eml".
    rest = MAX_DATEINAME - len(praefix) - len(abs_teil) - 2 - len(".eml")
    betr_teil = saeubern(betreff, max(rest, 10))
    name = f"{praefix}_{abs_teil}_{betr_teil}.eml"
    if len(name) > MAX_DATEINAME:
        name = name[: MAX_DATEINAME - len(".eml")] + ".eml"
    return name


def monats_pfad(dt: datetime) -> tuple[str, str]:
    """Liefert ('2026', '2026-08') fuer die Ordnerstruktur in Drive."""
    return dt.strftime("%Y"), dt.strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------


def drive_dienst(cfg: Konfig):
    """Baut den Drive-Client aus Client-ID, Secret und Refresh-Token."""
    creds = Credentials(
        token=None,
        refresh_token=cfg.refresh_token,
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except Exception as fehler:
        raise SystemExit(
            "Abbruch: Google-Zugangsdaten konnten nicht erneuert werden "
            f"({fehler}).\nStimmen GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN noch? "
            "Notfalls 'python mail_archive.py --auth' erneut ausfuehren."
        ) from fehler
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def mit_wiederholung(aufruf, versuche: int = 5, beschreibung: str = "Drive-Aufruf"):
    """
    Fuehrt einen Drive-Aufruf aus und wiederholt ihn bei temporaeren Fehlern
    (429, 5xx, Netzprobleme) mit wachsender Wartezeit.
    """
    wartezeit = 2
    for versuch in range(1, versuche + 1):
        try:
            return aufruf()
        except HttpError as fehler:
            status = getattr(fehler.resp, "status", None)
            if status not in (403, 429, 500, 502, 503, 504) or versuch == versuche:
                raise
            log.warning(
                "%s fehlgeschlagen (HTTP %s), Versuch %d/%d — warte %ds.",
                beschreibung, status, versuch, versuche, wartezeit,
            )
        except (TimeoutError, ConnectionError, OSError) as fehler:
            if versuch == versuche:
                raise
            log.warning(
                "%s fehlgeschlagen (%s), Versuch %d/%d — warte %ds.",
                beschreibung, fehler, versuch, versuche, wartezeit,
            )
        time.sleep(wartezeit)
        wartezeit *= 2
    raise RuntimeError(f"{beschreibung}: alle Versuche aufgebraucht.")


class DriveAblage:
    """Kapselt Ordnerverwaltung, Uploads und die Zustandsdatei in Drive."""

    def __init__(self, dienst, root_id: str) -> None:
        self.dienst = dienst
        self.root_id = root_id
        # Cache: (parent_id, ordnername) -> ordner_id. Spart eine Suchanfrage
        # pro Mail, sobald der Monatsordner einmal aufgeloest wurde.
        self._ordner_cache: dict[tuple[str, str], str] = {}
        # Cache: ordner_id -> Menge der dort bereits liegenden Dateinamen.
        # Wird je Ordner genau einmal gefuellt und macht den Lauf idempotent,
        # auch wenn ein Abbruch zwischen zwei Zustandsspeicherungen passiert ist.
        self._namen_cache: dict[str, set[str]] = {}

    # -- Ordner ------------------------------------------------------------

    def ordner_holen(self, name: str, parent_id: str) -> str:
        """Sucht den Ordner unterhalb von parent_id, legt ihn sonst an."""
        schluessel = (parent_id, name)
        if schluessel in self._ordner_cache:
            return self._ordner_cache[schluessel]

        sicher = name.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"name = '{sicher}' and '{parent_id}' in parents "
            f"and mimeType = '{ORDNER_MIME}' and trashed = false"
        )
        antwort = mit_wiederholung(
            lambda: self.dienst.files()
            .list(
                q=query,
                fields="files(id, name)",
                pageSize=1,
                spaces="drive",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute(),
            beschreibung=f"Ordnersuche '{name}'",
        )
        treffer = antwort.get("files", [])
        if treffer:
            ordner_id = treffer[0]["id"]
        else:
            metadaten = {
                "name": name,
                "mimeType": ORDNER_MIME,
                "parents": [parent_id],
            }
            neu = mit_wiederholung(
                lambda: self.dienst.files()
                .create(body=metadaten, fields="id", supportsAllDrives=True)
                .execute(),
                beschreibung=f"Ordner anlegen '{name}'",
            )
            ordner_id = neu["id"]
            log.info("Ordner angelegt: %s", name)

        self._ordner_cache[schluessel] = ordner_id
        return ordner_id

    def monatsordner(self, dt: datetime, unterordner: str | None = None) -> str:
        """
        Liefert die ID von <Root>/<Jahr>/<Jahr>-<Monat>/ bzw.
        <Root>/<Unterordner>/<Jahr>/<Jahr>-<Monat>/ fuer Gesendet, Geloescht usw.
        """
        basis = self.root_id
        if unterordner:
            basis = self.ordner_holen(unterordner, self.root_id)
        jahr, monat = monats_pfad(dt)
        jahr_id = self.ordner_holen(jahr, basis)
        return self.ordner_holen(monat, jahr_id)

    # -- Dateien -----------------------------------------------------------

    def _namen_im_ordner(self, ordner_id: str) -> set[str]:
        """Listet einmalig alle Dateinamen eines Ordners und merkt sie sich."""
        if ordner_id in self._namen_cache:
            return self._namen_cache[ordner_id]

        namen: set[str] = set()
        seite = None
        while True:
            aktuelle_seite = seite
            antwort = mit_wiederholung(
                lambda: self.dienst.files()
                .list(
                    q=f"'{ordner_id}' in parents and trashed = false",
                    fields="nextPageToken, files(name)",
                    pageSize=1000,
                    pageToken=aktuelle_seite,
                    spaces="drive",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute(),
                beschreibung="Dateiliste",
            )
            namen.update(datei["name"] for datei in antwort.get("files", []))
            seite = antwort.get("nextPageToken")
            if not seite:
                break

        self._namen_cache[ordner_id] = namen
        return namen

    def datei_vorhanden(self, ordner_id: str, name: str) -> bool:
        return name in self._namen_im_ordner(ordner_id)

    def hochladen(
        self, ordner_id: str, name: str, daten: bytes, mimetype: str
    ) -> str | None:
        """Laedt eine Datei hoch; ueberspringt sie, wenn der Name schon existiert."""
        if self.datei_vorhanden(ordner_id, name):
            log.debug("Bereits vorhanden, uebersprungen: %s", name)
            return None

        # Der Stream wird im Lambda erzeugt: bei einem Wiederholungsversuch
        # muss wieder von vorn gelesen werden koennen.
        datei = mit_wiederholung(
            lambda: self.dienst.files()
            .create(
                body={"name": name, "parents": [ordner_id]},
                media_body=MediaIoBaseUpload(
                    io.BytesIO(daten), mimetype=mimetype, resumable=False
                ),
                fields="id",
                supportsAllDrives=True,
            )
            .execute(),
            beschreibung=f"Upload '{name}'",
        )
        self._namen_im_ordner(ordner_id).add(name)
        return datei["id"]

    # -- Zustandsdatei -----------------------------------------------------

    def _state_datei_id(self) -> str | None:
        sicher = STATE_DATEINAME.replace("'", "\\'")
        antwort = mit_wiederholung(
            lambda: self.dienst.files()
            .list(
                q=(
                    f"name = '{sicher}' and '{self.root_id}' in parents "
                    f"and trashed = false"
                ),
                fields="files(id)",
                pageSize=1,
                spaces="drive",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute(),
            beschreibung="Zustandsdatei suchen",
        )
        treffer = antwort.get("files", [])
        return treffer[0]["id"] if treffer else None

    def zustand_laden(self) -> dict:
        """Liest den Zustand aus dem Drive-Root; bei Fehlern leerer Zustand."""
        datei_id = self._state_datei_id()
        if not datei_id:
            log.info("Keine Zustandsdatei gefunden — erster Lauf.")
            return {"folders": {}}
        try:
            roh = mit_wiederholung(
                lambda: self.dienst.files()
                .get_media(fileId=datei_id, supportsAllDrives=True)
                .execute(),
                beschreibung="Zustand lesen",
            )
            zustand = json.loads(roh.decode("utf-8"))
            if not isinstance(zustand, dict) or "folders" not in zustand:
                raise ValueError("unerwartete Struktur")
            log.info(
                "Zustand geladen: %d IMAP-Ordner bekannt.", len(zustand["folders"])
            )
            return zustand
        except Exception as fehler:
            log.warning(
                "Zustandsdatei unlesbar (%s) — beginne fuer alle Ordner neu.", fehler
            )
            return {"folders": {}}

    def zustand_speichern(self, zustand: dict) -> None:
        """Schreibt den Zustand als JSON in den Drive-Root-Ordner."""
        zustand["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        daten = json.dumps(zustand, ensure_ascii=False, indent=2).encode("utf-8")

        def medium() -> MediaIoBaseUpload:
            return MediaIoBaseUpload(
                io.BytesIO(daten), mimetype="application/json", resumable=False
            )

        datei_id = self._state_datei_id()
        if datei_id:
            mit_wiederholung(
                lambda: self.dienst.files()
                .update(fileId=datei_id, media_body=medium(), supportsAllDrives=True)
                .execute(),
                beschreibung="Zustand schreiben",
            )
        else:
            mit_wiederholung(
                lambda: self.dienst.files()
                .create(
                    body={"name": STATE_DATEINAME, "parents": [self.root_id]},
                    media_body=medium(),
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute(),
                beschreibung="Zustand anlegen",
            )
        log.debug("Zustand gespeichert.")


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------


def imap_verbinden(cfg: Konfig) -> imaplib.IMAP4_SSL:
    log.info("Verbinde zu %s:%d als %s", cfg.imap_host, cfg.imap_port, cfg.imap_user)
    imap = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
    try:
        imap.login(cfg.imap_user, cfg.imap_pass)
    except imaplib.IMAP4.error as fehler:
        raise SystemExit(
            f"Abbruch: IMAP-Anmeldung fehlgeschlagen ({fehler}). "
            "Stimmen IMAP_USER (volle E-Mail-Adresse) und IMAP_PASS?"
        ) from fehler
    log.info("IMAP-Anmeldung erfolgreich.")
    return imap


def vorhandene_ordner_loggen(imap: imaplib.IMAP4_SSL) -> None:
    """
    Schreibt alle auf dem Server vorhandenen IMAP-Ordner ins Protokoll.
    Reine Diagnose: so laesst sich nachsehen, wie die Ordner wirklich heissen,
    ohne sich zusaetzlich mit einem Mailprogramm verbinden zu muessen.
    """
    try:
        status, daten = imap.list()
    except imaplib.IMAP4.error as fehler:
        log.warning("Ordnerliste nicht abrufbar (%s).", fehler)
        return
    if status != "OK" or not daten:
        return

    namen = []
    for zeile in daten:
        if not zeile:
            continue
        text = zeile.decode("utf-8", "replace") if isinstance(zeile, bytes) else str(zeile)
        # Format: (\HasNoChildren) "." INBOX.Sent  — der Name steht am Ende.
        treffer = re.search(r'"([^"]*)"\s*$', text) or re.search(r"(\S+)\s*$", text)
        if treffer:
            namen.append(treffer.group(1))
    if namen:
        log.info("Auf dem Server vorhandene IMAP-Ordner: %s", ", ".join(namen))


def ordner_waehlen(imap: imaplib.IMAP4_SSL, ordner: str) -> int | None:
    """
    Waehlt den IMAP-Ordner schreibgeschuetzt und liefert dessen UIDVALIDITY.
    Existiert der Ordner nicht, wird None zurueckgegeben (Aufrufer ueberspringt).
    """
    name = f'"{ordner}"'
    try:
        status, _ = imap.select(name, readonly=True)
    except imaplib.IMAP4.error as fehler:
        log.warning("IMAP-Ordner '%s' nicht auswaehlbar (%s) — uebersprungen.",
                    ordner, fehler)
        return None
    if status != "OK":
        log.warning("IMAP-Ordner '%s' existiert nicht — uebersprungen.", ordner)
        return None

    try:
        status, daten = imap.status(name, "(UIDVALIDITY)")
    except imaplib.IMAP4.error as fehler:
        log.warning("UIDVALIDITY fuer '%s' nicht lesbar (%s) — uebersprungen.",
                    ordner, fehler)
        return None
    if status != "OK" or not daten:
        log.warning("UIDVALIDITY fuer '%s' nicht lesbar — uebersprungen.", ordner)
        return None

    treffer = re.search(rb"UIDVALIDITY\s+(\d+)", daten[0] or b"")
    if not treffer:
        log.warning("UIDVALIDITY fuer '%s' nicht auswertbar — uebersprungen.", ordner)
        return None
    return int(treffer.group(1))


def neue_uids(imap: imaplib.IMAP4_SSL, letzte_uid: int) -> list[int]:
    """
    Sucht alle UIDs oberhalb von letzte_uid.

    IMAP-Eigenheit: 'UID n:*' liefert immer mindestens eine Nachricht zurueck —
    naemlich die hoechste vorhandene, auch wenn deren UID kleiner als n ist.
    Darum wird zusaetzlich gegen den gemerkten Stand gefiltert.
    """
    status, daten = imap.uid("SEARCH", None, f"UID {letzte_uid + 1}:*")
    if status != "OK" or not daten or not daten[0]:
        return []
    uids = [int(teil) for teil in daten[0].split()]
    return sorted(uid for uid in uids if uid > letzte_uid)


def mail_laden(imap: imaplib.IMAP4_SSL, uid: int) -> bytes | None:
    """Holt die Rohnachricht (BODY.PEEK, damit \\Seen unangetastet bleibt)."""
    status, daten = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if status != "OK" or not daten:
        return None
    for teil in daten:
        if isinstance(teil, tuple) and len(teil) > 1 and teil[1]:
            return teil[1]
    return None


# ---------------------------------------------------------------------------
# Anhaenge
# ---------------------------------------------------------------------------


def anhaenge_sammeln(msg: Message) -> list[tuple[str, bytes]]:
    """Liefert (Dateiname, Inhalt) aller Anhaenge der Nachricht."""
    ergebnis: list[tuple[str, bytes]] = []
    if not msg.is_multipart():
        return ergebnis

    zaehler = 0
    for teil in msg.walk():
        if teil.get_content_maintype() == "multipart":
            continue
        disposition = (teil.get_content_disposition() or "").lower()
        rohname = teil.get_filename()
        if disposition != "attachment" and not rohname:
            continue
        try:
            inhalt = teil.get_payload(decode=True)
        except Exception as fehler:
            log.warning("Anhang konnte nicht dekodiert werden: %s", fehler)
            continue
        if not inhalt:
            continue

        zaehler += 1
        name = saeubern(header_dekodieren(rohname), 60) if rohname else ""
        if not name or name == "ohne":
            endung = teil.get_content_subtype() or "bin"
            name = f"anhang{zaehler}.{saeubern(endung, 10)}"
        ergebnis.append((name, inhalt))
    return ergebnis


# ---------------------------------------------------------------------------
# Hauptlauf
# ---------------------------------------------------------------------------


def mail_sichern(
    ablage: DriveAblage,
    cfg: Konfig,
    roh: bytes,
    uid: int,
    ordner: str,
    unterordner: str | None,
) -> None:
    """Legt eine einzelne Mail als .eml (und optional deren Anhaenge) in Drive ab."""
    msg = email.message_from_bytes(roh)

    dt = datum_ermitteln(msg)
    # Bei Gesendetem ist der Absender immer man selbst — dort ist der Empfaenger
    # die nuetzlichere Information fuer den Dateinamen.
    if unterordner == "Gesendet":
        adresse = empfaenger_ermitteln(msg)
    else:
        adresse = absender_ermitteln(msg)
    betreff = header_dekodieren(msg.get("Subject")) or "ohne Betreff"
    name = dateiname_bauen(dt, adresse, betreff)

    monats_id = ablage.monatsordner(dt, unterordner)
    datei_id = ablage.hochladen(monats_id, name, roh, "message/rfc822")
    if datei_id:
        log.info("[%s/UID %d] gesichert: %s", ordner, uid, name)
    else:
        log.info("[%s/UID %d] lag schon in Drive: %s", ordner, uid, name)

    if not cfg.save_attachments:
        return

    anhaenge = anhaenge_sammeln(msg)
    if not anhaenge:
        return

    anhang_id = ablage.ordner_holen(ANHANG_ORDNER, monats_id)
    praefix = name[: -len(".eml")]
    for anhang_name, inhalt in anhaenge:
        ziel = f"{praefix}__{anhang_name}"
        if len(ziel) > 200:  # Drive erlaubt mehr, aber lange Namen sind unpraktisch
            ziel = ziel[:200]
        try:
            ablage.hochladen(anhang_id, ziel, inhalt, "application/octet-stream")
        except Exception as fehler:
            log.warning("Anhang '%s' konnte nicht abgelegt werden: %s", ziel, fehler)


def ordner_verarbeiten(
    imap: imaplib.IMAP4_SSL,
    ablage: DriveAblage,
    cfg: Konfig,
    zustand: dict,
    ordner: str,
    unterordner: str | None,
    budget: int,
) -> int:
    """
    Sichert bis zu 'budget' Mails aus einem IMAP-Ordner.
    Rueckgabe: Anzahl der tatsaechlich verarbeiteten Mails.
    """
    uidvalidity = ordner_waehlen(imap, ordner)
    if uidvalidity is None:
        return 0

    eintrag = zustand["folders"].get(ordner, {})
    alte_validity = eintrag.get("uidvalidity")
    letzte_uid = int(eintrag.get("last_uid", 0))

    if alte_validity is not None and int(alte_validity) != uidvalidity:
        log.warning(
            "UIDVALIDITY von '%s' hat sich geaendert (%s -> %s) — "
            "der Ordner wird komplett neu durchlaufen.",
            ordner, alte_validity, uidvalidity,
        )
        letzte_uid = 0

    # Zustand sofort auf die aktuelle UIDVALIDITY heben, damit ein Abbruch
    # nicht dazu fuehrt, dass wir beim naechsten Lauf wieder den alten Wert sehen.
    eintrag = {"uidvalidity": uidvalidity, "last_uid": letzte_uid}
    zustand["folders"][ordner] = eintrag

    uids = neue_uids(imap, letzte_uid)
    if not uids:
        log.info("[%s] nichts Neues (letzte UID %d).", ordner, letzte_uid)
        return 0

    log.info(
        "[%s] %d neue Mail(s) ab UID %d, Ablage in '%s', Budget: %d.",
        ordner, len(uids), letzte_uid + 1, unterordner or "<Wurzel>", budget,
    )

    verarbeitet = 0
    for uid in uids:
        if verarbeitet >= budget:
            log.info("[%s] Budget aufgebraucht — Rest folgt im naechsten Lauf.", ordner)
            break
        try:
            roh = mail_laden(imap, uid)
            if roh is None:
                log.warning("[%s/UID %d] konnte nicht geladen werden — uebersprungen.",
                            ordner, uid)
            else:
                mail_sichern(ablage, cfg, roh, uid, ordner, unterordner)
        except Exception as fehler:
            # Eine einzelne kaputte Mail darf den Lauf nicht killen. Die UID wird
            # trotzdem als erledigt vermerkt, sonst haengt der Ordner ewig daran.
            log.error("[%s/UID %d] Fehler, wird uebersprungen: %s",
                      ordner, uid, fehler, exc_info=False)

        eintrag["last_uid"] = uid
        verarbeitet += 1

        if verarbeitet % STATE_INTERVALL == 0:
            try:
                ablage.zustand_speichern(zustand)
                log.info("[%s] Zwischenstand gespeichert (UID %d).", ordner, uid)
            except Exception as fehler:
                log.warning("Zwischenstand konnte nicht gespeichert werden: %s", fehler)

    return verarbeitet


def archivieren(cfg: Konfig) -> int:
    cfg.pruefen()

    dienst = drive_dienst(cfg)
    ablage = DriveAblage(dienst, cfg.drive_root_id)
    zustand = ablage.zustand_laden()

    imap = imap_verbinden(cfg)
    gesamt = 0
    try:
        vorhandene_ordner_loggen(imap)
        for ordner, unterordner in cfg.imap_folders:
            rest = cfg.max_per_run - gesamt
            if rest <= 0:
                log.info(
                    "Obergrenze von %d Mails erreicht — beende sauber, "
                    "der naechste Lauf macht weiter.", cfg.max_per_run,
                )
                break
            gesamt += ordner_verarbeiten(
                imap, ablage, cfg, zustand, ordner, unterordner, rest
            )
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception as fehler:
            log.warning("IMAP-Abmeldung fehlgeschlagen: %s", fehler)
        try:
            ablage.zustand_speichern(zustand)
            log.info("Zustand final gespeichert.")
        except Exception as fehler:
            log.error("Zustand konnte NICHT gespeichert werden: %s", fehler)

    log.info("Fertig. %d Mail(s) in diesem Lauf verarbeitet.", gesamt)
    return gesamt


# ---------------------------------------------------------------------------
# Einmalige lokale Autorisierung
# ---------------------------------------------------------------------------


def auth_flow() -> None:
    """
    Startet lokal den Browser-Login und gibt die drei Werte aus, die spaeter
    als GitHub-Secrets hinterlegt werden.

    Voraussetzung: 'client_secret.json' eines OAuth-Clients vom Typ
    'Desktop-App' liegt im aktuellen Verzeichnis.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    pfad = os.environ.get("CLIENT_SECRET_FILE", "client_secret.json")
    if not os.path.exists(pfad):
        raise SystemExit(
            f"Abbruch: '{pfad}' nicht gefunden. In der Google Cloud Console einen "
            "OAuth-Client vom Typ 'Desktop-App' anlegen, die JSON-Datei "
            "herunterladen und hier ablegen."
        )

    flow = InstalledAppFlow.from_client_secrets_file(pfad, SCOPES)
    # access_type=offline + prompt=consent erzwingen die Ausgabe eines
    # Refresh-Tokens, auch wenn schon einmal zugestimmt wurde.
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent"
    )

    if not creds.refresh_token:
        raise SystemExit(
            "Abbruch: Google hat kein Refresh-Token geliefert. Zugriff unter "
            "https://myaccount.google.com/permissions entziehen und erneut "
            "'--auth' ausfuehren."
        )

    print("\n" + "=" * 72)
    print("Diese drei Werte als GitHub-Secrets im Repository hinterlegen:")
    print("=" * 72)
    konfig = getattr(flow, "client_config", {}) or {}
    print(f"GOOGLE_CLIENT_ID     = {creds.client_id or konfig.get('client_id', '')}")
    print(
        f"GOOGLE_CLIENT_SECRET = "
        f"{creds.client_secret or konfig.get('client_secret', '')}"
    )
    print(f"GOOGLE_REFRESH_TOKEN = {creds.refresh_token}")
    print("=" * 72)
    print("Achtung: das Refresh-Token ist ein Passwort — nicht ins Repo committen.\n")


# ---------------------------------------------------------------------------


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if "--auth" in sys.argv[1:]:
        auth_flow()
        return 0

    try:
        archivieren(Konfig())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        log.warning("Abbruch durch Benutzer.")
        return 130
    except Exception as fehler:
        log.error("Lauf abgebrochen: %s", fehler, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
