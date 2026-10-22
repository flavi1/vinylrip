# SPDX-License-Identifier: GPL-3.0-or-later
"""What to do after a failure: what was kept, how to resume, how to finish by hand.

The text is built from the stage that failed and the job's real paths and
parameters, in French or English (``LC_ALL``, ``LC_MESSAGES`` or ``LANG``).
Front-ends (the Kodi add-on, disc-launcher) show it as is.
"""
from __future__ import annotations

import os

STAGES = ("open", "record", "split", "identify", "encode", "verify", "convert", "archive", "file")

# Audacity, « Fichier > Importer > Données brutes… » : encodage de chaque format.
AUDACITY_ENCODING = {
    "S16_LE": ("Signed 16-bit PCM", "PCM 16 bits signé"),
    "S24_3LE": ("Signed 24-bit PCM", "PCM 24 bits signé"),
    "S32_LE": ("Signed 32-bit PCM", "PCM 32 bits signé"),
}


def french() -> bool:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var, "")
        if v:
            return v.lower().startswith("fr")
    return False


def _raw_lines(job, fr: bool) -> list[str]:
    if job is None or not job.faces:
        return []
    p = job.params
    enc = AUDACITY_ENCODING.get(p.fmt, (p.fmt, p.fmt))[1 if fr else 0]
    files = ", ".join(os.path.basename(f["raw"]) for f in job.faces)
    if fr:
        return [
            f"Enregistrements bruts conservés dans {job.dir} : {files}.",
            "Pour les ouvrir dans Audacity : Fichier > Importer > Données brutes, "
            f"encodage « {enc} », little-endian, {p.channels} canaux, {p.rate} Hz.",
        ]
    return [
        f"Raw recordings kept in {job.dir}: {files}.",
        "To open them in Audacity: File > Import > Raw Data, "
        f"encoding \"{enc}\", little-endian, {p.channels} channels, {p.rate} Hz.",
    ]


def help_text(stage: str | None, job=None, kept: list[str] | None = None, message: str = "") -> str:
    """Multi-line explanation for a failure at `stage`."""
    fr = french()
    kept = [k for k in (kept or []) if k]
    resume = f"vinylrip process {job.dir}" if job is not None else ""
    out: list[str] = []
    if message:
        out.append(("Erreur : " if fr else "Error: ") + message)
        out.append("")
    if stage == "open" or job is None:
        out.append("Rien n'a été enregistré. Corrigez la cause (platine branchée et libre, espace disque) "
                   "puis recommencez." if fr else
                   "Nothing was recorded. Fix the cause (turntable plugged in and free, disk space) and start again.")
        return "\n".join(out)
    if stage == "record":
        if fr:
            out.append("L'enregistrement de la face en cours s'est interrompu ; les faces déjà terminées sont intactes.")
            out.append("Vous pouvez réenregistrer cette face, ou traiter ce qui a été enregistré :")
        else:
            out.append("Recording of the current face stopped; faces already finished are intact.")
            out.append("You can record that face again, or process what was recorded:")
        out.append(f"  {resume}")
        out.extend(_raw_lines(job, fr))
        return "\n".join(out)
    if stage in ("split", "identify"):
        if fr:
            out.append("Les enregistrements sont intacts. Une fois la cause corrigée, reprenez le traitement :")
        else:
            out.append("The recordings are untouched. Once the cause is fixed, resume processing:")
        out.append(f"  {resume}")
        out.append("")
        out.append("Ou terminez à la main : découpez dans Audacity, puis étiquetez avec MusicBrainz Picard."
                   if fr else "Or finish by hand: cut in Audacity, then tag with MusicBrainz Picard.")
        out.extend(_raw_lines(job, fr))
        return "\n".join(out)
    if stage in ("encode", "verify", "archive"):
        if fr:
            out.append("Aucun fichier n'a été rangé ; les enregistrements bruts sont intacts.")
            if kept:
                out.append(f"Travail partiel (à supprimer, ou à inspecter) : {', '.join(kept)}.")
            if stage == "verify":
                out.append("La vérification bit à bit a échoué : vérifiez la version de flac (≥ 1.4) et l'espace "
                           "disque, puis reprenez :")
            else:
                out.append("Vérifiez flac / ffmpeg et l'espace disque, puis reprenez :")
        else:
            out.append("Nothing was filed; the raw recordings are untouched.")
            if kept:
                out.append(f"Partial work (delete or inspect it): {', '.join(kept)}.")
            if stage == "verify":
                out.append("Bit-exact verification failed: check the flac version (>= 1.4) and disk space, then resume:")
            else:
                out.append("Check flac / ffmpeg and disk space, then resume:")
        out.append(f"  {resume}")
        out.extend(_raw_lines(job, fr))
        return "\n".join(out)
    if stage == "convert":
        if fr:
            out.append("Les maîtres FLAC, vérifiés bit à bit, sont prêts"
                       + (f" dans {', '.join(kept)}" if kept else "") + ".")
            out.append("Seule la version réduite a échoué : utilisez les maîtres tels quels, "
                       "ou corrigez la cause et reprenez :")
        else:
            out.append("The FLAC masters, verified bit for bit, are ready"
                       + (f" in {', '.join(kept)}" if kept else "") + ".")
            out.append("Only the reduced copy failed: use the masters as they are, or fix the cause and resume:")
        out.append(f"  {resume}")
        return "\n".join(out)
    if stage == "file":
        if fr:
            out.append("Les pistes sont encodées et vérifiées, mais n'ont pas pu être rangées "
                       "(rien n'est jamais écrasé)" + (f" ; elles sont dans {', '.join(kept)}" if kept else "") + ".")
            out.append("Déplacez ou renommez l'album existant (ou changez le gabarit de nom), puis reprenez :")
        else:
            out.append("The tracks are encoded and verified but could not be filed (nothing is ever overwritten)"
                       + (f"; they are in {', '.join(kept)}" if kept else "") + ".")
            out.append("Move or rename the existing album (or change the naming template), then resume:")
        out.append(f"  {resume}")
        return "\n".join(out)
    out.append(("Reprendre : " if fr else "Resume: ") + resume)
    out.extend(_raw_lines(job, fr))
    return "\n".join(out)


def attention_text(result: dict, job=None) -> str:
    """After a successful run that still needs checking (faces « à vérifier », no identification)."""
    fr = french()
    out: list[str] = []
    to_check = result.get("faces_to_check") or []
    if to_check:
        faces = ", ".join(to_check)
        if fr:
            out.append(f"Face(s) {faces} : découpage non confirmé par l'album identifié ; écoutez les coupures.")
            out.append("Si une piste est mal coupée : corrigez dans Audacity, ou ajustez cuts_X.json du job puis reprenez :")
        else:
            out.append(f"Face(s) {faces}: cuts not confirmed by the identified release; listen to the cuts.")
            out.append("If a track is badly cut: fix it in Audacity, or adjust the job's cuts_X.json and resume:")
        if job is not None:
            out.append(f"  vinylrip process {job.dir}")
            out.extend(_raw_lines(job, fr))
    if not result.get("release"):
        out.append("Album non identifié : étiquetez-le avec MusicBrainz Picard." if fr else
                   "Album not identified: tag it with MusicBrainz Picard.")
    return "\n".join(out)
