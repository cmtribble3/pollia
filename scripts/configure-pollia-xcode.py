"""Maintain variants and generate a curated, source-only Xcode navigator."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET


VARIANTS = ["scalar_rgb", "scalar_mono_polarized",
            "metal_ad_mono_polarized", "metal_rgb_polarized"]


def configure_variants(root, build):
    build.mkdir(parents=True, exist_ok=True)
    config = build / "mitsuba.conf"
    if config.exists():
        contents = config.read_text()
    else:
        contents = (root / "resources/mitsuba.conf.template").read_text()
        contents = contents.replace("@INSERT_CONFIGURATIONS_HERE@",
                                    ", ".join(json.dumps(v) for v in VARIANTS))
    # Preserve additional user-enabled variants while ensuring our requested set.
    match = re.search(r'"enabled"\s*:\s*\[(.*?)\]', contents, re.S)
    if match is None:
        raise RuntimeError("Cannot find enabled variants in mitsuba.conf")
    enabled = json.loads("[" + re.sub(r"#.*", "", match[1]) + "]")
    for variant in VARIANTS:
        if variant not in enabled:
            enabled.append(variant)
    contents = contents[:match.start(1)] + "\n        " + ", ".join(
        json.dumps(v) for v in enabled) + "\n    " + contents[match.end(1):]
    config.write_text(contents)
    # Xcode's Python launch must also avoid importing packages from Documents.
    # Only bin/python is used from this copy; generated pip/activate entrypoints
    # retain the original environment's paths and are not exposed to the user.
    environment = root / ".venv"
    if environment.is_dir():
        shutil.copytree(environment, build / "pollia-python-env",
                        symlinks=True, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))


def navigator(root, build):
    objects = {}

    def oid(key):
        return hashlib.sha256(key.encode()).hexdigest()[:24].upper()

    def quote(value):
        return json.dumps(str(value))

    def file(path):
        key = oid("file:" + str(path))
        suffix = path.suffix
        kind = {".cpp": "sourcecode.cpp.cpp", ".h": "sourcecode.c.h",
                ".hpp": "sourcecode.cpp.h", ".mm": "sourcecode.cpp.objcpp",
                ".py": "text.script.python", ".sh": "text.script.sh",
                ".xml": "text.xml", ".swift": "sourcecode.swift"}.get(suffix, "text")
        objects[key] = (f"isa = PBXFileReference; lastKnownFileType = {quote(kind)}; "
                        f"name = {quote(path.name)}; path = {quote(path)}; sourceTree = SOURCE_ROOT;")
        return key

    def group(label, children, key):
        ident = oid("group:" + key)
        objects[ident] = ("isa = PBXGroup; children = (" +
                          ",".join(children) + ",); " +
                          f"name = {quote(label)}; sourceTree = \"<group>\";")
        return ident

    def tree(relative, label=None):
        path = root / relative
        children = []
        if path.is_dir():
            for child in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
                if child.name.startswith(".") or child.name in {"__pycache__", "results"}:
                    continue
                if child.is_dir():
                    entry = tree(child.relative_to(root))
                    if entry:
                        children.append(entry)
                elif child.suffix in {".cpp", ".h", ".hpp", ".inl", ".mm", ".py",
                                      ".xml", ".swift"}:
                    if child.name.startswith("pollia_") and str(relative).startswith(("src/", "include/mitsuba")):
                        continue
                    children.append(file(Path("..") / child.relative_to(root)))
        if not children:
            return None
        return group(label or path.name, children, str(relative))

    integrators = group("Integrators", [file(Path("..") / p) for p in (
        "src/integrators/pollia_pis.cpp", "src/integrators/pollia_mueller.cpp")], "pollia-integrators")
    materials = group("Materials", [file(Path("..") / p) for p in (
        "src/bsdfs/pollia_jarecki.cpp", "include/mitsuba/render/pollia_jarecki.h")], "pollia-materials")
    pollia_sources = [integrators, materials]
    for path, label in [("rendering", "Python Rendering"), ("swift/Sources", "Swift Rendering")]:
        entry = tree(Path(path), label)
        if entry:
            pollia_sources.append(entry)
    top = [group("Pollia", pollia_sources, "pollia")]
    mitsuba = [tree(Path(p), label) for p, label in (
        ("src/core", "Core"), ("src/render", "Rendering"),
        ("src/mitsuba", "Command Line"), ("src/python", "Python Bindings"),
        ("include/mitsuba", "Headers"))]
    plugins = [tree(Path("src") / name) for name in (
        "bsdfs", "integrators", "emitters", "films", "media", "phase",
        "rfilters", "samplers", "sensors", "shapes", "spectra", "textures", "volumes")]
    mitsuba.append(group("Plugins", [p for p in plugins if p], "mitsuba-plugins"))
    top.append(group("Mitsuba", [p for p in mitsuba if p], "mitsuba"))
    drjit = [tree(Path(p), label) for p, label in (
        ("ext/drjit/include", "Headers"),
        ("ext/drjit/src", "Source"),
        ("ext/drjit/drjit", "Python"),
        ("ext/drjit/ext/drjit-core/include", "Core Headers"),
        ("ext/drjit/ext/drjit-core/src", "Core Source"))]
    top.append(group("Dr.Jit", [p for p in drjit if p], "drjit"))
    for path, label in [("scenes", "Scenes"), ("benchmarks", "Benchmarks")]:
        entry = tree(Path(path), label)
        if entry:
            top.append(entry)
    main = group("Pollia Sources", top, "main")
    project = oid("project")
    configs = oid("configs")
    entries = []
    for name in ("Debug", "Release", "RelWithDebInfo"):
        ident = oid("config:" + name)
        objects[ident] = f"isa = XCBuildConfiguration; buildSettings = {{}}; name = {name};"
        entries.append(ident)
    objects[configs] = ("isa = XCConfigurationList; buildConfigurations = (" +
                        ",".join(entries) + ",); defaultConfigurationIsVisible = 0; "
                        "defaultConfigurationName = RelWithDebInfo;")
    objects[project] = (f"isa = PBXProject; buildConfigurationList = {configs}; "
                        "compatibilityVersion = \"Xcode 14.0\"; developmentRegion = en; "
                        f"mainGroup = {main}; projectDirPath = \"\"; projectRoot = \"\"; targets = (); ")
    destination = root / "build-pollia-xcode/Pollia Sources.xcodeproj"
    destination.mkdir(parents=True, exist_ok=True)
    destination.joinpath("project.pbxproj").write_text(
        "// !$*UTF8*$!\n{ archiveVersion = 1; classes = {}; objectVersion = 56; objects = {\n" +
        "\n".join(f"{key} = {{ {value} }};" for key, value in objects.items()) +
        f"\n}}; rootObject = {project}; }}\n")
    workspace = root / "pollia.xcworkspace"
    workspace.mkdir(exist_ok=True)
    xml = ET.Element("Workspace", version="1.0")
    ET.SubElement(xml, "FileRef", location="group:build-pollia-xcode/Pollia Sources.xcodeproj")
    build_group = ET.SubElement(xml, "Group", location="container:", name="Build")
    ET.SubElement(build_group, "FileRef", location="absolute:" + str(build / "pollia.xcodeproj"))
    ET.indent(xml)
    workspace.joinpath("contents.xcworkspacedata").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(xml, encoding="unicode") + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--build-dir", type=Path, default=Path.home() / "Library/Developer/Xcode/DerivedData/pollia-runtime")
    parser.add_argument("--workspace", action="store_true")
    args = parser.parse_args()
    if args.workspace:
        navigator(args.root.resolve(), args.build_dir.resolve())
    else:
        configure_variants(args.root.resolve(), args.build_dir.resolve())
