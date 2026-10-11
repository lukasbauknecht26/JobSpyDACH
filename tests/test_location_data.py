from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from scripts.build_location_data import build_country


def place_row(identifier, name, feature_class="P", feature="PPL", region="01"):
    return [
        str(identifier),
        name,
        name,
        "",
        "0",
        "0",
        feature_class,
        feature,
        "DE",
        "",
        region,
        "",
        "",
        "",
        "0",
        "",
        "0",
        "Europe/Berlin",
        "2026-10-09",
    ]


def alias_row(identifier, language, name, preferred="", historical=""):
    return ["1", str(identifier), language, name, preferred, "", "", historical, "", ""]


def write_archive(path, rows):
    with ZipFile(path, "w") as archive:
        archive.writestr("DE.txt", "\n".join("\t".join(row) for row in rows) + "\n")


class LocationDataTests(unittest.TestCase):
    def test_generator_selects_german_names_and_excludes_non_place_aliases(self):
        with TemporaryDirectory() as directory:
            places = Path(directory) / "places.zip"
            names = Path(directory) / "names.zip"
            write_archive(
                places,
                [
                    place_row(1, "Region One", "A", "ADM1"),
                    place_row(2, "English Name"),
                    place_row(3, "Village with no German name"),
                    place_row(4, "Historic Place", feature="PPLH"),
                    place_row(5, "Abandoned Place", feature="PPLQ"),
                    place_row(6, "Destroyed Place", feature="PPLW"),
                    place_row(7, "Section", feature="PPLX"),
                    place_row(8, "Lake", "H", "LK"),
                ],
            )
            write_archive(
                names,
                [
                    alias_row(2, "de", "Deutscher Name", preferred="1"),
                    alias_row(2, "de", "Anderer Name"),
                    alias_row(2, "de", "Historischer Name", historical="1"),
                    alias_row(2, "fr", "Nom fran\u00e7ais"),
                    alias_row(2, "iata", "XYZ"),
                    alias_row(2, "link", "https://example.com"),
                    alias_row(2, "post", "12345"),
                    alias_row(2, "wkdt", "Q12345"),
                    alias_row(2, "abbr", "EN"),
                ],
            )
            with patch.dict(
                "scripts.build_location_data.REGIONS",
                {"DE.01": ("Region Eins", "RE")},
                clear=True,
            ):
                records, regions = build_country("DE", places, names)
        self.assertEqual([row[0] for row in records], [2, 3, 7])
        self.assertEqual(records[0][2], "Deutscher Name")
        self.assertEqual(records[1][2], "Village with no German name")
        self.assertEqual(
            records[0][3],
            ["Anderer Name", "Deutscher Name", "English Name", "Nom fran\u00e7ais"],
        )
        self.assertEqual(
            regions["DE.01"], ["Region Eins", ["RE", "Region Eins", "Region One"]]
        )

    def test_generator_rejects_incomplete_region_snapshots(self):
        with TemporaryDirectory() as directory:
            places = Path(directory) / "places.zip"
            names = Path(directory) / "names.zip"
            write_archive(places, [place_row(2, "Town")])
            write_archive(names, [])
            with self.assertRaises(ValueError):
                build_country("DE", places, names)


if __name__ == "__main__":
    unittest.main()
