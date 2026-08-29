# Wikipedia-artikler som PDF

Dette program henter Wikipedia-artikler via Wikipedias REST API og gemmer dem som læsevenlige PDF-filer til offline brug. API-svar caches lokalt i `.wikipedia-cache/`; cachefiler ældre end én måned slettes ved opstart.

## Installation

Programmet kræver Python 3.10 eller nyere. Installer Python-afhængighederne fra projektmappen:

```cmd
python -m pip install -r requirements.txt
```

PDF-genereringen bruger `fpdf2` og kræver ingen separat runtime.

## Opret inputfilen

Inputfilen skal ligge uden for projektet. Standardnavnet kan være `artikler.txt`; filen er med vilje ignoreret af Git.

Formatet er en overskrift med kolon efterfulgt af Wikipedia-links:

```text
Matematik:
https://da.wikipedia.org/wiki/Matematik
https://da.wikipedia.org/wiki/Line%C3%A6r_funktion

Fysik:
https://da.wikipedia.org/wiki/Fysik
```

Regler:

- Hvert emne skal afsluttes med et kolon, for eksempel `Matematik:`.
- Hvert link skal stå på sin egen linje.
- Tomme linjer og linjer, der begynder med `#`, ignoreres.
- Wikipedia-links må gerne indeholde danske tegn som `æ`, `ø` og `å`.
- Kun almindelige Wikipedia-artikler følges. Kategorier, filer, skabeloner, diskussionssider og special-sider ignoreres.

## Kør programmet

Kør programmet fra projektmappen og angiv inputfilen:

```cmd
python generate.py sti\til\artikler.txt
```

Eksempel:

```cmd
python generate.py C:\Users\DitNavn\Dokumenter\artikler.txt
```

Programmet downloader først artiklerne fra inputfilen og derefter Wikipedia-artikler, der er linket direkte fra disse artikler. Der følges kun ét niveau af links; links fra de nyfundne artikler downloades ikke.

## Resultat

Resultatet placeres som standard i mappen `Noter`:

```text
Noter/
├── Matematik/
│   ├── Matematik.lnk
│   └── Lineær funktion.lnk
├── Fysik/
│   └── Fysik.lnk
└── artikler/
    ├── Matematik.pdf
    ├── Lineær funktion.pdf
    └── Fysik.pdf
```

- De faktiske PDF-filer ligger i `Noter/artikler/`.
- På Windows oprettes `.lnk`-genveje i emnemapperne.
- På macOS og Linux oprettes symbolske links.
- Der oprettes kun genveje til links, der står direkte i inputfilen.
- Links inde i PDF-filer peger på lokale PDF-filer med relative stier, hvis artiklen er downloadet.
- Links til artikler, der ikke er downloadet, samt links til andre hjemmesider, vises som almindelig tekst, så materialet fungerer offline.

## Valgfri indstillinger

Vælg en anden outputmappe:

```cmd
python generate.py artikler.txt --output MitNoter
```

Styr renderingshastigheden med antal samtidige PDF-renderinger:

```cmd
python generate.py artikler.txt --workers 2
```

Programmet venter ét sekund mellem downloads som standard for at begrænse request-hastigheden. Forsinkelsen kan ændres efter behov:

```cmd
python generate.py artikler.txt --request-delay 1
```

Sammenlign hastigheden ved at køre samme input med for eksempel `--workers 1`, `--workers 2` og `--workers 4`. Sammenlign den samlede køretid og hold øje med fejl eller højt RAM-forbrug; vælg den hurtigste stabile indstilling.

