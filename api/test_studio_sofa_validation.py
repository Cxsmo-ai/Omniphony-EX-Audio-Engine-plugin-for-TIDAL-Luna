import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import omnifusion_service as service


HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"


def studio_config(path: str, *, source: str = "sofa") -> str:
    return f"""render:
  binaural:
    output_mode: binaural
    hrir_source: {source}
    hrtf_sofa_path: '{path}'
"""


class StudioSofaValidationTests(unittest.TestCase):
    def test_resolves_relative_sofa_path_from_config_directory(self):
        with TemporaryDirectory() as temp:
            config_dir = Path(temp)
            sofa_path = config_dir / "hrtf" / "profile.sofa"
            sofa_path.parent.mkdir()
            sofa_path.write_bytes(HDF5_SIGNATURE)

            resolved = service.studio_config_binaural_sofa_path(
                studio_config("hrtf/profile.sofa"), config_dir
            )

            self.assertEqual(resolved, sofa_path)
            self.assertTrue(service.has_sofa_container_signature(resolved))

    def test_accepts_hdf5_user_block_and_netcdf_signatures(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            user_block = root / "user-block.sofa"
            user_block.write_bytes(b"\0" * 512 + HDF5_SIGNATURE)
            netcdf = root / "classic.sofa"
            netcdf.write_bytes(b"CDF\x02" + b"\0" * 32)

            self.assertTrue(service.has_sofa_container_signature(user_block))
            self.assertTrue(service.has_sofa_container_signature(netcdf))

    def test_rejects_missing_unreadable_wrong_extension_and_invalid_container(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            missing = root / "missing.sofa"
            invalid = root / "invalid.sofa"
            invalid.write_bytes(b"not a SOFA container")
            wrong_extension = root / "profile.h5"
            wrong_extension.write_bytes(HDF5_SIGNATURE)
            directory = root / "directory.sofa"
            directory.mkdir()

            for path in (missing, invalid, wrong_extension, directory):
                with self.subTest(path=path.name):
                    self.assertFalse(service.has_sofa_container_signature(path))

    def test_requires_one_binaural_sofa_source_and_rejects_nul_paths(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cases = (
                (studio_config(str(root / "profile.sofa"), source="builtin"), None),
                (studio_config(str(root / "profile.sofa")).replace("output_mode: binaural", "output_mode: speakers"), None),
                (studio_config(str(root / "profile.sofa")) + "    hrir_source: sofa\n", None),
                (studio_config("bad\x00path.sofa"), None),
            )
            for config_text, expected in cases:
                with self.subTest(config_text=config_text):
                    self.assertEqual(
                        service.studio_config_binaural_sofa_path(config_text, root),
                        expected,
                    )

    def test_container_check_rechecks_file_contents_after_config_is_unchanged(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sofa_path = root / "profile.sofa"
            sofa_path.write_bytes(HDF5_SIGNATURE)
            config_path = root / "config.yaml"
            config_path.write_text(studio_config(str(sofa_path)), encoding="utf-8")

            with patch.object(service, "STUDIO_CONFIG_FILE", config_path):
                self.assertTrue(service.studio_binaural_sofa_container_is_configured())
                sofa_path.write_bytes(b"invalid")
                self.assertFalse(service.studio_binaural_sofa_container_is_configured())


if __name__ == "__main__":
    unittest.main()
