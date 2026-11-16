from pathlib import Path
import importlib.util
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        spec = importlib.util.spec_from_file_location("bundle_files", root / "bundle_files.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for path, relative in module.builtin_files(root):
            build_data["force_include"][str(path)] = "agent_nexus/builtins/" + relative.as_posix()
        # Versioned contracts (design §2 schemas/); agent_nexus.device_contract reads them.
        for schema in sorted((root / "schemas").glob("*.json")):
            build_data["force_include"][str(schema)] = "agent_nexus/schemas/" + schema.name
