"""Build the bundled DACH gazetteer from GeoNames country snapshots."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen
from zipfile import ZipFile

BASE_URL = "https://download.geonames.org/export/dump/"
COUNTRIES = ("DE", "AT", "CH")
EXCLUDED_FEATURES = {"PPLH", "PPLQ", "PPLW", "PPLCH", "PPLS"}
NON_LANGUAGE_CODES = {"abbr", "iata", "icao", "faac", "post", "link", "wkdt"}

# GeoNames uses FIPS admin1 codes in DE/AT and canton codes in CH.
REGIONS = {
    "DE.01": ("Baden-W\u00fcrttemberg", "BW", "DE-BW"),
    "DE.02": ("Bayern", "BY", "DE-BY"),
    "DE.03": ("Bremen", "HB", "DE-HB"),
    "DE.04": ("Hamburg", "HH", "DE-HH"),
    "DE.05": ("Hessen", "HE", "DE-HE"),
    "DE.06": ("Niedersachsen", "NI", "DE-NI"),
    "DE.07": ("Nordrhein-Westfalen", "NW", "NRW", "DE-NW"),
    "DE.08": ("Rheinland-Pfalz", "RP", "DE-RP"),
    "DE.09": ("Saarland", "SL", "DE-SL"),
    "DE.10": ("Schleswig-Holstein", "SH", "DE-SH"),
    "DE.11": ("Brandenburg", "BB", "DE-BB"),
    "DE.12": ("Mecklenburg-Vorpommern", "MV", "DE-MV"),
    "DE.13": ("Sachsen", "SN", "DE-SN"),
    "DE.14": ("Sachsen-Anhalt", "ST", "DE-ST"),
    "DE.15": ("Th\u00fcringen", "TH", "DE-TH"),
    "DE.16": ("Berlin", "BE", "DE-BE"),
    "AT.01": ("Burgenland", "B", "AT-1"),
    "AT.02": ("K\u00e4rnten", "K", "AT-2"),
    "AT.03": ("Nieder\u00f6sterreich", "N\u00d6", "NOE", "AT-3"),
    "AT.04": ("Ober\u00f6sterreich", "O\u00d6", "OOE", "AT-4"),
    "AT.05": ("Salzburg", "S", "AT-5"),
    "AT.06": ("Steiermark", "ST", "STMK", "AT-6"),
    "AT.07": ("Tirol", "T", "AT-7"),
    "AT.08": ("Vorarlberg", "V", "VBG", "AT-8"),
    "AT.09": ("Wien", "W", "AT-9"),
    "CH.AG": ("Aargau", "AG", "CH-AG"),
    "CH.AI": ("Appenzell Innerrhoden", "AI", "CH-AI"),
    "CH.AR": ("Appenzell Ausserrhoden", "AR", "CH-AR"),
    "CH.BE": ("Bern", "BE", "CH-BE"),
    "CH.BL": ("Basel-Landschaft", "BL", "CH-BL"),
    "CH.BS": ("Basel-Stadt", "BS", "CH-BS"),
    "CH.FR": ("Freiburg", "FR", "CH-FR"),
    "CH.GE": ("Genf", "GE", "CH-GE"),
    "CH.GL": ("Glarus", "GL", "CH-GL"),
    "CH.GR": ("Graub\u00fcnden", "GR", "CH-GR"),
    "CH.JU": ("Jura", "JU", "CH-JU"),
    "CH.LU": ("Luzern", "LU", "CH-LU"),
    "CH.NE": ("Neuenburg", "NE", "CH-NE"),
    "CH.NW": ("Nidwalden", "NW", "CH-NW"),
    "CH.OW": ("Obwalden", "OW", "CH-OW"),
    "CH.SG": ("St. Gallen", "SG", "CH-SG"),
    "CH.SH": ("Schaffhausen", "SH", "CH-SH"),
    "CH.SO": ("Solothurn", "SO", "CH-SO"),
    "CH.SZ": ("Schwyz", "SZ", "CH-SZ"),
    "CH.TG": ("Thurgau", "TG", "CH-TG"),
    "CH.TI": ("Tessin", "TI", "CH-TI"),
    "CH.UR": ("Uri", "UR", "CH-UR"),
    "CH.VD": ("Waadt", "VD", "CH-VD"),
    "CH.VS": ("Wallis", "VS", "CH-VS"),
    "CH.ZG": ("Zug", "ZG", "CH-ZG"),
    "CH.ZH": ("Z\u00fcrich", "ZH", "CH-ZH"),
}


def archive_rows(path: Path, country: str):
    with ZipFile(path) as archive:
        with archive.open(f"{country}.txt") as source:
            yield from csv.reader(
                io.TextIOWrapper(source, encoding="utf-8"),
                delimiter="\t",
                quoting=csv.QUOTE_NONE,
            )


def build_country(country: str, places_path: Path, names_path: Path):
    places = {}
    region_ids = {}
    for row in archive_rows(places_path, country):
        if len(row) != 19 or row[8] != country:
            raise ValueError(f"Unexpected GeoNames record in {places_path}")
        region = f"{country}.{row[10]}"
        if row[6] == "A" and row[7] == "ADM1" and region in REGIONS:
            region_ids[int(row[0])] = region
        elif row[6] != "P" or row[7] in EXCLUDED_FEATURES or region not in REGIONS:
            continue
        places[int(row[0])] = {
            "name": row[1],
            "region": region,
            "aliases": {row[1], row[2]} - {""},
            "german": [],
        }

    for row in archive_rows(names_path, country):
        if len(row) < 8:
            raise ValueError(f"Unexpected alternate-name record in {names_path}")
        place = places.get(int(row[1]))
        language = row[2]
        if (
            place is None
            or language in NON_LANGUAGE_CODES
            or (
                language and not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]+)*", language)
            )
            or row[6] == "1"
            or row[7] == "1"
            or not row[3]
        ):
            continue
        name = row[3]
        place["aliases"].add(name)
        if language == "de":
            place["german"].append(
                (row[4] != "1", name != place["name"], row[5] != "1", len(name), name)
            )

    regions = {}
    records = []
    for geoname_id, place in sorted(places.items()):
        if geoname_id in region_ids:
            code = region_ids[geoname_id]
            name, *extra_aliases = REGIONS[code]
            regions[code] = [
                name,
                sorted(place["aliases"] | set(extra_aliases) | {name}),
            ]
        else:
            name = min(place["german"])[-1] if place["german"] else place["name"]
            records.append(
                [geoname_id, place["region"], name, sorted(place["aliases"])]
            )
    expected = {code for code in REGIONS if code.startswith(country + ".")}
    if set(regions) != expected or not records:
        raise ValueError(f"Incomplete country data: {country}")
    return records, regions


def download(url: str, destination: Path):
    request = Request(url, headers={"User-Agent": "JobSpyDACH-location-data/1.0"})
    temporary = destination.with_suffix(".tmp")
    try:
        with urlopen(request, timeout=60) as response, temporary.open("wb") as target:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=root / ".cache" / "geonames")
    parser.add_argument("--output-dir", type=Path, default=root / "jobspy" / "data")
    parser.add_argument(
        "--download", action="store_true", help="Refresh all six source archives"
    )
    args = parser.parse_args()
    args.source_dir.mkdir(parents=True, exist_ok=True)
    sources = []
    records = []
    regions = {}
    for country in COUNTRIES:
        paths = []
        for alternate in (False, True):
            suffix = "-alternatenames" if alternate else ""
            url = f"{BASE_URL}{'alternatenames/' if alternate else ''}{country}.zip"
            path = args.source_dir / f"{country}{suffix}.zip"
            if args.download:
                print(f"Downloading {url}", flush=True)
                download(url, path)
            with ZipFile(path) as archive:
                snapshot = archive.getinfo(f"{country}.txt").date_time
            sources.append(
                {
                    "file": path.name,
                    "url": url,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "snapshot": "%04d-%02d-%02dT%02d:%02d:%02d" % snapshot,
                }
            )
            paths.append(path)
        country_records, country_regions = build_country(country, *paths)
        records.extend(country_records)
        regions.update(country_regions)
        print(
            f"{country}: {len(country_records)} places, {len(country_regions)} regions",
            flush=True,
        )

    payload = {"schema_version": 1, "regions": regions, "places": sorted(records)}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as target:
        target.write(encoded)
    compressed = buffer.getvalue()
    metadata = {
        "schema_version": 1,
        "attribution": "Contains modified GeoNames data (https://www.geonames.org/).",
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "sources": sources,
        "place_count": len(records),
        "region_count": len(regions),
        "data_sha256": hashlib.sha256(compressed).hexdigest(),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "locations.json.gz").write_bytes(compressed)
    (args.output_dir / "locations.sources.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(compressed):,} bytes to {args.output_dir}")


if __name__ == "__main__":
    main()
