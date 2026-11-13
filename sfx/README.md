Free sound effects for reels, used with `./reels sfx`. Human-made only, CC0 or Pixabay Content License,
with source page, author and license recorded in each <slug>.json. Nothing here is AI-generated.

    ./reels sfx list [--category whoosh]
    ./reels sfx add <file-or-url> --name whoosh-short --category whoosh --license CC0-1.0 --source <page-url> --author "<name>" [--trim 0.1,1.4] [--tags a,b]
    ./reels sfx add --from <asset-id> --at 12.5 --for 1.2 --name bike-chain [--clean]   (cut from your own footage; --clean = highpass + denoise for wind)

Every sound is 48 kHz 16-bit WAV with leading/trailing silence trimmed and a short fade, and sits at a predictable
level so they mix evenly: sounds of 1.5 s or more are set to -20 LUFS integrated (gain reduced if that would pass
a -1.5 dBTP true-peak ceiling); shorter ones can't be measured reliably, so they are peak-normalized to -6 dBFS.
Sounds cut from your own footage (category nat) are normalized the same way but not silence-trimmed.
Reels mix them in at config `sfx.gain` (default 0.7).

| slug | category | length | LUFS | peak | license | author | source |
|---|---|---|---|---|---|---|---|
| whoosh-long | whoosh | 1.39s | -20.0 | -6.0 | CC0-1.0 | ch_ase | https://freesound.org/s/423799/ |
| whoosh-medium | whoosh | 0.70s | -22.6 | -6.0 | CC0-1.0 | DJT4NN3R | https://freesound.org/s/449996/ |
| whoosh-short | whoosh | 0.15s |  | -6.0 | CC0-1.0 | 1bob | https://freesound.org/s/831936/ |
| swish-whip | swish | 0.17s |  | -6.0 | CC0-1.0 | JW_Audio | https://freesound.org/s/838766/ |
| riser-1-5s | riser | 1.51s | -20.0 | -1.8 | CC0-1.0 | dadudmc | https://freesound.org/s/757504/ |
| riser-3s | riser | 2.77s | -20.0 | -8.7 | CC0-1.0 | syntheffects | https://freesound.org/s/685256/ |
| hit-big | hit | 0.69s | -19.4 | -5.6 | CC0-1.0 | cabled_mess | https://freesound.org/s/350978/ |
| hit-nail | hit | 0.34s |  | -6.0 | CC0-1.0 | aidansamuel | https://freesound.org/s/540139/ |
| hit-soft | hit | 0.26s |  | -5.9 | CC0-1.0 | egomassive | https://freesound.org/s/536768/ |
| pop-soft | pop | 0.22s |  | -6.0 | CC0-1.0 | deraj | https://freesound.org/s/202230/ |
| pop-tight | pop | 0.08s |  | -6.0 | CC0-1.0 | quatricise | https://freesound.org/s/789793/ |
| click-tick | click | 0.15s |  | -5.3 | CC0-1.0 | Pixeliota | https://freesound.org/s/678248/ |
| shutter-dslr | shutter | 0.27s |  | -4.7 | CC0-1.0 | ristooooo1 | https://freesound.org/s/539136/ |
| bell-bike | bell | 1.40s | -17.6 | -6.0 | CC0-1.0 | gis_sweden | https://freesound.org/s/536554/ |
| scratch-record | scratch | 0.30s |  | -6.0 | CC0-1.0 | jameswrowles | https://freesound.org/s/274129/ |
| stop-vinyl | stop | 0.52s | -18.5 | -6.0 | CC0-1.0 | loganzsound | https://freesound.org/s/842582/ |
| creak-wood | other | 0.83s | -20.0 | -6.0 | CC0-1.0 | Rudmer_Rotteveel | https://freesound.org/s/506665/ |
| sting-showdown | other | 2.89s | -20.0 | -3.2 | CC0-1.0 | Banzai_Bonsai | https://freesound.org/s/447580/ |
| sting-slide-down | other | 3.00s | -20.0 | -9.3 | CC0-1.0 | MinigunFiend | https://freesound.org/s/175166/ |
