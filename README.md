# vinylrip

Numérisation de disques vinyle sous Linux, en ligne de commande, sans perte :
enregistrement du PCM brut depuis une platine USB, découpage des pistes,
identification en ligne, correction du découpage d'après l'album identifié,
rognage de l'amorce et du sillon de sortie, puis FLAC **vérifié bit à bit**.

Ce moteur est indépendant de Kodi. Le plugin Kodi
([vinylrip-kodi](https://github.com/flavi1/vinylrip-kodi)) n'est qu'une interface qui le pilote.

> **État : v0.1, testé sur signaux synthétiques.** La chaîne complète (découpage, alignement,
> encodage, vérification, conversion, rangement) est couverte par 100 tests automatiques.
> Non encore validé sur une vraie platine : la capture `arecord` sans rééchantillonnage,
> le taux de reconnaissance AcoustID sur des enregistrements de vinyle et l'API des pochettes
> restent à mesurer (voir « Limites »).

## Principe

1. **Capture** : `arecord` en accès `hw:` direct, PCM brut (`S16_LE`, `S24_3LE` ou `S32_LE`),
   un fichier par face, avec un fichier `.json` voisin (rythme, format, dépassements de tampon).
   Les qualités proposées viennent de ce que la carte annonce réellement.
2. **Passe 1, découpage par silences** : niveau robuste par 0,1 s (les craquements ne masquent pas
   les silences), plancher de bruit estimé sur la face elle-même, coupures dans les silences d'au
   moins 1 s, recollage des segments courts ne contenant que du bruit, rognage du début
   (−1 s avant la première musique) et de la fin (+3 s après la dernière).
3. **Identification** : empreintes Chromaprint → AcoustID → éditions MusicBrainz, vote sur
   l'album (pas piste par piste), puis score combiné avec la concordance des durées.
   Mode automatique, ou choix manuel parmi les candidats.
4. **Passe 2, alignement** : programmation dynamique qui choisit, parmi les silences détectés, les
   coupures dont les durées collent le mieux à celles de l'édition (avec un facteur de vitesse de
   ±3 %). Un silence au milieu d'un morceau est recollé ; un enchaînement sans pause est coupé.
   En cas de doute, le découpage de la passe 1 est conservé et la face est marquée « à vérifier ».
5. **Encodage** : FLAC maître, coupes exactes à l'échantillon, sans rééchantillonnage ni dither.
   Chaque piste est décodée et comparée (SHA-256 + nombre d'échantillons) au segment brut.
6. **Version réduite (option)** : par exemple 96 kHz / 24 bits dérivée du maître vérifié. Elle est
   contrôlée par mesure (nombre d'échantillons, niveau, écrêtage), pas bit à bit : une conversion
   ne se renverse pas.
7. **Rangement** : gabarit de nom paramétrable, étiquettes, pochette, écriture dans un dossier
   temporaire puis déplacement d'un seul geste. Un album existant n'est jamais écrasé.
   Le brut n'est supprimé que si tout est vérifié et qu'aucune face n'est « à vérifier ».

## Installation

Dépendances système : `alsa-utils` (arecord, aplay), `ffmpeg`, `flac` ≥ 1.4 (recommandé, requis
pour un FLAC 32 bits natif), `libchromaprint-tools` (fpcalc). Python ≥ 3.10 et numpy.

```sh
sudo apt install alsa-utils ffmpeg flac libchromaprint-tools python3-numpy
# Debian/Ubuntu récents (PEP 668) : pip refuse d'installer hors d'un environnement virtuel.
sudo apt install python3-venv python3-numpy
python3 -m venv --system-site-packages ~/.local/share/vinylrip-venv
~/.local/share/vinylrip-venv/bin/pip install .
sudo ln -s ~/.local/share/vinylrip-venv/bin/vinylrip /usr/local/bin/vinylrip   # facultatif
# ou, sans rien installer : PYTHONPATH=src python3 -m vinylrip …
```

Sans `flac` ≥ 1.4, le moteur se rabat sur ffmpeg (avec `-strict experimental`, sans lequel
ffmpeg 6.x encode silencieusement le 32 bits en 24 bits : la vérification bit à bit l'a révélé).

## Utilisation

```sh
vinylrip devices --qualities          # cartes de capture et qualités offertes
vinylrip record --device Device       # enregistre 2 faces puis traite
vinylrip record --discs 2 --faces 4   # album à plusieurs disques
vinylrip record --listen              # écoute pendant la prise (via aplay)
vinylrip record --no-process          # enregistre seulement
vinylrip process ~/.cache/vinylrip/jobs/<id>   # traite ou reprend un job
vinylrip cuts face_a.pcm              # affiche le plan de découpage (debug)
vinylrip name "{album}/{piste:02d}. {titre}.flac"   # aperçu d'un gabarit
vinylrip --json process <job>         # progression en lignes JSON (pour le plugin Kodi)
vinylrip listen --http                # écoute servie en flux WAV sur 127.0.0.1 (pour Kodi), au lieu d'aplay
```

Chaque session est un *job* (`job.json`, plans de coupe `cuts_X.json`) : une erreur ne supprime
jamais rien, et `process` reprend le travail. En cas d'échec, vinylrip explique, selon l'étape
(enregistrement, découpage, identification, encodage, vérification, conversion, rangement), ce qui
a été conservé et où, la commande qui reprend le job, et comment terminer à la main : réglages
exacts pour importer les faces brutes dans Audacity (Fichier > Importer > Données brutes),
puis étiquetage avec MusicBrainz Picard. Ces explications sont en français ou en anglais selon
`LANG`, et figurent aussi dans les événements JSON (`help`, `resume`).

## Configuration

`~/.config/vinylrip/config.toml` (voir `config.example.toml`). Gabarit par défaut :
`{artiste_album}/{annee} - {album}/{piste:02d}. {titre}.flac`, et pour plusieurs disques
`…/CD{disque}/…`. Champs : `artiste`, `artiste_album`, `album`, `annee`, `piste`, `titre`, `face`,
`disque`, `nb_disques`, `nb_pistes`, `type`. Le dossier de sortie par défaut est
`xdg-user-dir MUSIC` (`~/Musique` sur un système en français).

Les services en ligne exigent une **clé d'application AcoustID** (gratuite, `acoustid_key`) et un
contact dans le User-Agent MusicBrainz (`contact`) ; MusicBrainz est limité à une requête par seconde.

## Tests

```sh
PYTHONPATH=src:tests python3 -m unittest discover -s tests -v    # ou : pytest
```

Les tests utilisent des disques synthétiques (amorce, craquements, silences, sillon de sortie) et
ffmpeg. Aucun accès réseau ni matériel n'est nécessaire.

## Limites connues

- Non implémenté : pochettes Discogs (seul le Cover Art Archive l'est), identification Shazam.
- `S24_LE` (24 bits dans 32) n'est pas géré : seules `S16_LE`, `S24_3LE` et `S32_LE` le sont.
- Si PipeWire ou PulseAudio tient la carte, l'accès `hw:` échoue ; la libération de la carte est
  à écrire une fois testée sur du vrai matériel.
- Les seuils de découpage et d'identification sont des valeurs de départ, à régler sur des disques réels.

## Licence

GPL-3.0-or-later. Voir `LICENSE`.

## Session pilotée par un autre programme

`vinylrip --json session [--device hw:…] [--quality 192000:S32_LE:2] [--discs N]` ouvre une session
d'enregistrement pilotée en JSON-lignes (commandes sur stdin, événements sur stdout) ; c'est
l'interface utilisée par le plugin Kodi et par disc-launcher. Voir `src/vinylrip/session.py` pour le
protocole. `{"cmd": "listen", "on": true, "http": true}` sert l'écoute en flux WAV (l'événement
`listening` en donne l'`url`) : un lecteur qui tient déjà la carte son, comme Kodi, la joue avec sa
propre sortie audio.
`vinylrip --json devices --qualities` liste les cartes et les qualités proposées.
