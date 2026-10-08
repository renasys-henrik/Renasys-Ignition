# Ignition Project Library script: PLCUDTSync
# GVL-aware, low-load CODESYS -> Ignition UDT synchronizer.
# Compatible with Ignition's Jython 2.7 scripting environment.
#
# Recommended workflow from the Designer Script Console:
#
#     GLOBALVARS_NODE = "nsu=...GlobalVars"
#     OPC_SERVER = "LAB"
#     TAG_PROVIDER = "Edge"
#     INSTANCE_ROOT = "PLC"
#
#     report = PLCUDTSync.discover(
#         globalvarsNode=GLOBALVARS_NODE,
#         opcServer=OPC_SERVER,
#         tagProvider=TAG_PROVIDER,
#         instanceRoot=INSTANCE_ROOT,
#     )
#     PLCUDTSync.printReport(report)
#     result = PLCUDTSync.apply(report)
#     print result
#
# apply(report) performs NO OPC browse/read calls. It only writes the already
# discovered configuration into Ignition.

import re
import time


# =============================================================================
# Configuration defaults
# =============================================================================

# These are only defaults. The recommended commissioning workflow passes the
# actual values into discover() from the Designer Script Console.
DEFAULT_OPC_SERVER = "LAB"
DEFAULT_TAG_PROVIDER = "Edge"
DEFAULT_INSTANCE_ROOT = "PLC"
DEFAULT_GLOBALVARS_NODE = (
    "nsu=CODESYSSPV3/3S/IecVarAccess;"
    "s=|appo|CPX-E-CEC-M1.Application.GlobalVars"
)


TAG_GROUP = "Default"

# Everything directly below GLOBALVARS_NODE is treated as a GVL/source branch.
# By default each GVL is preserved as its own folder below INSTANCE_ROOT.
#
# Only GVLs explicitly listed together in MERGE_GVL_GROUPS are allowed to
# merge at the instance-layout level. The actual UDT pairing is still decided
# by the CODESYS TypeIds, for example:
#   UDT_M1_Inputs  -> Inputs
#   UDT_M1_Params  -> Params
#   UDT_M1_Outputs -> Outputs
#
# Example result for the default configuration:
#   PLC/
#     IO/       <- OS_IN + OS_OUT may merge here
#       M1      <- UDT_Machine_Params + UDT_Machine_Outputs
#     Alarms/   <- independent GVL
#       M1      <- raw UDT_M1_Alarms instance
#     Energy/   <- independent GVL
#       ...
#
# Add more merge groups if desired. A group activates only when at least two
# configured GVLs are present. If fewer are found, those GVLs remain standalone.
MERGE_GVL_GROUPS = [
    {
        "folder": "IO",
        "gvls": ["OS_IN", "OS_OUT"],
    },
]

MIN_GVLS_FOR_MERGE_GROUP = 2
MIN_COMPOSITE_SECTIONS = 2

GENERATED_TYPE_FOLDER = "PLC_Generated"
RAW_TYPE_FOLDER = GENERATED_TYPE_FOLDER + "/Raw"
COMPOSITE_TYPE_FOLDER = GENERATED_TYPE_FOLDER + "/Composite"

# Conservative OPC limits. Discovery only browses branch containers and
# structured nodes. Every unique PLC UDT schema is cached after first discovery.
MAX_BROWSE_DEPTH = 20
MAX_BROWSE_CALLS = 300
MAX_TYPE_READS = 300
MAX_TOTAL_NODES = 8000
OPC_DELAY_SECONDS = 0.10
POST_APPLY_SETTLE_SECONDS = 0.50
POST_APPLY_VERIFY_RETRIES = 5

OVERWRITE_GENERATED_DEFINITIONS = True
DELETE_STALE_INSTANCES = False
RECREATE_INSTANCE_ON_TYPE_CHANGE = True

METADATA_PARAMETERS = [
    "DisplayName",
    "Description",
    "Area",
    "EquipmentId",
]

LOGGER_NAME = "PLCUDTSync"
GENERATOR_VERSION = 5

# Longest suffixes first. Matching is case-insensitive. Add future interface
# variants here if required. The branch name itself does not matter.
TYPE_SUFFIXES = [
    ("_Parameters", "Params"),
    ("_Outputs", "Outputs"),
    ("_Inputs", "Inputs"),
    ("_Params", "Params"),
    ("_Output", "Outputs"),
    ("_Input", "Inputs"),
]

SECTION_PRIORITY = ["Inputs", "Params", "Outputs"]

# =============================================================================
# Public workflow
# =============================================================================

def discover(
    globalvarsNode=None,
    opcServer=None,
    tagProvider=None,
    instanceRoot=None,
):
    """
    Performs low-load OPC discovery and builds the complete generation plan.
    No Ignition tag or UDT configuration is written.

    Recommended Script Console usage:

        GLOBALVARS_NODE = "..."
        OPC_SERVER = "LAB"
        TAG_PROVIDER = "Edge"
        INSTANCE_ROOT = "PLC"

        report = PLCUDTSync.discover(
            globalvarsNode=GLOBALVARS_NODE,
            opcServer=OPC_SERVER,
            tagProvider=TAG_PROVIDER,
            instanceRoot=INSTANCE_ROOT,
        )
    """
    context = _build_context(
        globalvarsNode,
        opcServer,
        tagProvider,
        instanceRoot,
    )

    state = {
        "browseCalls": 0,
        "typeReads": 0,
        "totalNodes": 0,
        "schemaCacheHits": 0,
        "uniqueStructuredTypes": 0,
    }

    warnings = []
    schema_cache = {}
    building_type_keys = set()

    _info("Starting GVL-aware low-load PLC UDT discovery.")

    source_model = _discover_source_roots(
        context,
        state,
        schema_cache,
        building_type_keys,
        warnings,
    )

    type_registry = _build_type_registry(source_model, warnings)
    variants = _build_variant_registry(type_registry, warnings)

    raw_definitions = _build_raw_definitions(type_registry, context)
    composite_definitions = _build_composite_definitions(
        type_registry,
        variants,
        warnings,
        context,
    )

    instance_configs = _build_top_level_instances(
        source_model,
        variants,
        composite_definitions,
        warnings,
        context,
    )

    report = {
        "generatorVersion": GENERATOR_VERSION,
        "globalvarsNode": context["globalvarsNode"],
        "opcServer": context["opcServer"],
        "tagProvider": context["tagProvider"],
        "instanceRoot": context["instanceRoot"],
        "sourceBranches": [root["name"] for root in source_model["branches"]],
        "rootItemCount": len(source_model["rootItems"]),
        "requestCounts": dict(state),
        "rawTypeCount": len(raw_definitions),
        "compositeTypeCount": len(composite_definitions),
        "instanceCount": len(instance_configs),
        "rawTypes": sorted(raw_definitions.keys(), key=lambda x: x.lower()),
        "compositeTypes": sorted(
            composite_definitions.keys(),
            key=lambda x: x.lower(),
        ),
        "instanceNames": sorted(
            [config["name"] for config in instance_configs],
            key=lambda x: x.lower(),
        ),
        "variantSummary": _variant_summary(variants),
        "warnings": warnings,
        "rawDefinitions": raw_definitions,
        "compositeDefinitions": composite_definitions,
        "instances": instance_configs,
    }

    _info(
        "Discovery complete: %d source branches, %d browse calls, %d type "
        "reads, %d unique structured types, %d raw UDTs, %d composite UDTs, "
        "%d top-level tags."
        % (
            len(source_model["branches"]),
            state["browseCalls"],
            state["typeReads"],
            state["uniqueStructuredTypes"],
            len(raw_definitions),
            len(composite_definitions),
            len(instance_configs),
        )
    )

    for warning in warnings:
        _warn(warning)

    return report

def printReport(report, verbose=False):
    """Prints a concise report without dumping ExtensionObject bodies."""
    print("")
    print("============================================================")
    print("GVL-AWARE PLC -> IGNITION UDT DISCOVERY")
    print("============================================================")
    print("Generator version: %s" % report.get("generatorVersion"))
    print("OPC server:        %s" % report["opcServer"])
    print("Tag provider:      %s" % report["tagProvider"])
    print("Instance root:     %s" % report["instanceRoot"])
    print("Source node:       %s" % report["globalvarsNode"])

    print("")
    print("Discovered source branches:")
    if report["sourceBranches"]:
        for name in report["sourceBranches"]:
            print("  - %s" % name)
    else:
        print("  none")

    print("")
    print("Configured GVL merge groups:")
    if MERGE_GVL_GROUPS:
        for group in MERGE_GVL_GROUPS:
            print("  - %s <- %s" % (
                group.get("folder", ""),
                ", ".join(group.get("gvls", [])),
            ))
    else:
        print("  none")

    counts = report["requestCounts"]
    print("")
    print("OPC discovery load:")
    print("  Browse calls:            %d / %d" % (
        counts["browseCalls"], MAX_BROWSE_CALLS))
    print("  Structured type reads:   %d / %d" % (
        counts["typeReads"], MAX_TYPE_READS))
    print("  Schema cache hits:       %d" % counts["schemaCacheHits"])
    print("  Unique structured types: %d" % counts["uniqueStructuredTypes"])
    print("  Logical nodes modeled:   %d / %d" % (
        counts["totalNodes"], MAX_TOTAL_NODES))

    print("")
    print("Generated plan:")
    print("  Raw UDTs:       %d" % report["rawTypeCount"])
    print("  Composite UDTs: %d" % report["compositeTypeCount"])
    print("  Top-level tags: %d" % report["instanceCount"])

    print("")
    print("Merged interface variants:")
    if report.get("variantSummary"):
        for item in report["variantSummary"]:
            print("  - %s = %s" % (item["base"], ", ".join(item["sections"])))
    else:
        print("  none")

    print("")
    print("Composite UDTs:")
    if report["compositeTypes"]:
        for name in report["compositeTypes"]:
            print("  - %s" % name)
    else:
        print("  none")

    print("")
    print("Top-level generated tags:")
    if report["instanceNames"]:
        for name in report["instanceNames"]:
            print("  - %s" % name)
    else:
        print("  none")

    if verbose:
        print("")
        print("Raw UDTs:")
        for name in report["rawTypes"]:
            print("  - %s" % name)

    if report["warnings"]:
        print("")
        print("Warnings:")
        for warning in report["warnings"]:
            print("  - %s" % warning)
    else:
        print("")
        print("Warnings: none")

    print("============================================================")

def previewJson(report):
    """Returns an already-discovered report as formatted JSON."""
    return system.util.jsonEncode(report, 2)


def apply(report):
    """
    Writes the UDT definitions and instances from an existing discovery report.
    This function performs NO OPC browse or OPC read calls.
    """
    if not report:
        raise ValueError("A discovery report is required.")

    if report.get("generatorVersion") != GENERATOR_VERSION:
        raise ValueError(
            "Report generator version %s does not match script version %s. "
            "Run discover() again."
            % (report.get("generatorVersion"), GENERATOR_VERSION)
        )

    context = _build_context(
        report.get("globalvarsNode"),
        report.get("opcServer"),
        report.get("tagProvider"),
        report.get("instanceRoot"),
    )

    _info("Applying existing discovery report. No OPC discovery will occur.")

    warnings = report["warnings"]

    _ensure_output_folders(context)
    _apply_raw_definitions(report["rawDefinitions"], warnings, context)
    _apply_composite_definitions(
        report["compositeDefinitions"],
        warnings,
        context,
    )
    written_instances = _apply_instances(
        report["instances"],
        warnings,
        context,
    )
    visible_instances = _verify_instance_root(report["instances"], context)

    _info(
        "PLC UDT synchronization complete. %d/%d planned top-level instances "
        "are visible from the tag provider."
        % (len(visible_instances), report["instanceCount"])
    )

    return {
        "applied": True,
        "rawTypeCount": report["rawTypeCount"],
        "compositeTypeCount": report["compositeTypeCount"],
        "instanceCount": report["instanceCount"],
        "instanceRoot": "[%s]%s" % (
            context["tagProvider"],
            context["instanceRoot"],
        ),
        "writtenInstances": written_instances,
        "visibleInstances": visible_instances,
        "warnings": warnings,
    }

def run(
    applyChanges=False,
    globalvarsNode=None,
    opcServer=None,
    tagProvider=None,
    instanceRoot=None,
):
    """Convenience wrapper around discover() and apply()."""
    report = discover(
        globalvarsNode=globalvarsNode,
        opcServer=opcServer,
        tagProvider=tagProvider,
        instanceRoot=instanceRoot,
    )
    if applyChanges:
        apply(report)
    return report

# =============================================================================
# Low-load OPC discovery
# =============================================================================

def _discover_source_roots(
    context,
    state,
    schema_cache,
    building_type_keys,
    warnings,
):
    """
    Discovers every direct child below the configured root node.

    Structured direct children are treated as source branches/containers.
    Their names are deliberately not interpreted. Atomic direct children are
    retained as root-level items so the configured root is truly exhaustive.
    """
    root_elements = _safe_browse(
        context["globalvarsNode"],
        ["Root"],
        state,
        context,
    )

    branches = []
    root_items = []

    for element in root_elements:
        if _element_type(element).upper() == "METHOD":
            continue

        name = _element_name(element)
        data_type = _element_data_type(element)
        element_type = _element_type(element).upper()

        if _is_structured_data_type(data_type) or element_type in (
            "OBJECT", "FOLDER", "VIEW"
        ):
            branch = _discover_interface_root(
                element,
                state,
                schema_cache,
                building_type_keys,
                warnings,
                context,
            )
            branches.append(branch)
            _info(
                "Discovered source branch %s with %d direct children."
                % (branch["name"], len(branch["children"]))
            )
            continue

        node = _discover_element(
            element=element,
            source_branch="__ROOT__",
            parent_section=None,
            depth=0,
            logical_path=[name],
            state=state,
            schema_cache=schema_cache,
            building_type_keys=building_type_keys,
            warnings=warnings,
            context=context,
        )
        root_items.append(node)

    branches.sort(key=lambda item: item["name"].lower())
    root_items.sort(key=lambda item: item["name"].lower())

    if not branches and not root_items:
        raise RuntimeError("No OPC children were discovered below the source node.")

    return {
        "branches": branches,
        "rootItems": root_items,
    }

def _discover_interface_root(
    element,
    state,
    schema_cache,
    building_type_keys,
    warnings,
    context,
):
    name = _element_name(element)
    node_id = _element_node_id(element)
    logical_path = [name]

    _count_node(state, logical_path)

    root = {
        "name": name,
        "safeName": _safe_tag_name(name),
        "nodeId": node_id,
        "dataType": _element_data_type(element),
        "elementType": _element_type(element),
        "path": logical_path,
        "sourceBranch": name,
        "section": None,
        "plcType": None,
        "children": [],
        "relativeSuffix": "",
    }

    child_elements = _safe_browse(node_id, logical_path, state, context)

    for child_element in child_elements:
        if _element_type(child_element).upper() == "METHOD":
            continue

        child = _discover_element(
            element=child_element,
            source_branch=name,
            parent_section=None,
            depth=0,
            logical_path=logical_path + [_element_name(child_element)],
            state=state,
            schema_cache=schema_cache,
            building_type_keys=building_type_keys,
            warnings=warnings,
            context=context,
        )

        child["relativeSuffix"] = _relative_opc_suffix(
            node_id,
            child["nodeId"],
        )
        root["children"].append(child)

    root["children"].sort(key=lambda item: item["name"].lower())
    return root

def _discover_element(
    element,
    source_branch,
    parent_section,
    depth,
    logical_path,
    state,
    schema_cache,
    building_type_keys,
    warnings,
    context,
):
    name = _element_name(element)
    node_id = _element_node_id(element)
    data_type = _element_data_type(element)
    element_type = _element_type(element)

    if _is_structured_data_type(data_type):
        return _discover_structure(
            name=name,
            node_id=node_id,
            data_type=data_type,
            element_type=element_type,
            source_branch=source_branch,
            parent_section=parent_section,
            depth=depth,
            logical_path=logical_path,
            state=state,
            schema_cache=schema_cache,
            building_type_keys=building_type_keys,
            warnings=warnings,
            context=context,
        )

    _count_node(state, logical_path)

    return {
        "name": name,
        "safeName": _safe_tag_name(name),
        "nodeId": node_id,
        "dataType": data_type,
        "elementType": element_type,
        "path": list(logical_path),
        "sourceBranch": source_branch,
        "section": parent_section,
        "plcType": None,
        "children": [],
        "relativeSuffix": "",
    }

def _discover_structure(
    name,
    node_id,
    data_type,
    element_type,
    source_branch,
    parent_section,
    depth,
    logical_path,
    state,
    schema_cache,
    building_type_keys,
    warnings,
    context,
):
    if depth > MAX_BROWSE_DEPTH:
        raise RuntimeError(
            "Maximum structured browse depth (%d) exceeded at %s."
            % (MAX_BROWSE_DEPTH, "/".join(logical_path))
        )

    _count_node(state, logical_path)

    plc_type = _read_plc_type(node_id, logical_path, state, context)
    _, inferred_section = _split_type_variant(plc_type)
    section = inferred_section or parent_section
    type_key = plc_type.lower()

    if type_key in schema_cache:
        state["schemaCacheHits"] += 1
        prototype = schema_cache[type_key]
        return _clone_schema_instance(
            prototype=prototype,
            name=name,
            node_id=node_id,
            source_branch=source_branch,
            parent_section=section,
            logical_path=logical_path,
            state=state,
            count_self=False,
        )

    if type_key in building_type_keys:
        raise RuntimeError(
            "Recursive PLC UDT reference detected while discovering %s at %s."
            % (plc_type, "/".join(logical_path))
        )

    building_type_keys.add(type_key)

    node = {
        "name": name,
        "safeName": _safe_tag_name(name),
        "nodeId": node_id,
        "dataType": data_type,
        "elementType": element_type,
        "path": list(logical_path),
        "sourceBranch": source_branch,
        "section": section,
        "plcType": plc_type,
        "children": [],
        "relativeSuffix": "",
    }

    try:
        child_elements = _safe_browse(node_id, logical_path, state, context)

        for child_element in child_elements:
            if _element_type(child_element).upper() == "METHOD":
                continue

            child_name = _element_name(child_element)
            child_path = logical_path + [child_name]

            child = _discover_element(
                element=child_element,
                source_branch=source_branch,
                parent_section=section,
                depth=depth + 1,
                logical_path=child_path,
                state=state,
                schema_cache=schema_cache,
                building_type_keys=building_type_keys,
                warnings=warnings,
                context=context,
            )

            child["relativeSuffix"] = _relative_opc_suffix(
                node_id,
                child["nodeId"],
            )
            node["children"].append(child)

        node["children"].sort(key=lambda item: item["name"].lower())
        schema_cache[type_key] = node
        state["uniqueStructuredTypes"] = len(schema_cache)

    finally:
        building_type_keys.remove(type_key)

    return node

def _clone_schema_instance(
    prototype,
    name,
    node_id,
    source_branch,
    parent_section,
    logical_path,
    state,
    count_self=True,
):
    if count_self:
        _count_node(state, logical_path)

    plc_type = prototype.get("plcType")
    _, inferred_section = _split_type_variant(plc_type) if plc_type else (None, None)
    section = inferred_section or parent_section

    node = {
        "name": name,
        "safeName": _safe_tag_name(name),
        "nodeId": node_id,
        "dataType": prototype.get("dataType"),
        "elementType": prototype.get("elementType"),
        "path": list(logical_path),
        "sourceBranch": source_branch,
        "section": section,
        "plcType": plc_type,
        "children": [],
        "relativeSuffix": prototype.get("relativeSuffix", ""),
    }

    for prototype_child in prototype.get("children", []):
        child_name = prototype_child["name"]
        child_path = logical_path + [child_name]
        suffix = prototype_child.get("relativeSuffix", "")
        child_id = _apply_relative_suffix(node_id, suffix)

        if prototype_child.get("plcType"):
            child = _clone_schema_instance(
                prototype=prototype_child,
                name=child_name,
                node_id=child_id,
                source_branch=source_branch,
                parent_section=section,
                logical_path=child_path,
                state=state,
                count_self=True,
            )
        else:
            _count_node(state, child_path)
            child = {
                "name": child_name,
                "safeName": _safe_tag_name(child_name),
                "nodeId": child_id,
                "dataType": prototype_child.get("dataType"),
                "elementType": prototype_child.get("elementType"),
                "path": list(child_path),
                "sourceBranch": source_branch,
                "section": section,
                "plcType": None,
                "children": [],
                "relativeSuffix": suffix,
            }

        child["relativeSuffix"] = suffix
        node["children"].append(child)

    return node

def _safe_browse(node_id, logical_path, state, context):
    if state["browseCalls"] >= MAX_BROWSE_CALLS:
        raise RuntimeError(
            "Browse safety limit (%d) reached before browsing %s. "
            "Discovery was aborted."
            % (MAX_BROWSE_CALLS, "/".join(logical_path))
        )

    state["browseCalls"] += 1
    _info(
        "Browse %d/%d: %s"
        % (state["browseCalls"], MAX_BROWSE_CALLS, "/".join(logical_path))
    )

    result = list(system.opc.browseServer(context["opcServer"], node_id))
    time.sleep(OPC_DELAY_SECONDS)
    return result

def _read_plc_type(node_id, logical_path, state, context):
    if state["typeReads"] >= MAX_TYPE_READS:
        raise RuntimeError(
            "Structured type-read safety limit (%d) reached before reading %s. "
            "Discovery was aborted."
            % (MAX_TYPE_READS, "/".join(logical_path))
        )

    state["typeReads"] += 1
    _info(
        "Type read %d/%d: %s"
        % (state["typeReads"], MAX_TYPE_READS, "/".join(logical_path))
    )

    qv = system.opc.readValue(context["opcServer"], node_id)
    time.sleep(OPC_DELAY_SECONDS)

    try:
        good = qv.quality.isGood()
    except Exception:
        good = str(qv.quality).lower().startswith("good")

    if not good:
        raise RuntimeError(
            "Bad OPC quality while reading structured TypeId for %s: %s"
            % ("/".join(logical_path), qv.quality)
        )

    plc_type = _extract_plc_type(qv.value)
    if plc_type is None:
        raise RuntimeError(
            "Structured node %s did not expose a named CODESYS TypeId. "
            "Discovery stopped instead of generating an AUTO_* fallback type."
            % "/".join(logical_path)
        )

    return plc_type

def _count_node(state, logical_path):
    state["totalNodes"] += 1

    if state["totalNodes"] > MAX_TOTAL_NODES:
        raise RuntimeError(
            "Logical node safety limit (%d) exceeded at %s. Discovery aborted."
            % (MAX_TOTAL_NODES, "/".join(logical_path))
        )


def _is_structured_data_type(data_type):
    if data_type is None:
        return False
    return "DOCUMENT" in str(data_type).upper()


# =============================================================================
# CODESYS TypeId extraction
# =============================================================================

def _extract_plc_type(value):
    if value is None:
        return None

    candidates = []

    # PyDocumentObjectAdapter.get() requires a default value under Jython.
    for key in ("TypeId", "typeId", "TypeID", "typeID"):
        try:
            candidate = value.get(key, None)
            if candidate is not None:
                candidates.append(candidate)
        except Exception:
            pass

        try:
            candidate = value[key]
            if candidate is not None:
                candidates.append(candidate)
        except Exception:
            pass

    text = str(value)

    match = re.search(
        r'["\']?TypeI[Dd]["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        text,
    )
    if match:
        candidates.insert(0, match.group(1))

    candidates.append(text)

    for candidate in candidates:
        result = _normalise_type_name(candidate)
        if result is not None:
            return result

    return None


def _normalise_type_name(candidate):
    if candidate is None:
        return None

    text = str(candidate).strip().strip('"\'')

    # Handles application-local and library values such as:
    #   ...Application.UDT_Machine_Params
    #   ...Application.RenLib#UDT_Airknife_Params
    match = re.search(r'\b(UDT_[A-Za-z0-9_]+)\b', text)
    if match:
        return match.group(1)

    match = re.search(r'\b(ST_[A-Za-z0-9_]+)\b', text)
    if match:
        return match.group(1)

    return None

# =============================================================================
# PLC type registries
# =============================================================================

def _build_type_registry(source_model, warnings):
    registry = {}

    for root in source_model["branches"]:
        for child in root.get("children", []):
            _register_node_types(child, registry, warnings)

    for child in source_model.get("rootItems", []):
        _register_node_types(child, registry, warnings)

    return registry

def _register_node_types(node, registry, warnings):
    plc_type = node.get("plcType")
    if not plc_type:
        return

    signature = _node_signature(node)

    if plc_type not in registry:
        registry[plc_type] = {
            "node": node,
            "signature": signature,
        }
    elif registry[plc_type]["signature"] != signature:
        warnings.append(
            "PLC type %s appeared with more than one member structure. "
            "The first discovered structure will be used."
            % plc_type
        )

    for child in node.get("children", []):
        if child.get("plcType"):
            _register_node_types(child, registry, warnings)


def _node_signature(node):
    result = []

    for child in node.get("children", []):
        if child.get("plcType"):
            result.append(
                (
                    child["name"].lower(),
                    "struct",
                    child["plcType"].lower(),
                )
            )
        else:
            result.append(
                (
                    child["name"].lower(),
                    "atomic",
                    str(child.get("dataType")).lower(),
                )
            )

    return tuple(result)


def _build_variant_registry(type_registry, warnings):
    """
    Groups interface variants case-insensitively.

    Example:
        UDT_Airknife_Params
        UDT_AirKnife_Outputs

    are both grouped under one logical UDT_AirKnife composite base.
    """
    variants = {}

    for plc_type in sorted(type_registry.keys(), key=lambda item: item.lower()):
        base_name, section = _split_type_variant(plc_type)
        if base_name is None:
            continue

        key = base_name.lower()

        entry = variants.setdefault(
            key,
            {
                "baseNames": [],
                "sections": {},
            },
        )

        if base_name not in entry["baseNames"]:
            entry["baseNames"].append(base_name)

        if section in entry["sections"]:
            previous = entry["sections"][section]
            if previous.lower() != plc_type.lower():
                warnings.append(
                    "Multiple PLC types map to logical variant %s/%s: %s and %s. "
                    "The first one will be used."
                    % (key, section, previous, plc_type)
                )
            continue

        entry["sections"][section] = plc_type

    for entry in variants.values():
        entry["canonicalBase"] = _canonical_base_name(entry["baseNames"])

    return variants


def _split_type_variant(plc_type):
    lower = plc_type.lower()

    for suffix, section in TYPE_SUFFIXES:
        if lower.endswith(suffix.lower()):
            return plc_type[:-len(suffix)], section

    return None, None


def _canonical_base_name(names):
    if not names:
        return ""

    # Prefer spelling with more internal capitals, e.g. AirKnife > Airknife.
    def score(name):
        body = name[4:] if name.startswith("UDT_") else name
        return (
            sum(1 for char in body if char.isupper()),
            len(name),
            name,
        )

    return sorted(names, key=score, reverse=True)[0]


# =============================================================================
# Ignition UDT generation
# =============================================================================

def _build_raw_definitions(type_registry, context):
    definitions = {}

    for plc_type in sorted(type_registry.keys(), key=lambda item: item.lower()):
        node = type_registry[plc_type]["node"]
        tags = []

        for child in node.get("children", []):
            suffix = _relative_opc_suffix(node["nodeId"], child["nodeId"])

            if child.get("plcType"):
                tag = {
                    "name": child["safeName"],
                    "tagType": "UdtInstance",
                    "typeId": _raw_type_id(child["plcType"]),
                    "parameters": {
                        "OPCServer": _parameter_binding("{OPCServer}"),
                        "OPCPath": _parameter_binding("{OPCPath}" + suffix),
                    },
                }
                _add_original_name_documentation(tag, child)
            else:
                tag = _atomic_tag_config(child, "{OPCPath}" + suffix)

            tags.append(tag)

        definitions[plc_type] = {
            "name": _safe_tag_name(plc_type),
            "tagType": "UdtType",
            "documentation": (
                "Generated from CODESYS PLC type %s. Do not edit manually."
                % plc_type
            ),
            "parameters": {
                "OPCServer": _string_parameter(context["opcServer"]),
                "OPCPath": _string_parameter(""),
            },
            "tags": tags,
        }

    return definitions

def _build_composite_definitions(
    type_registry,
    variants,
    warnings,
    context,
):
    composite_keys = set()

    for key, entry in variants.items():
        if len(entry["sections"]) >= MIN_COMPOSITE_SECTIONS:
            composite_keys.add(key)

    definitions = {}

    for key in sorted(composite_keys):
        entry = variants[key]
        base_name = entry["canonicalBase"]
        section_types = entry["sections"]
        merge_sections = _ordered_sections(section_types.keys())

        grouped_children = {}

        for section in merge_sections:
            plc_type = section_types[section]
            parent_node = type_registry[plc_type]["node"]

            for child in parent_node.get("children", []):
                child_key = child["name"].lower()
                group = grouped_children.setdefault(
                    child_key,
                    {"displayNames": [], "bySection": {}},
                )
                group["displayNames"].append(child["name"])
                group["bySection"][section] = child

        section_members = dict((section, []) for section in merge_sections)
        nested_members = []

        for child_key in sorted(grouped_children.keys()):
            group = grouped_children[child_key]
            by_section = group["bySection"]
            display_name = _canonical_base_name(group["displayNames"])

            nested_key = _matching_composite_child_key(
                by_section,
                variants,
                composite_keys,
            )

            if nested_key is not None:
                nested_entry = variants[nested_key]
                nested_base = nested_entry["canonicalBase"]
                nested_sections = _ordered_sections(
                    nested_entry["sections"].keys()
                )

                nested_params = {
                    "OPCServer": _parameter_binding("{OPCServer}"),
                    "DisplayName": display_name,
                    "Description": "",
                    "Area": "",
                    "EquipmentId": display_name,
                }

                for section in nested_sections:
                    child = by_section[section]
                    parent_type = section_types[section]
                    parent_node = type_registry[parent_type]["node"]
                    suffix = _relative_opc_suffix(
                        parent_node["nodeId"],
                        child["nodeId"],
                    )
                    nested_params[section + "Path"] = _parameter_binding(
                        "{%sPath}%s" % (section, suffix)
                    )

                nested_tag = {
                    "name": _safe_tag_name(display_name),
                    "tagType": "UdtInstance",
                    "typeId": _composite_type_id(nested_base),
                    "parameters": nested_params,
                }
                _add_original_name_documentation(
                    nested_tag,
                    list(by_section.values())[0],
                )
                nested_members.append(nested_tag)
                continue

            for section in merge_sections:
                if section not in by_section:
                    continue

                child = by_section[section]
                parent_type = section_types[section]
                parent_node = type_registry[parent_type]["node"]
                suffix = _relative_opc_suffix(
                    parent_node["nodeId"],
                    child["nodeId"],
                )

                if child.get("plcType"):
                    member = {
                        "name": child["safeName"],
                        "tagType": "UdtInstance",
                        "typeId": _raw_type_id(child["plcType"]),
                        "parameters": {
                            "OPCServer": _parameter_binding("{OPCServer}"),
                            "OPCPath": _parameter_binding(
                                "{%sPath}%s" % (section, suffix)
                            ),
                        },
                    }
                    _add_original_name_documentation(member, child)
                else:
                    member = _atomic_tag_config(
                        child,
                        "{%sPath}%s" % (section, suffix),
                    )

                section_members[section].append(member)

        tags = []

        for section in merge_sections:
            members = section_members[section]
            if members:
                tags.append({
                    "name": section,
                    "tagType": "Folder",
                    "tags": members,
                })

        nested_members.sort(key=lambda item: item["name"].lower())
        tags.extend(nested_members)

        parameters = {
            "OPCServer": _string_parameter(context["opcServer"]),
            "DisplayName": _string_parameter(""),
            "Description": _string_parameter(""),
            "Area": _string_parameter(""),
            "EquipmentId": _string_parameter(""),
        }

        for section in merge_sections:
            parameters[section + "Path"] = _string_parameter("")

        source_names = [section_types[s] for s in merge_sections]

        definitions[base_name] = {
            "name": _safe_tag_name(base_name),
            "tagType": "UdtType",
            "documentation": (
                "Generated merged equipment type from %s. Do not edit manually."
                % ", ".join(source_names)
            ),
            "parameters": parameters,
            "tags": tags,
        }

    return definitions

def _matching_composite_child_key(by_section, variants, composite_keys):
    if len(by_section) < MIN_COMPOSITE_SECTIONS:
        return None

    candidate_keys = set()

    for parent_section, child in by_section.items():
        if not child.get("plcType"):
            return None

        child_base, child_section = _split_type_variant(child["plcType"])
        if child_base is None or child_section != parent_section:
            return None

        candidate_keys.add(child_base.lower())

    if len(candidate_keys) != 1:
        return None

    key = list(candidate_keys)[0]
    if key not in composite_keys or key not in variants:
        return None

    required_sections = set(variants[key]["sections"].keys())
    available_sections = set(by_section.keys())

    # A nested composite must be fully addressable at this location. If a type
    # globally has Inputs/Params/Outputs but this parent only exposes two of
    # them, keep the children in their source folders instead of creating a
    # partially broken nested UDT instance.
    if not required_sections.issubset(available_sections):
        return None

    return key

def _atomic_tag_config(node, opc_path_expression):
    config = {
        "name": node["safeName"],
        "tagType": "AtomicTag",
        "valueSource": "opc",
        "opcServer": _parameter_binding("{OPCServer}"),
        "opcItemPath": _parameter_binding(opc_path_expression),
        "tagGroup": TAG_GROUP,
    }

    ignition_type = _ignition_data_type(node.get("dataType"))
    if ignition_type is not None:
        config["dataType"] = ignition_type

    _add_original_name_documentation(config, node)
    return config


def _string_parameter(value):
    return {
        "dataType": "String",
        "value": value,
    }


def _parameter_binding(text):
    return {
        "bindType": "parameter",
        "binding": text,
    }


def _raw_type_id(plc_type):
    return RAW_TYPE_FOLDER + "/" + _safe_tag_name(plc_type)


def _composite_type_id(base_type):
    return COMPOSITE_TYPE_FOLDER + "/" + _safe_tag_name(base_type)


# =============================================================================
# Top-level Data Type Instance generation
# =============================================================================

def _build_top_level_instances(
    source_model,
    variants,
    composite_definitions,
    warnings,
    context,
):
    """
    Builds the instance tree below INSTANCE_ROOT.

    Rules:
      1. GVLs explicitly listed in the same MERGE_GVL_GROUPS entry may merge.
      2. Every other GVL becomes its own folder named after that GVL.
      3. Direct atomic/structured items below GLOBALVARS_NODE remain directly
         below INSTANCE_ROOT because they do not belong to a GVL container.
    """
    instances = []
    branch_lookup = {}

    for root in source_model["branches"]:
        key = str(root["name"]).lower()
        if key in branch_lookup:
            warnings.append(
                "More than one source GVL matched the name %s. Only the first "
                "will participate in configured merge groups."
                % root["name"]
            )
            continue
        branch_lookup[key] = root

    consumed = set()
    used_output_names = set()

    for merge_group in MERGE_GVL_GROUPS:
        folder_name = str(merge_group.get("folder", "")).strip()
        requested_gvls = merge_group.get("gvls", [])

        if not folder_name:
            warnings.append(
                "A MERGE_GVL_GROUPS entry has no folder name and was ignored."
            )
            continue

        found_roots = []
        found_keys = []

        for requested_name in requested_gvls:
            key = str(requested_name).lower()
            root = branch_lookup.get(key)
            if root is None:
                continue
            if key in consumed:
                warnings.append(
                    "GVL %s appears in more than one merge group. The later "
                    "group %s will ignore it."
                    % (root["name"], folder_name)
                )
                continue
            found_roots.append(root)
            found_keys.append(key)

        if len(found_roots) < MIN_GVLS_FOR_MERGE_GROUP:
            if found_roots:
                warnings.append(
                    "Merge folder %s found only %d configured GVL(s): %s. "
                    "They will remain standalone GVL folders."
                    % (
                        folder_name,
                        len(found_roots),
                        ", ".join(root["name"] for root in found_roots),
                    )
                )
            continue

        safe_folder_name = _safe_tag_name(folder_name)
        if safe_folder_name.lower() in used_output_names:
            raise RuntimeError(
                "Generated instance folder name collision for merge folder %s."
                % folder_name
            )

        merged_members = _build_merged_gvl_members(
            found_roots,
            variants,
            composite_definitions,
            warnings,
            context,
            folder_name,
        )

        instances.append({
            "name": safe_folder_name,
            "tagType": "Folder",
            "documentation": (
                "Generated merge folder from GVLs: %s"
                % ", ".join(root["name"] for root in found_roots)
            ),
            "tags": merged_members,
        })

        used_output_names.add(safe_folder_name.lower())
        for key in found_keys:
            consumed.add(key)

    # Every GVL that was not consumed by an active merge group is preserved
    # as an independent folder. Nothing from one standalone GVL is compared
    # or merged with another GVL.
    for root in sorted(
        source_model["branches"],
        key=lambda item: item["name"].lower(),
    ):
        branch_key = str(root["name"]).lower()
        if branch_key in consumed:
            continue

        folder_name = _safe_tag_name(root["name"])
        if folder_name.lower() in used_output_names:
            raise RuntimeError(
                "Generated instance folder name collision for GVL %s."
                % root["name"]
            )

        folder_members = []
        for child in root.get("children", []):
            folder_members.append(
                _single_source_instance_config(
                    child,
                    child["name"],
                    context,
                )
            )

        instances.append({
            "name": folder_name,
            "tagType": "Folder",
            "documentation": (
                "Generated from CODESYS GVL/source branch %s."
                % root["name"]
            ),
            "tags": folder_members,
        })
        used_output_names.add(folder_name.lower())

    # Preserve direct values located immediately below GLOBALVARS_NODE.
    for child in source_model.get("rootItems", []):
        name = _safe_tag_name(child["name"])
        if name.lower() in used_output_names:
            warnings.append(
                "Direct root item %s conflicts with a generated GVL/merge "
                "folder name and was skipped."
                % child["name"]
            )
            continue
        instances.append(
            _single_source_instance_config(child, child["name"], context)
        )
        used_output_names.add(name.lower())

    return instances


def _build_merged_gvl_members(
    roots,
    variants,
    composite_definitions,
    warnings,
    context,
    folder_name,
):
    """Builds the children of one explicitly configured merge folder."""
    grouped = {}

    for root in roots:
        for child in root.get("children", []):
            key = child["name"].lower()
            group = grouped.setdefault(
                key,
                {"displayNames": [], "items": []},
            )
            group["displayNames"].append(child["name"])
            group["items"].append(child)

    members = []

    for logical_key in sorted(grouped.keys()):
        group = grouped[logical_key]
        items = group["items"]
        logical_name = _canonical_base_name(group["displayNames"])

        composite_base, by_section = _top_level_composite_base(
            items,
            variants,
            composite_definitions,
        )

        if composite_base is not None:
            variant_key = composite_base.lower()
            variant_entry = variants[variant_key]
            sections = _ordered_sections(variant_entry["sections"].keys())

            parameters = {
                "OPCServer": context["opcServer"],
                "DisplayName": logical_name,
                "Description": "",
                "Area": "",
                "EquipmentId": logical_name,
            }

            for section in sections:
                parameters[section + "Path"] = by_section[section]["nodeId"]

            members.append({
                "name": _safe_tag_name(logical_name),
                "tagType": "UdtInstance",
                "typeId": _composite_type_id(
                    variant_entry["canonicalBase"]
                ),
                "parameters": parameters,
                "documentation": (
                    "Generated merged instance in %s from CODESYS object %s."
                    % (folder_name, logical_name)
                ),
            })
            continue

        if len(items) == 1:
            node = items[0]
            members.append(
                _single_source_instance_config(node, logical_name, context)
            )
            continue

        # The same object name exists in multiple whitelisted GVLs, but its
        # CODESYS TypeIds do not prove that the variants belong to one composite.
        # Preserve every source instead of guessing.
        source_members = []
        used_names = set()

        for node in sorted(
            items,
            key=lambda item: (
                str(item.get("sourceBranch", "")).lower(),
                str(item.get("section", "")).lower(),
            ),
        ):
            preferred_name = (
                node.get("section")
                or node.get("sourceBranch")
                or "Source"
            )
            member_name = _unique_member_name(preferred_name, used_names)
            used_names.add(member_name.lower())
            source_members.append(
                _source_member_config(node, member_name, context)
            )

        warnings.append(
            "Object %s appeared in multiple GVLs inside merge folder %s, "
            "but its CODESYS TypeIds could not be safely merged. Its source "
            "variants will remain in a folder."
            % (logical_name, folder_name)
        )

        members.append({
            "name": _safe_tag_name(logical_name),
            "tagType": "Folder",
            "tags": source_members,
        })

    return members

def _top_level_composite_base(
    items,
    variants,
    composite_definitions,
):
    if len(items) < MIN_COMPOSITE_SECTIONS:
        return None, {}

    candidate_keys = set()
    by_section = {}

    for node in items:
        plc_type = node.get("plcType")
        if not plc_type:
            return None, {}

        base_name, section = _split_type_variant(plc_type)
        if base_name is None or section is None:
            return None, {}

        candidate_keys.add(base_name.lower())

        if section in by_section:
            # Two branches expose the same logical section for the same object.
            # Do not guess which one should win.
            return None, {}

        by_section[section] = node

    if len(candidate_keys) != 1:
        return None, {}

    key = list(candidate_keys)[0]
    if key not in variants:
        return None, {}

    canonical_base = variants[key]["canonicalBase"]
    if canonical_base not in composite_definitions:
        return None, {}

    required_sections = set(variants[key]["sections"].keys())
    if not required_sections.issubset(set(by_section.keys())):
        return None, {}

    return key, by_section

# =============================================================================
# Apply generated configurations - NO OPC calls below this line
# =============================================================================

def _ensure_output_folders(context):
    provider_root = "[%s]" % context["tagProvider"]
    type_root = provider_root + "_types_"

    _ensure_folder(type_root, GENERATED_TYPE_FOLDER)
    _ensure_folder(type_root + "/" + GENERATED_TYPE_FOLDER, "Raw")
    _ensure_folder(type_root + "/" + GENERATED_TYPE_FOLDER, "Composite")
    _ensure_folder_path(provider_root, context["instanceRoot"])

def _ensure_folder(parent_path, folder_name):
    qualities = system.tag.configure(
        parent_path,
        [{"name": folder_name, "tagType": "Folder"}],
        "m",
    )
    _check_qualities(
        qualities,
        "creating folder %s/%s" % (parent_path, folder_name),
    )


def _apply_raw_definitions(definitions, warnings, context):
    graph = _raw_dependency_graph(definitions)
    order = _topological_order(graph, warnings, "raw UDT")
    base_path = "[%s]_types_/%s" % (
        context["tagProvider"],
        RAW_TYPE_FOLDER,
    )
    policy = "o" if OVERWRITE_GENERATED_DEFINITIONS else "m"

    for plc_type in order:
        qualities = system.tag.configure(
            base_path,
            [definitions[plc_type]],
            policy,
        )
        _check_qualities(qualities, "writing raw UDT %s" % plc_type)

def _apply_composite_definitions(definitions, warnings, context):
    graph = _composite_dependency_graph(definitions)
    order = _topological_order(graph, warnings, "composite UDT")
    base_path = "[%s]_types_/%s" % (
        context["tagProvider"],
        COMPOSITE_TYPE_FOLDER,
    )
    policy = "o" if OVERWRITE_GENERATED_DEFINITIONS else "m"

    for base_type in order:
        qualities = system.tag.configure(
            base_path,
            [definitions[base_type]],
            policy,
        )
        _check_qualities(
            qualities,
            "writing composite UDT %s" % base_type,
        )

def _apply_instances(instance_configs, warnings, context):
    """Creates/replaces generated top-level tags. No OPC calls occur here."""
    base_path = "[%s]%s" % (
        context["tagProvider"],
        context["instanceRoot"],
    )
    desired_names = set(config["name"] for config in instance_configs)
    written = []

    if not instance_configs:
        warnings.append(
            "No top-level instance configurations were present in the discovery "
            "report. The instance folder was created, but no child tags could "
            "be generated."
        )
        return written

    for original_config in instance_configs:
        config = system.util.jsonDecode(system.util.jsonEncode(original_config))
        name = str(config["name"])
        path = base_path + "/" + name
        existing = _get_single_configuration(path)

        if existing is not None:
            _preserve_metadata(existing, config)

        qualities = system.tag.configure(base_path, [config], "o")
        _check_qualities(qualities, "writing generated instance %s" % path)

        if not system.tag.exists(path):
            raise RuntimeError(
                "Ignition reported a successful configure for %s, but the tag "
                "does not exist afterward." % path
            )

        written.append(name)
        _info("Created/updated generated instance %s" % path)

    if DELETE_STALE_INSTANCES:
        browse_results = system.tag.browse(base_path, {"recursive": False})
        stale_paths = []

        for result in browse_results.getResults():
            name = str(result["name"])
            if name not in desired_names:
                stale_paths.append(str(result["fullPath"]))

        if stale_paths:
            system.tag.deleteTags(stale_paths)
            _info("Deleted %d stale generated instances." % len(stale_paths))

    return written

def _verify_instance_root(instance_configs, context):
    """Verifies generated top-level tags against the Ignition tag provider."""
    base_path = "[%s]%s" % (
        context["tagProvider"],
        context["instanceRoot"],
    )
    desired = set(str(config["name"]) for config in instance_configs)

    if not desired:
        return []

    visible = set()

    for attempt in range(POST_APPLY_VERIFY_RETRIES):
        if attempt > 0:
            time.sleep(POST_APPLY_SETTLE_SECONDS)

        browse_results = system.tag.browse(base_path, {"recursive": False})
        visible = set(
            str(result["name"])
            for result in browse_results.getResults()
        )

        if desired.issubset(visible):
            break

    missing = sorted(desired - visible, key=lambda x: x.lower())
    if missing:
        raise RuntimeError(
            "Generated top-level instances were configured but are still "
            "missing from a provider browse after verification: %s"
            % ", ".join(missing)
        )

    return sorted(desired, key=lambda x: x.lower())

def _get_single_configuration(path):
    if not system.tag.exists(path):
        return None

    configs = system.tag.getConfiguration(path, False)
    if not configs:
        return None

    return configs[0]


def _preserve_metadata(existing, new_config):
    existing_parameters = existing.get("parameters", {})
    new_parameters = new_config.setdefault("parameters", {})

    for key in METADATA_PARAMETERS:
        if key in existing_parameters:
            new_parameters[key] = existing_parameters[key]


def _remove_metadata_from_update(config):
    parameters = config.get("parameters")
    if not parameters:
        return

    for key in METADATA_PARAMETERS:
        if key in parameters:
            del parameters[key]



def _build_context(globalvars_node, opc_server, tag_provider, instance_root):
    globalvars_node = globalvars_node or DEFAULT_GLOBALVARS_NODE
    opc_server = opc_server or DEFAULT_OPC_SERVER
    tag_provider = tag_provider or DEFAULT_TAG_PROVIDER
    instance_root = instance_root or DEFAULT_INSTANCE_ROOT

    tag_provider = str(tag_provider).strip().strip("[]")
    instance_root = str(instance_root).strip().strip("/")

    if not str(globalvars_node).strip():
        raise ValueError("globalvarsNode cannot be empty.")
    if not str(opc_server).strip():
        raise ValueError("opcServer cannot be empty.")
    if not tag_provider:
        raise ValueError("tagProvider cannot be empty.")
    if not instance_root:
        raise ValueError("instanceRoot cannot be empty.")

    return {
        "globalvarsNode": str(globalvars_node),
        "opcServer": str(opc_server),
        "tagProvider": tag_provider,
        "instanceRoot": instance_root,
    }


def _ordered_sections(sections):
    sections = list(sections)
    priority = dict((name, index) for index, name in enumerate(SECTION_PRIORITY))
    return sorted(
        sections,
        key=lambda name: (priority.get(name, 1000), str(name).lower()),
    )


def _variant_summary(variants):
    result = []
    for key in sorted(variants.keys()):
        entry = variants[key]
        if len(entry["sections"]) < MIN_COMPOSITE_SECTIONS:
            continue
        result.append({
            "base": entry["canonicalBase"],
            "sections": _ordered_sections(entry["sections"].keys()),
        })
    return result


def _single_source_instance_config(node, logical_name, context):
    if node.get("plcType"):
        return {
            "name": _safe_tag_name(logical_name),
            "tagType": "UdtInstance",
            "typeId": _raw_type_id(node["plcType"]),
            "parameters": {
                "OPCServer": context["opcServer"],
                "OPCPath": node["nodeId"],
            },
            "documentation": (
                "Generated raw instance from source branch %s, CODESYS object %s."
                % (node.get("sourceBranch", ""), logical_name)
            ),
        }

    config = {
        "name": _safe_tag_name(logical_name),
        "tagType": "AtomicTag",
        "valueSource": "opc",
        "opcServer": context["opcServer"],
        "opcItemPath": node["nodeId"],
        "tagGroup": TAG_GROUP,
    }
    ignition_type = _ignition_data_type(node.get("dataType"))
    if ignition_type is not None:
        config["dataType"] = ignition_type
    return config


def _source_member_config(node, member_name, context):
    if node.get("plcType"):
        return {
            "name": _safe_tag_name(member_name),
            "tagType": "UdtInstance",
            "typeId": _raw_type_id(node["plcType"]),
            "parameters": {
                "OPCServer": context["opcServer"],
                "OPCPath": node["nodeId"],
            },
            "documentation": (
                "Source branch: %s; CODESYS object: %s"
                % (node.get("sourceBranch", ""), node.get("name", ""))
            ),
        }

    config = {
        "name": _safe_tag_name(member_name),
        "tagType": "AtomicTag",
        "valueSource": "opc",
        "opcServer": context["opcServer"],
        "opcItemPath": node["nodeId"],
        "tagGroup": TAG_GROUP,
        "documentation": (
            "Source branch: %s; CODESYS object: %s"
            % (node.get("sourceBranch", ""), node.get("name", ""))
        ),
    }
    ignition_type = _ignition_data_type(node.get("dataType"))
    if ignition_type is not None:
        config["dataType"] = ignition_type
    return config


def _unique_member_name(preferred, used_lower_names):
    base = _safe_tag_name(preferred)
    candidate = base
    counter = 2
    while candidate.lower() in used_lower_names:
        candidate = "%s_%d" % (base, counter)
        counter += 1
    return candidate


def _ensure_folder_path(provider_root, relative_path):
    current = provider_root
    for part in str(relative_path).split("/"):
        part = part.strip()
        if not part:
            continue
        _ensure_folder(current, part)
        current = current + "/" + part

# =============================================================================
# UDT dependency ordering
# =============================================================================

def _raw_dependency_graph(definitions):
    graph = dict((name, set()) for name in definitions.keys())

    for name, definition in definitions.items():
        for tag in definition.get("tags", []):
            _collect_type_dependencies(
                tag,
                RAW_TYPE_FOLDER + "/",
                graph[name],
            )

    return graph


def _composite_dependency_graph(definitions):
    graph = dict((name, set()) for name in definitions.keys())

    for name, definition in definitions.items():
        for tag in definition.get("tags", []):
            dependencies = set()
            _collect_type_dependencies(
                tag,
                COMPOSITE_TYPE_FOLDER + "/",
                dependencies,
            )

            for dependency in dependencies:
                if dependency in definitions:
                    graph[name].add(dependency)

    return graph


def _collect_type_dependencies(tag, prefix, output):
    if str(tag.get("tagType", "")) == "UdtInstance":
        type_id = str(tag.get("typeId", ""))
        if type_id.startswith(prefix):
            output.add(type_id[len(prefix):])

    for child in tag.get("tags", []):
        _collect_type_dependencies(child, prefix, output)


def _topological_order(graph, warnings, graph_name):
    ordered = []
    temporary = set()
    permanent = set()

    def visit(node):
        if node in permanent:
            return

        if node in temporary:
            warnings.append(
                "Dependency cycle detected in %s graph at %s."
                % (graph_name, node)
            )
            return

        temporary.add(node)

        for dependency in sorted(graph.get(node, set())):
            if dependency in graph:
                visit(dependency)

        temporary.remove(node)
        permanent.add(node)
        ordered.append(node)

    for node in sorted(graph.keys()):
        visit(node)

    return ordered


# =============================================================================
# OPC path and datatype helpers
# =============================================================================

def _relative_opc_suffix(parent_id, child_id):
    if child_id.startswith(parent_id):
        return child_id[len(parent_id):]

    parent_address = _node_address(parent_id)
    child_address = _node_address(child_id)

    if child_address.startswith(parent_address):
        return child_address[len(parent_address):]

    raise RuntimeError(
        "Cannot parameterize OPC child path %s from parent %s."
        % (child_id, parent_id)
    )


def _apply_relative_suffix(parent_id, suffix):
    if not suffix:
        return parent_id
    return parent_id + suffix


def _node_address(node_id):
    marker = ";s="
    if marker in node_id:
        return node_id.split(marker, 1)[1]
    return node_id


def _ignition_data_type(opc_data_type):
    if opc_data_type is None:
        return None

    text = str(opc_data_type).upper().replace(" ", "")
    is_array = "[]" in text or "ARRAY" in text

    if "BOOLEAN" in text or text.endswith("BOOL"):
        base = "Boolean"
    elif "BYTESTRING" in text:
        return "ByteArray"
    elif "DATETIME" in text or "DATE_AND_TIME" in text:
        base = "DateTime"
    elif "DOUBLE" in text or "LREAL" in text or "DURATION" in text:
        base = "Float8"
    elif "FLOAT" in text or "REAL" in text:
        base = "Float4"
    elif "UINT64" in text or "ULONG" in text or "LWORD" in text:
        base = "Int8"
    elif "INT64" in text or "LONG" in text or "LINT" in text:
        base = "Int8"
    elif "UINT32" in text or "DWORD" in text or "UDINT" in text:
        base = "Int8"
    elif "INT32" in text or "DINT" in text:
        base = "Int4"
    elif "UINT16" in text or "WORD" in text or "UINT" in text:
        base = "Int4"
    elif "INT16" in text or text.endswith("INT"):
        base = "Int2"
    elif "UINT8" in text or "USINT" in text or text.endswith("BYTE"):
        base = "Int2"
    elif "SBYTE" in text or "SINT" in text or "INT8" in text:
        base = "Int1"
    elif "ENUM" in text:
        base = "Int4"
    elif "STRING" in text or "CHAR" in text or "GUID" in text:
        base = "String"
    elif "DOCUMENT" in text or "EXTENSIONOBJECT" in text:
        base = "Document"
    else:
        return None

    if is_array:
        array_map = {
            "Int1": "Int1Array",
            "Int2": "Int2Array",
            "Int4": "Int4Array",
            "Int8": "Int8Array",
            "Float4": "Float4Array",
            "Float8": "Float8Array",
            "Boolean": "BooleanArray",
            "String": "StringArray",
            "DateTime": "DateTimeArray",
        }
        return array_map.get(base)

    return base


def _safe_tag_name(name):
    text = str(name)
    text = re.sub(r"[^A-Za-z0-9_ ()'\-:]", "_", text)

    if not text:
        text = "_"

    if not re.match(r"^[A-Za-z_]", text):
        text = "_" + text

    return text


def _add_original_name_documentation(config, node):
    if node.get("safeName") != node.get("name"):
        config["documentation"] = (
            "Original PLC member name: %s" % node.get("name")
        )


# =============================================================================
# Ignition OPC object helpers
# =============================================================================

def _element_name(element):
    return str(element.getDisplayName())


def _element_node_id(element):
    try:
        value = element.getNodeId()
        if value is not None:
            return str(value)
    except Exception:
        pass

    try:
        value = element.getServerNodeId()
        try:
            return str(value.getNodeId())
        except Exception:
            return str(value)
    except Exception as exc:
        raise RuntimeError("Could not obtain OPC node id: %s" % exc)


def _element_data_type(element):
    for method_name in ("getDataType", "getDatatype"):
        try:
            method = getattr(element, method_name)
            value = method()
            if value is not None:
                return str(value)
        except Exception:
            pass

    return None


def _element_type(element):
    try:
        return str(element.getElementType())
    except Exception:
        return ""


# =============================================================================
# Ignition write validation and logging
# =============================================================================

def _check_qualities(qualities, operation):
    failures = []

    for quality in qualities:
        try:
            good = quality.isGood()
        except Exception:
            good = str(quality).lower().startswith("good")

        if not good:
            failures.append(str(quality))

    if failures:
        raise RuntimeError(
            "Ignition reported bad quality while %s: %s"
            % (operation, ", ".join(failures))
        )


def _info(message):
    system.util.getLogger(LOGGER_NAME).info(str(message))


def _warn(message):
    system.util.getLogger(LOGGER_NAME).warn(str(message))