# DACH Location Data

This directory contains a modified extract of [GeoNames](https://www.geonames.org/),
licensed under [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/).
GeoNames provides the source data without a guarantee of accuracy or completeness.

`locations.json.gz` contains populated places (including villages and town sections)
and first-level administrative regions in Germany, Austria and Switzerland.
Historical, abandoned, destroyed and aggregate place records are excluded.
Place aliases include linguistic alternate names, excluding historical and colloquial
names, identifiers, postal codes and links. Preferred German names are used for
display, with the source's main name as the fallback. Region display names and
common abbreviations are standardized separately in the generator.

`locations.sources.json` records source URLs, archive timestamps, SHA-256 checksums
and output counts. The data format is versioned; each place row contains its
GeoNames ID, country-qualified admin1 code, display name and aliases. Each region
entry contains its display name and aliases. GeoNames FIPS region codes in DE/AT
are distinct from the ISO abbreviations accepted by the normalizer.

## Updating

From the repository root, using the project's configured Python interpreter:

```shell
python scripts/build_location_data.py --download
python -m unittest discover -s tests
```

The generator downloads the DE/AT/CH country archives and corresponding country
alternate-name archives into the ignored `.cache/geonames` directory. Downloads
only occur with `--download`. Keep those six archives to reproduce a snapshot:

```shell
python scripts/build_location_data.py --source-dir .cache/geonames
```

The same sources and generator produce byte-identical output (sorted records and
JSON keys, fixed gzip timestamp). Review both generated files together when
updating. Runtime normalization reads only the bundled data and never downloads
or updates it. No additional third-party packages are required by the generator.
