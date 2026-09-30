# Wikipedia-artikler som PDF

Programmet henter artikler fra registrerede Wikipedia- og Wikimedia-wikier via deres REST API'er og gemmer dem som læsevenlige PDF-filer til offline brug. Behandlede artikler og permanente fejl caches lokalt i `.article-cache/`. Billeder caches lokalt i `.article-media-cache/`.

## Installation

Programmet kræver Python 3.10 eller nyere. Installer afhængighederne:

```cmd
python -m pip install -r requirements.txt
```

PDF-genereringen bruger den fastlåste `fpdf2`-version i `requirements.txt`. Matematiske formler hentes ikke som SVG-billeder fra Wikimedia: i stedet gengives LaTeX-koden lokalt til PNG via `matplotlib` under PDF-genereringen (med læsbar tekst som fallback).

## Kør fra kommandolinjen

```cmd
python cli.py artikler.txt
```

Valgfrie indstillinger:

```cmd
python cli.py artikler.txt --output MitNoter --workers 2 --request-delay 1
```

Artikel-downloads bruger en global Wikimedia-begrænsning, og billed-downloads bruger samme begrænsning. Der køres som standard højst fire samtidige PDF-renderinger/download-relaterede jobs (begrænset af CPU-antallet) for at holde belastningen lav. Eksisterende PDF-filer genbruges automatisk, når titel og indhold ikke har ændret sig. Cache hits udløser ingen netværksventetid, heller ikke for tidligere permanente fejl. Wikimedia-svar med 429/503 respekterer `Retry-After` og bruger exponential backoff, hvis headeren mangler. Links fra de oprindelige artikler følges ét niveau, og lokale PDF-links skrives som relative stier.

## Grafisk brugerflade

Start tkinter-brugerfladen med:

```cmd
python gui.py
```

GUI'en lader dig vælge inputfil, outputmappe, antal PDF-workers og request delay. Genereringen kører i en baggrundstråd, så vinduet forbliver responsivt.

## Inputfil

Inputfilen består af en overskrift med kolon efterfulgt af Wikipedia-links:

```text
Matematik:
https://da.wikipedia.org/wiki/Matematik
https://da.wikipedia.org/wiki/Line%C3%A6r_funktion

Fysik:
https://da.wikipedia.org/wiki/Fysik
```

Tomme linjer og kommentarer, der begynder med `#`, ignoreres. Kun almindelige Wikipedia-artikler følges; kategorier, filer, skabeloner, diskussionssider og special-sider ignoreres.

## Resultat

```text
Noter/
├── Matematik/
│   ├── Matematik.lnk
│   └── Lineær funktion.lnk
└── artikler/
    ├── Matematik.pdf
    └── Lineær funktion.pdf
```

De faktiske PDF-filer ligger i `Noter/artikler/`. På Windows oprettes `.lnk`-genveje; på macOS og Linux oprettes symbolske links. Links til downloadede artikler i PDF'erne er relative og afhænger derfor ikke af den oprindelige computerplacering. Links til ikke-downloadede artikler og eksterne websteder vises som almindelig tekst.

## Kildekode

```text
cli.py                 # Kommandolinje-entrypoint
 gui.py                # tkinter-entrypoint
src/
├── app.py             # Fælles genereringsworkflow
├── articles.py        # Artikel-hentning og cache (site-agnostisk)
├── config.py          # Konstanter og cacheindstillinger
├── html_processing.py # HTML, links og billeder
├── input.py           # Inputfil-parser
├── math_render.py     # Lokal LaTeX→PNG-gengivelse af matematik
├── pdf_renderer.py    # fpdf2-rendering
├── shortcuts.py       # Windows-genveje og Unix-links
├── utils.py           # Fælles hjælpefunktioner
├── rate_limit.py      # Fælles request-begrænsning
└── sites/             # Site-adaptere (én fil per side)
    ├── registry.py    # Site-beskrivelse og registrering
    ├── wikipedia.py   # Registrerer wikipedia.org
    └── wikimedia.py   # Registrerer øvrige Wikimedia-wikier
```

Hver side-fil i `src/sites/` registrerer sine domæner og hvordan de håndteres. Domæner uden registreret adapter ignoreres. Nye sider tilføjes ved at droppe en ny fil i `src/sites/`.
