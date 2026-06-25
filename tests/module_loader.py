import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADDON_DIR = ROOT / "anki_tts_addon"
_SYNTHETIC_PACKAGE_ATTR = "__module_loader_synthetic__"


def load_addon_module(name: str):
    package_name = "anki_tts_addon"
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(ADDON_DIR)]
        package.__package__ = package_name
        setattr(package, _SYNTHETIC_PACKAGE_ATTR, True)
        sys.modules[package_name] = package
    elif getattr(package, _SYNTHETIC_PACKAGE_ATTR, False):
        package.__path__ = [str(ADDON_DIR)]
        package.__package__ = package_name

    module_name = f"{package_name}.{name}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        ADDON_DIR / f"{name}.py",
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {module_name}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
