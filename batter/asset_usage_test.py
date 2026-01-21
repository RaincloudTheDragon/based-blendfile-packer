import abc
import unittest
from pathlib import Path

from . import asset_usage as au
from .asset_usage import AssetUsage


_my_dir = Path(__file__).resolve().parent
_testfile_root = _my_dir.parent / "batter-tests"


class BlendfileLoadingTestCase(unittest.TestCase, metaclass=abc.ABCMeta):
    test_blend_file: Path
    """Set on a subclass to load this blendfile.

    Relative paths are interpreted relative to ../batter-tests/
    """

    @classmethod
    def setUpClass(cls) -> None:
        import bpy

        blendfile: Path = cls.test_blend_file
        if not blendfile.is_absolute():
            blendfile = _testfile_root / blendfile

        bpy.ops.wm.open_mainfile(filepath=str(blendfile))


class AssetUsageTest(BlendfileLoadingTestCase):
    test_blend_file = Path("root/scene.blend")

    def test_find_blend_asset_usage(self) -> None:
        import bpy

        usages = au.find_blend_asset_usage()
        expected = {
            None: {
                AssetUsage(
                    abspath=_testfile_root / "root/char/cube.blend",
                    reference_path="//char/cube.blend",
                    is_blendfile=True,
                ),
                AssetUsage(
                    abspath=_testfile_root / "root/char/little_cube.blend",
                    reference_path="//char/little_cube.blend",
                    is_blendfile=True,
                ),
            },
            bpy.data.libraries["cube.blend"]: {
                AssetUsage(
                    abspath=_testfile_root / "material_textures.blend",
                    reference_path="//../material_textures.blend",
                    is_blendfile=True,
                )
            },
            bpy.data.libraries["little_cube.blend"]: {
                AssetUsage(
                    abspath=_testfile_root / "material_textures.blend",
                    reference_path="//../material_textures.blend",
                    is_blendfile=True,
                )
            },
            bpy.data.libraries["material_textures.blend"]: set(),
        }

        self.assertEqual(expected, usages)

    def test_find_nonblend_asset_usage(self) -> None:
        import bpy

        usages = au.find_nonblend_asset_usage()

        expected = {
            bpy.data.libraries["material_textures.blend"]: {
                AssetUsage(
                    abspath=_testfile_root / "textures/Bricks/brick_dotted_04-bump.jpg",
                    reference_path="//textures/Bricks/brick_dotted_04-bump.jpg",
                    is_blendfile=False,
                ),
                AssetUsage(
                    abspath=_testfile_root
                    / "textures/Bricks/brick_dotted_04-color.jpg",
                    reference_path="//textures/Bricks/brick_dotted_04-color.jpg",
                    is_blendfile=False,
                ),
                AssetUsage(
                    abspath=_testfile_root
                    / "textures/Textures/Buildings/buildings_roof_04-color.jpg",
                    reference_path="//textures/Textures/Buildings/buildings_roof_04-color.jpg",
                    is_blendfile=False,
                ),
            }
        }

        self.assertEqual(expected, usages)

    def test_find(self) -> None:
        import bpy

        usages = au.find()

        expected = {
            None: {
                AssetUsage(
                    abspath=_testfile_root / "root/char/cube.blend",
                    reference_path="//char/cube.blend",
                    is_blendfile=True,
                ),
                AssetUsage(
                    abspath=_testfile_root / "root/char/little_cube.blend",
                    reference_path="//char/little_cube.blend",
                    is_blendfile=True,
                ),
            },
            bpy.data.libraries["cube.blend"]: {
                AssetUsage(
                    abspath=_testfile_root / "material_textures.blend",
                    reference_path="//../material_textures.blend",
                    is_blendfile=True,
                )
            },
            bpy.data.libraries["little_cube.blend"]: {
                AssetUsage(
                    abspath=_testfile_root / "material_textures.blend",
                    reference_path="//../material_textures.blend",
                    is_blendfile=True,
                )
            },
            bpy.data.libraries["material_textures.blend"]: {
                AssetUsage(
                    abspath=_testfile_root / "textures/Bricks/brick_dotted_04-bump.jpg",
                    reference_path="//textures/Bricks/brick_dotted_04-bump.jpg",
                    is_blendfile=False,
                ),
                AssetUsage(
                    abspath=_testfile_root
                    / "textures/Bricks/brick_dotted_04-color.jpg",
                    reference_path="//textures/Bricks/brick_dotted_04-color.jpg",
                    is_blendfile=False,
                ),
                AssetUsage(
                    abspath=_testfile_root
                    / "textures/Textures/Buildings/buildings_roof_04-color.jpg",
                    reference_path="//textures/Textures/Buildings/buildings_roof_04-color.jpg",
                    is_blendfile=False,
                ),
            },
        }

        self.assertEqual(expected, usages)
